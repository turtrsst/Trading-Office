# REST API

Base URL: `http://localhost:8000` (the web terminal is served from `/`). All payloads are
JSON. Every data-bearing response carries a `source` (`idx` | `ccxt` | `synthetic`) so a
consumer can tell live from demo data without guessing.

```sh
curl -s localhost:8000/api/screen?market=idx | jq '.rows[:3] | .[] | {symbol,label,conviction}'
```

## Meta

### `GET /api/health`

```json
{
  "ok": true,
  "ts": "2026-11-06T21:49:49+07:00",
  "providers": {
    "idx":    {"command": "idx", "available": false, "cache_ttl_s": 120, "install": "npm install -g github:fzn0x/idx"},
    "crypto": {"exchange": "bybit", "available": false, "install": "pip install ccxt"}
  },
  "execution": {"paper": true, "dry_run": true, "exchange": "bybit", "testnet": true},
  "data_mode": "synthetic"
}
```

### `GET /api/config`
The effective configuration (thresholds, profiles, execution mode) and the file it came
from.

### `GET /api/mcp/tools`
The MCP tool catalog as plain JSON (name, description, parameters) plus copy-paste
registration snippets — for agents that cannot speak MCP.

## Market overview

### `GET /api/market/idx?refresh=false`
`indices[]` (IHSG, LQ45, …), `ihsg`, `breadth{advancing,declining,unchanged}`,
`top_movers[]`, and the same `screening` object as `/api/screen`.

### `GET /api/market/crypto`
`tickers[]` with `price`, `bid`, `ask`, `spread_bps`, `change_pct`, `source`, plus
`screening`.

## Screening

### `GET /api/screen?market=idx&limit=20&symbols=BBCA,TLKM&deep=true`

| param | meaning |
| --- | --- |
| `market` | `idx` or `crypto` |
| `limit` | how many symbols (most liquid first when `symbols` is omitted) |
| `symbols` | comma-separated override |
| `deep` | include KSEI ownership and corporate actions (more provider calls) |

```json
{
  "market": "idx",
  "scanned": 18,
  "counts": {"STRONG_BUY": 1, "BUY": 2, "WATCH": 5, "HOLD": 7, "REDUCE": 2, "SELL": 1},
  "rows": [
    {
      "symbol": "BBCA", "name": "Bank Central Asia", "price": 8250, "change_pct": 0.61,
      "label": "SELL", "conviction": -7.1, "action": "SHORT", "score": 5, "stage": "signal",
      "reasons": ["macd_confirmation +1 ...", "bandar big distribution", "foreign net sell"],
      "components": {"pipeline": -3.1, "trend_filter": -1.0, "bandar": -2.5, "foreign_flow": -1.5},
      "context": {"filter_checks": {...}, "bandar": {...}, "foreign": {...}, "fundamentals": {...}},
      "source": "synthetic"
    }
  ]
}
```

Labels: `STRONG_BUY`, `BUY`, `WATCH`, `HOLD`, `REDUCE`, `SELL`. On IDX a `SHORT` reading is
reported as `SELL` and `executable: false`, because the cash market is long-only here.

## One symbol

### `GET /api/symbol/{market}/{symbol}`
Slashes are allowed in `symbol` (`/api/symbol/crypto/ETH/USDT:USDT`), and `?refresh=true`
drops the provider cache first. Returns an `execution_costs` block (spread, expected
slippage, risk multiplier) next to the four gates:

- `scan` — the four gates: `filter.checks[]`, `signal.components[]` with weights,
  `risk.plan` (entry, stop, target, size, lots, RR, costs) or `risk.rejections[]`,
- `row` — the label and conviction score,
- `candles[]`, `htf_candles[]`, `quote`,
- `context` — bandarmology, broker summary, foreign flow, ownership, corporate actions,
  fundamentals (IDX only).

### `GET /api/candles/{market}/{symbol}?limit=260`
Closed candles and indicator series aligned 1:1 with `bars` (`ema50`, `ema200`, `rsi14`,
`macd`, `macd_signal`, `macd_hist`, `bb_upper`, `bb_mid`, `bb_lower`, `atr14`).

## Backtest

`POST /api/backtest` also returns `drawdown_curve` (the OOS `equity_curve` with `peak` and
`drawdown_pct` per point) and, in `out_of_sample.metrics`, `max_drawdown_pct` (closed
trades) next to `max_drawdown_pct_mtm` (worst open excursion), `avg_mae_r`, `profit_factor`
(`null` = no losing trade), `partials`, `gap_stops` and the `costs` block.

