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
* **Reconciled on restart**: ``startup_sync()`` compares the persisted positions, the
  entry bar locks, the venue's real positions and its resting stop orders, then adopts /
  closes / blocks accordingly. It runs before the first cycle, so a VPS reboot can never
  produce a duplicate position or an untracked stop.
* **Costs at execution time**: the same :mod:`trading_office.costs` model the backtest
  uses decides whether the current spread makes a fill too expensive (the entry is
  deferred, not faked), inflates risk while the tape is stressed, and blocks entries for
  a few bars after a gap/spread shock.

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

from . import costs, indicators as ind
from .market import CryptoClient, IdxClient
from .market import demo
from .config import Settings
from .execution import ClosedTrade, PaperBroker, make_broker, order_from_plan
from .management import Position, manage_position, partial_size, should_exit_on_signal
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
    reconciled: dict[str, Any] = field(default_factory=dict)
    partials: int = 0
    # live-only broker feeds; None means "the venue did not tell us", never "empty"
    equity_source: str = "local"

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
            "reconciled": self.reconciled,
            "partials": self.partials,
            "equity_source": self.equity_source,
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
        self.cost_cfg = costs.CostConfig.for_market(market, self.profile)
        self.gap_cooldowns: dict[str, int] = {}  # symbol -> cycles of entry block left
        self.reconciled: dict[str, Any] = {}  # last startup_sync() report
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
        if not self.reconciled:
            try:
                self.startup_sync()
            except Exception as exc:  # never let reconciliation block the loop
                self.status.last_errors.append(f"startup_sync: {exc}")
        for symbol in list(self.gap_cooldowns):
            self.gap_cooldowns[symbol] = max(0, self.gap_cooldowns[symbol] - 1)
            if not self.gap_cooldowns[symbol]:
                self.gap_cooldowns.pop(symbol, None)
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
        # a gap/spread shock blocks new entries for a few bars (black-swan guard)
        if self.gap_cooldowns.get(symbol):
            payload["action"] = "WAIT"
            payload["reasons"] = [
                f"gap cooldown: {self.gap_cooldowns[symbol]} cycle(s) left after a shock"
            ] + payload["reasons"][:2]
            payload["executable"] = False
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

        # ---- execution-time cost check: the frozen plan was priced on the closed bar,
        # the fill happens now. If the book widened (or a shock hit), defer instead of
        # paying for it: the bar lock above already stops a second attempt on this bar.
        check = self.cost_check(symbol, plan)
        if not check["ok"]:
            self._skip(symbol, f"entry deferred: {check['reason']}")
            payload["execution"] = {"skipped": check["reason"], "cost": check}
            return
        payload["cost"] = check

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

    # ------------------------------------------------------------------ costs
    def cost_check(self, symbol: str, plan) -> dict[str, Any]:
        """Would filling ``plan`` right now cost more than the plan budgeted for?"""
        snapshot = self.last_scans.get(symbol) or {}
        checks = (snapshot.get("filter") or {}).get("checks") or []
        spread_now = next((c.get("value") for c in checks if c.get("name") == "spread"), None)
        max_spread = float(self.profile.get("max_spread_bps", 8.0))
        try:
            tick = self.fetch(symbol)
        except Exception:
            tick = {}
        atr = snapshot.get("signal", {}).get("atr") or snapshot.get("indicators", {}).get("atr")
        if atr is None:
            try:
                bars = tick.get("bars") or []
                atr = ind.last_defined(
                    ind.atr([b["high"] for b in bars], [b["low"] for b in bars],
                            [b["close"] for b in bars], 14)
                )
            except Exception:
                atr = 0.0
        quote_volume = None
        bars = tick.get("bars") or []
        if bars:
            recent = sorted(float(b.get("volume") or 0) * float(b["close"]) for b in bars[-20:])
            quote_volume = recent[len(recent) // 2] if recent else None
        price = float(plan.entry)
        notional = price * float(plan.size)
        estimate = costs.estimate_slippage(
            price=price, atr=float(atr or 0.0), notional=notional,
            quote_volume=quote_volume, cfg=self.cost_cfg,
        )
        planned_cost_pct = float(plan.costs.get("round_trip_cost_pct", 0.0)) / 100.0
        now_cost_pct = (estimate.slippage_bps + float(spread_now or max_spread)) / 10_000.0
        tolerance = float(self.settings.get("execution.max_cost_drift_pct", 40.0)) / 100.0
        reason = ""
        if spread_now is not None and spread_now > max_spread * float(self.profile.get("extreme_spread_mult", 1.6)):
            reason = f"spread {spread_now:.1f} bps above the stress cap"
        elif planned_cost_pct and now_cost_pct > planned_cost_pct * (1 + tolerance):
            reason = (
                f"live cost {now_cost_pct*100:.3f}% vs planned {planned_cost_pct*100:.3f}% "
                f"(slippage {estimate.slippage_bps:.1f} bps)"
            )
        result = {
            "ok": not reason,
            "reason": reason,
            "spread_bps": spread_now,
            "slippage_bps": round(estimate.slippage_bps, 3),
            "slippage_capped": estimate.capped,
            "planned_cost_pct": round(planned_cost_pct * 100, 4),
            "live_cost_pct": round(now_cost_pct * 100, 4),
            "risk_multiplier": costs.risk_multiplier(
                spread_bps=float(spread_now or 0.0), max_spread_bps=max_spread,
                atr_pct=(float(atr or 0.0) / price * 100.0) if price else None,
                profile=self.profile, cfg=self.cost_cfg,
            ),
        }
        if reason:
            self.state.append_ledger(
                {"event": "cost_deferred", "market": self.market, "symbol": symbol, "reason": reason,
                 "cost": result}
            )
        return result

    def _venue_positions(self) -> list[dict[str, Any]] | None:
        """Real positions from the venue, or ``None`` when the broker cannot tell us."""
        if isinstance(self.broker, PaperBroker):
            return [
                {
                    "symbol": symbol,
                    "side": pos.get("side"),
                    "contracts": float(pos.get("size") or 0.0),
                    "entryPrice": float(pos.get("entry") or 0.0),
                    "stopLoss": pos.get("stop"),
                }
                for symbol, pos in self.broker.positions.items()
            ]
        fetch = getattr(self.broker, "positions", None)
        if fetch is None:
            return None
        try:
            return fetch()
        except Exception:
            return None

    # ------------------------------------------------------------------ startup reconciliation
    def startup_sync(self, *, adopt_untracked: bool = False, close_orphans: bool = False) -> dict[str, Any]:
        """Reconcile persisted state with the venue before trading starts.

        Compares, per symbol:

        * our ``state/positions.json`` (what we think is open),
        * the venue's live positions (what is really open),
        * whether the entry bar lock exists (proof the order was authorised once),
        * whether the venue still has a resting stop for that position.

        Actions: ``adopt`` (venue has it, state lost it -> re-attach tracking),
        ``restore`` (state has it, venue agrees -> keep), ``close`` (venue flat but state
        open -> the trade left without us: record and clear), ``block`` (state open, venue
        unknown -> no new entries until a human looks), ``untracked`` (venue has a position
        we never authorised: reported, adopted only when asked).
        """
        timeframe = str(self.profile.get("timeframe", ""))
        venue = self._venue_positions()
        venue_by_symbol: dict[str, dict[str, Any]] = {}
        for row in venue or []:
            key = row.get("symbol")
            if key:
                venue_by_symbol[key] = row
        report: dict[str, Any] = {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "market": self.market,
            "mode": self.mode,
            "venue_available": venue is not None,
            "symbols": [],
            "adopted": [],
            "restored": [],
            "closed": [],
            "blocked": [],
            "untracked": [],
            "stops_missing": [],
        }

        stops_by_symbol: dict[str, list[dict[str, Any]]] = {}
        list_orders = getattr(self.broker, "exchange", None)
        if list_orders is not None:
            try:
                for order in list_orders.fetch_open_orders():
                    stops_by_symbol.setdefault(order.get("symbol"), []).append(order)
            except Exception:
                pass

        with self.lock:
            for symbol, position in list(self.positions.items()):
                live = venue_by_symbol.get(symbol)
                entry = {"symbol": symbol, "side": position.side, "size": position.size, "entry": position.entry}
                if venue is None:
                    report["blocked"].append({**entry, "reason": "venue positions unavailable"})
                    self.gap_cooldowns[symbol] = max(self.gap_cooldowns.get(symbol, 0), 1)
                    report["symbols"].append({**entry, "state": "unverified"})
                    continue
                if not live:
                    closed = self.broker.close(symbol, position.entry, "reconciled: venue is flat")
                    if closed is not None:
                        self._record_close(symbol, closed)
                    self.positions.pop(symbol, None)
                    report["closed"].append({**entry, "reason": "venue flat while state was open"})
                    continue
                venue_size = float(live.get("contracts") or live.get("size") or 0.0)
                position.size = venue_size or position.size
                venue_entry = float(live.get("entryPrice") or 0.0)
                if venue_entry > 0:
                    position.entry = venue_entry
                if list_orders is not None and not stops_by_symbol.get(symbol):
                    report["stops_missing"].append(symbol)
                self.state.save_position(symbol, position.to_dict())
                report["restored"].append({**entry, "venue_size": venue_size})
                report["symbols"].append({**entry, "state": "matched"})

            for symbol, live in venue_by_symbol.items():
                if symbol in self.positions:
                    continue
                key_known = self.state.is_locked(symbol, timeframe, str(live.get("timestamp", "")))
                entry = {
                    "symbol": symbol,
                    "side": live.get("side"),
                    "size": float(live.get("contracts") or live.get("size") or 0.0),
                    "entry": float(live.get("entryPrice") or 0.0),
                    "bar_lock": key_known,
                }
                if adopt_untracked:
                    side = "long" if str(live.get("side")) == "long" else "short"
                    position = Position(
                        symbol=symbol,
                        market=self.market,
                        side=side,
                        entry=float(live.get("entryPrice") or 0.0),
                        stop=float(live.get("stopLoss") or 0.0) or float(live.get("entryPrice") or 0.0),
                        target=0.0,
                        size=entry["size"],
                        initial_stop=float(live.get("stopLoss") or 0.0) or float(live.get("entryPrice") or 0.0),
                        opened_at=str(live.get("timestamp", "")),
                        bar_time="adopted",
                        meta={"adopted_at": report["at"], "reconciled": True},
                    )
                    self.positions[symbol] = position
                    self.state.save_position(symbol, position.to_dict())
                    report["adopted"].append(entry)
                else:
                    report["untracked"].append(entry)
                # never trade a symbol whose exposure we cannot explain
                self.gap_cooldowns[symbol] = max(self.gap_cooldowns.get(symbol, 0), 1)

        if close_orphans and report["untracked"]:
            for row in list(report["untracked"]):
                self.broker.close(row["symbol"], row["entry"], "reconciled: untracked position closed")
                report["closed"].append({**row, "reason": "untracked position closed on request"})
            report["untracked"] = []

        self.reconciled = report
        self.status.reconciled = {
            k: len(v) for k, v in report.items() if isinstance(v, list)
        } | {"venue_available": report["venue_available"], "at": report["at"]}
        self.state.append_ledger({"event": "reconciliation", "market": self.market, **report})
        self._emit({"event": "reconciliation", "market": self.market, **report})
        return report

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
            atr = ind.last_defined(
                ind.atr([b["high"] for b in bars], [b["low"] for b in bars], [b["close"] for b in bars], 14)
            ) or 0.0
            scan = self.last_scans.get(symbol) or {}
            opposite = should_exit_on_signal(position, scan.get("action", ""))
            checks = (scan.get("filter") or {}).get("checks") or []
            spread_now = next((c.get("value") for c in checks if c.get("name") == "spread"), None)
            tick = None
            if self.market == "idx":
                from .config import idx_tick_size

                tick = idx_tick_size(price)
            stop_before = position.stop
            decided = manage_position(
                position,
                closed_bar=closed_bar,
                price=price,
                atr=float(atr),
                now=dt.datetime.now(ZoneInfo(str(self.profile.get("sessions_timezone", "Asia/Jakarta")))),
                mgmt_cfg=self.settings.section("management"),
                profile=self.profile,
                spread_bps=float(spread_now if spread_now is not None else 5.0),
                opposite_signal=opposite,
                tick_size=tick,
                size_step=self.profile.get("size_step"),
            )
            for action in decided:
                actions.append({"symbol": symbol, **action.to_dict()})
                self.state.append_ledger(
                    {"event": "manage", "market": self.market, "symbol": symbol, "action": action.to_dict()}
                )
                if action.kind == "modify_stop" and action.new_stop:
                    moved = self.broker.modify_stop(symbol, action.new_stop)
                    if isinstance(moved, dict) and moved.get("ok") is False:
                        # never pretend the venue moved a stop it did not move: management
                        # already advanced the local stop, so roll it back and retry next bar
                        position.stop = stop_before
                        position.be_moved = False
                        self.status.last_errors.append(
                            f"{symbol}: stop not moved ({moved.get('reason')}); "
                            "the local stop was rolled back"
                        )
                        self.state.append_ledger(
                            {"event": "stop_move_failed", "market": self.market, "symbol": symbol,
                             "wanted": action.new_stop, "detail": moved}
                        )
                        continue
                    position.stop = action.new_stop
                    self.state.save_position(symbol, position.to_dict())
                elif action.kind == "partial":
                    fraction = float(action.fraction or 0.5)
                    slice_size = partial_size(position, fraction, self.market, self.profile.get("size_step"))
                    if slice_size <= 0:
                        continue
                    result = self.broker.partial_close(
                        symbol, slice_size, action.price or price, reason=action.reason
                    )
                    if not result:
                        continue
                    position.size = max(0.0, position.size - float(result["size"]))
                    position.realised_pnl += float(result["pnl"])
                    position.partial_done = True
                    position.partial_price = float(result["price"])
                    self.status.partials += 1
                    self.state.save_position(symbol, position.to_dict())
                    self.state.append_ledger(
                        {"event": "partial", "market": self.market, "symbol": symbol, **result}
                    )
                    self._emit({"event": "partial", "market": self.market, "symbol": symbol, **result})
                    if position.size <= 0:
                        closed = self.broker.close(symbol, float(result["price"]), "partial closed the position")
                        if closed:
                            self._record_close(symbol, closed)
                        break
                elif action.kind == "close":
                    trade = self.broker.close(symbol, action.price or price, action.reason)
                    if trade:
                        self._record_close(symbol, trade)
                    break
        return actions

    def _record_close(self, symbol: str, trade: ClosedTrade) -> None:
        with self.lock:
            position = self.positions.pop(symbol, None)
        if position is not None and position.realised_pnl:
            trade.pnl = round(float(trade.pnl) + float(position.realised_pnl), 4)
            trade.meta = {**trade.meta, "banked_partial_pnl": round(position.realised_pnl, 4)}
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
            "costs": self.cost_cfg.to_dict(),
            "gap_cooldowns": dict(self.gap_cooldowns),
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
