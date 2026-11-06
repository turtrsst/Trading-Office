# Operations

## Install

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # fastapi, uvicorn, pydantic, ccxt, mcp, pytest
```

Optional providers:

```sh
npm install -g github:fzn0x/idx           # IDX data (static binary, no runtime)
idx ownership sync                        # KSEI 1%+ register into local SQLite
pip install ccxt                          # crypto public data + live execution
```

Verification:

```sh
python -m trading_office.cli doctor
```

```
  OK    config       /repo/config/settings.json
  OK    paper broker no capital at risk
  WARN  idx CLI      `idx` on PATH            <- synthetic fallback, app still works
  WARN  ccxt         pip install ccxt
```

## Run

| what | command |
| --- | --- |
| web terminal + REST | `python -m trading_office.cli serve --host 0.0.0.0 --port 8000` |
| MCP (stdio) | `python -m trading_office.api.mcp_server` |
| MCP (HTTP) | `python -m trading_office.api.mcp_server --http --port 8765` |
| scans only (no orders) | `python -m trading_office.engine --market crypto --no-execute` |
| one cycle, for cron | `python -m trading_office.engine --market idx --cycles 1` |
| backtest | `python -m trading_office.cli backtest --market idx --symbol BBCA --oos 0.35` |

Health probe: `GET /api/health` (returns `ok`, provider availability and execution mode).

## Execution modes

| mode | how to get it | behaviour |
| --- | --- | --- |
| **paper (default)** | `execution.paper = true` | fills simulated with spread, slippage and commission; margin tracked; nothing leaves the machine |
| scan only | `execution.dry_run = true`, or `engine --no-execute` | gates and plans run, no order is submitted |
| testnet | `paper=false`, `dry_run=false`, `testnet=true`, keys in env | real orders on the exchange sandbox |
| live | `paper=false`, `dry_run=false`, `testnet=false`, keys in env | real money — requires all four |

`paper: true` always wins: as long as it is set, the broker is the paper broker no matter
what the other flags say.

Keys never live in the repo: `TO_API_KEY` / `TO_API_SECRET` (names configurable through
`execution.api_key_env` / `api_secret_env`).

Live crypto orders are submitted with exchange-side brackets:

```python
params = {
    "stopLoss":   {"triggerPrice": stop},
    "takeProfit": {"triggerPrice": target},
}
```

so a dead bot cannot leave a naked position, and `clientOrderId` is deterministic
(`to-<symbol>-<bar>-<side>`) so a retry after a timeout cannot double-fill.

## State

```
state/
  crypto/  signals.json positions.json cooldowns.json ledger.jsonl account.json
           snapshot.json snapshot_reconciled.json runtime.json
  idx/     (same)
