"""FastAPI app: the web terminal's backend and the REST surface for agents.

Endpoints (all JSON, CORS open because the UI is served from the same origin and agents
may call it from anywhere)::

    GET  /api/health                 provider status (idx CLI, ccxt, config)
    GET  /api/config                 effective configuration (thresholds, profiles)
    GET  /api/market/idx             IHSG overview: index, breadth, screen summary
    GET  /api/market/crypto          crypto overview: tickers + spreads
    GET  /api/screen?market=idx      ranked BUY / WATCH / SELL list
    GET  /api/symbol/{market}/{sym}  one-symbol deep dive (all four gates + context)
    GET  /api/candles/{market}/{sym} candles + indicators for the chart
    POST /api/backtest               run the backtester on a symbol (in-sample / OOS)
    GET  /api/engine/status          live engine state
    POST /api/engine/start|stop      control the live (paper by default) engine
    POST /api/engine/cycle           run exactly one scan cycle
    POST /api/engine/close-all       flatten open positions
    GET  /api/ledger                 audit trail

MCP: ``POST /mcp`` is the streamable-HTTP MCP endpoint when the ``mcp`` package is
installed (``python -m trading_office.api.mcp_server``); ``GET /api/mcp/tools`` lists the
same tools as plain JSON so any agent can read the schema without an MCP client.
"""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import indicators as ind
from ..backtest import Backtester, walk_forward
from ..config import Settings
from ..engine import TradingEngine
from ..market import demo
from ..market.idx_client import mcp_registration_snippets
from ..strategy.screener import Screener

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
SETTINGS = Settings.load()
SCREENER = Screener(SETTINGS, workers=4)
IDX = SCREENER.idx
CRYPTO = SCREENER.crypto

_ENGINES: dict[str, TradingEngine] = {}
_ENGINE_LOCK = threading.RLock()


# --------------------------------------------------------------------------- helpers
def _engine(market: str) -> TradingEngine:
    with _ENGINE_LOCK:
        if market not in _ENGINES:
            _ENGINES[market] = TradingEngine(
                market=market,
                settings=SETTINGS,
                execute=not bool(SETTINGS.get("execution.dry_run", True)),
                state_dir=SETTINGS.get("engine.state_dir", "state"),
            )
        return _ENGINES[market]


def _iso_now() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Jakarta")).isoformat()


def _candles_payload(market: str, symbol: str, limit: int = 260) -> dict[str, Any]:
    profile = SETTINGS.profile(market)
    if market == "idx":
        data = SCREENER._idx_symbol_data(symbol.upper())
    else:
        data = SCREENER._crypto_symbol_data(symbol)
    bars = data["bars"][-limit:]
    closes = [b["close"] for b in data["bars"]]
    highs = [b["high"] for b in data["bars"]]
    lows = [b["low"] for b in data["bars"]]
    ema50 = ind.ema(closes, 50)
    ema200 = ind.ema(closes, 200)
    rsi = ind.rsi(closes, 14)
    macd_line, macd_sig, hist = ind.macd(closes)
    bb_up, bb_mid, bb_low = ind.bollinger(closes, 20, 2.0)
    atr = ind.atr(highs, lows, closes, 14)
    offset = len(closes) - len(bars)
    series = {
        "ema50": ema50[offset:],
        "ema200": ema200[offset:],
        "rsi14": rsi[offset:],
        "macd": macd_line[offset:],
        "macd_signal": macd_sig[offset:],
        "macd_hist": hist[offset:],
        "bb_upper": bb_up[offset:],
        "bb_mid": bb_mid[offset:],
        "bb_lower": bb_low[offset:],
        "atr14": atr[offset:],
    }
    return {
        "market": market,
        "symbol": symbol,
        "timeframe": profile.get("timeframe"),
        "source": data.get("source"),
        "bars": bars,
        "series": series,
        "quote": data.get("quote") or {},
        "warnings": data.get("warnings") or [],
    }


# --------------------------------------------------------------------------- schemas
class BacktestRequest(BaseModel):
    market: Literal["crypto", "idx"] = "crypto"
    symbol: str = "BTC/USDT:USDT"
    oos: float = Field(0.3, ge=0.0, le=0.6)
    bars: int = Field(900, ge=150, le=2000)
    htf_factor: int | None = Field(None, ge=2, le=40)
    min_score: int | None = Field(None, ge=1, le=9)
    rr_target: float | None = Field(None, ge=1.0, le=5.0)


class EngineRequest(BaseModel):
    market: Literal["crypto", "idx"] = "crypto"
    symbols: list[str] | None = None


