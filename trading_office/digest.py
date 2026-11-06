"""The daily digest: one message an operator can actually read in twenty seconds.

Why this is its own module
--------------------------
The first version of ``digest`` was built inside the CLI and had two flaws that a "it works,
I got the message" test would never catch:

* it constructed a **fresh engine** in a new process, so ``cycles 0`` and a stale
  ``status.equity`` described the process that had just started, not the bot that was
  running. A digest that describes the wrong process is worse than no digest: it is
  confidently wrong every morning.
* the equity it printed came from realised trades only, so an open position down 2 % was
  invisible. That is the same failure mode as the kill-switch baseline bug: the number
  looked fine and the risk was not in it.

So the report is assembled from two sources with an explicit preference:

1. **the running server** (``/api/metrics`` + ``/api/engine/status`` + ``/api/approvals``)
   — real marks, real cycle counters, real heartbeat;
2. **offline state** (a local engine rebuilt from the ledger) — used only when the server
   cannot be reached, and labelled as such in the message.

Every line carries its own unit and its own source. If a number cannot be known, it says
so instead of printing a plausible zero.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

DEFAULT_API = "http://127.0.0.1:8000"


# --------------------------------------------------------------------------- collection
def fetch_remote(base: str = DEFAULT_API, market: str = "crypto", timeout: float = 5.0) -> dict[str, Any] | None:
    """Pull the three payloads the digest needs from the running server."""
    base = base.rstrip("/")
    out: dict[str, Any] = {}
    try:
        for name, path in (
            ("metrics", f"/api/metrics?market={market}"),
            ("status", f"/api/engine/status?market={market}"),
            ("approvals", f"/api/approvals?market={market}"),
        ):
            with urllib.request.urlopen(base + path, timeout=timeout) as response:
                out[name] = json.loads(response.read())
    except Exception:  # noqa: BLE001 - any failure means "fall back to local state"
        return None
    out["base"] = base
    return out


def _guard_probe(base: str, market: str, timeout: float = 5.0) -> dict[str, Any]:
    """``/api/engine/status`` only carries a guard once a cycle has evaluated it."""
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/api/engine/kill-switch?market={market}",
                                    timeout=timeout) as response:
            data = json.loads(response.read())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _uptime(started_at: Any, now: dt.datetime | None = None) -> float | None:
    """Seconds since the engine last (re)started, or ``None`` if it never did.

    Uptime is the cheapest way to spot a bot that is *up* but was restarted five minutes
    ago by the supervisor — a state where every performance number resets to zero and
    looks like a healthy flat book.
    """
    if not started_at:
        return None
    try:
        stamp = dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    return max(0.0, (now - stamp).total_seconds())


def _duration(seconds: Any) -> str:
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, _ = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _short_stamp(value: Any) -> str:
    """``2026-11-06T17:10:12+00:00`` -> ``11-06 17:10 UTC`` (a phone shows one line per fact)."""
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(dt.timezone.utc)
    return stamp.strftime("%m-%d %H:%M UTC")


def _num(value: Any) -> str:
    """Adaptive price/size: ``61,230`` for BTC, ``0.004`` for a size, ``3,012.5`` for ETH.

    Six fixed decimals turned a phone line into ``61,230.000000``, which is unreadable and
    hides the one number that matters. Trailing zeros are dropped, never a significant digit.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    magnitude = abs(number)
    digits = 2 if magnitude >= 1000 else 3 if magnitude >= 10 else 4 if magnitude >= 1 else 6
    text = f"{number:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _money(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def snapshot_from_remote(payload: dict[str, Any], market: str) -> dict[str, Any]:
    """Normalise the server payloads into the digest's own shape."""
    metrics = payload.get("metrics") or {}
    status = payload.get("status") or {}
    approvals = payload.get("approvals") or {}
    kpi = metrics.get("kpi") or {}
    perf = metrics.get("performance") or {}
    gate = (metrics.get("execution_gate") or {})
    trades = metrics.get("active_trades") or []
    beat = metrics.get("heartbeat") or {}
    status_body = status.get("status") or {}
    guard = status_body.get("guard") or status.get("guard") or {}
    if not guard:
        # guard_state() is only evaluated on demand; ask for it rather than print "None"
        guard = _guard_probe(payload.get("base") or DEFAULT_API, market)
    drawdown = metrics.get("drawdown_pct")
    current_dd = drawdown[-1] if isinstance(drawdown, list) and drawdown else (
        drawdown if isinstance(drawdown, (int, float)) else None)
    equity_curve = metrics.get("equity") or []
    unrealised = None
    if isinstance(equity_curve, list) and len(equity_curve) >= 2 and trades:
        unrealised = round(sum(float(r.get("pnl") or 0.0) for r in trades), 2) or None
    return {
        "source": f"api {payload.get('base') or DEFAULT_API}",
        "market": market,
        "mode": (gate.get("mode") or status.get("mode") or "?"),
        "broker": gate.get("broker") or "?",
        "equity": kpi.get("equity", metrics.get("equity")),
        "equity_kind": "mark-to-market",
        "net_pct": kpi.get("net_profit_pct"),
        "drawdown_pct": kpi.get("max_drawdown_pct"),
        "current_dd_pct": current_dd,
        "trades": kpi.get("total_trades"),
        "win_rate": kpi.get("win_rate"),
        "profit_factor": kpi.get("profit_factor"),
        "guard": guard,
        "loop": {
            "running": bool(beat.get("running", status_body.get("running"))),
            "ok": bool(beat.get("ok", True)),
            "silent_s": beat.get("silent_s"),
            "limit_s": beat.get("limit_s"),
            "cycles": status_body.get("cycle_count"),
            "orders": status_body.get("orders"),
            "blocked": status_body.get("blocked"),
            "last_cycle_at": status_body.get("last_cycle_at"),
            "started_at": status_body.get("started_at"),
            "uptime_s": _uptime(status_body.get("started_at")),
        },
        "positions": [
            {
                "symbol": row.get("symbol"), "side": row.get("side"), "size": row.get("size"),
                "entry": row.get("entry"), "stop": row.get("stop"), "target": row.get("target"),
                "pnl": row.get("pnl"), "r": row.get("r_multiple"),
                "partial_done": row.get("partial_done"),
            }
            for row in trades
        ],
        "unrealised": unrealised,
        "pending": approvals.get("requests") or [],
        "entries": gate.get("entries") or {},
        "autonomy_mode": approvals.get("mode"),
        "skips": (status.get("status") or {}).get("skips") or [],
        "alerts": (payload.get("metrics") or {}).get("alerts_enabled"),
        "perf": perf,
    }


