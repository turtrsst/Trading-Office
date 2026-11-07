"""MCP server exposing the engine to AI agents.

Two transports, one tool surface:

* ``python -m trading_office.api.mcp_server``            → stdio (Claude Code, Cursor, Codex, …)
* ``python -m trading_office.api.mcp_server --http``     → streamable HTTP on :8765/mcp

The tools return the *same JSON* as the REST API, so an agent that learned one knows the
other. Every response carries ``source`` (``idx`` / ``ccxt`` / ``synthetic``) so an agent
can tell live data from demo data without guessing.

Register with an agent, e.g. Claude Code::

    claude mcp add --scope user trading-office -- python -m trading_office.api.mcp_server
"""

from __future__ import annotations

import argparse
from typing import Any, Literal, Sequence

from ..config import Settings
from ..strategy.screener import Screener

_SETTINGS = Settings.load()
_SCREENER: Screener | None = None


def screener() -> Screener:
    global _SCREENER
    if _SCREENER is None:
        _SCREENER = Screener(_SETTINGS, workers=4)
    return _SCREENER


# --------------------------------------------------------------------------- tool bodies
def screen_market(
    market: Literal["idx", "crypto", "fx"] = "idx",
    limit: int = 15,
    symbols: str = "",
    deep: bool = True,
) -> dict[str, Any]:
    """Rank a market with the four-gate pipeline and return BUY/WATCH/SELL labels.

    Args:
        market: "idx" for IDX/IHSG equities, "crypto" for perpetual futures, "fx" for
            Exness CFDs (XAUUSD first).
        limit: how many symbols to scan (the most liquid first).
        symbols: optional comma-separated tickers to scan instead of the universe.
        deep: include bandarmology, foreign flow, KSEI ownership and corporate actions.
    """
    wanted = [s.strip() for s in symbols.split(",") if s.strip()] or None
    return screener().screen(market, symbols=wanted, limit=limit, deep_context=deep, with_risk=False)


def analyse_symbol(market: Literal["idx", "crypto", "fx"] = "idx", symbol: str = "BBCA") -> dict[str, Any]:
    """Full reading for one symbol: market filter, signal score, risk plan, IDX context.

    Args:
        market: "idx", "crypto" or "fx".
        symbol: a ticker ("BBCA", "TLKM") or a ccxt symbol ("BTC/USDT:USDT").
    """
    return screener().analyse(market, symbol)


def run_backtest(
    market: Literal["idx", "crypto", "fx"] = "crypto",
    symbol: str = "BTC/USDT:USDT",
    oos: float = 0.3,
    bars: int = 900,
) -> dict[str, Any]:
    """Walk-forward backtest on closed candles: select in-sample, report out-of-sample.

    Args:
        market: "idx", "crypto" or "fx".
        symbol: instrument to test.
        oos: fraction of the data kept untouched for out-of-sample validation.
        bars: how many historical bars to use.
    """
    from ..backtest import walk_forward
    from .server import _backtest_data

    data = _backtest_data(market, symbol, bars)
    return walk_forward(
        market=market, symbol=symbol, bars=data["bars"][-bars:], settings=_SETTINGS, oos_fraction=oos
    )


def execution_costs(market: Literal["idx", "crypto", "fx"] = "crypto") -> dict[str, Any]:
    """The fill model used by the backtest and the live engine.

    Returns latency, slippage coefficients and caps, the gap-rejection thresholds, the
    auction windows (IDX) and the partial-TP / trailing defaults — i.e. everything a
    caller needs to know *why* a fill was priced the way it was.
    """
    from .. import costs as cost_model

    profile = _SETTINGS.profile(market)
    return {
        "market": market,
        "model": cost_model.CostConfig.for_market(market, profile).to_dict(),
        "spread_cap_bps": float(profile.get("max_spread_bps", 8.0)),
        "stress_spread_cap_bps": cost_model.risky_spread_cap(profile),
        "management": _SETTINGS.section("management"),
    }


def reconcile_state(
    market: Literal["idx", "crypto", "fx"] = "crypto",
    adopt: bool = False,
    close_orphans: bool = False,
) -> dict[str, Any]:
    """Reconcile persisted state with the venue (the same call the engine makes on boot).

    Reports adopted / restored / closed / untracked / blocked positions and any position
    whose resting stop order is missing. ``adopt`` re-attaches tracking to a venue position
    the bot does not know about; ``close_orphans`` closes those positions instead.
    """
    from .server import _engine

    return _engine(market).startup_sync(adopt_untracked=adopt, close_orphans=close_orphans)


def idx_bandarmology(symbol: str = "BBCA", period: Literal["1d", "1w", "1m", "3m"] = "1w") -> dict[str, Any]:
    """Broker accumulation/distribution (bandarmology) for one IDX ticker.

    Args:
        symbol: IDX ticker.
        period: aggregation window.
    """
    return screener().idx.bandar(symbol.upper(), period=period)


def idx_broker_summary(symbol: str = "BBCA", date: str = "") -> dict[str, Any]:
    """Top buyers and sellers from the broker summary (bandarmology input).

    Args:
        symbol: IDX ticker.
        date: optional session date YYYY-MM-DD (defaults to the latest session).
    """
    return screener().idx.broker(symbol.upper(), date=date or None)


