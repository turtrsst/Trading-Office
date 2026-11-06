"""Autonomy gate: what an unattended bot is allowed to do, and what must stay manual.

This module is deliberately the *smallest* piece of code in the project, because it is the
one that decides whether real money may be moved without a human in the loop. It exists
because of a bug class that is otherwise invisible: a gate written as a three-line function
gets "temporarily" relaxed during debugging and never tightened again.

Two mechanisms, both cheap:

1. :func:`execution_gate_fingerprint` — a hash of the *source* of the gate and of the risk
   limits it enforces. Arming live entries stores the fingerprint; changing the gate (or
   the limits) invalidates the arm. Reviewing the grant therefore means reviewing exactly
   the code and numbers that matter.
2. :func:`execution_gate_limits` — the numbers themselves, in one dict, so a diff is
   readable in a Telegram message or a code review.

Everything here is pure: no I/O, no state, no environment. It is called from
:meth:`trading_office.engine.TradingEngine._remote_entry_allowed` and from the CLI/API.
"""

from __future__ import annotations

import hashlib
import inspect
from typing import Any

#: limits that are part of the grant. Change one -> live entries stop until re-armed.
LIMIT_KEYS: tuple[tuple[str, str, float], ...] = (
    ("max_open_positions", "engine.max_open_positions", 4),
    ("max_portfolio_risk_pct", "engine.max_portfolio_risk_pct", 4.0),
    ("max_daily_loss_pct", "engine.max_daily_loss_pct", 2.0),
    ("max_consecutive_losses", "engine.max_consecutive_losses", 4),
    ("require_trend_filter", "engine.require_trend_filter", True),
    ("min_filters_passed", "engine.min_filters_passed", 3),
)


def execution_gate_limits(settings: Any) -> dict[str, Any]:
    """The risk limits a grant covers, normalised so a JSON compare is stable."""
    out: dict[str, Any] = {}
    for name, dotted, default in LIMIT_KEYS:
        value = settings.get(dotted, default)
        if isinstance(value, bool):
            out[name] = bool(value)
        elif isinstance(value, (int, float)):
            out[name] = round(float(value), 4)
        else:
            out[name] = value
    return out


def execution_gate_fingerprint() -> str:
    """Hash of the gate's own source plus the limit names it reads.

    Not a security boundary against a determined attacker with write access to the
    repository — nothing in-process can be. It is a *change detector*: it makes "somebody
    relaxed the gate" impossible to do quietly, which is the realistic failure.
    """
    try:
        source = inspect.getsource(_evaluate_gate)
    except OSError:  # pragma: no cover - frozen/zip imports
        source = "_evaluate_gate"
    payload = source + "|" + ",".join(name for name, _dotted, _default in LIMIT_KEYS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _evaluate_gate(*, is_paper: bool, armed: dict[str, Any] | None, limits: dict[str, Any],
                   fingerprint: str) -> dict[str, Any]:
    """Pure decision function (its source is what :func:`execution_gate_fingerprint` hashes).

    Kept separate from the engine method so the fingerprint covers only this logic: a
    refactor elsewhere does not invalidate an armed grant, a change *here* always does.
    """
    if is_paper:
        return {"allowed": True, "gate": "paper", "reason": "paper broker: simulated fills"}
    grant = armed or {}
    if not grant.get("armed"):
        return {"allowed": False, "gate": "armed", "fingerprint": fingerprint, "limits": limits,
                "reason": "live entries are not armed (trading-office arm-live --yes)"}
    if grant.get("fingerprint") != fingerprint:
        return {"allowed": False, "gate": "fingerprint", "fingerprint": fingerprint,
                "armed_fingerprint": grant.get("fingerprint"), "limits": limits,
                "reason": "the execution gate changed since it was armed: re-arm required"}
    if grant.get("limits") != limits:
        return {"allowed": False, "gate": "limits", "fingerprint": fingerprint, "limits": limits,
                "armed_limits": grant.get("limits"),
                "reason": "risk limits changed since arming: re-arm required"}
    return {"allowed": True, "gate": "armed", "fingerprint": fingerprint, "limits": limits,
            "reason": f"armed by {grant.get('armed_by') or 'operator'} at {grant.get('armed_at')}"}


def evaluate(*, is_paper: bool, armed: dict[str, Any] | None, limits: dict[str, Any]) -> dict[str, Any]:
    return _evaluate_gate(is_paper=is_paper, armed=armed, limits=limits,
                          fingerprint=execution_gate_fingerprint())
