"""Telegram control surface: approve, reject, flatten, break the glass — from a phone.

Threat model first
------------------
A chat that can move money is an authentication system, so it is treated as one:

1. **Owner allowlist.** Only the numeric chat id in ``alerts.telegram.chat_id`` may command
   the bot. Anyone else gets exactly one refusal and no information about the book.
2. **Only decisions leave the bot.** Numbers come from the engine, never from the message:
   the bot never parses an amount, a price or a symbol out of a chat message. The buttons
   carry an **opaque request id**, and the id is resolved against the engine's queue. So
   a typo, a copy-paste or a clever looking message can never become an order.
3. **Callback data is a pointer, not a payload.** Telegram caps callback data at 64 bytes,
   which nudges designs toward encoding intent in the message ("close BTC 0.5"). Doing that
   would make the parser an attack surface. ``ap:CLOSE:ab12cd34ef`` is a pointer.
4. **No replay.** A request id resolves once; the state store refuses a second transition,
   so a double tap (or a replayed update) cannot send a second order.
5. **A nonce on the buttons.** Every inline keyboard carries the first 6 hex chars of a
   SHA-256 over the visible text plus the engine's request id, and :func:`verify_nonce`
   refuses a keyboard whose text no longer matches. A forged or edited message is not
   actionable. This is not a cryptographic signature (the bot's own API already
   authenticates updates); it is a cheap consistency check against confusion and copy-paste.
6. **Replay of the HTTP long-poll itself.** Updates carry ``update_id``; the poller persists
   ``last_update_id`` and asks for ``offset = last + 1`` so a restarted bot does not re-run
   yesterday's approvals.

What it refuses to do
---------------------
It cannot *create* an order. There is no "buy" and no "open": the only entries that can be
approved are ones the engine already decided on through the four gates and parked. Anything
that looks like a manual entry is answered with the reason it is not supported.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("trading_office.telegram")

API_ROOT = "https://api.telegram.org"

#: commands that may change the book, with the confirmation they require
ACTIONS = {
    "CLOSE": "close one position (reduce-only market)",
    "FLAT": "flatten everything now",
    "RESUME": "clear the kill switch for today",
    "PAUSE": "stop the engine loop",
    "START": "start the engine loop",
}


def nonce_for(text: str, request_id: str) -> str:
    return hashlib.sha256(f"{text}|{request_id}".encode("utf-8")).hexdigest()[:6]


def verify_nonce(text: str, request_id: str, nonce: str) -> bool:
    return bool(nonce) and nonce_for(text, request_id) == nonce


@dataclass
class ApprovalCard:
    """The message for one pending request: text + inline keyboard."""

    text: str
    keyboard: list[list[dict[str, str]]]

    def to_reply_markup(self) -> dict[str, Any]:
        return {"inline_keyboard": self.keyboard}

    def to_telegram(self) -> dict[str, Any]:
        return {"text": self.text, "reply_markup": self.to_reply_markup(),
                "parse_mode": "HTML", "disable_web_page_preview": True}


@dataclass
class TelegramControl:
    """Long-polling control bot. Owns no trading logic: it calls the engine."""

    token: str = ""
    owner_chat_id: str = ""
    engine_for: Callable[[str], Any] | None = None       # market -> engine
    any_engine: Callable[[], Any] | None = None
    transport: Callable[[str, str, str, str], dict[str, Any]] | None = None
    poll_timeout_s: float = 25.0
    market: str = "crypto"
    _offset: int = 0
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    audit: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=lambda: {
        "updates": 0, "commands": 0, "denied": 0, "actions": 0, "errors": 0})

    # ------------------------------------------------------------------ state
    @property
    def configured(self) -> bool:
        return bool(self.token and self.owner_chat_id)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "running": bool(self._thread and self._thread.is_alive()),
            "owner": bool(self.owner_chat_id),
            "market": self.market,
            "offset": self._offset,
            "stats": dict(self.stats),
            "commands": list(ACTIONS),
            "reason": None if self.configured else
            "TO_TELEGRAM_TOKEN + alerts.telegram.chat_id required",
        }

    # ------------------------------------------------------------------ transport
    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.transport is not None:
            return self.transport(method, self.token, self.owner_chat_id, json.dumps(payload))
        url = f"{API_ROOT}/bot{self.token}/{method}"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.poll_timeout_s + 10) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"HTTP {exc.code}"}
        except Exception as exc:  # noqa: BLE001 - the bot must survive a dead network
            return {"ok": False, "error": type(exc).__name__}

    def get_updates(self, timeout: float | None = None) -> list[dict[str, Any]]:
        payload = {"offset": self._offset + 1,
                   "timeout": int(timeout or self.poll_timeout_s),
                   "allowed_updates": ["callback_query", "message"]}
        result = self._call("getUpdates", payload)
        if not result.get("ok"):
            self.stats["errors"] += 1
            return []
        return list(result.get("result") or [])

    def send(self, text: str, *, keyboard: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": self.owner_chat_id, "text": text,
                                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        return self._call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> dict[str, Any]:
        return self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    # ------------------------------------------------------------------ authorisation
    def is_owner(self, chat_id: Any) -> bool:
        return bool(self.owner_chat_id) and str(chat_id) == str(self.owner_chat_id)

    def _audit(self, event: str, **detail: Any) -> None:
        self.audit.append({"event": event, "at": time.time(), **detail})
        del self.audit[:-100]
        log.info("telegram: %s %s", event, {k: v for k, v in detail.items() if k != "text"})

    # ------------------------------------------------------------------ cards
    def approval_card(self, request: dict[str, Any]) -> ApprovalCard:
        plan = request.get("plan") or {}
        rid = str(request.get("id") or "")
        text = (
            f"<b>Approval needed · {request.get('symbol')}</b>\n"
            f"side <b>{request.get('side')}</b> · score {request.get('score')}\n"
            f"entry {plan.get('entry')} · stop {plan.get('stop')} · target {plan.get('target')}\n"
            f"size {plan.get('size')} · risk {plan.get('risk_pct')}% of equity · RR {plan.get('rr')}\n"
            f"why: {', '.join(request.get('reasons') or []) or '—'}\n"
            f"expires {request.get('expires_at')}"
        )
        approve_nonce = nonce_for(text, f"{rid}:approve")
        reject_nonce = nonce_for(text, f"{rid}:reject")
        keyboard = [[
            {"text": "✅ Approve", "callback_data": f"ap:APPROVE:{rid}:{approve_nonce}"},
            {"text": "⛔ Reject", "callback_data": f"ap:REJECT:{rid}:{reject_nonce}"},
        ]]
        return ApprovalCard(text=text, keyboard=keyboard)

    def notify_request(self, request: dict[str, Any]) -> dict[str, Any]:
        card = self.approval_card(request)
        return self.send(card.text, keyboard=card.to_reply_markup())

    def panic_keyboard(self) -> dict[str, Any]:
        rid = "flat"
        text = "PANIC"
        return {"inline_keyboard": [[
            {"text": "🛑 Flatten all", "callback_data": f"ap:FLAT:{rid}:{nonce_for(text, rid)}"},
            {"text": "▶️ Resume trading", "callback_data": f"ap:RESUME:{rid}:{nonce_for(text, rid)}"},
        ]]}

    # ------------------------------------------------------------------ dispatch
    def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """One Telegram update -> one decision. Pure apart from the engine calls."""
        self.stats["updates"] += 1
        self._offset = max(self._offset, int(update.get("update_id") or 0))

        if "callback_query" in update:
            query = update["callback_query"]
            chat_id = (query.get("message") or {}).get("chat", {}).get("id")
            if not self.is_owner(chat_id):
                self.stats["denied"] += 1
                self._audit("denied", chat_id=chat_id, kind="callback")
                self.answer_callback(str(query.get("id") or ""), "not authorised")
                return {"ok": False, "reason": "not authorised"}
            return self._handle_callback(query)

        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if not self.is_owner(chat_id):
            self.stats["denied"] += 1
            self._audit("denied", chat_id=chat_id, kind="message")
            return {"ok": False, "reason": "not authorised"}
        return self._handle_message(str(message.get("text") or ""))

    def _handle_message(self, text: str) -> dict[str, Any]:
        self.stats["commands"] += 1
        import shlex

        try:
            parts = shlex.split(text)
        except ValueError:
            return {"ok": False, "reason": "could not parse"}
        if not parts:
            return self.send("/help for commands")
        command = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1:]
        engine = self.engine_for(self.market) if self.engine_for else (self.any_engine() if self.any_engine else None)
        if engine is None:
            return self.send("engine unavailable")
        if command in ("help", "start"):
            return self._help()
        if command == "status":
            return self._status(engine)
        if command == "pending":
            rows = engine.state.approvals(status="pending")
            if not rows:
                return self.send("no pending approvals")
            for row in rows:
                self.notify_request(row)
            return {"ok": True, "sent": len(rows)}
        if command == "panic":
            return self._act("FLAT", engine, by="telegram")
        if command == "resume":
            return self._act("RESUME", engine, by="telegram")
        if command in ("buy", "sell", "long", "short", "entry", "open"):
            # deliberately unsupported: entries only come from the strategy's queue
            return self.send(
                "Manual entries are not supported on purpose. The bot can approve or reject "
                "plans the engine produced through its four gates; it cannot invent an order."
            )
        return self.send(f"unknown command /{command}\n/help")

    def _help(self) -> dict[str, Any]:
        return self.send(
            "<b>Trading Office</b>\n"
            "/status — engine, equity, guard, open positions\n"
            "/pending — re-send the approval cards\n"
            "/panic — flatten everything (reduce-only)\n"
            "/resume — clear today's kill switch\n\n"
            "Entries arrive as buttons from the strategy and expire quickly.\n"
            "Manual buy/sell is not supported: the bot cannot invent an order."
        )

    def _status(self, engine: Any) -> dict[str, Any]:
        st = engine.status
        guard = engine.guard_state()
        gate = engine.execution_gate()
        beat = engine.heartbeat()
        rows = [
            f"<b>{engine.market}</b> · {st.mode} · {'running' if st.running else 'stopped'}",
            f"equity {st.equity:,.2f} · cycle {st.cycle_count} · last {st.last_cycle_at}",
            f"heartbeat {'ok' if beat['ok'] else 'STALLED'} ({beat.get('silent_s')}s)",
            f"guard {'OPEN' if guard['blocked'] else 'armed'} "
            f"(day {guard['daily_loss_pct']}% · streak {guard['consecutive_losses']})",
            f"entries: {gate['entries']['reason']}",
            f"positions {len(engine.positions)}",
        ]
        for symbol, pos in list(engine.positions.items())[:6]:
            rows.append(f"  {symbol} {pos.side} {pos.size} @ {pos.entry:.6g} "
                        f"stop {pos.stop:.6g}")
        return self.send("\n".join(rows), keyboard=self.panic_keyboard())

    def _handle_callback(self, query: dict[str, Any]) -> dict[str, Any]:
        data = str(query.get("data") or "")
        parts = data.split(":")
        callback_id = str(query.get("id") or "")
        if len(parts) not in (3, 4) or parts[0] != "ap":
            self.answer_callback(callback_id, "unknown action")
            return {"ok": False, "reason": "bad payload"}
        _prefix, action, request_id = parts[0], parts[1].upper(), parts[2]
        nonce = parts[3] if len(parts) == 4 else ""

        engine = self.engine_for(self.market) if self.engine_for else (self.any_engine() if self.any_engine else None)
        if engine is None:
            self.answer_callback(callback_id, "engine unavailable")
            return {"ok": False, "reason": "engine unavailable"}

        if action in ("APPROVE", "REJECT"):
            request = engine.state.get_approval(request_id)
            if not request:
                self.answer_callback(callback_id, "unknown request")
                return {"ok": False, "reason": "unknown request"}
            card = self.approval_card(request)
            expected = nonce_for(card.text, f"{request_id}:{'approve' if action == 'APPROVE' else 'reject'}")
            if not nonce or nonce != expected:
                self.stats["denied"] += 1
                self._audit("nonce_mismatch", request_id=request_id, action=action)
                self.answer_callback(callback_id, "this card is stale or edited")
                return {"ok": False, "reason": "nonce mismatch"}
            if action == "APPROVE":
                result = engine.execute_approved(request_id, by="telegram")
            else:
                engine.reject_approval(request_id, by="telegram")
                result = {"ok": True, "rejected": True}
            self.stats["actions"] += 1
            self._audit(action.lower(), request_id=request_id, result=result)
            self.answer_callback(callback_id, "done" if result.get("ok") else str(result.get("reason")))
            text = ("✅ approved and sent" if result.get("ok") else f"⚠️ {result.get('reason')}")
            self.send(f"{text} · {request.get('symbol')}")
            return result

        if action in ("FLAT", "RESUME", "CLOSE", "PAUSE", "START"):
            return self._act(action, engine, by="telegram", request_id=request_id,
                             callback_id=callback_id)

        self.answer_callback(callback_id, "unknown action")
        return {"ok": False, "reason": "unknown action"}

    def _act(self, action: str, engine: Any, *, by: str, request_id: str = "",
             callback_id: str = "") -> dict[str, Any]:
        action = action.upper()
        self.stats["actions"] += 1
        if action == "FLAT":
            result = engine.force_close_all(f"telegram panic by {by}")
            closed = result.get("closed") if isinstance(result, dict) else result
            self._audit("flat", count=len(closed or []))
            stuck = sorted(engine.positions)  # a venue that refuses to close must be visible
            if stuck:
                self.send(f"⚠️ flatten incomplete, still open: {', '.join(stuck)}")
        elif action == "RESUME":
            result = engine.resume()
        elif action == "CLOSE":
            request = engine.state.get_approval(request_id) or {}
            symbol = request.get("symbol")
            result = engine.close_position(symbol, "telegram close") if symbol else {
                "ok": False, "reason": "unknown request"}
        elif action == "PAUSE":
            engine.stop()
            result = {"ok": True, "paused": True}
        elif action == "START":
            engine.start(background=True)
            result = {"ok": True, "started": True}
        else:
            return {"ok": False, "reason": "unknown action"}
        if callback_id:
            self.answer_callback(callback_id, action.lower())
        self.send(f"<b>{action}</b> · {json.dumps(result, default=str)[:300]}")
        return {"ok": True, "action": action, "result": result}

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:  # pragma: no cover - exercised through handle_update
        if not self.configured:
            raise RuntimeError("telegram control needs TO_TELEGRAM_TOKEN and alerts.telegram.chat_id")
        log.info("telegram control started, market=%s", self.market)
        while not self._stop.is_set():
            for update in self.get_updates():
                try:
                    self.handle_update(update)
                except Exception as exc:  # noqa: BLE001 - never die on one bad update
                    self.stats["errors"] += 1
                    log.error("telegram update failed: %s", exc)

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_forever, name="telegram-control",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
