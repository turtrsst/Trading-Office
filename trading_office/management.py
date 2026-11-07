"""Gate 4a — Trade Management ("what happens after entry?").

Runs on every closed bar (state decisions) and on every tick (trailing), and returns a
list of :class:`Action` objects the executor applies. It never places an order itself,
which keeps it testable and lets the backtester reuse it verbatim.

Rules, in the order they are applied on each closed bar:

1. **hard bracket** — stop / target inside the bar's range (pessimistic: stop first);
2. **gap through the stop** — ``fill_price`` set by the caller when the bar opened beyond
   the stop, so the loss is recorded at the real (worse) fill instead of the stop price;
3. **invalidation** — opposite signal exits at market;
4. **partial take profit** — ``partial_tp_r`` (default **2R**) closes
   ``partial_tp_fraction`` (default 50 %) and, per ``partial_be_at_r``, immediately moves
   the stop of the remainder to **break-even + round-trip costs** (fees, spread, slippage)
   so the runner is risk-free from that moment;
5. **break-even** — ``breakeven_at_r`` (1R) moves the stop to entry + costs;
6. **ATR trail** — after ``trail_start_r``, ``trail_atr_mult`` (1.5) × ATR behind the best
   price, ratchet-only (never loosens, never into the previous stop);
7. **time stop** — ``max_bars_in_trade`` bars without reaching +0.5R;
8. **time-based exits** — weekend flat (crypto) / before-the-close flat (IDX).

Fills for exits carry an assumption: a partial take profit is a limit order and fills at
its *target price* (``pnl_target_price``), net of the taker fee on the part sold; a
market exit fills at the given price. That is what makes the backtest's realised R
smaller than the planned R — costs are not a footnote.
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
    partial_price: float | None = None  # price the partial TP filled at
    initial_size: float = 0.0  # size before any partial fill
    realised_pnl: float = 0.0  # banked by partial take profits
    r_multiple: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    #: units per lot: 1 for spot/perp, 100 oz for XAUUSD, 100 shares for IDX. Without this a
    #: gold position's risk reads 100x too small, and every R-multiple downstream with it.
    contract_size: float = 1.0

    @property
    def risk_per_price_unit(self) -> float:
        """Price distance to the stop — the unit R is measured in."""
        return abs(self.entry - self.initial_stop)

    @property
    def risk_per_unit(self) -> float:
        """Cash at risk per *lot* (or per unit for spot): distance × contract size."""
        return self.risk_per_price_unit * (self.contract_size or 1.0)

    def unrealised_r(self, price: float) -> float:
        rpu = self.risk_per_price_unit
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
    pnl_target_price: float | None = None  # limit fill for a partial take profit
    fill_assumption: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- helpers
#: a stop that is gapped through cannot fill at its own price; the engine/backtester
#: fills these worse and widens the loss beyond the 1 % risk budget on purpose
GAP_STOP_NOTE = (
    "gap risk: a stop is a request, not a promise — a session gap can fill beyond it and "
    "lose more than the planned risk"
)


def _stop_reason(pos: Position) -> str:
    """Why this exit is a *win* or a *loss* matters when reading the trade list:
    after the 2R partial the stop sits above entry, so "stop loss hit" would be a lie."""
    long = pos.side == "long"
    risk_free = pos.partial_done or pos.stop > pos.entry if long else pos.partial_done or pos.stop < pos.entry
    if pos.entry and risk_free:
        return "trailing stop hit (profit locked)" if pos.stop != pos.initial_stop else "break-even stop hit"
    return "stop loss hit"


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


def breakeven_stop(
    pos: Position,
    *,
    profile: dict[str, Any],
    spread_bps: float,
    multiplier: float = 1.0,
    tick_size: float | None = None,
) -> float:
    """Break-even **plus round-trip costs** — the level at which the trade is truly flat.

    Moving the stop to the entry price still loses the commissions, the spread and the
    slippage; this walks it out by exactly that amount so "risk-free" means risk-free.
    """
    from .risk import round_trip_cost_pct

    cost_pct = round_trip_cost_pct(pos.market, profile, spread_bps, pos.entry) * float(multiplier)
    buffer = cost_pct * pos.entry
    new_stop = pos.entry + buffer if pos.side == "long" else pos.entry - buffer
    if tick_size:
        from .config import round_to_tick

        new_stop = round_to_tick(new_stop, "down" if pos.side == "long" else "up", tick_size)
    return round(new_stop, 10)


def _better_fill(closed_bar: dict[str, Any], level: float, long: bool) -> float | None:
    """Fill at the bar's open when it gapped beyond the level (a limit still fills there)."""
    open_ = float(closed_bar.get("open") or 0.0)
    if not open_:
        return None
    if long and open_ > level:
        return open_
    if (not long) and open_ < level:
        return open_
    return None