def snapshot_from_engine(engine: Any) -> dict[str, Any]:
    """Build the same shape offline, from state on disk (server unreachable)."""
    marks: dict[str, float] = {}
    for symbol, scan in getattr(engine, "last_scans", {}).items():
        price = (scan or {}).get("quote", {}).get("price")
        if price:
            marks[symbol] = float(price)
    rows = engine.active_trades(marks)
    guard = engine.guard_state()
    beat = engine.heartbeat()
    perf = engine.performance(marks)
    skips = [
        str(e.get("reason", "")).split(":")[0]
        for e in engine.state.ledger(400) if e.get("event") == "skip"
    ]
    live_equity = engine.broker.equity(marks) if marks else engine.broker.equity()
    return {
        "source": "local state (server not reachable)",
        "market": engine.market,
        "mode": engine.mode,
        "broker": getattr(engine.broker, "name", "paper"),
        "equity": live_equity,
        "equity_kind": "mark-to-market" if marks else "entry marks (no live quote in this process)",
        "net_pct": perf.get("net_profit_pct"),
        "drawdown_pct": perf.get("max_drawdown_pct"),
        "current_dd_pct": perf.get("current_drawdown_pct"),
        "trades": perf.get("trades"),
        "win_rate": perf.get("win_rate_pct"),
        "profit_factor": perf.get("profit_factor"),
        "guard": guard,
        "loop": {
            "running": bool(engine.status.running),
            "ok": bool(beat.get("ok", True)),
            "silent_s": beat.get("silent_s"),
            "cycles": engine.status.cycle_count,
            "orders": engine.status.orders,
            "blocked": engine.status.blocked,
            "last_cycle_at": engine.status.last_cycle_at,
            "started_at": engine.status.started_at,
            "uptime_s": _uptime(engine.status.started_at),
        },
        "positions": [
            {"symbol": r["symbol"], "side": r["side"], "size": r["size"], "entry": r["entry"],
             "stop": r["stop"], "target": r["target"], "pnl": r["pnl"], "r": r["r_multiple"],
             "partial_done": r.get("partial_done")}
            for r in rows
        ],
        "unrealised": sum(float(r.get("pnl") or 0.0) for r in rows) or None,
        "pending": engine.state.approvals(status="pending"),
        "entries": engine.execution_gate().get("entries") or {},
        "autonomy_mode": engine.settings.get("autonomy.mode", "ask"),
        "skips": skips,
        "alerts": None,
        "perf": perf,
    }