def idx_foreign_flow(symbol: str = "BBCA", days: int = 20) -> dict[str, Any]:
    """Foreign net buy/sell per session plus totals and the current streak.

    Args:
        symbol: IDX ticker.
        days: number of sessions to read.
    """
    return screener().idx.foreign(symbol.upper(), days=days)


def idx_ownership(symbol: str = "BBCA") -> dict[str, Any]:
    """KSEI register: holders above 1 % of the ticker, with month-over-month change.

    Args:
        symbol: IDX ticker.
    """
    return screener().idx.ownership(symbol.upper())


def idx_corporate_actions(symbol: str = "BBCA") -> dict[str, Any]:
    """Dividends, rights issues, splits, bonus shares and RUPS for one IDX ticker."""
    return screener().idx.actions(symbol.upper())


def market_overview(market: Literal["idx", "crypto", "fx"] = "idx") -> dict[str, Any]:
    """IHSG indices and breadth (IDX) or tickers and spreads (crypto)."""
    from . import server

    return server.market_idx() if market == "idx" else server.market_crypto()


def candles(
    market: Literal["idx", "crypto", "fx"] = "idx", symbol: str = "BBCA", limit: int = 200
) -> dict[str, Any]:
    """Closed OHLCV candles plus indicator series (EMA, RSI, MACD, Bollinger, ATR)."""
    from . import server

    return server._candles_payload(market, symbol, limit)


TOOLS = {
    "screen_market": screen_market,
    "analyse_symbol": analyse_symbol,
    "run_backtest": run_backtest,
    "market_overview": market_overview,
    "candles": candles,
    "execution_costs": execution_costs,
    "reconcile_state": reconcile_state,
    "idx_bandarmology": idx_bandarmology,
    "idx_broker_summary": idx_broker_summary,
    "idx_foreign_flow": idx_foreign_flow,
    "idx_ownership": idx_ownership,
    "idx_corporate_actions": idx_corporate_actions,
}

DESCRIPTIONS = {
    "screen_market": "Rank a market (IDX or crypto) through the market-filter -> signal -> risk pipeline and return BUY/WATCH/HOLD/REDUCE/SELL labels with reasons.",
    "analyse_symbol": "One-symbol deep dive: the four gates, the risk plan (stop, target, size), indicators and IDX context.",
    "run_backtest": "Walk-forward backtest: parameters chosen in-sample, metrics reported on unseen out-of-sample data.",
    "market_overview": "IHSG indices and breadth, or crypto tickers with spreads.",
    "candles": "Closed candles and indicator series for charting.",
    "execution_costs": "Latency, ATR/impact slippage, gap guards, auction windows and the partial-TP/trailing defaults.",
    "reconcile_state": "Compare persisted positions with the exchange: adopt, restore, close or block, and flag missing stops.",
    "idx_bandarmology": "Broker accumulation/distribution (bandarmology) for an IDX ticker.",
    "idx_broker_summary": "Broker summary: the top buyers and sellers of an IDX ticker.",
    "idx_foreign_flow": "Foreign net flow per session with totals and streak.",
    "idx_ownership": "KSEI 1%+ holders and their monthly change.",
    "idx_corporate_actions": "Dividends, rights, splits, bonus shares and RUPS.",
}

PARAMS = {
    "screen_market": {"market": "idx|crypto", "limit": "int", "symbols": "csv", "deep": "bool"},
    "analyse_symbol": {"market": "idx|crypto", "symbol": "str"},
    "run_backtest": {"market": "idx|crypto", "symbol": "str", "oos": "float", "bars": "int"},
    "market_overview": {"market": "idx|crypto"},
    "candles": {"market": "idx|crypto", "symbol": "str", "limit": "int"},
    "execution_costs": {"market": "idx|crypto"},
    "reconcile_state": {"market": "idx|crypto", "adopt": "bool", "close_orphans": "bool"},
    "idx_bandarmology": {"symbol": "str", "period": "1d|1w|1m|3m"},
    "idx_broker_summary": {"symbol": "str", "date": "YYYY-MM-DD"},
    "idx_foreign_flow": {"symbol": "str", "days": "int"},
    "idx_ownership": {"symbol": "str"},
    "idx_corporate_actions": {"symbol": "str"},
}


def tool_catalog() -> list[dict[str, Any]]:
    """Plain-JSON tool list (served by /api/mcp/tools for non-MCP agents)."""
    return [
        {"name": name, "description": DESCRIPTIONS[name], "parameters": PARAMS[name]}
        for name in TOOLS
    ]


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    if name not in TOOLS:
        raise KeyError(f"unknown tool {name!r}; available: {', '.join(TOOLS)}")
    return TOOLS[name](**(arguments or {}))


# --------------------------------------------------------------------------- build
def build_server():
    """Build the MCP server (requires the ``mcp`` package)."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "the mcp package is required for the MCP server: pip install mcp"
        ) from exc

    server = MCPServer(
        name="trading-office",
        title="Trading Office",
        instructions=(
            "IDX (IHSG) and crypto analysis for the four-gate Trading Office engine. "
            "Every result says whether it came from 'idx', 'ccxt' or 'synthetic' demo data. "
            "Screen first, then analyse_symbol for detail, and run_backtest before trusting "
            "any parameter set. Nothing here is investment advice."
        ),
        version="1.0.0",
    )
    for name, fn in TOOLS.items():
        server.add_tool(fn, name=name, description=DESCRIPTIONS[name])
    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP server for Trading Office.")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = build_server()
    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