def partial_size(pos: Position, fraction: float, market: str, step: float | None = None) -> float:
    """Size of a partial close, never violating the venue's minimum/step rules.

    A 50 % slice of a small position can be below the exchange minimum; in that case the
    slice is rounded **down** to the step and, if that leaves nothing, the caller is told
    (0.0) so it can skip the partial instead of sending a rejected order.
    """
    fraction = max(0.05, min(0.95, float(fraction)))
    target = pos.size * fraction
    if market == "idx":
        lots = int(target // 100)  # IDX: whole lots of 100 shares
        if lots < 1 or pos.size - lots * 100 < 100:
            return 0.0
        return float(lots * 100)
    unit = float(step) if step and step > 0 else 1e-8
    slices = int(target / unit)
    size = slices * unit
    remainder = pos.size - size
    if slices <= 0 or remainder < unit:
        return 0.0
    return round(size, 12)


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
    size_step: float | None = None,
) -> list[Action]:
    """Decide what to do with one open position on a closed bar.

    ``closed_bar`` is the last *closed* candle (its high/low are safe to use), ``price``
    the live price used for market exits, ``size_step`` the venue amount precision (so a
    partial slice can be rounded the way the exchange demands).
    """
    actions: list[Action] = []
    long = pos.side == "long"
    pos.bars_held += 1
    rpu = pos.risk_per_unit
    if not pos.initial_size:
        pos.initial_size = pos.size
    if rpu <= 0:
        return [Action("close", pos.symbol, "invalid risk distance (zero)", price=price)]

    # best excursion (uses closed-bar extremes so the trail is reproducible)
    bar_high, bar_low = float(closed_bar["high"]), float(closed_bar["low"])
    if pos.best_price is None:
        pos.best_price = pos.entry
    pos.best_price = max(pos.best_price, bar_high) if long else min(pos.best_price, bar_low)
    pos.r_multiple = pos.unrealised_r(price)

    # ---------------------------------------------------------------- 1. stop first
    # pessimistic: when both levels sit inside one bar, the stop is assumed hit first
    if long and bar_low <= pos.stop:
        actions.append(Action("close", pos.symbol, _stop_reason(pos), price=pos.stop))
        return actions
    if (not long) and bar_high >= pos.stop:
        actions.append(Action("close", pos.symbol, _stop_reason(pos), price=pos.stop))
        return actions

    # ---------------------------------------------------------------- 2. invalidation
    if mgmt_cfg.get("exit_on_opposite_signal", True) and opposite_signal:
        actions.append(Action("close", pos.symbol, "invalidation: opposite signal", price=price))
        return actions

    # ---------------------------------------------------------------- 3. partial TP
    # Default 2R: bank half at 1:2, then make the runner risk-free with costs included.
    partial_r = mgmt_cfg.get("partial_tp_r")
    fraction = float(mgmt_cfg.get("partial_tp_fraction", 0.5))
    if partial_r and not pos.partial_done and pos.r_multiple >= float(partial_r):
        slice_size = partial_size(pos, fraction, pos.market, size_step)
        target_price = pos.entry + (float(partial_r) * rpu) * (1.0 if long else -1.0)
        if slice_size > 0:
            actions.append(
                Action(
                    "partial",
                    pos.symbol,
                    f"partial take profit {fraction*100:.0f}% at {float(partial_r):.2f}R",
                    price=target_price,
                    fraction=round(slice_size / pos.size, 6) if pos.size else fraction,
                    pnl_target_price=target_price,
                    fill_assumption="limit at the target price (fees still paid)",
                )
            )
            pos.partial_done = True
            pos.partial_price = target_price
            be_at_partial = mgmt_cfg.get("partial_be_at_r", partial_r)
            if be_at_partial is not None and pos.r_multiple >= float(be_at_partial or 0):
                new_stop = breakeven_stop(
                    pos,
                    profile=profile,
                    spread_bps=spread_bps,
                    multiplier=float(mgmt_cfg.get("breakeven_buffer_spread", 1.0)),
                    tick_size=tick_size,
                )
                if _stop_improves(pos, new_stop):
                    actions.append(
                        Action(
                            "modify_stop",
                            pos.symbol,
                            "break-even + costs after partial TP (runner is risk-free)",
                            new_stop=new_stop,
                        )
                    )
                    pos.stop = new_stop
                    pos.be_moved = True
        # a slice below the venue minimum is skipped here, never sent as a rejected order

    # ---------------------------------------------------------------- 4. target / runner cap
    # Before a partial the planned target closes everything; after it, the runner is left
    # to the ATR trail unless ``runner_target_r`` caps it.
    runner_cap = mgmt_cfg.get("runner_target_r")
    if pos.partial_done:
        cap_price = (
            pos.entry + float(runner_cap) * rpu * (1.0 if long else -1.0) if runner_cap else None
        )
    else:
        cap_price = pos.target
    if cap_price:
        hit = bar_high >= cap_price if long else bar_low <= cap_price
        if hit:
            filled = cap_price
            if action_fill := _better_fill(closed_bar, cap_price, long):
                filled = action_fill
            actions.append(
                Action(
                    "close",
                    pos.symbol,
                    ("runner target" if pos.partial_done else "take profit") + " hit",
                    price=filled,
                )
            )
            return actions

    # ---------------------------------------------------------------- 5. break-even
    be_r = float(mgmt_cfg.get("breakeven_at_r", 1.0))
    if not pos.be_moved and pos.r_multiple >= be_r:
        new_stop = breakeven_stop(
            pos,
            profile=profile,
            spread_bps=spread_bps,
            multiplier=float(mgmt_cfg.get("breakeven_buffer_spread", 1.0)),
            tick_size=tick_size,
        )
        if _stop_improves(pos, new_stop):
            actions.append(
                Action("modify_stop", pos.symbol, f"break-even at {be_r:.2f}R", new_stop=new_stop)
            )
            pos.stop = new_stop
            pos.be_moved = True

    # ---------------------------------------------------------------- 6. ATR trail
    trail_start = float(mgmt_cfg.get("trail_start_r", 1.0))
    trail_mult = float(mgmt_cfg.get("trail_atr_mult", 1.5))
    step_mult = float(mgmt_cfg.get("trail_step_atr", 0.0) or 0.0)
    if pos.r_multiple >= trail_start and atr > 0 and trail_mult > 0:
        candidate = _trail_price(pos, pos.best_price or price, trail_mult * atr, tick_size)
        # a minimum step avoids rewriting the stop on every tick for a few ticks of noise
        min_gap = step_mult * atr
        improves = _stop_improves(pos, candidate)
        if improves and min_gap > 0:
            improves = abs(candidate - pos.stop) >= min_gap
        if improves:
            actions.append(
                Action(
                    "modify_stop",
                    pos.symbol,
                    f"ATR trail {trail_mult:g}x ATR behind best {pos.best_price:.4f}",
                    new_stop=candidate,
                )
            )
            pos.stop = candidate

    # ---------------------------------------------------------------- 7. time stop
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

    # ---------------------------------------------------------------- 8. time-based exits
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
