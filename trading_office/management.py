"""Gate 4a — Trade Management ("what happens after entry?").

Runs on every closed bar (state decisions) and on every tick (trailing), and returns a
list of :class:`Action` objects the executor applies. It never places an order itself,
which keeps it testable and lets the backtester reuse it verbatim.

Rules:
* break-even at ``breakeven_at_r`` (SL to entry + costs);
* ATR trail after ``trail_start_r``, ratchet-only (never loosens);
* optional partial take profit at ``partial_tp_r``;
* time stop after ``max_bars_in_trade`` if the trade never reached +0.5R;
* time-based exits (weekend flat for crypto, before-the-close flat for IDX);
* invalidation exit when an opposite signal fires.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

from .risk import round_trip_cost_pct


@dataclass
class Position:
    symbol: str
    market: str
    side: str  # "long" | "short"
    entry: float
    stop: float
    target: float
    size: float
    initial_stop: float
    opened_at: str
    bar_time: str  # bar the entry belongs to
    bars_held: int = 0
    best_price: float | None = None
    be_moved: bool = False
    partial_done: bool = False
    r_multiple: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.initial_stop)

    def unrealised_r(self, price: float) -> float:
        rpu = self.risk_per_unit
        if rpu <= 0:
            return 0.0
        move = (price - self.entry) if self.side == "long" else (self.entry - price)
        return move / rpu

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["risk_per_unit"] = self.risk_per_unit
        data["risk_cash"] = self.risk_per_unit * self.size
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Action:
    kind: str  # "modify_stop" | "close" | "partial"
    symbol: str
    reason: str
    price: float | None = None
    new_stop: float | None = None
    fraction: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- helpers
def _trail_price(pos: Position, price: float, trail_dist: float, tick: float | None) -> float:
    if pos.side == "long":
        raw = price - trail_dist
    else:
        raw = price + trail_dist
    if tick:
        from .config import round_to_tick

        raw = round_to_tick(raw, "down" if pos.side == "long" else "up", tick)
    return raw


def _stop_improves(pos: Position, new_stop: float) -> bool:
    return new_stop > pos.stop if pos.side == "long" else new_stop < pos.stop


# --------------------------------------------------------------------------- main
def manage_position(
    pos: Position,
    *,
    closed_bar: dict[str, Any],
    price: float,
    atr: float,
    now: dt.datetime,
    mgmt_cfg: dict[str, Any],
    profile: dict[str, Any],
    spread_bps: float = 5.0,
    opposite_signal: bool = False,
    tick_size: float | None = None,
) -> list[Action]:
    """Decide what to do with one open position.

    ``closed_bar`` is the last *closed* candle (its high/low are safe to use), ``price``
    the live price used for market exits.
    """
    actions: list[Action] = []
    long = pos.side == "long"
    pos.bars_held += 1
    rpu = pos.risk_per_unit
    if rpu <= 0:
        return [Action("close", pos.symbol, "invalid risk distance (zero)", price=price)]

    # best excursion (uses closed-bar extremes so the trail is reproducible)
    bar_high, bar_low = float(closed_bar["high"]), float(closed_bar["low"])
    if pos.best_price is None:
        pos.best_price = pos.entry
    pos.best_price = max(pos.best_price, bar_high) if long else min(pos.best_price, bar_low)
    pos.r_multiple = pos.unrealised_r(price)

    # ---------------------------------------------------------------- hard bracket
    if long and bar_low <= pos.stop:
        actions.append(Action("close", pos.symbol, "stop loss hit", price=pos.stop))
        return actions
    if (not long) and bar_high >= pos.stop:
        actions.append(Action("close", pos.symbol, "stop loss hit", price=pos.stop))
        return actions
    if long and bar_high >= pos.target:
        actions.append(Action("close", pos.symbol, "take profit hit", price=pos.target))
        return actions
    if (not long) and bar_low <= pos.target:
        actions.append(Action("close", pos.symbol, "take profit hit", price=pos.target))
        return actions

    # ---------------------------------------------------------------- invalidation
    if mgmt_cfg.get("exit_on_opposite_signal", True) and opposite_signal:
        actions.append(Action("close", pos.symbol, "invalidation: opposite signal", price=price))
        return actions

    # ---------------------------------------------------------------- partial TP
    partial_r = mgmt_cfg.get("partial_tp_r")
    if partial_r and not pos.partial_done and pos.r_multiple >= float(partial_r):
        actions.append(
            Action(
                "partial",
                pos.symbol,
                f"partial take profit at {float(partial_r):.2f}R",
                price=price,
                fraction=float(mgmt_cfg.get("partial_tp_fraction", 0.5)),
            )
        )
        pos.partial_done = True

    # ---------------------------------------------------------------- break-even
    be_r = float(mgmt_cfg.get("breakeven_at_r", 1.0))
    if not pos.be_moved and pos.r_multiple >= be_r:
        cost_pct = round_trip_cost_pct(pos.market, profile, spread_bps, pos.entry)
        buffer = cost_pct * pos.entry * float(mgmt_cfg.get("breakeven_buffer_spread", 1.0))
        new_stop = pos.entry + buffer if long else pos.entry - buffer
        if tick_size:
            from .config import round_to_tick

            new_stop = round_to_tick(new_stop, "down" if long else "up", tick_size)
        if _stop_improves(pos, new_stop):
            actions.append(
                Action("modify_stop", pos.symbol, f"break-even at {be_r:.2f}R", new_stop=new_stop)
            )
            pos.stop = new_stop
            pos.be_moved = True

    # ---------------------------------------------------------------- ATR trail
    trail_start = float(mgmt_cfg.get("trail_start_r", 1.0))
    trail_mult = float(mgmt_cfg.get("trail_atr_mult", 1.0))
    if pos.r_multiple >= trail_start and atr > 0 and trail_mult > 0:
        candidate = _trail_price(pos, pos.best_price or price, trail_mult * atr, tick_size)
        if _stop_improves(pos, candidate):
            actions.append(
                Action(
                    "modify_stop",
                    pos.symbol,
                    f"ATR trail {trail_mult:g}x ATR behind best {pos.best_price:.4f}",
                    new_stop=candidate,
                )
            )
            pos.stop = candidate

    # ---------------------------------------------------------------- time stop
    max_bars = int(
        mgmt_cfg.get(
            "max_bars_in_trade_idx" if pos.market == "idx" else "max_bars_in_trade_crypto",
            15 if pos.market == "idx" else 96,
        )
    )
    if pos.bars_held >= max_bars and pos.r_multiple < 0.5:
        actions.append(
            Action("close", pos.symbol, f"time stop: {pos.bars_held} bars without 0.5R", price=price)
        )
        return actions

    # ---------------------------------------------------------------- time-based exits
    tz_name = profile.get("sessions_timezone", "Asia/Jakarta")
    try:
        from zoneinfo import ZoneInfo

        local = now.astimezone(ZoneInfo(tz_name))
    except Exception:  # pragma: no cover - tzdata missing
        local = now
    if pos.market == "crypto" and mgmt_cfg.get("flat_before_weekend", False):
        if local.weekday() == 4 and local.hour >= 22:
            actions.append(Action("close", pos.symbol, "weekend flat (funding/swap)", price=price))
    if pos.market == "idx":
        minutes_before = int(mgmt_cfg.get("idx_close_before_minutes", 10))
        close_minute = 15 * 60 + 50 - minutes_before  # IDX closes 15:50 WIB (post-closing 16:15)
        if local.hour * 60 + local.minute >= close_minute and local.weekday() < 5:
            actions.append(Action("close", pos.symbol, "before IDX close (no overnight risk)", price=price))
    return actions


def should_exit_on_signal(position: Position, signal_action: str) -> bool:
    """True when a fresh opposite signal invalidates the open side."""
    if signal_action == "LONG" and position.side == "short":
        return True
    if signal_action == "SHORT" and position.side == "long":
        return True
    return False
