"""Out-of-band alerting: a second pair of eyes that does not depend on the web UI.

Why this exists
---------------
The dashboard is only useful while somebody is looking at it. A 24/7 desk needs a channel
that fires when *nobody* is looking: breaker trips, startup reconciliation mismatches,
watchdog failures. Telegram's bot API is a single HTTPS POST, has no SDK to install, and
works from a VPS behind NAT.

Design rules
------------
* **Off by default, never fatal.** No token -> every call is a no-op that reports why.
* **Never block the engine.** Sends happen on a daemon thread with a short timeout; a
  dead network can never stall a trading cycle.
* **No secrets in the payload.** The bot token is part of the URL, so it is never logged;
  the fingerprint of the chat id is enough to identify a channel in a log line.
* **Deduplicated.** The same alert inside ``cooldown_s`` is dropped — a wedged provider
  can otherwise generate thousands of identical messages and get the bot rate-limited.
* **Testable without the internet.** The transport is injectable, so the offline suite
  covers formatting, dedup and failure handling.

Configuration (``config/settings.json`` → ``alerts``)::

    {"enabled": true, "channel": "telegram", "events": [...],
     "cooldown_s": 300, "telegram": {"chat_id": "123456"}}

The token comes from the environment (``TO_TELEGRAM_TOKEN``) and never from config.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

#: events worth waking somebody up for (engine events, not every cycle)
DEFAULT_EVENTS = (
    "guard_set",           # daily-loss / loss-streak breaker tripped
    "guard_clear",         # breaker cleared (operator resume)
    "engine_error",        # an exception escaped a cycle
    "reconciliation",      # startup_sync found a mismatch worth a human look
    "watchdog_trip",       # the engine loop stopped beating
    "kill_switch_trip",
    "trade_opened",        # optional, useful while learning the system
)

SEVERITY = {
    "guard_set": "critical",
    "kill_switch_trip": "critical",
    "engine_error": "critical",
    "watchdog_trip": "critical",
    "reconciliation": "warning",
    "guard_clear": "info",
    "trade_opened": "info",
}


@dataclass
class Alerter:
    """Formats, deduplicates and (optionally) delivers alerts."""

    enabled: bool = False
    channel: str = "telegram"
    events: tuple[str, ...] = DEFAULT_EVENTS
    cooldown_s: float = 300.0
    token: str = ""            # injected from the environment by the caller
    chat_id: str = ""
    market: str = ""
    timeout: float = 5.0
    transport: Callable[[str, str], dict[str, Any]] | None = None
    sink: list[dict[str, Any]] = field(default_factory=list)   # what was sent/attempted
    _last_sent: dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_settings(cls, settings: Any, market: str = "", *, token: str | None = None,
                      transport: Callable[[str, str], dict[str, Any]] | None = None) -> "Alerter":
        cfg = settings.get("alerts", {}) or {}
        telegram = cfg.get("telegram", {}) or {}
        if token is None:
            from .env import get

            # explicit env name from config first, then the conventional one
            token = get(str(telegram.get("token_env") or "TO_TELEGRAM_TOKEN")) or get("TO_TELEGRAM_TOKEN")
        events = cfg.get("events") or list(DEFAULT_EVENTS)
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            channel=str(cfg.get("channel", "telegram")),
            events=tuple(str(e) for e in events),
            cooldown_s=float(cfg.get("cooldown_s", 300) or 0),
            token=str(token or ""),
            chat_id=str(telegram.get("chat_id", "") or ""),
            market=market,
            timeout=float(cfg.get("timeout_s", 5) or 5),
            transport=transport,
        )

    # ------------------------------------------------------------------ state
    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "channel": self.channel,
            "events": list(self.events),
            "cooldown_s": self.cooldown_s,
            "market": self.market,
            "sent": sum(1 for s in self.sink if s.get("ok")),
            "attempted": len(self.sink),
            "reason": None if (self.enabled and self.configured)
            else ("disabled in config" if not self.enabled
                  else "TO_TELEGRAM_TOKEN / alerts.telegram.chat_id missing"),
        }

    # ------------------------------------------------------------------ formatting
    def should_send(self, event: dict[str, Any], now: float | None = None) -> bool:
        if not self.enabled:
            return False
        name = str(event.get("event") or "")
        if name not in self.events:
            return False
        key = f"{self.market}:{name}:{event.get('symbol') or event.get('error') or ''}"
        now = time.time() if now is None else now
        last = self._last_sent.get(key, 0.0)
        return (now - last) >= self.cooldown_s

    def format(self, event: dict[str, Any]) -> str:
        """Plain text, readable on a phone lock screen. No secrets, no JSON dump."""
        name = str(event.get("event") or "event")
        severity = SEVERITY.get(name, "info").upper()
        lines = [f"[{severity}] Trading Office · {self.market or event.get('market') or ''}".strip()]
        detail = {
            "guard_set": lambda: "breaker open: " + "; ".join(event.get("reasons") or []),
            "guard_clear": lambda: "breaker cleared",
            "engine_error": lambda: f"cycle error: {event.get('error')}",
            "reconciliation": lambda: (
                f"startup sync: {len(event.get('closed') or [])} closed, "
                f"{len(event.get('adopted') or [])} adopted, "
                f"{len(event.get('stops_missing') or [])} without a stop"
            ),
            "watchdog_trip": lambda: (
                f"engine loop silent {event.get('silent_s')}s (last cycle "
                f"{event.get('last_cycle_at')})"
            ),
            "trade_opened": lambda: (
                f"{event.get('symbol')} {str((event.get('position') or {}).get('side', '')).upper()} "
                f"@ {((event.get('position') or {}).get('entry'))}"
            ),
        }.get(name, lambda: json.dumps({k: v for k, v in event.items()
                                        if k not in ("event", "market")}, default=str)[:300])()
        lines.append(detail)
        if name == "guard_set":
            lines.append(f"day P&L {event.get('day_pnl')} · "
                         f"loss {event.get('daily_loss_pct')}% · "
                         f"streak {event.get('consecutive_losses')}")
        return "\n".join(line for line in lines if line)

    # ------------------------------------------------------------------ delivery
    def _send_telegram(self, text: str) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = json.dumps({"chat_id": self.chat_id, "text": text,
                           "disable_web_page_preview": True}).encode("utf-8")
        request = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return {"ok": 200 <= response.status < 300, "status": response.status}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "status": exc.code, "error": f"HTTP {exc.code}"}
        except Exception as exc:  # noqa: BLE001 - network failures are expected, not fatal
            return {"ok": False, "error": type(exc).__name__}

    def deliver(self, text: str) -> dict[str, Any]:
        transport = self.transport or self._send_telegram
        try:
            return transport(text, self.chat_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def notify(self, event: dict[str, Any], *, blocking: bool = False) -> dict[str, Any]:
        """Consider one engine event. Returns what happened, always a dict."""
        if not self.should_send(event):
            return {"sent": False, "reason": "filtered" if self.enabled else "disabled"}
        if not self.configured:
            return {"sent": False, "reason": "not configured (token/chat_id)"}
        text = self.format(event)
        key = f"{self.market}:{event.get('event')}:{event.get('symbol') or event.get('error') or ''}"
        with self._lock:
            self._last_sent[key] = time.time()
        outcome = self.deliver(text) if blocking else self._deliver_async(text)
        record = {"event": event.get("event"), "text": text, **outcome}
        self.sink.append(record)
        del self.sink[:-50]  # keep the last 50 for the UI/diagnostics
        return {"sent": bool(outcome.get("ok")), "channel": self.channel, **outcome}

    def _deliver_async(self, text: str) -> dict[str, Any]:
        """Fire and forget: a trading cycle must never wait on a chat API."""
        outcome: dict[str, Any] = {"ok": True, "queued": True}
        thread = threading.Thread(target=self.deliver, args=(text,), daemon=True)
        thread.start()
        return outcome

    # ------------------------------------------------------------------ helpers
    def chat_fingerprint(self) -> str:
        if not self.chat_id:
            return ""
        import hashlib

        return hashlib.sha256(self.chat_id.encode("utf-8")).hexdigest()[:8]
