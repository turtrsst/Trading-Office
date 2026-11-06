"""Gate 3 — Risk & Position Sizing ("how big, and where do I get out?").

The math, in order (docs/STRATEGY_LOGIC.md §3):

1. **Structural stop**: swing low/high (``swing_lookback``) OR ``atr_sl_mult * ATR``;
   the wider of the two wins, then a floor of ``max(3 * spread, 2 ticks, 0.35 ATR)``.
2. **Target**: nearest structural level vs ``rr_target``; must reach ``min_rr``.
3. **Size**: ``risk_cash = equity * risk_pct``, ``units = risk_cash / |entry - sl|``,
   then capped by margin/leverage, per-symbol exposure and liquidity participation.
4. **Cost sanity**: the target has to clear ``cost_multiple`` × round-trip costs.
5. **Portfolio**: total open risk, concurrent positions, daily-loss and losing-streak
   kill switches.

Nothing here reads a clock or a feed, so the same code sizes live orders and backtest
fills.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from .config import idx_tick_size, round_to_tick


# --------------------------------------------------------------------------- objects
@dataclass
class TradePlan:
    symbol: str
    market: str
    side: str  # "long" | "short"
    entry: float
    stop: float
    target: float
    size: float
    notional: float
    risk_cash: float
    risk_pct: float
    rr: float
    stop_distance: float
    target_distance: float
    leverage: float
    margin: float
    tick_size: float | None = None
    lots: int | None = None  # IDX only
    # diagnostics
    stop_source: str = ""
    target_source: str = ""
    caps_applied: list[str] = field(default_factory=list)
    risk_note: str = ""
    costs: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RiskDecision:
    approved: bool
    plan: TradePlan | None = None
    rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "plan": self.plan.to_dict() if self.plan else None,
            "rejections": list(self.rejections),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- helpers
def _round_price(price: float, market: str, tick: float | None = None) -> float:
    if market == "idx":
        return round_to_tick(price, "nearest", tick or idx_tick_size(price))
    digits = 8 if price < 1 else 6 if price < 100 else 4 if price < 10_000 else 2
    return round(price, digits)


def _round_down(value: float, decimals: int) -> float:
    if decimals <= 0:
        return float(int(value))
    factor = 10**decimals
    return math.floor(value * factor) / factor


def structural_levels(
    bars: Sequence[dict[str, Any]], lookback: int
) -> tuple[float | None, float | None]:
    """(recent swing low, recent swing high) over the previous ``lookback`` closed bars."""
    if len(bars) < 2:
        return None, None
    window = bars[-lookback:] if lookback > 0 else bars
    lows = [b["low"] for b in window]
    highs = [b["high"] for b in window]
    return (min(lows) if lows else None), (max(highs) if highs else None)


def round_trip_cost_pct(
    market: str, profile: dict[str, Any], spread_bps: float, price: float
) -> float:
    """Round-trip cost as a fraction of notional (commissions + spread + slippage)."""
    slippage_bps = float(profile.get("slippage_bps", 2.0))
    if market == "idx":
        buy = float(profile.get("commission_buy_pct", 0.15)) / 100.0
        sell = float(profile.get("commission_sell_pct", 0.25)) / 100.0
        return buy + sell + (spread_bps + 2 * slippage_bps) / 10_000.0
    commission_bps = float(profile.get("commission_bps", 5.5))
    return (2 * commission_bps + spread_bps + 2 * slippage_bps) / 10_000.0


def build_plan(
    *,
    symbol: str,
    market: str,
    side: str,
    entry: float,
    atr: float,
    bars: Sequence[dict[str, Any]],
    equity: float,
    config: dict[str, Any],
    risk_cfg: dict[str, Any],
    profile: dict[str, Any],
    spread_bps: float,
    tick_size: float | None = None,
    quote_volume: float | None = None,
    leverage: float | None = None,
    risk_multiplier: float = 1.0,
    risk_note: str = "",
) -> RiskDecision:
    """Compute a fully-populated :class:`TradePlan` or explain why the trade is refused."""
    rejections: list[str] = []
    warnings: list[str] = []
    caps: list[str] = []

    long = side.lower() == "long"
    lookback = int(config.get("swing_lookback", 10))
    swing_low, swing_high = structural_levels(bars, lookback)
    atr_sl_mult = float(risk_cfg.get("atr_sl_mult", 1.5))
    # IDX cash equities are not levered: notional can never exceed the account.
    if leverage is not None:
        max_lev = float(leverage)
    elif market == "idx":
        max_lev = 1.0
    else:
        max_lev = float(risk_cfg.get("max_leverage", 3))

    tick = tick_size
    if market == "idx" and tick is None:
        tick = idx_tick_size(entry)

    # ------------------------------------------------------------ 1. stop loss
    atr_stop_dist = atr_sl_mult * atr
    struct_dist = None
    if long and swing_low:
        struct_dist = entry - swing_low
    if (not long) and swing_high:
        struct_dist = swing_high - entry
    if struct_dist and struct_dist > 0:
        stop_distance = max(struct_dist, atr_stop_dist)
        stop_source = "structure" if struct_dist >= atr_stop_dist else "atr"
    else:
        stop_distance = atr_stop_dist
        stop_source = "atr"
        warnings.append("no structural swing available, using pure ATR stop")

    floor = max(3.0 * spread_bps / 10_000.0 * entry, 2.0 * (tick or 0.0), 0.35 * atr)
    if stop_distance < floor:
        stop_distance = floor
        stop_source += "+floor"
        caps.append("stop_floor")
    if stop_distance <= 0:
        return RiskDecision(False, None, ["stop distance is zero (ATR unavailable)"], warnings)

    stop = entry - stop_distance if long else entry + stop_distance
    stop = _round_price(stop, market, tick)
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return RiskDecision(False, None, ["stop rounds onto the entry price (price too coarse)"], warnings)

    # ------------------------------------------------------------ 2. target
    rr_target = max(float(risk_cfg.get("rr_target", 2.0)), float(risk_cfg.get("min_rr", 1.5)))
    rr_distance = rr_target * stop_distance
    struct_target_dist = None
    if long and swing_high and swing_high > entry:
        struct_target_dist = swing_high - entry
    if (not long) and swing_low and swing_low < entry:
        struct_target_dist = entry - swing_low
    if struct_target_dist and struct_target_dist >= float(risk_cfg.get("min_rr", 1.5)) * stop_distance:
        target_distance = min(struct_target_dist, rr_distance * 1.5)
        target_source = "structure"
    else:
        target_distance = rr_distance
        target_source = "rr_target"
    cap = float(risk_cfg.get("atr_tp_cap_mult", 6.0)) * atr
    if cap > 0 and target_distance > cap:
        target_distance = cap
        target_source += "+atr_cap"
        caps.append("target_atr_cap")

    target = entry + target_distance if long else entry - target_distance
    target = _round_price(target, market, tick)
    target_distance = abs(target - entry)
    rr = target_distance / stop_distance if stop_distance else 0.0
    min_rr = float(risk_cfg.get("min_rr", 1.5))
    if rr < min_rr:
        rejections.append(f"reward-to-risk {rr:.2f} < minimum {min_rr:.2f}")

    # ------------------------------------------------------------ 3. cost sanity
    cost_pct = round_trip_cost_pct(market, profile, spread_bps, entry)
    cost_multiple = float(risk_cfg.get("cost_multiple", 3.0))
    if target_distance / entry < cost_pct * cost_multiple:
        rejections.append(
            f"target {target_distance/entry*100:.3f}% inside {cost_multiple:g}x round-trip cost "
            f"({cost_pct*100:.3f}%)"
        )

    # ------------------------------------------------------------ 4. size
    # A stressed tape (wide book, fat ATR) shrinks the budget instead of the discipline:
    # the distance stays where structure put it, the size absorbs the stress.
    risk_pct = float(risk_cfg.get("risk_per_trade_pct", 1.0))
    mult = max(0.1, float(risk_multiplier or 1.0))
    if mult != 1.0:
        risk_pct = risk_pct / mult
        caps.append(f"risk_scale_{mult:.2f}x")
    risk_cash = equity * risk_pct / 100.0
    size = risk_cash / stop_distance
    notional = size * entry
    size_cap_reasons: list[str] = []

    max_margin_pct = float(
        profile.get("max_margin_per_trade_pct", risk_cfg.get("max_margin_per_trade_pct", 8.0))
    )
    max_notional_by_margin = equity * max_margin_pct / 100.0 * max_lev
    if notional > max_notional_by_margin:
        notional = max_notional_by_margin
        caps.append("margin_cap")
        size_cap_reasons.append(f"margin cap {max_margin_pct}% of equity at {max_lev:g}x")

    max_symbol_pct = float(risk_cfg.get("max_symbol_exposure_pct", 20.0))
    max_notional_by_exposure = equity * max_symbol_pct / 100.0
    if notional > max_notional_by_exposure:
        notional = max_notional_by_exposure
        caps.append("exposure_cap")
        size_cap_reasons.append(f"symbol exposure cap {max_symbol_pct}%")

    participation = float(risk_cfg.get("liquidity_participation_pct", 0.5)) / 100.0
    if quote_volume and quote_volume > 0:
        max_notional_by_liq = quote_volume * participation
        if notional > max_notional_by_liq:
            notional = max_notional_by_liq
            caps.append("liquidity_cap")
            size_cap_reasons.append(f"liquidity cap {participation*100:.2f}% of median bar volume")

    if notional < size * entry:  # recompute size from the capped notional
        size = notional / entry
        risk_cash = size * stop_distance

    # venue rounding
    lots = None
    if market == "idx":
        lots = int(size // 100)  # IDX trades in lots of 100 shares
        size = float(lots * 100)
        if lots < 1:
            rejections.append(
                f"minimum 1 lot (100 shares, {entry*100:,.0f} IDR) exceeds the risk budget"
            )
            size = 0.0
    else:
        size = _round_down(size, 6)
        if size <= 0:
            rejections.append("size rounds to zero at the venue precision")

    if size > 0:
        notional = size * entry
        risk_cash = size * stop_distance
    margin = notional / max_lev if max_lev > 0 else notional

    if risk_cash > equity * risk_pct / 100.0 * 1.05:
        rejections.append("sized risk exceeds the configured per-trade budget")

    plan = TradePlan(
        symbol=symbol,
        market=market,
        side=side.lower(),
        entry=entry,
        stop=stop,
        target=target,
        size=size,
        notional=notional,
        risk_cash=risk_cash,
        risk_pct=risk_cash / equity * 100.0 if equity else 0.0,
        rr=rr,
        stop_distance=stop_distance,
        target_distance=target_distance,
        leverage=max_lev,
        margin=margin,
        tick_size=tick,
        lots=lots,
        stop_source=stop_source,
        target_source=target_source,
        caps_applied=caps,
        risk_note=risk_note,
        costs={
            "spread_bps": round(spread_bps, 3),
            "round_trip_cost_pct": round(cost_pct * 100.0, 4),
            "round_trip_cost_cash": round(notional * cost_pct, 2),
            "expected_profit_cash": round(notional * target_distance / entry, 2),
        },
    )
    if size_cap_reasons:
        warnings.extend(size_cap_reasons)
    return RiskDecision(approved=not rejections, plan=plan, rejections=rejections, warnings=warnings)


# --------------------------------------------------------------------------- portfolio
def portfolio_check(
    *,
    plans: Sequence[TradePlan],
    open_positions: Sequence[dict[str, Any]],
    equity: float,
    risk_cfg: dict[str, Any],
    engine_cfg: dict[str, Any],
    daily_pnl_pct: float = 0.0,
    consecutive_losses: int = 0,
    candidate: TradePlan | None = None,
) -> list[str]:
    """Return a list of blocking reasons for a new entry (empty = clear)."""
    blocks: list[str] = []
    max_positions = int(engine_cfg.get("max_open_positions", 4))
    if len(open_positions) + (1 if candidate else 0) > max_positions:
        blocks.append(f"max concurrent positions reached ({max_positions})")

    open_risk = sum(float(p.get("risk_cash", 0.0)) for p in open_positions)
    max_risk = equity * float(engine_cfg.get("max_portfolio_risk_pct", 4.0)) / 100.0
    if candidate and open_risk + candidate.risk_cash > max_risk * 1.0001:
        blocks.append(
            f"portfolio risk {open_risk + candidate.risk_cash:,.0f} > cap {max_risk:,.0f}"
        )

    if daily_pnl_pct <= -abs(float(engine_cfg.get("max_daily_loss_pct", 2.0))):
        blocks.append(f"daily loss limit hit ({daily_pnl_pct:.2f}%)")
    if consecutive_losses >= int(engine_cfg.get("max_consecutive_losses", 4)):
        blocks.append(f"{consecutive_losses} consecutive losses, entries paused")
    return blocks


def account_snapshot(
    equity: float,
    open_positions: Sequence[dict[str, Any]],
    closed_today: Sequence[dict[str, Any]],
    engine_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Equity, open risk and the kill-switch state, for the dashboard."""
    daily_pnl = sum(float(t.get("pnl", 0.0)) for t in closed_today)
    daily_pnl_pct = daily_pnl / equity * 100.0 if equity else 0.0
    streak = 0
    for t in reversed(list(closed_today)):
        if float(t.get("pnl", 0.0)) < 0:
            streak += 1
        else:
            break
    open_risk = sum(float(p.get("risk_cash", 0.0)) for p in open_positions)
    return {
        "equity": equity,
        "open_positions": len(open_positions),
        "open_risk_cash": open_risk,
        "open_risk_pct": open_risk / equity * 100.0 if equity else 0.0,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": daily_pnl_pct,
        "consecutive_losses": streak,
        "entries_blocked": bool(
            daily_pnl_pct <= -abs(float(engine_cfg.get("max_daily_loss_pct", 2.0)))
            or streak >= int(engine_cfg.get("max_consecutive_losses", 4))
        ),
    }
