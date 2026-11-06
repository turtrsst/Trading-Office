"""Analyse-then-confirm: one button that proposes a trade, one that sends it.

Why this module exists
----------------------
The engine can already scan and it can already park a plan for approval, but both of those
are *unattended* paths: the loop decides, the human signs. An operator sitting in front of
the dashboard wants the reverse order of control — "look at the whole book now, show me the
best setups, let me pick one" — and that path did not exist anywhere except in the CLI.

Two properties make this safe rather than a second, weaker execution path:

* **The analysis is the bot's own analysis.** It calls :meth:`TradingEngine.evaluate`, so the
  four gates, the risk sizing, the cost check and the kill switch are the same code that
  trades unattended. A confirm button that used a different pipeline would be a way to
  approve a decision the bot would never have made.
* **A confirmation is bound to what was shown.** Every analysis gets an
  :func:`analysis_id` — a hash of the symbol, the *closed* bar it was computed on, the side
  and the size — and :func:`confirm` **recomputes the analysis** before sending. A click on a
  stale card therefore cannot become a trade at a different price, and the server never has
  to trust a plan that arrived from a browser (nothing in the request can change the size,
  the stop or the target).

What it deliberately does not do
--------------------------------
* no path around ``_handle_entry`` (one execution door means one set of guards);
* no bypass of the autonomy gate or of ``arm-live`` — a browser click is consent *inside*
  an armed setup, never a substitute for arming it;
* no execution from ``WAIT``/``BLOCKED`` scans, or while the breaker is open: the card says
  why, the button stays disabled;
* no execution of ``synthetic`` (demo) prices **on a live venue**. On the paper broker it is
  allowed and labelled ``PAPER · synthetic``: that is a rehearsal against demo candles and
  nothing can leave the building. The moment the venue is real, demo data is analysis-only.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

#: actions that may become an order. Anything else is information only.
TRADEABLE = ("LONG", "SHORT")

#: sources that are real market data. ``synthetic`` is deterministic demo data.
REAL_SOURCES = ("ccxt", "idx")

#: why a scan cannot be executed, in operator language.
BLOCK_TEXT = {
    "synthetic": "data is synthetic (no provider) — analysis only",
    "kill_switch": "circuit breaker open — no new entries today",
    "position": "a position is already open on this symbol",
    "gate": "autonomy gate closed for live entries (arm-live --yes)",
    "wait": "no setup on this bar",
    "no_plan": "no risk plan (the move is not worth the cost)",
    "stale": "the setup moved since it was shown — analyse again",
    "symbol": "this symbol is not in the watchlist",
    "error": "analysis failed for this symbol",
}


def analysis_id(symbol: str, bar_time: str, side: str, size: float) -> str:
    """Fingerprint of the decision a confirm button would authorise.

    Includes the size, because the size is what turns a plan into a number of contracts:
    approving a plan whose size changed underneath is approving a different trade.
    """
    payload = f"{symbol}|{bar_time}|{side.upper()}|{float(size):.10f}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _fingerprint(scan: dict[str, Any]) -> str:
    plan = (scan.get("risk") or {}).get("plan") or {}
    return analysis_id(str(scan.get("symbol")), str(scan.get("bar_time")),
                       str(scan.get("action") or ""), float(plan.get("size") or 0.0))


def _row(scan: dict[str, Any], *, source: str, block: str = "",
         extra: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = (scan.get("risk") or {}).get("plan") or {}
    signal = scan.get("signal") or {}
    tradeable = not block and plan and scan.get("action") in TRADEABLE
    row = {
        "symbol": scan.get("symbol"),
        "action": scan.get("action"),
        "stage": scan.get("stage"),
        "score": scan.get("score"),
        "max_score": 9,
        "price": scan.get("price"),
        "bar_time": scan.get("bar_time"),
        "source": source,
        "executable": bool(tradeable and scan.get("executable", True)),
        "block": block,
        "block_text": BLOCK_TEXT.get(block, ""),
        "plan": plan,
        "rr": plan.get("rr"),
        "risk_pct": plan.get("risk_pct"),
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "target": plan.get("target"),
        "size": plan.get("size"),
        "reasons": list(scan.get("reasons") or [])[:4],
        "warnings": list(scan.get("warnings") or [])[:3],
        "ltf_trend": (signal.get("trend") or {}).get("ltf") or signal.get("ltf_trend"),
        "htf_trend": (signal.get("trend") or {}).get("htf") or signal.get("htf_trend"),
        "data_simulated": source not in REAL_SOURCES,
        "id": _fingerprint(scan) if plan else "",
    }
    row.update(extra or {})
    return row


def _result(ok: bool, *, symbol: str = "", side: str = "", reason: str | None = None,
            block: str = "", **extra: Any) -> dict[str, Any]:
    """One shape for every answer, so a caller never has to guess which keys exist."""
    out: dict[str, Any] = {"ok": ok, "symbol": symbol, "side": side, "reason": reason,
                           "block": block, "block_text": BLOCK_TEXT.get(block, "") if block else ""}
    out.update(extra)
    return out


def analyse_all(engine: Any) -> dict[str, Any]:
    """Scan every symbol the engine watches, newest closed bar, and rank the results.

    Ranking is by |score| so a strong short heads the list just like a strong long: the
    operator is choosing an opportunity, not a direction. Ties keep the configured order,
    which is the order the engine itself would have used.
    """
    started = time.time()
    guard = engine.guard_state()
    gate = engine._remote_entry_allowed()
    paper = str(gate.get("gate") or "") == "paper"
    symbols = list(engine.symbols)
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            scan = engine.evaluate(symbol)          # read-only: analysis must never trade
        except Exception as exc:  # noqa: BLE001 - one broken feed must not blank the board
            rows.append(_row({"symbol": symbol, "action": "ERROR", "stage": "error", "score": 0,
                              "risk": {}, "reasons": [], "warnings": []},
                             source="", block="error",
                             extra={"block_text": f"{type(exc).__name__}: {exc}"[:140]}))
            continue
        source = str(scan.get("source") or "")
        block = ""
        if guard.get("blocked"):
            block = "kill_switch"
        elif symbol in engine.positions:
            block = "position"
        elif scan.get("action") not in TRADEABLE:
            block = "wait"
        elif not ((scan.get("risk") or {}).get("plan")):
            block = "no_plan"
        elif source not in REAL_SOURCES and not paper:
            # demo candles are fine for a rehearsal, never for a real venue
            block = "synthetic"
        elif not gate.get("allowed"):
            block = "gate"
        rows.append(_row(scan, source=source, block=block, extra={"paper": paper}))

    rows.sort(key=lambda r: (-abs(float(r.get("score") or 0)), str(r.get("symbol"))))
    board = {
        "market": engine.market,
        "mode": engine.mode,
        "broker": getattr(engine.broker, "name", "paper"),
        "paper": paper,
        "guard": guard,
        "entries_gate": gate,
        "symbols": symbols,
        "analysed": len(rows),
        "tradeable": sum(1 for r in rows if r.get("executable")),
        "elapsed_s": round(time.time() - started, 3),
        "rows": rows,
    }
    engine.last_analysis = board       # handy for the UI, the ledger or a test; never an order
    return board


def confirm(engine: Any, payload: dict[str, Any], *, by: str = "browser") -> dict[str, Any]:
    """Execute the plan the operator just confirmed, or refuse with a reason.

    The plan is **recomputed**, not read back from the request or from a cache: the browser
    sends a fingerprint, never a size or a stop. Refusals are the interesting part — every
    one of them is a case where a naive "buy now" endpoint would have opened a position the
    rest of the system was actively preventing.
    """
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        return _result(False, reason="symbol is required")
    if symbol not in list(engine.symbols):
        return _result(False, symbol=symbol, block="symbol",
                       reason=f"{symbol} is not in the watchlist — {BLOCK_TEXT['symbol']}")

    try:
        fresh = engine.evaluate(symbol)
    except Exception as exc:  # noqa: BLE001
        return _result(False, symbol=symbol, block="error",
                       reason=f"could not refresh the analysis: {type(exc).__name__}: {exc}"[:160])

    action = str(fresh.get("action") or "")
    plan = (fresh.get("risk") or {}).get("plan") or {}
    if action not in TRADEABLE:
        return _result(False, symbol=symbol, side=action, block="wait",
                       reason=f"nothing to execute: {action or 'no signal'}")
    if not plan:
        return _result(False, symbol=symbol, side=action, block="no_plan",
                       reason=BLOCK_TEXT["no_plan"])

    shown_side = str(payload.get("side") or "").upper()
    if shown_side and shown_side != action:
        return _result(False, symbol=symbol, side=action, block="stale",
                       reason=f"the analysis now says {action}, the request said {shown_side}")

    shown_bar = str(payload.get("bar_time") or "")
    if shown_bar and shown_bar != str(fresh.get("bar_time")):
        return _result(False, symbol=symbol, side=action, block="stale",
                       reason="a new bar closed since the analysis — analyse again",
                       bar_shown=shown_bar, bar_now=fresh.get("bar_time"))

    expected = str(payload.get("analysis_id") or "")
    current = _fingerprint(fresh)
    if expected and expected != current:
        return _result(False, symbol=symbol, side=action, block="stale",
                       reason="the plan changed since it was shown — analyse again",
                       analysis_id=current)

    # Cheap checks before the expensive door. ``_handle_entry`` writes the bar lock *before*
    # it consults the gate, so an attempt that was going to be refused anyway would burn the
    # bar and make the operator's retry look like a duplicate.
    gate = engine._remote_entry_allowed()
    if not gate.get("allowed"):
        return _result(False, symbol=symbol, side=action, block="gate",
                       reason=f"autonomy gate: {gate.get('reason')}", entries_gate=gate)
    guard = engine.guard_state()
    if guard.get("blocked"):
        return _result(False, symbol=symbol, side=action, block="kill_switch",
                       reason="kill switch: " + "; ".join(guard.get("reasons") or []))

    before = dict(engine.positions)
    # a fresh copy: the payload is what the strategy produced, and ``_handle_entry`` writes
    # its verdict into it
    outcome: dict[str, Any] = dict(fresh)
    engine._handle_entry(symbol, outcome)
    execution = outcome.get("execution") or {}
    opened = symbol in engine.positions and symbol not in before
    if opened:
        # the engine's ledger already has the order; this records *who* asked for it, so a
        # button-confirmed trade is distinguishable from one the loop decided on its own
        engine.state.append_ledger({"event": "confirmed_trade", "market": engine.market,
                                    "symbol": symbol, "side": action, "by": by,
                                    "bar_time": fresh.get("bar_time"),
                                    "analysis_id": current})
    return _result(bool(opened), symbol=symbol, side=action,
                   reason=None if opened else _refusal(execution, engine, symbol),
                   opened=opened, analysis_id=current, execution=execution,
                   position=(engine.positions.get(symbol).to_dict()
                             if opened and symbol in engine.positions else None))


def _refusal(execution: dict[str, Any], engine: Any = None, symbol: str = "") -> str:
    """Turn ``_handle_entry``'s side channel into one line an operator can act on.

    Not every refusal writes to ``payload['execution']`` (the bar lock and the duplicate
    checks return early), so the ledger is the authoritative place to look when the side
    channel is empty.
    """
    if execution.get("skipped"):
        return str(execution["skipped"])
    if execution.get("queued_for_approval"):
        return "queued for approval (autonomy.mode=ask)"
    if execution.get("filled"):
        return "order sent but no position was recorded"
    if engine is not None and symbol:
        for entry in engine.state.ledger(40):
            if entry.get("event") == "skip" and entry.get("symbol") == symbol:
                return str(entry.get("reason") or "no order was sent")
    return "no order was sent"


def watches(engine: Any) -> dict[str, Any]:
    """What the Analyse button may cover, for a caller that wants it before scanning."""
    return {
        "market": engine.market,
        "mode": engine.mode,
        "symbols": list(engine.symbols),
        "positions": sorted(engine.positions.keys()),
        "entries_gate": engine._remote_entry_allowed(),
        "guard": engine.guard_state(),
    }