```

- `signals.json` — the per-bar decision locks (bounded to 5 000 entries).
- `positions.json` — open positions, restored on restart.
- `ledger.jsonl` — append-only audit trail: `scan`, `skip`, `order`, `manage`, `partial`,
  `cost_deferred`, `reconciliation`, `trade_closed`. This is what the UI's ledger and the
  equity curve are built from.
- Reset a paper account: `rm -rf state/<market>` (the ledger goes with it).

### Recovering from a crash or a restart

The engine calls `startup_sync()` before its first cycle (and the terminal's Live Engine
tab has a **Reconcile** button that calls the same code path). It compares
`state/positions.json` with the venue:

```sh
python -m trading_office.cli reconcile --market crypto            # report only
python -m trading_office.cli reconcile --market crypto --adopt    # take over venue positions
python -m trading_office.cli reconcile --market crypto --close-orphans
```

| Report key | Meaning | What to do |
| --- | --- | --- |
| `restored` | state and venue agree | nothing: `initial_stop` / `partial_done` / trail high-water mark were kept |
| `closed` | state said open, venue is flat | nothing: the trade is booked and the phantom removed |
| `untracked` | venue position the bot never opened | inspect it; `--adopt` or `--close-orphans` |
| `blocked` | the venue did not answer | fix credentials/network; no new entries until it answers |
| `stops_missing` | no resting stop order found | re-bracket manually or let the next management pass move it |

`partial_done` is written *before* the next bar, so a partial can never execute twice; a
partial that is smaller than the venue's `amount_to_precision` step is skipped instead of
rounded up.

### Two sources of truth (`snapshot.json`)

`positions.json` is the live file, but a crash between two writes (partial close, stop
move) can leave it one step behind. Every position change therefore also writes
`state/<market>/snapshot.json` (`{source, saved_at, positions}`), and `startup_sync()`:

1. merges `positions.json` over `snapshot.json` per symbol (the freshest write wins) and
   restores anything the in-memory engine lost (`restored_from_disk` in the report),
2. then reconciles that against the venue (venue size/entry win),
3. then writes `snapshot_reconciled.json` — the *venue truth* — which the next boot
   prefers when it is newer than the local snapshot.

So the order of authority is: **venue → reconciled snapshot → local snapshot → positions.json**.
No position is ever resurrected from a stale file without the exchange agreeing to it.

### Kill switch (daily loss / loss streak)

Every new entry first asks `guard_state()`: the realised P&L of the current market day
(against `max_daily_loss_pct`, default 2 %) and the trailing loss streak (against
`max_consecutive_losses`, default 4). When either is exceeded the engine refuses new
entries, keeps managing what is open, and writes the reason to the ledger.

```sh
curl -s localhost:8000/api/engine/kill-switch?market=crypto
curl -s -X POST localhost:8000/api/engine/resume -H 'content-type: application/json' -d '{"market":"crypto"}'
```

`/api/engine/resume` (and the **Resume trading** button) records `guard_resumed_day` in
`state/<market>/runtime.json`, so the override applies to *that day only* — a restart does
not silently re-arm trading.

## Configuration recipes

**Change risk per trade** — `config/settings.json`:

```json
"risk": {"risk_per_trade_pct": 0.5, "rr_target": 2.5, "atr_sl_mult": 1.8}
```

**Trade a different crypto list** —

```json
"profiles": {"crypto": {"symbols": ["BTC/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT"]}}
```

**Looser IDX screening** — lower `signal.min_score` to 4 and `min_filters_passed` to 2,
then re-run the backtest before trusting it.

**News blackout** —

```json
"market_filter": {"news_blackout": [
  {"at": "2026-11-07T19:30:00+07:00", "minutes": 30, "label": "US payrolls"},
  {"weekday": 3, "time": "01:00", "minutes": 45, "label": "FOMC", "tz": "Asia/Jakarta"}
]}
```

## Deploying behind the preview proxy

- Bind to `0.0.0.0` (the CLI and `serve` do by default).
- The UI uses **relative** URLs only (`/api/...`, `/static/...`), so it works under any
  host/port without configuration.
- CORS is open (`allow_origins=["*"]`) because the UI is same-origin and agents may call
  the API directly; put the server behind your own auth if it is public — there is no
  built-in login.
- MCP over HTTP needs its own process/port; forward `/mcp` at the proxy to it.

## Troubleshooting

| symptom | cause | fix |
| --- | --- | --- |
| every row says `source: synthetic` | no provider reachable | install `idx` / `ccxt`; check `GET /api/health` |
| `idx ... failed: blocked` | idx.co.id bot protection on this network | use `--offline` cache or the `ipot` source: `idx stocks foreign BBCA --source ipot` |
| no trades in the backtest | filters veto everything (by design) | read the skip funnel: `metrics.filters_blocked` / `metrics.rejections` |
| engine opens nothing | daily-loss or losing-streak kill switch | `/api/engine/status` → `status.last_errors`, ledger `skip` rows |
| `position already open for this symbol` | duplicate guard | expected: one position per symbol at a time |
| `duplicate client_id refused` | a retried order | expected: idempotency working |

## Cost model used by the risk gate, the backtest and the paper broker

Fees are fixed; the *fill* is modelled (`trading_office/costs.py`):

| market | fees | latency | slippage (`bps`) | cap | gap reject | cooldown |
| --- | --- | --- | --- | --- | --- | --- |
| crypto | 5.5 bps taker per side | 400 ms | `1.5 + k_slip × ATR% + 1.5 × √participation%`, `k_slip` 0.06 → 0.18 | 45 bps | 2.0 ATR / 3× spread cap | 3 cycles |
| IDX | 0.15 % buy, 0.25 % sell (incl. 0.1 % sales tax) | 900 ms | `2.0 + k_slip × ATR% + 2.5 × √participation%`, `k_slip` 0.05 → 0.125 | 80 bps | 1.5 ATR / 2.5× spread cap | 2 sessions |

`k_slip` is the ATR fraction the fill is pushed against you (the price form of the same
model: `fill = open ± k_slip × ATR`). It rises linearly from `atr_mult` to
`atr_mult × shock_slippage_mult` as ATR stretches from 1× to `shock_atr_mult`× its own
50-bar mean, so a flash-crash bar pays panic money while a merely volatile instrument does
not.

`participation % = notional / median(quote volume over 20 bars)`. The risk gate still
refuses any target smaller than `3 ×` the round-trip cost, and the live engine defers an
entry whose live cost drifted more than 40 % above the plan.

```sh
python -m trading_office.cli costs --market crypto            # show the model
python -m trading_office.cli costs --market idx --notional 50000000 --atr-pct 1.8
```

Adjust the model per market in `profiles.<market>.execution_costs` (`latency_ms`,
`slippage_base_bps`, `atr_mult` = k_slip, `impact_coeff`, `max_slippage_bps`,
`shock_atr_mult`, `shock_slippage_mult`, `gap_reject_atr`, `gap_reject_spread_mult`,
`gap_fill_fraction`, `gap_cooldown_bars`, `max_risk_multiplier`, `auction_windows`).

## Manual controls (CLOSE / CANCEL)

The Live Engine tab lists open positions and resting orders in one table and lets an
operator act on a single row. Two things are deliberately explicit:

| Action | Endpoint | What it does | What it refuses |
| --- | --- | --- | --- |
| **CLOSE** | `POST /api/trade/close` | reduce-only market order for that symbol only, then its resting brackets are cancelled and the trade is booked | a symbol the engine does not hold (a UI typo cannot send an order), and any broker that does not report a fill |
| **CANCEL** | `POST /api/order/cancel` | cancels one resting order at the venue | anything on the paper broker (it fills instantly and has no resting orders) |

`execution_gate` (in `/api/metrics`, `/api/engine/status` and every websocket frame) says
whether an action is real: `simulated: true` covers the paper broker **and** dry-run, and the
table prints a small `paper` / `dry-run` tag next to the side so a screenshot is never
mistaken for a live fill. IDX stays paper-only — there is no retail order API, so the button
simulates the sale and says so.

The browser never sends an order id it invented: the ids come from the venue listing in the
same frame that rendered the button, and a refusal (position vanished, order already filled)
is surfaced as a toast instead of a silent no-op.

## Live equity & drawdown dashboard

`GET /api/metrics?market=crypto` returns parallel `timestamps` / `equity` / `drawdown`
arrays, a `series` with `peak` and `drawdown_pct` per point, a `performance` block and a
compact `kpi` block, all built from the ledger — no separate history file to corrupt, and it
survives a restart. `WS /api/ws/metrics?market=…` pushes the identical frame once per
second, so the Live Engine tab does not poll for metrics at all:

```
GET  /api/metrics       → {status, equity[], drawdown_pct[], series[], kpi{}, performance{}}
WS   /api/ws/metrics    → {type:"metrics", seq, ...the same payload}   (1 Hz)
```

The tab is a two-column CSS grid: a 280 px **KPI sidebar** (win rate, profit factor, total
trades, plus max DD / net profit / equity and a websocket status dot) next to the equity
and drawdown canvases and the open-position list. Charting is `web/charts.js` (plain
canvas) — no CDN, works offline and behind the preview proxy.

**Reconnect** uses exponential backoff with jitter: `min(30 s, 1 s × 2^(attempt−1)) + 0–400
ms`, counter reset on `open`. While the socket is down the cards fall back to one HTTP
`/api/metrics` read, so the dashboard never shows stale numbers without saying so
(the status dot turns amber and the chart header shows `http fallback`).

```sh
curl -s 'localhost:8000/api/metrics?market=crypto' | jq '{status, kpi}'
curl -s 'localhost:8000/api/performance?market=idx'
```

Open positions contribute an unrealised mark to the last point; a flat book ends the curve
at the last closed trade.