# --------------------------------------------------------------------------- rendering
def format_digest(snap: dict[str, Any]) -> str:
    """HTML accepted by Telegram's parse_mode=HTML (see the tag whitelist note below).

    Only ``<b>``, ``<i>`` and ``<code>`` are used, and every value that comes from the
    outside (a symbol, a skip reason) is escaped, because Telegram rejects the whole
    message — not just the offending line — when the markup does not parse.
    """
    from html import escape

    guard = snap.get("guard") or {}
    loop = snap.get("loop") or {}
    entries = snap.get("entries") or {}
    pending = snap.get("pending") or []

    lines: list[str] = []
    lines.append(f"<b>Trading Office · {escape(str(snap.get('market')))} · digest</b>")
    mode = escape(str(snap.get("mode")))
    lines.append(f"<i>{mode} · {escape(str(snap.get('broker')))} · {escape(str(snap.get('source')))}</i>")

    # ---- equity & performance
    lines.append("")
    lines.append("<b>📊 Equity &amp; performance</b>")
    lines.append(f"equity {_money(snap.get('equity'))} <i>({escape(str(snap.get('equity_kind')))})</i>")
    if snap.get("unrealised") is not None:
        lines.append(f"unrealised {_money(snap.get('unrealised'))}")
    lines.append(f"realised net {_pct(snap.get('net_pct'))} · max DD {_money(snap.get('drawdown_pct'))}%"
                 f" · now {_money(snap.get('current_dd_pct'))}%")
    pf = snap.get("profit_factor")
    pf_text = "∞" if pf is None and (snap.get("trades") or 0) > 0 else ("—" if pf is None else _money(pf))
    lines.append(f"{snap.get('trades') or 0} closed · win {_money(snap.get('win_rate'), 1)}% · PF {pf_text}")

    # ---- risk
    lines.append("")
    lines.append("<b>🛡️ Guard</b>")
    blocked = bool(guard.get("blocked"))
    state = "OPEN — no new entries" if blocked else "armed"
    lines.append(f"{state} · day {_money(guard.get('daily_loss_pct'))}% · "
                 f"streak {guard.get('consecutive_losses')}/{guard.get('max_consecutive_losses')}")
    if guard.get("basis"):
        lines.append(f"<i>basis {escape(str(guard.get('basis')))} · day open "
                     f"{_money(guard.get('day_start_equity'))}</i>")
    if blocked and guard.get("reasons"):
        lines.append("⚠️ " + escape("; ".join(guard["reasons"])))

    # ---- loop
    lines.append("")
    lines.append("<b>💓 Loop</b>")
    if loop.get("running") is False:
        lines.append("engine <b>stopped</b> (not cycling)")
    elif loop.get("ok") is False:
        lines.append(f"⚠️ <b>stalled</b> — silent {loop.get('silent_s')}s "
                     f"(limit {loop.get('limit_s')})")
    else:
        silent = loop.get("silent_s")
        lines.append(f"running · last cycle {_money(silent, 1)}s ago"
                     if silent is not None else "running · no cycle yet")
    lines.append(f"cycles {loop.get('cycles') or 0} · orders {loop.get('orders') or 0} · "
                 f"blocked {loop.get('blocked') or 0}")
    if loop.get("uptime_s") is not None:
        since = ""
        if loop.get("started_at"):
            since = _short_stamp(loop.get("started_at"))
        lines.append(f"uptime {_duration(loop.get('uptime_s'))}"
                     + (f" · up since {since}" if since else ""))

    # ---- book
    lines.append("")
    positions = snap.get("positions") or []
    lines.append(f"<b>📈 Positions ({len(positions)})</b>")
    if not positions:
        lines.append("flat")
    for row in positions[:8]:
        partial = " · ½ banked" if row.get("partial_done") else ""
        lines.append(
            f"{escape(str(row.get('symbol')))} {escape(str(row.get('side')))} "
            f"{_num(row.get('size'))} @ {_num(row.get('entry'))} "
            f"stop {_num(row.get('stop'))} · {_money(row.get('pnl'))} "
            f"({_num(row.get('r'))}R){partial}"
        )
    if len(positions) > 8:
        lines.append(f"… +{len(positions) - 8} more")

    # ---- approvals
    lines.append("")
    lines.append(f"<b>⏳ Approvals ({len(pending)})</b>")
    if not pending:
        lines.append("none waiting")
    for row in pending[:5]:
        plan = row.get("plan") or {}
        lines.append(f"{escape(str(row.get('symbol')))} {escape(str(row.get('side')))} "
                     f"entry {_num(plan.get('entry'))} · id {escape(str(row.get('id')))}")
    if entries:
        lines.append(f"entries: {'allowed' if entries.get('allowed') else 'BLOCKED'} "
                     f"({escape(str(entries.get('reason') or '?'))})")

    # ---- skips
    skips = snap.get("skips") or []
    if skips:
        top = Counter(skips).most_common(4)
        lines.append("")
        lines.append("<b>⏭️ Skipped</b>")
        lines.append(", ".join(f"{escape(str(reason or 'other'))} ×{count}" for reason, count in top))

    lines.append("")
    lines.append(f"<i>{dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · "
                 f"autonomy {escape(str(snap.get('autonomy_mode') or '?'))}</i>")
    return "\n".join(lines)


def plain(text: str) -> str:
    """The same message without markup, for a terminal."""
    import re

    text = re.sub(r"</?(b|i|code)>", "", text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#x27;", "'")


# --------------------------------------------------------------------------- send
def send(settings: Any, text: str, *, market: str = "crypto") -> dict[str, Any]:
    """Deliver through the configured alert channel, reporting how it went."""
    from .alerts import Alerter

    alerter = Alerter.from_settings(settings, market)
    if not alerter.configured:
        return {"ok": False, "reason": "alert channel not configured "
                                       "(TO_TELEGRAM_TOKEN + alerts.telegram.chat_id)"}
    outcome = alerter.deliver(text)
    return {"ok": bool(outcome.get("ok")), "channel": alerter.channel, **outcome}
