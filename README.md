# Trading Office

An **Expert-Advisor style trading engine** — the MQL5 pipeline rebuilt as a transparent
Python system with a web terminal, a CLI, a REST API and an MCP server:

```
[ Market Filter ] ──▶ [ Signal Generation ] ──▶ [ Risk Validation ] ──▶ [ Order Execution ]
```

Two switches, one engine:

| mode | universe | data | execution |
| --- | --- | --- | --- |
| **IDX / IHSG** | Indonesian equities | [`idx`](https://github.com/fzn0x/idx) CLI/MCP (prices, bandarmology, broker summary, foreign flow, KSEI, corporate actions) | paper (IDX cash is long-only here) |
| **Crypto** | perpetual futures (BTC, ETH, SOL, XRP, …) | `ccxt` (bybit / binance / bitget / okx) | paper by default, testnet/live `ccxt` when configured |

> **This is research and simulation tooling, not investment advice.** Every payload says
> whether it came from a live provider (`idx`, `ccxt`) or from the deterministic
> synthetic series used when no provider is reachable (`synthetic`).

---

## Ringkasan (ID)

- **Satu pipeline, empat gerbang**: filter pasar → sinyal → risiko → eksekusi. Semua
  aturan ada di `config/settings.json`, tidak ada lot size yang di-hardcode.
- **Hanya candle tertutup**: indikator dihitung dari bar yang sudah close; bar berjalan
  dibuang. Sinyal dinilai **sekali per bar** (`state/signals.json`), jadi candle goyang
  tidak bisa membuka dua posisi.
- **Anti-duplikat 3 lapis**: kunci bar + satu posisi per simbol + client order id
  deterministik (`to-<symbol>-<bar>-<side>`) + cooldown setelah posisi ditutup.
- **Stop loss wajib**: struktural (swing) atau `1.5 × ATR`, mana yang lebih lebar; TP
  minimal 1:1.5 (default 1:2) dan ditolak jika target tidak menutup biaya (spread +
  komisi + slippage × 3).
- **Emiten IDX**: screening BUY/WATCH/SELL dengan bandarmology, broker summary, foreign
  flow, KSEI 1%, dan corporate actions (dividen, right, RUPS).
- **Crypto**: multi-koin, long/short, leverage dibatasi, margin per trade dihitung dari
  risiko (bukan `15 USDT` tetap seperti skrip awal).
- **Unseen data**: backtest walk-forward — parameter dipilih in-sample, metrik dilaporkan
  pada potongan OOS yang tidak pernah dipakai memilih parameter.

```sh
python -m trading_office.cli serve --port 8000     # web terminal + REST
python -m trading_office.cli screen --market idx   # screening emiten
python -m trading_office.cli analyse --market crypto --symbol BTC/USDT:USDT
python -m trading_office.cli engine --market crypto # eksekusi paper
```

---

## Quick start

```sh
git clone <this repo> && cd Trading-Office
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m trading_office.cli doctor          # what is live, what falls back
python -m trading_office.cli serve --port 8000
# open http://localhost:8000
```

The app runs with **zero providers installed**: every adapter falls back to a
deterministic synthetic series (same symbol → same candles) and labels it, so the UI,
the CLI, the tests and the agent tools are always usable.

For live IDX data install the `idx` CLI (one static binary):

```sh
npm install -g github:fzn0x/idx
# or: curl -fsSL https://raw.githubusercontent.com/fzn0x/idx/main/install.sh | sh
# or: cargo install --locked ihsg-cli
idx stocks quote BBCA                      # sanity check
idx ownership sync                         # KSEI 1%+ register (once, then monthly)
```

For live crypto data: `pip install ccxt` (public endpoints need no keys).

---

## The strategy, in one page

Full reasoning: **[docs/STRATEGY_LOGIC.md](docs/STRATEGY_LOGIC.md)**. The rules the code
implements:

### 1 · Market filter — "may I trade this symbol at all?"

| filter | rule | default |
| --- | --- | --- |
| Trend | higher-timeframe close vs EMA200, tolerance `0.25 × ATR` | crypto HTF `4h` (from `15m`), IDX HTF `1wk` (from `1d`) |
| Volatility | `ATR%` inside an absolute band **and** `0.6–2.5×` its own 100-bar median | crypto `0.25–6 %`, IDX `0.8–9 %` |
| Session | local-time windows, weekday guard, news blackout, data freshness | crypto `07–10` & `13–22` WIB, IDX `09:15–11:30` & `13:30–15:45` WIB |
| Spread | `spread ≤ max bps` (book if available, else 1 tick + slippage, marked *estimated*), liquidity floor | crypto `8 bps`, IDX `60 bps`, IDX min `5 B IDR` traded |

`min_filters_passed = 3` of 4, and the trend filter is **mandatory** (`require_trend_filter`)
because it supplies the directional bias.

### 2 · Signal — "is there a setup on the last closed bar?"

Confluence score out of **9**: trend alignment (2), trigger — RSI cross out of 30/70 **or**
Bollinger re-entry (2), MACD confirmation (1), momentum health (1), BB mid reclaim (1),
candle structure (1), volume expansion (1).

An entry needs `score ≥ 5` **and** at least one *trigger* **and** one *confirmation*
(so a pure trend can never fire), and it is vetoed when overextended (RSI > 75 and price
above the upper band by 0.5 ATR) or when long and short scores conflict.

### 3 · Risk — "how big, and where do I get out?"

```
stop   = wider(structure swing, 1.5 × ATR), floored at max(3 × spread, 2 ticks, 0.35 ATR)
target = max(structural level, 2 × risk)         → reject if RR < 1.5 or inside costs
size   = (equity × 1 %) / |entry − stop|         → capped by margin, exposure, liquidity
```

Every order carries an SL. Portfolio guards: max 4 concurrent positions, 4 % open risk,
2 % daily loss and 4 consecutive losses stop new entries.

### 4 · Management & execution

**Mandatory partial at 2R**: half the position is closed with a reduce-only order and the
stop moves to **break-even + fees** in the same bar, so the runner cannot lose. The
remainder then trails `1.5 × ATR` behind the best price (ratchet-only, minimum step
`0.25 × ATR`). Plus a time stop after 96 bars (crypto) / 15 sessions (IDX) if the trade
never reached +0.5R, invalidation on an opposite signal, weekend/close flattening, and
exchange-side brackets for live crypto orders.

### 5 · Costs, gaps and restarts

- Fills are never free or instant: the backtest *and* the live engine apply the same
  model — `latency (400 ms crypto / 900 ms IDX)` + `base + k_slip × ATR% + impact × √participation`
  slippage, capped per market (`45` / `80` bps). `k_slip` **is** the ATR fraction the fill
  moves against you (`fill = open ± k_slip × ATR`), and it triples on a shock bar (ATR ≥ 3×
  its own 50-bar mean) so a flash crash costs panic money instead of the calm-tape rate.
- Gate 1 refuses entries after a gap of `2.0 ATR` (crypto) / `1.5 ATR` (IDX), or when the
  spread blows past `3×` its cap, and blocks the symbol for a cooldown; a stop that is
  gapped through fills *worse* than the stop, and that loss shows up in the results.
- Size shrinks (up to 1.75×) while the book is stressed, and the live engine re-prices
  every entry before sending it — a fill that got expensive is deferred, not paid for.
- A stop can still be **jumped**: a gap through the bracket fills worse than the stop and
  can lose more than the 1 % per-trade budget. The guard makes it rarer, the fill model
  makes it visible in the results (`gap_stops`, `gap_risk` per trade), and position size —
  not the stop — is what bounds the damage.
- **Crash-safe**: on startup the engine reconciles `state/positions.json` against
  `exchange.fetch_positions()`, restores (or adopts) what really exists, drops ghosts,
  flags positions without a resting stop, and never re-sells a partial (`partial_done`
  is persisted).

---

## Features

### IDX / IHSG desk
- **Screening** of the most liquid names with `BUY / WATCH / HOLD / REDUCE / SELL` and a
  transparent conviction score (pipeline ±4, trend ±1, bandarmology ±2.5, foreign flow
  ±1.5, valuation ±1).
- **Bandarmology** — broker accumulation/distribution signal and phase, top-5 net share
  of value traded, bandar average price vs last price.
- **Broker summary** — top buyers and sellers with broker code, class (local / foreign /
  state-owned), lots and value.
- **Foreign flow** — per-session net, totals, net-buy/net-sell streak.
- **KSEI** — holders above 1 % with month-over-month change.
- **Corporate actions** — cash/stock dividends, splits, bonus, rights, RUPS calendar.
- **IHSG overview** — index, breadth bar, top movers.

### Crypto desk
- Multi-symbol scan (BTC, ETH, SOL, XRP by default), long **and** short.
- Ticker, spread (bps) and order-book reads; the spread filter uses the real book.
- Leverage capped (3× default), margin per trade derived from risk, not fixed notional.
- Execution through `ccxt` with `stopLoss` / `takeProfit` trigger prices and
  `clientOrderId` idempotency; testnet-first.

### Terminal, API, agents
- **Web terminal** (`web/`) — screening table, one-symbol deep dive with candles/RSI/EMA
  chart, gate cards, trade plan, bandarmology tab, walk-forward backtest tab (equity +
  drawdown), live engine monitor with **real-time equity and drawdown charts pushed once a
  second** over a websocket (`/api/ws/metrics`, exponential-backoff reconnect with HTTP
  fallback) in a KPI sidebar (win rate, profit factor, trade count) next to the charts, an
  **active-trades / open-orders table with per-row CLOSE and CANCEL** (inline confirm
  dialog + toast stack, no `alert()`), a **server-health badge** driven by the websocket
  lifecycle (Healthy / Reconnecting / Offline, with a 5 s frame watchdog), audit ledger,
  and a CLI/MCP reference tab.
  Charting is the repo's own canvas module — no CDN, works offline and behind the preview
  proxy.
- **CLI** — `screen`, `analyse`, `bandar`, `broker`, `foreign`, `ownership`, `actions`,
  `crypto`, `backtest`, `engine`, `costs`, `reconcile`, `serve`, `doctor`; every command
  takes `--json`.
- **REST** — documented in [docs/API.md](docs/API.md).
- **MCP server** — 12 tools over stdio or streamable HTTP, same JSON as REST:
  [docs/MCP.md](docs/MCP.md).

---

## Backtesting on unseen data

```sh
# in-sample selection + out-of-sample report
python -m trading_office.cli backtest --market crypto --symbol XRP/USDT:USDT --oos 0.35
python -m trading_office.cli backtest --market idx --symbol BBCA --oos 0.35 --bars 900

# or from locked datasets
python scripts/make_demo_data.py               # writes data/*.json
python -m trading_office.backtest --market idx --data data/idx_BBCA_1d.json --oos 0.3
```

Honesty rules in `trading_office/backtest/`:

- decisions use closed bars only; **fills happen at the next bar's open**, moved by a
  latency drift, then by ATR/impact slippage on top of spread and commission;
- gaps fill at the open, a stop that is gapped through fills between the stop and the
  open (never at the stop), and when a stop and a target sit inside the same bar the
  **stop** is assumed hit first (pessimistic);
- the OOS report is generated on a slice that never influenced parameter selection, and
  indicators are warmed on the full series so the slice is not handicapped.

Results are reported as trades, win rate, expectancy in R, profit factor, **two drawdowns**
(closed-trade peak-to-trough and mark-to-market, which counts the worst open excursion),
Sharpe, partials taken, gap-through stops, and a **funnel of why trades were skipped**
(which filter, which signal rule). A cost block reports realised slippage p50/p90/worst,
the delay cost of the latency model, how many fills hit the slippage cap, and the pnl
banked by partials — so the execution assumptions are auditable, not folklore.

---

## Layout

```
config/settings.json          every threshold the engine uses (no magic numbers in code)
trading_office/
  indicators.py               SMA/EMA/RSI/MACD/ATR/Bollinger/swings — pure python, no pandas
  market_filter.py            gate 1: trend, volatility, session/news, spread/liquidity
  signals.py                  gate 2: confluence score with trigger + confirmation requirement
  risk.py                     gate 3: stop/target/size, cost sanity, portfolio guards
  management.py               trailing, break-even, partial TP, time & invalidation exits
  execution/broker.py         paper broker + ccxt broker (testnet first), idempotent orders
  strategy/runner.py          one scan = the four gates, shared by API/CLI/screener
  strategy/screener.py        universe scan, conviction score, IDX context, labels
  engine.py                   live loop: bar locks, duplicates, management, ledger
  state.py                    atomic JSON state (signals, positions, cooldowns, ledger)
  market/idx_client.py        idx CLI bridge (cached, failure-tolerant, synthetic fallback)
  market/crypto_client.py     ccxt bridge (closed candles only, ticks, book)
  market/demo.py              deterministic synthetic series (labelled everywhere)
  api/server.py               FastAPI: REST + static UI
  api/mcp_server.py           MCP tools (stdio / streamable HTTP)
  backtest/engine.py          bar-by-bar backtester + walk-forward
  cli.py                      terminal
web/                          the web terminal (no build step, no frameworks)
tests/                        103 unit + API tests, all offline
docs/                         strategy logic, API, MCP, operations
scripts/make_demo_data.py     writes data/*.json for the backtester
```

## Secrets & state at rest

```bash
cp .env.example .env      # git-ignored; real env vars win over the file
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# .env: TO_STATE_ENCRYPTION_KEY=…   (or TO_STATE_PASSPHRASE=…)
trading-office doctor     # shows credential presence + whether state/ is sealed
```

* `TO_API_KEY` / `TO_API_SECRET` (aliases `CCXT_API_KEY` / `CCXT_SECRET_KEY`) enable live
  ccxt orders — and only together with `execution.paper=false` + `dry_run=false`.
* Missing keys raise instead of silently falling back to paper; a wrong state key raises
  `StateLocked` instead of pretending the book is empty.
* `MT5_*` variables are reported as **ignored**: MetaTrader 5 is not a supported venue.
* `GET /api/security` returns presence, source and an 8-char fingerprint — never a secret.

## Tests

```sh
pip install pytest httpx
python -m pytest -q           # 103 passed, entirely offline
```

The suite covers indicator maths, each filter's pass/block path, the no-trigger veto, the
per-bar state lock, tick rounding, lot sizing, cost rejection, portfolio guards, all five
management rules, broker idempotency, order placement (one per bar), stop-loss closure,
position restore after restart, every API endpoint, the engine's bar-lock idempotency and
the walk-forward split. On top of that: latency drift, ATR/impact slippage and its cap,
the gap guard (ATR and spread triggers), gap-through-stop fills, the risk multiplier,
partial TP at 2R (one action list, reduce-only, break-even ≥ entry + costs), the
below-minimum partial skip, the ATR ratchet, and every branch of `startup_sync()`
(restore / close ghost / untracked / adopt / venue-unreachable).

## Safety

- Paper broker is the default (`execution.paper = true`): fills are simulated, nothing reaches an exchange.
- Live crypto trading requires `execution.paper = false`, `execution.dry_run = false`
  **and** `TO_API_KEY` / `TO_API_SECRET` in the environment; testnet stays on unless you
  turn it off explicitly.
- IDX orders are never routed to a real venue: there is no public retail IDX order API
  here, so the IDX side is paper-only by design.
- The ledger (`state/ledger.jsonl`) records every decision, taken or skipped, with the
  score, reasons and sizing maths.

## License / attribution

- IDX data and analysis come from [`fzn0x/idx`](https://github.com/fzn0x/idx) (MIT) — the
  CLI/MCP binary is invoked, not vendored. Bandarmology thresholds are theirs.
- Crypto data and execution through [`ccxt`](https://github.com/ccxt/ccxt).
- The strategy is an original implementation of the pipeline in
  [docs/STRATEGY_LOGIC.md](docs/STRATEGY_LOGIC.md).