# --------------------------------------------------------------------------- app
def create_app() -> FastAPI:
    app = FastAPI(
        title="Trading Office",
        version="1.0.0",
        description=(
            "Expert-Advisor style trading engine: market filter -> signal -> risk -> "
            "execution, for IDX equities and crypto perpetual futures."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "ts": _iso_now(),
            "version": "1.0.0",
            "providers": {
                "idx": IDX.status(),
                "crypto": {**CRYPTO.status(), "available": _crypto_ok()},
            },
            "execution": {
                "paper": bool(SETTINGS.get("execution.paper", True)),
                "dry_run": bool(SETTINGS.get("execution.dry_run", True)),
                "exchange": SETTINGS.get("execution.exchange_id"),
                "testnet": bool(SETTINGS.get("execution.testnet", True)),
            },
            "data_mode": "live" if (_idx_ok() or _crypto_ok()) else "synthetic",
            "config_path": str(SETTINGS.path),
        }

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {"config": SETTINGS.as_dict(), "path": str(SETTINGS.path)}

    @app.get("/api/mcp/tools")
    def mcp_tools() -> dict[str, Any]:
        from . import mcp_server

        return {
            "tools": mcp_server.tool_catalog(),
            "registration": mcp_registration_snippets(),
            "endpoint": "/mcp (streamable HTTP, run `python -m trading_office.api.mcp_server`)",
            "cli": "python -m trading_office.cli --help",
        }

    # ------------------------------------------------------------------ market overview
    @app.get("/api/market/idx")
    def market_idx(refresh: bool = False) -> dict[str, Any]:
        if refresh:
            IDX.cache.clear()
        indices = IDX.indices()
        symbol_count = 0
        rows = IDX.screen(limit=SETTINGS.get("idx.universe_limit", 40)).get("rows") or []
        symbol_count = len(rows)
        ups = sum(1 for r in rows if (r.get("change_pct") or 0) > 0)
        downs = sum(1 for r in rows if (r.get("change_pct") or 0) < 0)
        ihsg = None
        for row in (indices.get("data") or {}).get("indices", []) or []:
            if row.get("code") in ("COMPOSITE", "IHSG"):
                ihsg = row
                break
        screen = SCREENER.screen("idx", limit=int(SETTINGS.get("idx.universe_limit", 40)),
                                 deep_context=False)
        return {
            "market": "idx",
            "generated_at": _iso_now(),
            "source": indices.get("source") or ("idx" if rows else "synthetic"),
            "indices": (indices.get("data") or {}).get("indices") or [],
            "ihsg": ihsg,
            "breadth": {"advancing": ups, "declining": downs, "unchanged": max(0, symbol_count - ups - downs)},
            "universe_size": symbol_count,
            "top_movers": sorted(
                rows, key=lambda r: abs(r.get("change_pct") or 0), reverse=True
            )[:8],
            "screening": screen,
            "providers": {"idx": IDX.status()},
            "warnings": indices.get("warnings") or [],
        }

    @app.get("/api/market/crypto")
    def market_crypto(refresh: bool = False) -> dict[str, Any]:
        symbols = list(SETTINGS.get("profiles.crypto.symbols") or demo.CRYPTO_REFERENCE)
        out = []
        for symbol in symbols:
            tick = CRYPTO.ticker(symbol)
            price = float(tick.get("price") or 0.0)
            spread = (
                (float(tick["ask"]) - float(tick["bid"])) / price * 10_000.0
                if tick.get("bid") and tick.get("ask") and price
                else demo.spread_bps(symbol, price, "crypto")
            )
            data = CRYPTO.candles(symbol, str(SETTINGS.get("profiles.crypto.timeframe", "15m")), 120)
            closes = [b["close"] for b in data["bars"]]
            change = (closes[-1] / closes[0] - 1) * 100.0 if len(closes) > 1 else 0.0
            out.append(
                {
                    "symbol": symbol,
                    "price": price,
                    "bid": tick.get("bid"),
                    "ask": tick.get("ask"),
                    "spread_bps": round(spread, 2),
                    "change_pct": round(change, 2),
                    "quote_volume": tick.get("quote_volume"),
                    "source": tick.get("source"),
                    "warnings": tick.get("warnings") or [],
                }
            )
        screen = SCREENER.screen("crypto", limit=len(symbols))
        return {
            "market": "crypto",
            "generated_at": _iso_now(),
            "tickers": out,
            "screening": screen,
            "providers": {"crypto": CRYPTO.status()},
        }

    # ------------------------------------------------------------------ screening
    @app.get("/api/screen")
    def screen(
        market: Literal["idx", "crypto"] = "idx",
        limit: int = Query(20, ge=1, le=80),
        symbols: str = "",
        deep: bool = True,
    ) -> dict[str, Any]:
        wanted = [s.strip() for s in symbols.split(",") if s.strip()] or None
        return SCREENER.screen(market, symbols=wanted, limit=limit, deep_context=deep)

    @app.get("/api/symbol/{market}/{symbol:path}")
    def symbol_detail(market: Literal["idx", "crypto"], symbol: str) -> dict[str, Any]:
        try:
            return SCREENER.analyse(market, symbol)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    @app.get("/api/candles/{market}/{symbol:path}")
    def candles(market: Literal["idx", "crypto"], symbol: str, limit: int = Query(260, ge=60, le=1200)):
        try:
            return _candles_payload(market, symbol, limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ------------------------------------------------------------------ backtest
    @app.post("/api/backtest")
    def backtest(req: BacktestRequest) -> dict[str, Any]:
        data = _backtest_data(req.market, req.symbol, req.bars)
        bars = data["bars"][-req.bars :]
        overrides: dict[str, Any] = {}
        if req.min_score:
            overrides["signal"] = {"min_score": req.min_score}
        if req.rr_target:
            overrides["risk"] = {"rr_target": req.rr_target}
        bt = Backtester(
            market=req.market,
            settings=SETTINGS,
            signal_override=(overrides.get("signal") or {"min_score": SETTINGS.get("signal.min_score", 5)}),
            risk_override=overrides.get("risk"),
        )
        if req.oos and req.oos > 0:
            report = walk_forward(
                market=req.market,
                symbol=req.symbol,
                bars=bars,
                settings=SETTINGS,
                oos_fraction=req.oos,
                htf_factor=req.htf_factor,
            )
            report["source"] = data.get("source")
            return report
        result = bt.run(req.symbol, bars, htf_factor=req.htf_factor).to_dict()
        result["source"] = data.get("source")
        return result

    # ------------------------------------------------------------------ engine
    @app.get("/api/engine/status")
    def engine_status(market: Literal["crypto", "idx"] = "crypto") -> dict[str, Any]:
        engine = _engine(market)
        return engine.snapshot()

    @app.post("/api/engine/start")
    def engine_start(req: EngineRequest) -> dict[str, Any]:
        engine = _engine(req.market)
        engine.start(background=True)
        return {"started": True, "status": engine.status.to_dict()}

    @app.post("/api/engine/stop")
    def engine_stop(req: EngineRequest) -> dict[str, Any]:
        engine = _engine(req.market)
        engine.stop()
        return {"stopped": True, "status": engine.status.to_dict()}

    @app.post("/api/engine/cycle")
    def engine_cycle(req: EngineRequest) -> dict[str, Any]:
        engine = _engine(req.market)
        summary = engine.cycle()
        return summary

    @app.post("/api/engine/close-all")
    def engine_close_all(req: EngineRequest) -> dict[str, Any]:
        engine = _engine(req.market)
        return {"closed": engine.force_close_all("manual flatten from web terminal")}

    @app.get("/api/ledger")
    def ledger(market: Literal["crypto", "idx"] = "crypto", limit: int = Query(120, ge=1, le=1000)):
        engine = _engine(market)
        return {"market": market, "events": engine.state.ledger(limit), "account": engine.state.account()}

    # ------------------------------------------------------------------ static UI
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(str(WEB_DIR / "index.html"))

        @app.get("/mcp")
        def mcp_hint() -> dict[str, Any]:
            return {
                "endpoint": "/mcp",
                "note": (
                    "The MCP transport needs its own process: run "
                    "`python -m trading_office.api.mcp_server --http --port 8765` and point "
                    "your client at http://<host>:8765/mcp. The tool catalog is at "
                    "/api/mcp/tools."
                ),
            }

    @app.exception_handler(Exception)
    async def unhandled(_request, exc: Exception) -> JSONResponse:  # pragma: no cover
        return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

    return app


def _backtest_data(market: str, symbol: str, bars: int) -> dict[str, Any]:
    """Fetch enough history for a backtest (more than a dashboard refresh needs)."""
    if market == "idx":
        period = "5y" if bars > 400 else "2y"
        candles = IDX.candles(symbol.upper(), period=period, interval="1d")
        htf = IDX.candles(symbol.upper(), period="10y", interval="1wk")
    else:
        candles = CRYPTO.candles(symbol, str(SETTINGS.get("profiles.crypto.timeframe", "15m")), max(bars, 400))
        htf = CRYPTO.candles(symbol, str(SETTINGS.get("profiles.crypto.trend_timeframe", "4h")), 300)
    return {"bars": candles["bars"], "htf_bars": htf["bars"], "source": candles.get("source"),
            "warnings": candles.get("warnings") or []}


def _idx_ok() -> bool:
    return IDX.available()


def _crypto_ok() -> bool:
    try:
        CRYPTO.exchange()
        return True
    except Exception:
        return False


app = create_app()
