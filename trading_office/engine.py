"""The live engine: state locking, duplicate prevention, execution and management.

Loop (one ``poll_seconds`` cycle):

    for each symbol in the active market:
        bars = closed candles only
        if the last closed bar was already decided -> skip (bar lock)
        run the four gates (market filter -> signal -> risk)
        if an order is approved and execution is enabled:
            bar lock + cooldown + duplicate checks, then submit
        manage every open position (trailing, break-even, time exits)

Guarantees
----------
* **One decision per bar**: ``StateStore.lock`` is written *before* the order is sent; a
  crash between the two means "no order", never "two orders".
* **No duplicates**: one position per symbol, a deterministic client order id
  (``to-<symbol>-<bar>-<side>``) and a cooldown after a position closes.
* **Dry run by default**: ``execution.paper`` / ``execution.dry_run`` keep the engine on
  the paper broker unless the config and the environment both say live.
* **Everything audited**: each decision (taken or skipped) is appended to
  ``state/ledger.jsonl`` with the gate results, so the UI can replay the reasoning.

The engine can run **in-process** (the web server starts it as a background thread) or as
its own process (``python -m trading_office.engine --market crypto``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from .market import CryptoClient, IdxClient
from .market import demo
from .config import Settings
from .execution import ClosedTrade, PaperBroker, make_broker, order_from_plan
from .management import Position, manage_position, should_exit_on_signal
from .state import StateStore
from .strategy.runner import scan_symbol


@dataclass
class EngineStatus:
    running: bool
    market: str
    mode: str  # "paper" | "live" | "dry-run"
    started_at: str | None = None
    last_cycle_at: str | None = None
    cycle_count: int = 0
    symbols: list[str] = field(default_factory=list)
    scans: int = 0
    orders: int = 0
    blocked: int = 0
    last_errors: list[str] = field(default_factory=list)
    equity: float = 0.0
    open_positions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "market": self.market,
            "mode": self.mode,
            "started_at": self.started_at,
            "last_cycle_at": self.last_cycle_at,
            "cycle_count": self.cycle_count,
            "symbols": list(self.symbols),
            "scans": self.scans,
            "orders": self.orders,
            "blocked": self.blocked,
            "last_errors": self.last_errors[-5:],
            "equity": self.equity,
            "open_positions": self.open_positions,
        }


class TradingEngine:
    def __init__(
        self,
        *,
        market: str = "crypto",
        settings: Settings | None = None,
        symbols: Sequence[str] | None = None,
        execute: bool = True,
        state_dir: str | Path | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings or Settings.load()
        self.market = market
        self.profile = self.settings.profile(market)
        self.symbols = [s.upper() if market == "idx" else s for s in (symbols or self.profile.get("symbols") or [])]
        self.execute = execute
        self.state = StateStore(
            Path(state_dir or self.settings.get("engine.state_dir", "state")) / market
        )
        self.on_event = on_event
        self.lock = threading.RLock()

        equity = float(
            self.settings.get(
                "engine.equity_idr" if market == "idx" else "engine.equity_usdt", 1_000.0
            )
        )
        self.broker = make_broker(self.settings.raw, market, equity, self.profile)
        self.mode = "live" if self.broker.__class__.__name__ == "CcxtBroker" else "paper"
        if self.mode == "paper" and not execute:
            self.mode = "dry-run"

        self.crypto: CryptoClient | None = None
        self.idx: IdxClient | None = None
        if market == "crypto":
            exec_cfg = self.settings.section("execution")
            self.crypto = CryptoClient(
                exchange_id=str(exec_cfg.get("exchange_id", "bybit")),
                symbols=self.symbols,
            )
        else:
            self.idx = IdxClient(self.settings.section("idx"))

        self.positions: dict[str, Position] = {}
        self._load_positions()
        self.status = EngineStatus(
            running=False,
            market=market,
            mode=self.mode,
            symbols=self.symbols,
            equity=self.broker.equity(),
            open_positions=len(self.positions),
        )
        self.last_scans: dict[str, dict[str, Any]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ lifecycle
    def start(self, background: bool = True) -> None:
        self._stop.clear()
        if not background:
            self.run_forever()
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_forever, name=f"engine-{self.market}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.status.running = False
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def run_forever(self) -> None:
        self.status.running = True
        self.status.started_at = dt.datetime.now(dt.timezone.utc).isoformat()
        poll = float(self.settings.get("execution.poll_seconds", 15))
        self._emit({"event": "engine_started", "market": self.market, "mode": self.mode})
        try:
            while not self._stop.is_set():
                try:
                    self.cycle()
                except Exception as exc:  # keep the loop alive, log loudly
                    self.status.last_errors.append(f"{type(exc).__name__}: {exc}")
                    self._emit({"event": "engine_error", "error": str(exc)})
                self._stop.wait(poll)
        finally:
            self.status.running = False
            self._emit({"event": "engine_stopped", "market": self.market})

    # ------------------------------------------------------------------ one cycle
    def cycle(self) -> dict[str, Any]:
        started = dt.datetime.now(dt.timezone.utc)
        scans = []
        for symbol in self.symbols:
            try:
                scans.append(self.scan(symbol))
            except Exception as exc:
                self.status.last_errors.append(f"{symbol}: {exc}")
            time.sleep(0.25)  # be gentle with rate limits
        self.manage_open_positions()
        self.status.cycle_count += 1
        self.status.last_cycle_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self.status.equity = self.broker.equity()
        self.status.open_positions = len(self.positions)
        self.state.save_account(
            {
                "equity": self.status.equity,
                "open_positions": self.status.open_positions,
                "updated_at": self.status.last_cycle_at,
                "market": self.market,
            }
        )
        summary = {
            "event": "cycle",
            "market": self.market,
            "started_at": started.isoformat(),
            "duration_s": round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 2),
            "scans": scans,
            "status": self.status.to_dict(),
        }
        self._emit(summary)
        return summary

    # ------------------------------------------------------------------ market data
    def fetch(self, symbol: str) -> dict[str, Any]:
        profile = self.profile
        if self.market == "crypto" and self.crypto is not None:
            data = self.crypto.candles(symbol, str(profile.get("timeframe", "15m")), 400)
            htf = self.crypto.htf_candles(symbol, str(profile.get("trend_timeframe", "4h")), 260)
            tick = self.crypto.ticker(symbol)
            age = None
            if tick.get("timestamp"):
                age = max(0.0, (time.time() * 1000 - float(tick["timestamp"])) / 1000.0)
            return {
                "bars": data["bars"],
                "htf_bars": htf["bars"],
                "quote": tick,
                "source": data["source"],
                "warnings": list(data["warnings"]) + list(htf.get("warnings", [])),
                "data_age_s": age,
                "stale": data["source"] == "ccxt" and bool(tick.get("warnings")),
                "bid": tick.get("bid"),
                "ask": tick.get("ask"),
            }
        if self.idx is not None:
            interval = str(profile.get("timeframe", "1d"))
            candles = self.idx.candles(symbol, period="1y", interval=interval)
            htf = self.idx.candles(symbol, period="2y", interval=str(profile.get("trend_timeframe", "1wk")))
            quote_payload = self.idx.quote(symbol)
            quote = quote_payload.get("quote") or {}
            return {
                "bars": candles["bars"],
                "htf_bars": htf["bars"],
                "quote": quote,
                "source": candles["source"],
                "warnings": list(candles.get("warnings", [])) + list(quote_payload.get("warnings", [])),
                "data_age_s": None,
                "stale": False,  # idx delays are documented; a failed provider falls back to demo
                "bid": None,
                "ask": None,
                "daily_value": quote.get("value"),
            }
        # synthetic fallback
        bars = demo.crypto_bars(symbol, "15m", 400) if self.market == "crypto" else demo.idx_bars(symbol, "1d", 400)
        return {
            "bars": bars, "htf_bars": demo.htf_bars(symbol, self.market, 8 if self.market == "crypto" else 5, 60),
            "quote": demo.crypto_quote(symbol) if self.market == "crypto" else demo.idx_quote(symbol),
            "source": "synthetic", "warnings": ["no market client configured"], "data_age_s": None, "stale": True,
        }

    # ------------------------------------------------------------------ scan + execute
    def scan(self, symbol: str) -> dict[str, Any]:
        data = self.fetch(symbol)
        self.status.scans += 1
        result = scan_symbol(
            symbol=symbol,
            market=self.market,
            bars=data["bars"],
            htf_bars=data["htf_bars"],
            quote=data.get("quote"),
            settings=self.settings,
            equity=self.broker.equity(),
            stale=bool(data.get("stale")),
            data_age_s=data.get("data_age_s"),
            source=str(data.get("source", "")),
            warnings=data.get("warnings") or [],
            daily_value=data.get("daily_value"),
            bid=data.get("bid"),
            ask=data.get("ask"),
        )
        payload = result.to_dict()
        self.last_scans[symbol] = payload
        self.state.append_ledger(
            {
                "event": "scan",
                "market": self.market,
                "symbol": symbol,
                "action": payload["action"],
                "score": payload["score"],
                "stage": payload["stage"],
                "bar_time": payload["bar_time"],
                "reasons": payload["reasons"][:5],
                "source": payload["source"],
            }
        )
        execut_ok = payload.get("executable", payload["action"] != "SHORT" or self.market == "crypto")
        if payload["action"] in ("LONG", "SHORT") and execut_ok:
            self._handle_entry(symbol, payload)
        else:
            self.status.blocked += 1
        return payload

    def _handle_entry(self, symbol: str, payload: dict[str, Any]) -> None:
        timeframe = str(self.profile.get("timeframe", ""))
        bar_time = payload["bar_time"]
        side = payload["action"].lower()

        # ---- duplicate / cooldown checks (before touching any state)
        with self.lock:
            if symbol in self.positions:
                self._skip(symbol, "position already open")
                return
            last_exit = self.state.last_exit_bar(symbol)
            cooldown = int(self.settings.get("engine.cooldown_bars", 3))
            if last_exit and _bars_since(last_exit, bar_time, timeframe) < cooldown:
                self._skip(symbol, f"cooldown after last exit ({last_exit})")
                return

        # ---- bar lock: one decision per bar, written before the order
        locked = self.state.lock(
            symbol, timeframe, bar_time,
            {"action": payload["action"], "score": payload["score"], "reasons": payload["reasons"][:5]},
        )
        if not locked:
            self._skip(symbol, "bar already decided (state lock)")
            return

        plan_dict = payload.get("risk", {}).get("plan")
        if not plan_dict:
            self._skip(symbol, "no plan")
            return

        from .risk import TradePlan

        plan = TradePlan(**{k: v for k, v in plan_dict.items() if k in TradePlan.__dataclass_fields__})
        if not self.execute:
            payload["execution"] = {"skipped": "execution disabled"}
            return

        order = order_from_plan(plan, bar_time, payload.get("reasons", []))
        fill = self.broker.submit(order)
        self.state.append_ledger(
            {"event": "order", "market": self.market, "symbol": symbol, "order": order.to_dict(),
             "fill": fill.to_dict()}
        )
        if not fill.ok:
            self._skip(symbol, f"order rejected: {fill.message}")
            return
        self.status.orders += 1
        position = Position(
            symbol=symbol,
            market=self.market,
            side=side,
            entry=fill.price,
            stop=float(order.stop or plan.stop),
            target=float(order.target or plan.target),
            size=fill.quantity,
            initial_stop=float(order.stop or plan.stop),
            opened_at=fill.at,
            bar_time=bar_time,
            meta={
                "reasons": payload.get("reasons", []),
                "score": payload["score"],
                "bar_time": bar_time,
                "risk_cash": plan.risk_cash,
                "rr": plan.rr,
            },
        )
        with self.lock:
            self.positions[symbol] = position
        self.state.save_position(symbol, position.to_dict())
        payload["execution"] = {"filled": fill.to_dict(), "position": position.to_dict()}
        self._emit({"event": "trade_opened", "market": self.market, "symbol": symbol,
                    "position": position.to_dict(), "fill": fill.to_dict()})

    def _skip(self, symbol: str, reason: str) -> None:
        self.state.append_ledger({"event": "skip", "market": self.market, "symbol": symbol, "reason": reason})
        self._emit({"event": "skip", "market": self.market, "symbol": symbol, "reason": reason})

    # ------------------------------------------------------------------ management
    def manage_open_positions(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for symbol in list(self.positions.keys()):
            position = self.positions.get(symbol)
            if position is None:
                continue
            try:
                data = self.fetch(symbol)
            except Exception as exc:
                self.status.last_errors.append(f"manage {symbol}: {exc}")
                continue
            bars = data["bars"]
            if len(bars) < 2:
                continue
            closed_bar = bars[-1]
            price = float((data.get("quote") or {}).get("price") or closed_bar["close"])
            from . import indicators as ind

            atr = ind.last_defined(
                ind.atr([b["high"] for b in bars], [b["low"] for b in bars], [b["close"] for b in bars], 14)
            ) or 0.0
            scan = self.last_scans.get(symbol) or {}
            opposite = should_exit_on_signal(position, scan.get("action", ""))
            tick = None
            if self.market == "idx":
                from .config import idx_tick_size

                tick = idx_tick_size(price)
            decided = manage_position(
                position,
                closed_bar=closed_bar,
                price=price,
                atr=float(atr),
                now=dt.datetime.now(ZoneInfo(str(self.profile.get("sessions_timezone", "Asia/Jakarta")))),
                mgmt_cfg=self.settings.section("management"),
                profile=self.profile,
                spread_bps=float(scan.get("filter", {}).get("checks", [{}])[-1].get("value") or 5.0),
                opposite_signal=opposite,
                tick_size=tick,
            )
            for action in decided:
                actions.append({"symbol": symbol, **action.to_dict()})
                self.state.append_ledger(
                    {"event": "manage", "market": self.market, "symbol": symbol, "action": action.to_dict()}
                )
                if action.kind == "modify_stop" and action.new_stop:
                    position.stop = action.new_stop
                    if isinstance(self.broker, PaperBroker):
                        self.broker.modify_stop(symbol, action.new_stop)
                    self.state.save_position(symbol, position.to_dict())
                elif action.kind == "close":
                    trade = self.broker.close(symbol, action.price or price, action.reason)
                    if trade:
                        self._record_close(symbol, trade)
                    break
        return actions

    def _record_close(self, symbol: str, trade: ClosedTrade) -> None:
        with self.lock:
            self.positions.pop(symbol, None)
        self.state.drop_position(symbol, exit_bar=dt.datetime.now(dt.timezone.utc).isoformat())
        self.state.append_ledger(
            {"event": "trade_closed", "market": self.market, **trade.to_dict()}
        )
        self._emit({"event": "trade_closed", "market": self.market, "trade": trade.to_dict()})

    def force_close_all(self, reason: str = "manual flatten") -> list[dict[str, Any]]:
        out = []
        for symbol in list(self.positions.keys()):
            try:
                price = float(self.fetch(symbol).get("quote", {}).get("price") or 0.0)
            except Exception:
                price = 0.0
            trade = self.broker.close(symbol, price or self.positions[symbol].entry, reason)
            if trade:
                self._record_close(symbol, trade)
                out.append(trade.to_dict())
        return out

    # ------------------------------------------------------------------ state
    def _load_positions(self) -> None:
        for symbol, raw in self.state.positions().items():
            try:
                self.positions[symbol] = Position.from_dict(raw)
            except TypeError:
                continue

    def snapshot(self) -> dict[str, Any]:
        marks = {}
        for symbol in self.positions:
            scan = self.last_scans.get(symbol)
            if scan and scan.get("price"):
                marks[symbol] = float(scan["price"])
        broker_state = {}
        if isinstance(self.broker, PaperBroker):
            broker_state = self.broker.snapshot(marks)
        closed = self.state.closed_trades()
        return {
            "status": self.status.to_dict(),
            "broker": broker_state or {"equity": self.broker.equity()},
            "positions": [p.to_dict() | {"unrealised": self.broker_marks(marks).get(p.symbol, 0.0)}
                          for p in self.positions.values()],
            "last_scans": self.last_scans,
            "closed_trades": closed[-25:],
            "ledger": self.state.ledger(60),
            "equity_curve": self._equity_curve(),
        }

    def broker_marks(self, marks: dict[str, float]) -> dict[str, float]:
        if isinstance(self.broker, PaperBroker):
            return self.broker.unrealised(marks)
        return {}

    def _equity_curve(self) -> list[dict[str, Any]]:
        """Equity over time, rebuilt from the ledger (cheap and always consistent)."""
        points = []
        equity = float(self.settings.get("engine.equity_idr" if self.market == "idx" else "engine.equity_usdt", 0))
        points.append({"ts": None, "equity": equity})
        for event in self.state.ledger(100_000):
            if event.get("event") == "trade_closed":
                equity += float(event.get("pnl", 0.0))
                points.append({"ts": event.get("closed_at") or event.get("ts"), "equity": round(equity, 2)})
        return points[-200:]

    # ------------------------------------------------------------------ events
    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass


def _bars_since(earlier: str, later: str, timeframe: str) -> int:
    try:
        a = dt.datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(str(later).replace("Z", "+00:00"))
    except ValueError:
        return 10_000
    minutes = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60,
        "4h": 240, "1d": 1440, "1wk": 10080,
    }.get(timeframe, 15)
    return int(abs((b - a).total_seconds()) / (minutes * 60))


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Trading Office live engine.")
    parser.add_argument("--market", choices=["crypto", "idx"], default="crypto")
    parser.add_argument("--symbols", default="", help="comma-separated; defaults to the profile")
    parser.add_argument("--cycles", type=int, default=0, help="stop after N cycles (0 = forever)")
    parser.add_argument("--no-execute", action="store_true", help="scan only (dry run)")
    parser.add_argument("--config", default="")
    args = parser.parse_args(argv)

    settings = Settings.load(args.config or None)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    engine = TradingEngine(market=args.market, settings=settings, symbols=symbols,
                           execute=not args.no_execute)

    def printer(event: dict[str, Any]) -> None:
        if event.get("event") == "cycle":
            for scan in event.get("scans", []):
                print(
                    f"[{scan['symbol']:<14}] {scan['action']:<7} score {scan['score']}/9 "
                    f"stage={scan['stage']:<7} {(' | '.join(scan['reasons'][:2]))[:110]}",
                    flush=True,
                )
        elif event.get("event") in ("trade_opened", "trade_closed", "skip", "engine_error"):
            print(json.dumps(event, default=str), flush=True)

    engine.on_event = printer
    if args.cycles:
        for _ in range(args.cycles):
            engine.cycle()
    else:
        engine.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