### `POST /api/backtest`

```json
{"market": "crypto", "symbol": "XRP/USDT:USDT", "oos": 0.35, "bars": 1500,
 "htf_factor": 16, "min_score": 5, "rr_target": 2.0}
```

With `oos > 0` the response is a walk-forward report:

```json
{
  "chosen_params": {"signal": {"min_score": 5}},
  "split_time": "2026-08-01T12:00:00+07:00",
  "in_sample":    {"metrics": {...}, "trades": [...]},
  "out_of_sample":{"metrics": {"trades": 7, "win_rate_pct": 85.7, "expectancy_r": 0.62,
                               "profit_factor": 5.65, "max_drawdown_pct": 0.2,
                               "rejections": {"score_below_min": 115},
                               "filters_blocked": {"trend": 209}}, "trades": [...]}
}
```

With `oos = 0` it returns a single `BacktestResult` (`metrics`, `trades`,
`equity_curve`). See [STRATEGY_LOGIC.md §6](STRATEGY_LOGIC.md) for the honesty rules.

## Live engine

| endpoint | body | effect |
| --- | --- | --- |
| `GET /api/engine/status?market=crypto` | — | status (incl. `status.reconciled` and `status.partials`), broker snapshot, positions, last scans, ledger tail, equity curve, `costs`, `gap_cooldowns` |
| `POST /api/engine/start` | `{"market":"crypto","symbols":["BTC/USDT:USDT"]}` | starts the background loop |
| `POST /api/engine/stop` | `{"market":"crypto"}` | stops it |
| `POST /api/engine/cycle` | `{"market":"idx"}` | exactly one scan cycle (synchronous) |
| `POST /api/engine/close-all` | `{"market":"crypto"}` | flatten everything |
| `POST /api/engine/reconcile` | `{"market":"crypto","adopt":false,"close_orphans":false}` | compare `state/positions.json` with the exchange (the boot-time `startup_sync()`) |
| `GET /api/cost-model?market=crypto` | — | the fill model: latency, slippage coefficients/caps, gap thresholds, auction windows, partial-TP defaults |
| `GET /api/metrics?market=crypto&limit=200` | — | `status`, parallel `timestamps`/`equity`/`drawdown`, `series[{ts,equity,peak,drawdown_pct}]`, `performance`, `kpi{win_rate, profit_factor, total_trades, max_drawdown_pct, net_profit_pct, equity}` |
| `WS /api/ws/metrics?market=crypto` | — | the same JSON as `/api/metrics` pushed once per second with `{type:"metrics", seq}`; the client answers `{"type":"pong"}` and is dropped after 3 unanswered cycles |
| `GET /api/performance?market=crypto` | — | profit factor, max drawdown, win rate, net profit % of the live/paper book |
| `GET /api/engine/kill-switch?market=crypto` | — | daily-loss and consecutive-loss breaker state (`blocked`, `reasons`, `day_pnl`) |
| `POST /api/engine/resume` | `{"market":"crypto"}` | human override of the breaker for the current day (ledgered) |
| `GET /api/ledger?market=crypto&limit=120` | — | raw audit trail |

One cycle returns:

```json
{"event": "cycle", "market": "crypto", "duration_s": 0.42,
 "scans": [{"symbol": "BTC/USDT:USDT", "action": "WAIT", "score": 3, "stage": "signal",
            "bar_time": "2026-11-06T15:45:00+07:00", "reasons": ["score 3/5"]}],
 "status": {"orders": 0, "scans": 4, "blocked": 4, "equity": 1000.0}}
```

## Errors

| code | when |
| --- | --- |
| 422 | request body/schema validation (FastAPI) |
| 500 | unhandled pipeline error — `{"detail": "TypeError: ..."}` |

Provider failures do **not** produce 5xx: the adapters fall back to synthetic data and add
a warning, so a dashboard stays up when idx.co.id or the exchange refuses.

## Notes for agents

- Prefer `/api/screen` first (cheap), then `/api/symbol/...` for detail, then
  `/api/backtest` before trusting any parameter.
- `context.filter_checks` contains the four gate results verbatim — quote them instead of
  re-deriving.
- Check `source` on every row; `synthetic` means simulated, deterministic demo data.
