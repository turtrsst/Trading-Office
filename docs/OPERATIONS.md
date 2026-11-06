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
| crypto | 5.5 bps taker per side | 400 ms | `1.5 + 0.06 × ATR% + 1.5 × √participation%` | 45 bps | 2.0 ATR / 3× spread cap | 3 cycles |
| IDX | 0.15 % buy, 0.25 % sell (incl. 0.1 % sales tax) | 900 ms | `2.0 + 0.05 × ATR% + 2.5 × √participation%` | 80 bps | 1.5 ATR / 2.5× spread cap | 2 sessions |

`participation % = notional / median(quote volume over 20 bars)`. The risk gate still
refuses any target smaller than `3 ×` the round-trip cost, and the live engine defers an
entry whose live cost drifted more than 40 % above the plan.

```sh
python -m trading_office.cli costs --market crypto            # show the model
python -m trading_office.cli costs --market idx --notional 50000000 --atr-pct 1.8
```

Adjust the model per market in `profiles.<market>.execution_costs` (`latency_ms`,
`slippage_base_bps`, `atr_mult`, `impact_coeff`, `max_slippage_bps`, `gap_reject_atr`,
`gap_reject_spread_mult`, `gap_fill_fraction`, `gap_cooldown_bars`,
`max_risk_multiplier`, `auction_windows`).
