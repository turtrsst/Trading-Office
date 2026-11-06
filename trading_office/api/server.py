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

import asyncio
import datetime as dt
import threading
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import costs as cost_model
from .. import indicators as ind
from .. import secure_state
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
_TELEGRAM: Any = None
_ENGINE_LOCK = threading.RLock()
#: set by tests / a hot reconfiguration; takes precedence over the file-backed SETTINGS
_SETTINGS_OVERRIDE: Settings | None = None


def _settings() -> Settings:
    """The live settings.

    A module global is fine for a process, but not for tests or for a config reload while
    the server is running: a Python reload re-executes the module and would silently
    replace the very object a test (or an operator) just patched. An explicit override
    slot keeps that observable instead of magic.
    """
    return _SETTINGS_OVERRIDE or SETTINGS


# --------------------------------------------------------------------------- helpers
def _engine(market: str) -> TradingEngine:
    with _ENGINE_LOCK:
        if market not in _ENGINES:
            settings = _settings()
            _ENGINES[market] = TradingEngine(
                market=market,
                settings=settings,
                execute=not bool(settings.get("execution.dry_run", True)),
                state_dir=settings.get("engine.state_dir", "state"),
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


class ApprovalDecisionRequest(BaseModel):
    market: Literal["crypto", "idx"] = "crypto"
    request_id: str
    by: str = "browser"


class ArmLiveRequest(BaseModel):
    market: Literal["crypto", "idx"] = "crypto"
    confirm: bool = False           # must be true; an accidental POST must not arm trading
    by: str = "operator"


class ClosePositionRequest(BaseModel):
    market: Literal["crypto", "idx"] = "crypto"
    symbol: str


class CancelOrderRequest(BaseModel):
    market: Literal["crypto", "idx"] = "crypto"
    order_id: str
    symbol: str | None = None


class EngineReconcileRequest(BaseModel):
    market: Literal["crypto", "idx"] = "crypto"
    adopt: bool = False
    close_orphans: bool = False


# --------------------------------------------------------------------------- lifecycle
from contextlib import asynccontextmanager


def _telegram_control() -> Any:
    """The control bot (approve/reject/panic from a phone), built once per process."""
    global _TELEGRAM
    with _ENGINE_LOCK:
        if _TELEGRAM is None:
            from ..env import get
            from ..telegram_bot import TelegramControl

            alerts = SETTINGS.get("alerts", {}) or {}
            telegram = alerts.get("telegram", {}) or {}
            token = get(str(telegram.get("token_env") or "TO_TELEGRAM_TOKEN"))
            _TELEGRAM = TelegramControl(
                token=token,
                owner_chat_id=str(telegram.get("chat_id") or ""),
                market=str(SETTINGS.get("autonomy.control_market", "crypto")),
                engine_for=lambda market: _engine(market),
            )
            # every engine event flows through here: the bot only sends the interesting ones
            for market in ("crypto", "idx"):
                try:
                    _attach_telegram(_engine(market))
                except Exception:
                    continue
        return _TELEGRAM


def _attach_telegram(engine: TradingEngine) -> None:
    """Route approval events to the chat without taking over ``on_event``."""
    bot = _TELEGRAM
    if bot is None or engine is None:
        return

    def listener(event: dict[str, Any]) -> None:
        if not bot.configured:
            return
        try:
            name = event.get("event")
            if name == "approval_requested" and event.get("request"):
                bot.notify_request(event["request"])
            elif name in ("guard_set", "watchdog_trip", "engine_error"):
                bot.send(f"⚠️ <b>{name}</b> · {str(event)[:400]}")
        except Exception:  # a chat failure must never reach the trading loop
            pass

    engine.on_event = listener


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start/stop the engine loops with the server process.

    The engine is a background thread, not an asyncio task: ccxt and the IDX client are
    blocking, and one slow provider call inside the event loop would freeze the websocket
    that feeds the dashboard — exactly the failure this project is built to avoid. The
    separation that matters is the *synchronous executor*: `/api/metrics` uses
    ``def``/``run_in_threadpool``, so a manual CLOSE never runs on the event loop and the
    1 Hz stream keeps its cadence while the trade is being booked.

    ``engine.auto_start_engine`` (default off) makes a supervised deployment start the
    loop by itself; without it the web terminal comes up idle and the operator presses Start.
    """
    # resolved at lifespan time, not at import time: the flag is part of the running config
    settings = _settings()
    autostart = bool(settings.get("engine.auto_start_engine", False))
    markets = settings.get("engine.auto_start_markets") or ["crypto"]
    started: list[str] = []
    if autostart:
        for market in markets:
            if market not in ("crypto", "idx"):
                continue
            try:
                engine = _engine(str(market))
                engine.start(background=True)
                started.append(str(market))
            except Exception as exc:  # noqa: BLE001 - never block the API from starting
                print(f"[engine] could not autostart {market}: {exc}")
    bot = None
    if SETTINGS.get("alerts.telegram.chat_id"):
        try:
            bot = _telegram_control()
            bot.start_background()
        except Exception as exc:  # noqa: BLE001 - a chat outage must not stop trading
            print(f"[telegram] control disabled: {exc}")
    try:
        yield
    finally:
        if bot is not None:
            bot.stop()
        for market, engine in list(_ENGINES.items()):
            try:
                engine.stop()
            except Exception:
                pass


# --------------------------------------------------------------------------- app
def create_app() -> FastAPI:
    app = FastAPI(
        title="Trading Office",
        version="1.0.0",
        description=(
            "Expert-Advisor style trading engine: market filter -> signal -> risk -> "
            "execution, for IDX equities and crypto perpetual futures."
        ),
        lifespan=_lifespan,
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
            # presence only: never the key itself, never a prefix of it
            "secrets": _secrets_report(),
            "state_encryption": secure_state.status(),
            "alerts": _alerts_summary(),
        }

    @app.get("/api/telegram")
    def telegram_status() -> dict[str, Any]:
        bot = _TELEGRAM
        if bot is None:
            return {"configured": False, "running": False,
                    "reason": "no alerts.telegram.chat_id configured"}
        status = bot.status()
        status["audit"] = bot.audit[-5:]
        return status

    @app.get("/api/approvals")
    def approvals(market: Literal["crypto", "idx"] = "crypto", status: str = "pending") -> dict[str, Any]:
        engine = _engine(market)
        return {
            "market": market,
            "mode": engine.settings.get("autonomy.mode", "ask"),
            "approval_required": engine.approval_required(),
            "entries_gate": engine.execution_gate()["entries"],
            "requests": engine.state.approvals(status=None if status == "all" else status),
        }

    @app.post("/api/approvals/approve")
    def approve(req: ApprovalDecisionRequest) -> dict[str, Any]:
        return _engine(req.market).execute_approved(req.request_id, by=req.by)

    @app.post("/api/approvals/reject")
    def reject(req: ApprovalDecisionRequest) -> dict[str, Any]:
        ok = _engine(req.market).reject_approval(req.request_id, by=req.by, reason="browser")
        return {"ok": ok}

    @app.post("/api/autonomy/arm")
    def arm_live(req: ArmLiveRequest) -> dict[str, Any]:
        """Arm unattended live entries. Bound to the gate fingerprint (see autonomy.py)."""
        from ..autonomy import execution_gate_fingerprint, execution_gate_limits

        if not req.confirm:
            return {"ok": False, "reason": "confirm must be true (this authorises live orders)"}
        engine = _engine(req.market)
        grant = {
            "armed": True,
            "fingerprint": execution_gate_fingerprint(),
            "limits": execution_gate_limits(engine.settings),
            "armed_by": req.by,
            "armed_at": _iso_now(),
        }
        engine.state.set_setting("live_entry_grant", grant)
        engine.state.append_ledger({"event": "live_armed", "market": req.market, **grant})
        return {"ok": True, "grant": grant, "entries": engine.execution_gate()["entries"]}

    @app.post("/api/autonomy/disarm")
    def disarm_live(req: ArmLiveRequest) -> dict[str, Any]:
        engine = _engine(req.market)
        engine.state.set_setting("live_entry_grant", {"armed": False, "disarmed_at": _iso_now()})
        engine.state.append_ledger({"event": "live_disarmed", "market": req.market,
                                    "by": req.by})
        return {"ok": True, "entries": engine.execution_gate()["entries"]}

    @app.get("/api/alerts")
    def alerts_status() -> dict[str, Any]:
        """What the out-of-band channel would send, and whether it can (never the token)."""
        engine = _engine("crypto")
        alerter = getattr(engine, "alerter", None)
        status = alerter.status() if alerter is not None else {"enabled": False}
        if alerter is not None:
            status["chat"] = alerter.chat_fingerprint()
            status["recent"] = [
                {"event": row.get("event"), "ok": row.get("ok"), "error": row.get("error")}
                for row in (alerter.sink or [])[-5:]
            ]
        return status

    @app.get("/api/engine/heartbeat")
    def engine_heartbeat(market: Literal["crypto", "idx"] = "crypto") -> dict[str, Any]:
        return _engine(market).heartbeat()

    @app.get("/api/security")
    def security() -> dict[str, Any]:
        """What is protected and what is not. Deliberately boring: presence, never values."""
        from .. import env as env_module

        return {
            "env": env_module.load_env(),
            "credentials": _secrets_report(),
            "state_encryption": secure_state.status(),
            "ignored": [
                {"prefix": p, "reason": "MetaTrader 5 is not a supported venue here "
                                        "(Windows-only, no execution path)"}
                for p in env_module.UNSUPPORTED_PREFIXES
            ],
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
        refresh: bool = False,
    ) -> dict[str, Any]:
        wanted = [s.strip() for s in symbols.split(",") if s.strip()] or None
        if refresh:
            IDX.cache.clear()
        return SCREENER.screen(market, symbols=wanted, limit=limit, deep_context=deep)

    @app.get("/api/symbol/{market}/{symbol:path}")
    def symbol_detail(market: Literal["idx", "crypto"], symbol: str, refresh: bool = False) -> dict[str, Any]:
        try:
            if refresh:
                IDX.cache.clear()
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
            report["drawdown_curve"] = [
                {"time": p.get("time"), "equity": p.get("equity"),
                 "peak": p.get("peak"), "drawdown_pct": p.get("drawdown_pct")}
                for p in (report.get("out_of_sample") or {}).get("equity_curve") or []
            ]
            return report
        result = bt.run(req.symbol, bars, htf_factor=req.htf_factor).to_dict()
        result["source"] = data.get("source")
        return result

    # ------------------------------------------------------------------ engine
    @app.post("/api/engine/reconcile")
    def engine_reconcile(req: EngineReconcileRequest) -> dict[str, Any]:
        engine = _engine(req.market)
        return engine.startup_sync(adopt_untracked=req.adopt, close_orphans=req.close_orphans)

    @app.get("/api/metrics")
    def live_metrics(market: Literal["crypto", "idx"] = "crypto", limit: int = Query(200, ge=2, le=2000)) -> dict[str, Any]:
        """Equity + drawdown time series for the dashboard charts.

        ``timestamps`` / ``equity`` / ``drawdown`` are parallel arrays (chart-friendly);
        the same points are also grouped under ``series`` for callers that want the meta
        fields. ``status: "empty"`` means the engine has never traded, never an error.
        """
        engine = _engine(market)
        marks = {
            symbol: float(scan["price"])
            for symbol, scan in engine.last_scans.items()
            if scan.get("price")
        }
        curve = engine._equity_curve(marks)[-limit:]
        perf = engine.performance(marks)
        return {
            "status": "success" if len(curve) > 1 else "empty",
            "market": market,
            "timestamps": [p.get("ts") for p in curve],
            "equity": [p["equity"] for p in curve],
            "drawdown": [p.get("drawdown_pct", 0.0) for p in curve],
            "drawdown_pct": [p.get("drawdown_pct", 0.0) for p in curve],
            "peak": [p.get("peak") for p in curve],
            "series": curve,
            "performance": perf,
            # compact KPI block: what the sidebar cards read (and what a WS frame carries)
            "kpi": {
                "win_rate": perf["win_rate_pct"],
                "profit_factor": perf["profit_factor"],
                "total_trades": perf["trades"],
                "max_drawdown_pct": perf["max_drawdown_pct"],
                "net_profit_pct": perf["net_profit_pct"],
                "equity": perf["ending_equity"],
            },
            "starting_equity": perf["starting_equity"],
            # the same rows the websocket pushes, so one render path serves both transports
            "active_trades": engine.active_trades(marks),
            "open_orders": engine.open_orders(),
            "execution_gate": engine.execution_gate(),
            "heartbeat": engine.heartbeat(),
            "updated_at": _iso_now(),
        }

    # ------------------------------------------------------------------ live websocket
    @app.websocket("/api/ws/metrics")
    async def ws_metrics(websocket: WebSocket, market: str = "crypto") -> None:
        """Push the equity/drawdown series + KPI block once per second.

        Cheaper and lower-latency than polling, and the frame is the *same* JSON as
        ``GET /api/metrics`` (plus ``type``/``seq``) so one render path serves both
        transports. The client ack/ping keeps a half-dead connection from lingering:
        a client that stops answering is dropped after three missed cycles.
        """
        await websocket.accept()
        seq = 0
        misses = 0
        interval = 1.0  # one frame per second, *regardless* of how fast the client answers
        loop = asyncio.get_running_loop()
        try:
            while True:
                cycle_start = loop.time()
                payload = live_metrics(market, limit=120)  # type: ignore[arg-type]
                seq += 1
                await websocket.send_json({"type": "metrics", "seq": seq, "ts": _iso_now(), **payload})
                try:
                    # a client may send {"type":"pong"} or anything; only liveness matters
                    await asyncio.wait_for(websocket.receive_text(), timeout=interval)
                    misses = 0
                except asyncio.TimeoutError:
                    misses += 1
                    if misses >= 3:
                        await websocket.send_json({"type": "stale", "ts": _iso_now(),
                                                   "detail": "client did not answer 3 pings"})
                        break
                # pace the loop: an eager client must not turn this into a busy stream
                remaining = interval - (loop.time() - cycle_start)
                if remaining > 0:
                    await asyncio.sleep(remaining)
        except WebSocketDisconnect:
            pass
        except Exception:  # pragma: no cover - transport level
            pass

    @app.get("/api/performance")
    def performance(market: Literal["crypto", "idx"] = "crypto") -> dict[str, Any]:
        """Profit factor, max drawdown, win rate and net profit of the live/paper book."""
        return _engine(market).performance()

    @app.get("/api/engine/kill-switch")
    def engine_kill_switch(market: Literal["crypto", "idx"] = "crypto") -> dict[str, Any]:
        engine = _engine(market)
        return engine.guard_state()

    @app.post("/api/engine/resume")
    def engine_resume(req: EngineRequest) -> dict[str, Any]:
        engine = _engine(req.market)
        return engine.resume()

    @app.get("/api/cost-model")
    def cost_model_endpoint(market: Literal["crypto", "idx"] = "crypto") -> dict[str, Any]:
        profile = SETTINGS.profile(market)
        return {
            "market": market,
            "model": cost_model.CostConfig.for_market(market, profile).to_dict(),
            "spread_cap_bps": float(profile.get("max_spread_bps", 8.0)),
            "stress_spread_cap_bps": cost_model.risky_spread_cap(profile),
            "management": SETTINGS.section("management"),
        }

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

    @app.post("/api/trade/close")
    def trade_close(req: ClosePositionRequest) -> dict[str, Any]:
        """Close ONE position (the CLOSE button). Refuses symbols the engine does not hold."""
        engine = _engine(req.market)
        if req.market == "idx":
            # IDX has no retail order API: the book is simulated, and saying so beats a
            # button that silently pretends to have sold shares on the exchange
            result = engine.close_position(req.symbol, reason="manual close (IDX paper)")
            result["gate"] = engine.execution_gate()
            return result
        result = engine.close_position(req.symbol)
        result["gate"] = engine.execution_gate()
        return result

    @app.post("/api/order/cancel")
    def order_cancel(req: CancelOrderRequest) -> dict[str, Any]:
        engine = _engine(req.market)
        return engine.cancel_order(req.order_id, req.symbol)

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


def _alerts_summary() -> dict[str, Any]:
    try:
        alerter = getattr(_engine("crypto"), "alerter", None)
        if alerter is None:
            return {"enabled": False, "configured": False}
        status = alerter.status()
        return {k: status[k] for k in ("enabled", "configured", "channel", "cooldown_s")}
    except Exception:
        return {"enabled": False, "configured": False}


def _secrets_report() -> dict[str, Any]:
    """Presence/length/fingerprint of the credentials the config points at."""
    from ..env import credential_status

    names = (
        str(SETTINGS.get("execution.api_key_env", "TO_API_KEY")),
        str(SETTINGS.get("execution.api_secret_env", "TO_API_SECRET")),
    )
    report = credential_status(*names)
    report["_note"] = "set/via/length/fingerprint only — secrets are never returned"
    return report


def _crypto_ok() -> bool:
    try:
        CRYPTO.exchange()
        return True
    except Exception:
        return False


app = create_app()
