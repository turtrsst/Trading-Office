# MCP server (for AI agents)

`trading_office/api/mcp_server.py` exposes the desk as Model Context Protocol tools. The
tools return the **same JSON** as the REST API, including `source`
(`idx` | `ccxt` | `synthetic`).

## Run

```sh
# stdio — what local agents launch (Claude Code, Cursor, Codex, Zed, Cline, …)
python -m trading_office.api.mcp_server

# streamable HTTP — for clients that only accept a URL
python -m trading_office.api.mcp_server --http --host 0.0.0.0 --port 8765
# connect to: http://<host>:8765/mcp
```

## Register

```sh
# Claude Code
claude mcp add --scope user trading-office -- python -m trading_office.api.mcp_server

# Codex
codex mcp add trading-office -- python -m trading_office.api.mcp_server

# Gemini CLI
gemini mcp add -s user trading-office python -m trading_office.api.mcp_server
```

```json
{
  "mcpServers": {
    "trading-office": {
      "command": "python",
      "args": ["-m", "trading_office.api.mcp_server"],
      "env": {"TO_CONFIG": "/abs/path/config/settings.json"}
    }
  }
}
```

`GET /api/mcp/tools` on the web server returns the same catalog as JSON for clients that
do not speak MCP.

## Tools

| tool | arguments | returns |
| --- | --- | --- |
| `screen_market` | `market` (`idx`\|`crypto`), `limit`, `symbols` (csv), `deep` | ranked rows with `label`, `conviction`, `score`, `reasons`, `components` |
| `analyse_symbol` | `market`, `symbol` | the four gates, the risk plan, indicators, IDX context |
| `run_backtest` | `market`, `symbol`, `oos`, `bars` | walk-forward: `chosen_params`, `in_sample`, `out_of_sample` |
| `market_overview` | `market` | IHSG indices + breadth, or crypto tickers + spreads |
| `candles` | `market`, `symbol`, `limit` | closed OHLCV + EMA/RSI/MACD/BB/ATR series |
| `idx_bandarmology` | `symbol`, `period` (`1d`\|`1w`\|`1m`\|`3m`) | accumulation/distribution signal, phase, top-5 net %, bandar P/L |
| `idx_broker_summary` | `symbol`, `date` | top buyers/sellers with broker codes, lots, value |
| `idx_foreign_flow` | `symbol`, `days` | per-session foreign net, totals, streak |
| `idx_ownership` | `symbol` | KSEI holders above 1 % and their monthly change |
| `idx_corporate_actions` | `symbol` | dividends, splits, bonus, rights, RUPS |

`execution_costs` returns the latency/slippage/gap model (and the partial-TP and trailing
defaults) for a market. `reconcile_state` runs the same startup reconciliation the engine
does and returns adopted / restored / closed / untracked / blocked positions plus any
missing stop order — call it before trusting a position list after a restart.

`GET /api/engine/kill-switch` exposes the circuit breaker (daily loss ≥ `max_daily_loss_pct`
or N consecutive losses); it is evaluated on every scan, so a blocked engine refuses new
entries until the next day or a `guard_resumed_day` override.

## Suggested agent loop

1. `market_overview` — is the tape risk-on or risk-off?
2. `screen_market` — shortlist `BUY`/`STRONG_BUY` (IDX) or `LONG`/`SHORT` (crypto).
3. `analyse_symbol` — read `filter.checks` and `risk.plan`; quote the reasons, do not
   invent them.
4. `run_backtest` — parameters on unseen data before any parameter is called "good".
5. Only then talk about a trade, and repeat the `source` field in the answer.

## Answer hygiene (please keep)

- Say **"synthetic demo data"** whenever `source` is `synthetic`; it is deterministic
  simulation, not a live quote.
- Cite the gate that blocked a trade (`trend` / `volatility` / `session` / `spread`) and
  the signal rule that was missing (`no trigger`, `no confirmation`, `score_below_min`,
  `overextended`, `conflict`).
- Never present the screener output as a recommendation to buy or sell someone's money;
  it is a computed reading of a mechanical system.
