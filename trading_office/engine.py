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
    guard: dict[str, Any] = field(default_factory=dict)
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
            "guard": self.guard,
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
        # out-of-band alerting: disabled and inert unless config + environment say otherwise
        from .alerts import Alerter

        self.alerter: Alerter | None = Alerter.from_settings(self.settings, market)

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
        self._guard_resumed_on: str | None = self.state.get_setting("guard_resumed_day")
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
        self._last_beat = time.time()
        self._emit({"event": "engine_started", "market": self.market, "mode": self.mode})
        try:
            while not self._stop.is_set():
                try:
                    self.cycle()
                    self._last_beat = time.time()
                except Exception as exc:  # keep the loop alive, log loudly
                    self.status.last_errors.append(f"{type(exc).__name__}: {exc}")
                    self._emit({"event": "engine_error", "error": str(exc)})
                self._stop.wait(poll)
        finally:
            self.status.running = False
            self._emit({"event": "engine_stopped", "market": self.market})

    def heartbeat(self) -> dict[str, Any]:
        """Is the loop actually cycling?

        ``status.running`` only means the thread object exists. A cycle wedged inside a
        provider call looks perfectly healthy from the outside — the web UI keeps serving,
        the badge stays green, and no order is ever managed again. This compares the last
        cycle timestamp with the configured poll interval, and exposes the verdict so both
        the supervisor and the UI can act on it.
        """
        poll = float(self.settings.get("execution.poll_seconds", 15))
        # a cycle may legitimately take a few polls (rate limits, retries); allow 3 + slack
        limit = max(60.0, poll * 3.0 + 30.0)
        last = self.status.last_cycle_at
        silent = None
        if last:
            try:
                stamp = dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                silent = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
            except ValueError:
                silent = None
        age = (time.time() - getattr(self, "_last_beat", 0.0)) if getattr(self, "_last_beat", 0) else None
        silent_s = max([v for v in (silent, age) if v is not None], default=None)
        ok = not self.status.running or silent_s is None or silent_s <= limit
        if not ok:
            self._emit({
                "event": "watchdog_trip", "market": self.market,
                "silent_s": round(silent_s or 0, 1), "limit_s": limit,
                "last_cycle_at": last,
            })
        return {
            "ok": ok,
            "running": self.status.running,
            "silent_s": round(silent_s, 1) if silent_s is not None else None,
            "limit_s": limit,
            "last_cycle_at": last,
        }

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

    def _handle_entry(self, symbol: str, payload: dict[str, Any],
                      approved_request: dict[str, Any] | None = None) -> None:
        timeframe = str(self.profile.get("timeframe", ""))
        bar_time = payload["bar_time"]
        side = payload["action"].lower()

        guard = self.guard_state()
        if guard["blocked"]:
            self._skip(symbol, "kill switch: " + "; ".join(guard["reasons"]))
            return

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

        # ---- bar lock: one decision per bar, written before the order. An approved
        # request already holds that lock (written when it was parked), so it must not
        # take it twice — that is what proves the approval is the same decision.
        if approved_request is None:
            locked = self.state.lock(
                symbol, timeframe, bar_time,
                {"action": payload["action"], "score": payload["score"],
                 "reasons": payload["reasons"][:5]},
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

        # ---- unattended-execution gate (live venues only; paper is always allowed).
        # A request a human approved in Telegram is already authorised, and the grant was
        # checked when it was parked, so re-checking it here would block the very entry
        # the operator just approved if the grant were rotated in between.
        gate = self._remote_entry_allowed()
        if approved_request is not None:
            gate = {"allowed": True, "gate": "approved",
                    "reason": f"approved via {approved_request.get('id')}",
                    "approved_by": "operator"}
        if not gate["allowed"]:
            self._skip(symbol, f"autonomy gate: {gate['reason']}")
            payload["execution"] = {"skipped": f"autonomy gate: {gate['reason']}", "gate": gate}
            self._emit({"event": "autonomy_block", "market": self.market, "symbol": symbol,
                        **gate})
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

        if approved_request is None and self.approval_required():
            request = self.park_for_approval(symbol, payload)
            payload["execution"] = {"queued_for_approval": request}
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
        self._persist_snapshot()
        payload["execution"] = {"filled": fill.to_dict(), "position": position.to_dict()}
        self._emit({"event": "trade_opened", "market": self.market, "symbol": symbol,
                    "position": position.to_dict(), "fill": fill.to_dict()})

    def approval_required(self) -> bool:
        """Does a new entry need a human yes?

        ``autonomy.mode``: ``ask`` (default) queues every live entry, ``auto`` sends it —
        and ``auto`` is only reachable when the venue is the paper broker or the execution
        gate is armed. Everything else queues, which is also what happens when the setting
        is missing or malformed.
        """
        # paper is the rehearsal: queuing approvals for simulated fills teaches nothing
        if isinstance(self.broker, PaperBroker):
            return False
        mode = str(self.settings.get("autonomy.mode", "ask") or "ask").lower()
        if mode not in ("ask", "auto", "off"):
            mode = "ask"
        if mode == "off":
            return False
        if mode == "auto":
            return not self._remote_entry_allowed()["allowed"]
        return True

    # ------------------------------------------------------------------ approval queue
    def park_for_approval(self, symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a plan and wait for a human decision instead of sending an order.

        The plan is written **before** anything reaches the venue, so an approval can
        never arrive for a plan that no longer exists, and a crash while waiting loses
        nothing: the request is in ``state/<market>/approvals.json``. Requests expire
        quickly because the price they were computed at expires too.
        """
        import uuid

        plan = payload.get("risk", {}).get("plan") or {}
        ttl = int(self.settings.get("autonomy.approval_ttl_s", 1800))
        now = dt.datetime.now(dt.timezone.utc)
        request = {
            "id": uuid.uuid4().hex[:10],
            "market": self.market,
            "symbol": symbol,
            "side": str(payload.get("action") or "").upper(),
            "score": payload.get("score"),
            "reasons": list(payload.get("reasons") or [])[:4],
            "plan": plan,
            "bar_time": payload.get("bar_time"),
            "created_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(seconds=ttl)).isoformat(),
            "status": "pending",
        }
        self.state.add_approval(request)
        self._emit({"event": "approval_requested", "market": self.market,
                    "symbol": symbol, "request": request})
        return request

    def take_approval(self, symbol: str) -> dict[str, Any] | None:
        """The pending request for a symbol, or None (expired requests self-expire)."""
        for request in self.state.approvals(status="pending"):
            if request.get("symbol") != symbol:
                continue
            try:
                expires = dt.datetime.fromisoformat(str(request.get("expires_at")))
            except ValueError:
                expires = None
            if expires and expires <= dt.datetime.now(dt.timezone.utc):
                self.state.resolve_approval(request["id"], "expired")
                continue
            return request
        return None

    def execute_approved(self, request_id: str, *, by: str = "operator") -> dict[str, Any]:
        """Send the order for an approved plan at the current market price.

        The price is re-derived now: the plan was computed on the closed bar, so the
        original entry may no longer be where the book is. If the market moved beyond the
        plan's tolerance the request is rejected instead of chased (an approval is not a
        licence to pay any price).
        """
        request = self.state.get_approval(request_id)
        if not request:
            return {"ok": False, "reason": "unknown request"}
        if request.get("status") != "pending":
            return {"ok": False, "reason": f"request is {request.get('status')}"}
        if self.take_approval(request["symbol"]) is None:
            return {"ok": False, "reason": "request expired"}
        symbol = request["symbol"]
        payload = {
            "action": request.get("side"),
            "symbol": symbol,
            "score": request.get("score"),
            "reasons": request.get("reasons") or [],
            "bar_time": request.get("bar_time"),
            "risk": {"plan": request.get("plan") or {}},
        }
        self.state.resolve_approval(request_id, "approved", by=by)
        # The bar lock was written when the plan was parked, so re-locking here would make
        # the engine refuse its own approved entry. Instead the decision is stamped onto
        # the existing lock: one entry per bar still holds, and the ledger says who
        # authorised this one.
        self.state.stamp_decision(
            symbol, str(self.profile.get("timeframe", "")), str(request.get("bar_time") or ""),
            {"approved_by": by, "approval_id": request_id},
        )
        self._handle_entry(symbol, payload, approved_request=request)
        return {"ok": True, "symbol": symbol, "execution": payload.get("execution")}

    def reject_approval(self, request_id: str, *, by: str = "operator", reason: str = "") -> bool:
        return self.state.resolve_approval(request_id, "rejected", by=by, reason=reason)

    def _skip(self, symbol: str, reason: str) -> None:
        self.state.append_ledger({"event": "skip", "market": self.market, "symbol": symbol, "reason": reason})
        self._emit({"event": "skip", "market": self.market, "symbol": symbol, "reason": reason})

    # ------------------------------------------------------------------ remote execution gate
    def _remote_entry_allowed(self) -> dict[str, Any]:
        """May an *unattended* entry be sent to this venue?

        In live mode the answer is **not mine to invent**. A gate written as a three-line
        function is one "temporary" relaxation away from never blocking anything, and
        nothing notices: the engine just trades. So the decision is bound to a fingerprint
        of the pure function that implements it plus the risk limits it enforces
        (:mod:`trading_office.autonomy`). Editing either invalidates the grant and entries
        are refused until an operator re-arms them with ``trading-office arm-live``.

        Paper trading is always allowed: it is the rehearsal, and a rehearsal that needs a
        signature every bar teaches nothing.
        """
        from .autonomy import evaluate, execution_gate_limits

        return evaluate(
            is_paper=isinstance(self.broker, PaperBroker),
            armed=self.state.get_setting("live_entry_grant") or {},
            limits=execution_gate_limits(self.settings),
        )

    # ------------------------------------------------------------------ manual controls
    def execution_gate(self) -> dict[str, Any]:
        """Is this engine allowed to send real orders from the UI right now?

        Manual CLOSE/CANCEL are the only actions a browser can trigger, so they get an
        explicit gate instead of relying on the mode wording alone. DRY-RUN is reported as
        *simulated*: the paper book is real state, the venue is not.
        """
        return {
            "market": self.market,
            "mode": self.mode,
            "broker": getattr(self.broker, "name", "paper"),
            "execute": bool(self.execute),
            "live": self.mode == "live",
            "simulated": isinstance(self.broker, PaperBroker) or not self.execute,
            "entries": self._remote_entry_allowed(),
            "note": (
                "paper + dry-run: nothing leaves this machine"
                if isinstance(self.broker, PaperBroker) and not self.execute
                else "paper: fills are simulated locally"
                if isinstance(self.broker, PaperBroker)
                else "dry-run: the engine will not send orders"
                if not self.execute
                else "live: orders go to the venue"
            ),
        }

    # ------------------------------------------------------------------ kill switch
    def guard_state(self) -> dict[str, Any]:
        """Daily-loss / consecutive-loss circuit breaker, evaluated from the ledger."""
        closed = self.state.closed_trades()[-500:]
        equity = self.broker.equity()
        day = dt.datetime.now(ZoneInfo(str(self.profile.get("sessions_timezone", "Asia/Jakarta")))).strftime("%Y-%m-%d")
        today_pnl = 0.0
        for trade in closed:
            closed_at = str(trade.get("closed_at") or trade.get("ts") or "")
            if closed_at.startswith(day) or (not closed_at and False):
                today_pnl += float(trade.get("pnl") or 0.0)
        day_start = equity - today_pnl
        streak = 0
        for trade in reversed(closed):
            if float(trade.get("pnl") or 0.0) < 0:
                streak += 1
            else:
                break
        max_daily = float(self.settings.get("engine.max_daily_loss_pct", 2.0))
        max_streak = int(self.settings.get("engine.max_consecutive_losses", 4))

        # Daily loss must be measured against the equity the day OPENED with, not against
        # (equity - realised pnl). The second form silently understates the loss whenever
        # the book carries margin: with 3x leverage a -1.5% move in the positions is -4.5%
        # of equity, and against a realised-only baseline that reads as -1.5%, so a -3%
        # breaker never fires. The mark is written once per day and survives restarts.
        mark = self.state.get_setting("day_start_equity") or {}
        if isinstance(mark, dict) and str(mark.get("day")) == day and float(mark.get("equity") or 0) > 0:
            day_start = float(mark["equity"])
            basis = "day_open_mark"
        else:
            # first look today (fresh process, or first run): realised P&L is already in
            # equity, so subtracting it recovers the opening balance of the cash book
            day_start = equity - today_pnl
            try:
                self.state.set_setting("day_start_equity", {
                    "day": day, "equity": round(day_start, 4),
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                })
            except Exception:
                pass
            basis = "first_look_of_day"
        daily_loss_pct = ((day_start - equity) / day_start * 100.0) if day_start > 0 else 0.0
        daily_loss_pct = max(0.0, daily_loss_pct)
        resumed_today = self._guard_resumed_on == day
        reasons = []
        if not resumed_today:
            if daily_loss_pct >= max_daily:
                reasons.append(f"daily loss {daily_loss_pct:.2f}% >= {max_daily:.2f}%")
            if streak >= max_streak:
                reasons.append(f"{streak} consecutive losses >= {max_streak}")
        was_blocked = bool((self.status.guard or {}).get("blocked"))
        self.status.guard = {
            "blocked": bool(reasons),
            "reasons": reasons,
            "day": day,
            "day_pnl": round(today_pnl, 4),
            "daily_loss_pct": round(daily_loss_pct, 3),
            "consecutive_losses": streak,
            "max_daily_loss_pct": max_daily,
            "max_consecutive_losses": max_streak,
            "day_start_equity": round(day_start, 2),
            "basis": basis,
            "unrealised_included": True,
            "resumed_on": self._guard_resumed_on,
            "venue": getattr(self.broker, "name", "paper") if not isinstance(self.broker, PaperBroker) else "paper",
        }
        # emit on the transition only: a breaker that fires every cycle would spam the
        # notifier (and rate-limit itself out of usefulness)
        if self.status.guard["blocked"] and not was_blocked:
            self._emit({"event": "guard_set", "market": self.market, **self.status.guard})
        elif was_blocked and not self.status.guard["blocked"]:
            self._emit({"event": "guard_clear", "market": self.market, **self.status.guard})
        return self.status.guard

    def resume(self) -> dict[str, Any]:
        """Manual override after a human looked at the day (recorded in the ledger)."""
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        day = dt.datetime.now(
            ZoneInfo(str(self.profile.get("sessions_timezone", "Asia/Jakarta")))
        ).strftime("%Y-%m-%d")
        self.state.append_ledger(
            {"event": "guard_resumed", "market": self.market, "at": stamp, "day": day,
             "guard": self.status.guard}
        )
        self.state.set_setting("guard_resumed_day", day)
        self._guard_resumed_on = day
        self.status.guard = self.guard_state()
        self._emit({"event": "guard_resumed", "market": self.market, "at": stamp})
        return self.status.guard

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
    def _restore_local_state(self) -> dict[str, Any]:
        """Rebuild open positions from disk *before* comparing with the venue.

        ``state/positions.json`` is written on every change, but a crash between the two
        writes (partial close, stop move) can leave it behind. The snapshot carries the
        same data plus the last-exit bars, and `startup_sync` merges the two by symbol
        (venue size wins). Returns a small report for the caller.
        """
        on_disk = self.state.positions()
        snapshot = self.state.load_snapshot()
        merged = dict(snapshot.get("positions") or {})
        merged.update(on_disk)  # the freshest write always wins
        restored = 0
        with self.lock:
            for symbol, raw in merged.items():
                if symbol in self.positions:
                    continue
                try:
                    position = Position.from_dict(raw)
                except Exception:
                    continue
                if not position.entry or not position.size:
                    continue
                self.positions[symbol] = position
                restored += 1
        if restored:
            self.state.restore_positions(
                {sym: pos.to_dict() for sym, pos in self.positions.items()}
            )
        return {
            "restored_from_disk": restored,
            "disk_positions": len(on_disk),
            "snapshot_source": snapshot.get("source"),
        }

    def _persist_snapshot(self, source: str = "local") -> None:
        try:
            self.state.save_snapshot(
                {
                    "positions": {sym: pos.to_dict() for sym, pos in self.positions.items()},
                    "last_exit_bar": {},
                },
                source=source,
            )
        except Exception:  # never let a snapshot write break trading
            pass

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
        restored_local = self._restore_local_state()
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
            "state_source": restored_local.get("snapshot_source") or "local",
            "restored_from_disk": restored_local["restored_from_disk"],
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

        # the venue truth becomes the new baseline for the next crash
        self._persist_snapshot(source="reconciled" if report["venue_available"] else "local")
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
                    self._persist_snapshot()
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
                    self._persist_snapshot()
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
        self._persist_snapshot()
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

    def close_position(self, symbol: str, reason: str = "manual close from web terminal") -> dict[str, Any]:
        """Close exactly one position (what the CLOSE button calls).

        Refuses symbols the engine does not hold, so a typo in the UI cannot send a market
        order into something that is not ours. Returns a plain dict: ``ok``, ``closed``
        (trade), ``orders_cancelled`` and ``reason`` when it refused.
        """
        position = self.positions.get(symbol)
        if position is None:
            return {"ok": False, "symbol": symbol, "reason": "no open position for this symbol"}
        price = 0.0
        try:
            price = float(self.fetch(symbol).get("quote", {}).get("price") or 0.0)
        except Exception:
            price = 0.0
        mark = price or position.entry
        trade = None
        orders_cancelled = []
        if isinstance(self.broker, PaperBroker):
            trade = self.broker.close(symbol, mark, reason)
        else:
            result = self.broker.close_symbol(symbol)  # type: ignore[attr-defined]
            if not result.get("ok"):
                return {"ok": False, "symbol": symbol, "reason": result.get("reason") or "venue refused"}
            orders_cancelled = result.get("orders_cancelled") or []
            trade = ClosedTrade(
                symbol=symbol, market=self.market, side=position.side, entry=position.entry,
                exit=float(result.get("price") or mark), size=float(result.get("size") or position.size),
                pnl=float(result.get("pnl") or 0.0), fees=0.0, reason=reason,
                r_multiple=0.0, opened_at=position.opened_at,
                closed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        if trade is None:
            return {"ok": False, "symbol": symbol, "reason": "the broker did not report a fill"}
        self._record_close(symbol, trade)
        self.state.append_ledger(
            {"event": "manual_close", "market": self.market, "symbol": symbol,
             "reason": reason, "orders_cancelled": orders_cancelled}
        )
        return {"ok": True, "symbol": symbol, "closed": trade.to_dict(),
                "orders_cancelled": orders_cancelled}

    def cancel_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        if isinstance(self.broker, PaperBroker):
            return {"ok": False, "reason": "the paper broker has no resting orders"}
        result = self.broker.cancel_order(order_id, symbol)  # type: ignore[attr-defined]
        self.state.append_ledger(
            {"event": "cancel_order", "market": self.market, "order_id": order_id,
             "symbol": symbol, "result": result}
        )
        return result

    def open_orders(self) -> list[dict[str, Any]]:
        """Resting orders at the venue, annotated with whether the engine knows the position."""
        if isinstance(self.broker, PaperBroker):
            return []
        try:
            orders = self.broker.open_orders()  # type: ignore[attr-defined]
        except Exception:
            return []
        for order in orders:
            order["ours"] = order.get("symbol") in self.positions
        return orders

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
            "execution_gate": self.execution_gate(),
            "broker": broker_state or {"equity": self.broker.equity()},
            "costs": self.cost_cfg.to_dict(),
            "gap_cooldowns": dict(self.gap_cooldowns),
            "positions": [p.to_dict() | {"unrealised": self.broker_marks(marks).get(p.symbol, 0.0)}
                          for p in self.positions.values()],
            "active_trades": self.active_trades(marks),
            "open_orders": self.open_orders(),
            "last_scans": self.last_scans,
            "closed_trades": closed[-25:],
            "ledger": self.state.ledger(60),
            "equity_curve": self._equity_curve(marks),
            "performance": self.performance(marks),
        }

    def broker_marks(self, marks: dict[str, float]) -> dict[str, float]:
        if isinstance(self.broker, PaperBroker):
            return self.broker.unrealised(marks)
        return {}

    def _equity_curve(self, marks: dict[str, float] | None = None) -> list[dict[str, Any]]:
        """Realised equity over time (rebuilt from the ledger) + a live mark.

        Rebuilding from the ledger instead of appending to a separate history file means
        the curve can never drift from the trades, and it survives a restart for free.
        Every point carries its running peak and drawdown, so the UI does not need to
        re-derive them (and cannot disagree with the metrics).
        """
        points = []
        equity = float(self.settings.get("engine.equity_idr" if self.market == "idx" else "engine.equity_usdt", 0))
        points.append({"ts": None, "equity": equity})
        for event in self.state.ledger(100_000):
            if event.get("event") == "trade_closed":
                equity += float(event.get("pnl", 0.0))
                points.append({"ts": event.get("closed_at") or event.get("ts"), "equity": round(equity, 2)})
        # live mark-to-market: open positions move the curve before they close
        unrealised = 0.0
        for symbol, position in self.positions.items():
            mark = (marks or {}).get(symbol)
            if mark:
                direction = 1.0 if position.side == "long" else -1.0
                unrealised += (float(mark) - position.entry) * position.size * direction
        if self.positions and unrealised:
            points.append({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                           "equity": round(equity + unrealised, 2), "unrealised": round(unrealised, 2)})
        peak = points[0]["equity"] if points else 0.0
        for point in points:
            peak = max(peak, float(point["equity"]))
            point["peak"] = round(peak, 2)
            point["drawdown_pct"] = round(((peak - float(point["equity"])) / peak * 100.0) if peak > 0 else 0.0, 3)
        return points[-200:]

    def active_trades(self, marks: dict[str, float] | None = None) -> list[dict[str, Any]]:
        """Open positions as table rows: side, size, entry/stop/target, live P&L, R.

        Every number the table shows is computed here, so the browser never has to know
        about R-multiples, partials or which side is which.
        """
        marks = marks or {}
        rows: list[dict[str, Any]] = []
        for symbol, position in self.positions.items():
            mark = float(marks.get(symbol) or position.entry)
            direction = 1.0 if position.side == "long" else -1.0
            pnl = (mark - position.entry) * position.size * direction
            rpu = position.risk_per_unit or 0.0
            rows.append({
                "symbol": symbol,
                "side": position.side.upper(),
                "size": round(position.size, 8),
                "entry": round(position.entry, 6),
                "stop": round(position.stop, 6),
                "target": round(position.target, 6),
                "mark": round(mark, 6),
                "pnl": round(pnl, 2),
                "pnl_pct": round((pnl / (position.entry * position.size) * 100.0)
                                 if position.entry and position.size else 0.0, 3),
                "r_multiple": round((mark - position.entry) * direction / rpu, 3) if rpu else 0.0,
                "partial_done": bool(position.partial_done),
                "banked_pnl": round(position.realised_pnl, 2),
                "bars_held": position.bars_held,
                "opened_at": position.opened_at,
                "kind": "position",
            })
        return rows

    def performance(self, marks: dict[str, float] | None = None) -> dict[str, Any]:
        """Closed-trade performance + the drawdown of the equity curve, for the UI/API."""
        curve = self._equity_curve(marks)
        closed = self.state.closed_trades()
        pnls = [float(t.get("pnl") or 0.0) for t in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        start = curve[0]["equity"] if curve else 0.0
        end = curve[-1]["equity"] if curve else start
        max_dd = max((float(p.get("drawdown_pct") or 0.0) for p in curve), default=0.0)
        return {
            "market": self.market,
            "trades": len(pnls),
            "win_rate_pct": round(len(wins) / len(pnls) * 100.0, 2) if pnls else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "gross_profit": round(gross_win, 2),
            "gross_loss": round(gross_loss, 2),
            "net_pnl": round(sum(pnls), 2),
            "net_profit_pct": round(((end - start) / start * 100.0) if start else 0.0, 2),
            "starting_equity": round(start, 2),
            "ending_equity": round(end, 2),
            "max_drawdown_pct": round(max_dd, 3),
            "current_drawdown_pct": round(float(curve[-1].get("drawdown_pct") or 0.0), 3) if curve else 0.0,
            "points": len(curve),
        }

    # ------------------------------------------------------------------ events
    def _emit(self, event: dict[str, Any]) -> None:
        if self.alerter is not None:
            try:
                self.alerter.notify(event)   # off the hot path, deduplicated inside
            except Exception:
                pass
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
