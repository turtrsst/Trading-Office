"""Execution-cost models: latency, dynamic slippage, gap risk and risk multipliers.

A backtest that fills at the next bar's open with a fixed ``slippage_bps`` is optimistic
in exactly the two regimes that hurt a trend system: fast crypto books and IDX auctions /
overnight gaps. This module models the cost the way it actually behaves — as a function of
volatility and of the size being pushed through the book:

```
slippage_bps = base_bps  +  atr_mult * (ATR / price * 10_000)  +  impact_coeff * sqrt(participation %)
```

* ``atr_mult`` is the **slippage coefficient in ATR units** (``k_slip``): a fill moves
  ``atr_mult × ATR`` against you, which is the same number expressed in bps as
  ``atr_mult × ATR/price × 10⁴``. ``k_slip = 0.06`` on a 96 400 BTC with ATR 700 is a
  651-point worse fill (≈ 6.8 bps); the same coefficient in a quiet large-cap is cents.
* the coefficient is not constant: while ATR is stretched against its own 50-bar mean
  (``shock_atr_mult``, default 3×) it is scaled up to ``shock_slippage_mult`` (default 3×),
  the panic regime of a flash crash or a scheduled macro release.
* ``impact_coeff`` is a square-root market-impact law against the bar's quote volume, so a
  size that is 0.5 % of a bar costs more than a size that is 0.02 %.
* ``latency_ms`` delays the fill: the fill price is the open plus the bar's own drift over
  the latency window (modelled from the open→close move), so a 400 ms delay in a bar that
  ran 0.8 % does not fill at the open.
* ``gap`` produces the gap/spread tolerance used by the *next* session's filters
  (overnight gap in ATR units + spread multiple), so entries resume only once the shock
  has normalised — the black-swan guard for IDX openings and weekend crypto wicks.
* ``risk multiplier`` inflates position risk (and therefore shrinks size) while the tape
  is stressed: ``1 + 0.5 * (spread / max_spread)`` clamped to ``max_risk_multiplier``.
  A wider book means the stop is more likely to be jumped, so the same 1 % has to be
  spread over a wider distance.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from . import indicators as ind

#: Market defaults — overridable per profile under ``profiles.<market>.execution_costs``.
DEFAULTS: dict[str, dict[str, Any]] = {
    "crypto": {
        "latency_ms": 400,
        "slippage_base_bps": 1.5,
        "atr_mult": 0.06,
        "impact_coeff": 1.5,
        "max_slippage_bps": 45.0,
        "gap_reject_atr": 2.0,
        "gap_reject_spread_mult": 3.0,
        "gap_fill_fraction": 0.5,
        "gap_cooldown_bars": 3,
        "max_risk_multiplier": 1.75,
        "shock_atr_mult": 3.0,
        "shock_slippage_mult": 3.0,
        "auction_windows": [],
    },
    "idx": {
        "latency_ms": 900,
        "slippage_base_bps": 2.0,
        "atr_mult": 0.05,
        "impact_coeff": 2.5,
        "max_slippage_bps": 80.0,
        "gap_reject_atr": 1.5,
        "gap_reject_spread_mult": 2.5,
        "gap_fill_fraction": 0.75,
        "gap_cooldown_bars": 2,
        "max_risk_multiplier": 1.5,
        "shock_atr_mult": 3.0,
        "shock_slippage_mult": 2.5,
        # IDX pre-opening / opening auction and the closing auction: fills are unreliable
        "auction_windows": [["08:45", "09:15"], ["15:45", "16:15"]],
    },
}


@dataclass
class CostConfig:
    latency_ms: float = 400.0
    slippage_base_bps: float = 1.5
    atr_mult: float = 0.06
    impact_coeff: float = 1.5
    max_slippage_bps: float = 45.0
    gap_reject_atr: float = 2.0
    gap_reject_spread_mult: float = 3.0
    gap_fill_fraction: float = 0.5
    gap_cooldown_bars: int = 3
    max_risk_multiplier: float = 1.75
    #: ATR >= shock_atr_mult × ATR(50) = a shock bar; the slippage coefficient is then
    #: multiplied by shock_slippage_mult (panic / flash-crash regime).
    shock_atr_mult: float = 3.0
    shock_slippage_mult: float = 3.0
    auction_windows: list[list[str]] = field(default_factory=list)

    @classmethod
    def for_market(cls, market: str, profile: dict[str, Any] | None = None) -> "CostConfig":
        base = dict(DEFAULTS.get(market, DEFAULTS["crypto"]))
        base.update((profile or {}).get("execution_costs") or {})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in base.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SlippageEstimate:
    fill_price: float
    slippage_bps: float
    components: dict[str, float]
    capped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_price": round(self.fill_price, 8),
            "slippage_bps": round(self.slippage_bps, 3),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "capped": self.capped,
        }


@dataclass
class LatencyResult:
    fill_price: float
    drift_bps: float
    moved_bps: float  # |drift| against the intended direction

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_price": round(self.fill_price, 8),
            "drift_bps": round(self.drift_bps, 3),
            "moved_bps": round(self.moved_bps, 3),
        }


# --------------------------------------------------------------------------- latency
def bar_seconds(timeframe: str) -> float:
    minutes = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60,
        "4h": 240, "1d": 1440, "1wk": 10080,
    }.get(str(timeframe), 15)
    return minutes * 60.0


def apply_latency(
    intended_price: float,
    bar: dict[str, Any],
    *,
    latency_ms: float,
    timeframe: str = "15m",
    side: str = "buy",
) -> LatencyResult:
    """Fill ``latency_ms`` into a bar: the price drifts at the bar's own open→close rate.

    A delay of one whole bar would fill at the close, which is the worst case; anything
    shorter is proportional. Direction matters: a buy that drifts up is worse than the
    open, a sell that drifts down is worse — the function reports the adverse move.
    """
    open_ = float(bar.get("open") or intended_price)
    close = float(bar.get("close") or intended_price)
    if open_ <= 0 or not math.isfinite(open_) or not math.isfinite(close):
        return LatencyResult(intended_price, 0.0, 0.0)
    bar_frac = max(0.0, min(1.0, latency_ms / 1000.0 / bar_seconds(timeframe)))
    drift_bps = (close / open_ - 1.0) * 10_000.0 * bar_frac
    fill = intended_price * (1.0 + drift_bps / 10_000.0)
    # "adverse" = bps of cost: buying after the price rose costs, selling after it fell costs
    adverse = drift_bps if side == "buy" else -drift_bps
    return LatencyResult(fill, drift_bps, adverse)


# --------------------------------------------------------------------------- slippage
def slippage_coefficient(
    *, atr: float, atr_baseline: float | None, cfg: CostConfig
) -> tuple[float, float]:
    """Effective ``k_slip`` and its stress ratio.

    ``k_slip = atr_mult`` in a normal bar. When ATR is at least ``shock_atr_mult`` times
    its own 50-bar mean, the book is in a shock: market makers widen, stops cascade, and
    the same order pays ``shock_slippage_mult`` times more. The multiplier ramps linearly
    between the two thresholds so there is no cliff, and is reported so a backtest can
    explain *why* a fill was expensive.
    """
    base_k = float(cfg.atr_mult)
    atr = float(atr or 0.0)
    baseline = float(atr_baseline or 0.0)
    if atr <= 0 or baseline <= 0:
        return base_k, 0.0
    stress = atr / baseline
    threshold = max(1.0, float(cfg.shock_atr_mult))
    if stress <= 1.0:
        return base_k, stress
    ramp = min(1.0, (stress - 1.0) / max(1e-9, threshold - 1.0))
    k = base_k * (1.0 + (float(cfg.shock_slippage_mult) - 1.0) * ramp)
    return round(k, 6), round(stress, 4)


def estimate_slippage(
    *,
    price: float,
    atr: float,
    notional: float,
    quote_volume: float | None,
    cfg: CostConfig,
    atr_baseline: float | None = None,
) -> SlippageEstimate:
    """Volatility- and size-aware slippage in basis points (always ≥ 0)."""
    price = float(price)
    atr_bps = (float(atr) / price * 10_000.0) if atr and atr > 0 else 0.0
    k_slip, stress = slippage_coefficient(atr=atr, atr_baseline=atr_baseline, cfg=cfg)
    components: dict[str, float] = {"base": float(cfg.slippage_base_bps)}
    if price <= 0:
        return SlippageEstimate(price, 0.0, components)

    components["volatility"] = k_slip * atr_bps
    components["k_slip"] = round(k_slip, 6)
    components["atr_stress"] = round(stress, 4)

    participation_pct = 0.0
    if quote_volume and quote_volume > 0 and notional > 0:
        participation_pct = notional / quote_volume * 100.0
    components["impact"] = cfg.impact_coeff * math.sqrt(max(0.0, participation_pct))

    # only the *cost* components add up: k_slip / atr_stress are informational, and
    # summing every dict value would silently add them to the bill (a real bug once)
    total = components["base"] + components["volatility"] + components["impact"]
    capped = total > cfg.max_slippage_bps
    total = min(total, cfg.max_slippage_bps)
    components["total"] = total
    components["participation_pct"] = participation_pct
    return SlippageEstimate(price * total / 10_000.0 + price, total, components, capped)


def risky_spread_cap(profile: dict[str, Any], atr_pct: float | None = None) -> float:
    """Extra spread allowance in stressed volatility (gate 1, check 4).

    ``2 * atr_pct * 100`` bps of ATR is roughly the spread a market maker needs to be
    compensated for one bar of risk; the cap is ``max_spread_bps`` times
    ``extreme_spread_mult`` (default 1.6) so normal-but-wide books still pass.
    """
    base = float(profile.get("max_spread_bps", 8.0))
    mult = float(profile.get("extreme_spread_mult", 1.6))
    return base * mult


def is_in_auction(now_local, profile: dict[str, Any], cfg: CostConfig) -> tuple[bool, str]:
    """IDX pre-opening / opening / closing auction windows (unreliable fills)."""
    windows = cfg.auction_windows or (profile.get("auction_windows") or [])
    if not windows:
        return False, ""
    minute = now_local.hour * 60 + now_local.minute
    for start, end in windows:
        sh, sm = (int(x) for x in str(start).split(":"))
        eh, em = (int(x) for x in str(end).split(":"))
        if sh * 60 + sm <= minute < eh * 60 + em:
            return True, f"{start}-{end} auction"
    return False, ""


# --------------------------------------------------------------------------- gap risk
@dataclass
class GapGuard:
    cooldown: int = 0
    reason: str = ""
    gap_atr: float = 0.0
    spread_mult: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def gap_guard(
    *,
    prev_close: float,
    open_price: float,
    atr: float,
    spread_bps: float,
    max_spread_bps: float,
    cfg: CostConfig,
) -> GapGuard:
    """Detect a gap/spread shock between two sessions.

    ``open`` beyond ``gap_reject_atr × ATR`` from the previous close, or a book wider than
    ``gap_reject_spread_mult × max_spread_bps``, blocks new entries for
    ``gap_cooldown_bars`` bars: after a shock the structure the stop relies on is gone.
    """
    atr = float(atr or 0.0)
    gap_atr = abs(float(open_price) - float(prev_close)) / atr if atr > 0 else 0.0
    spread_mult = (spread_bps / max_spread_bps) if max_spread_bps > 0 else 0.0
    reasons = []
    if gap_atr >= cfg.gap_reject_atr:
        reasons.append(f"open gapped {gap_atr:.2f} ATR from the previous close")
    if spread_mult >= cfg.gap_reject_spread_mult:
        reasons.append(f"spread {spread_bps:.1f} bps = {spread_mult:.2f}x the normal cap")
    if reasons:
        return GapGuard(cfg.gap_cooldown_bars, "; ".join(reasons), gap_atr, spread_mult)
    return GapGuard(0, "", gap_atr, spread_mult)


def gap_adjusted_stop(
    *,
    exit_price: float,
    next_open: float,
    side: str,
    cfg: CostConfig,
    atr: float = 0.0,
) -> float:
    """Worse-than-stop fill on a gap, interpolated between the stop and the open.

    A long whose stop sat at 100 and opens at 94 cannot fill at 100: the bracket is
    already through the market. ``gap_fill_fraction`` interpolates — 0 means "assume the
    stop price somehow held" (optimistic), 1 means "fill at the open" (worst case).
    """
    stop = float(exit_price)
    open_ = float(next_open)
    unfavourable = open_ < stop if side == "long" else open_ > stop
    if not unfavourable:
        return stop  # the gap went our way: the resting order fills at its own price
    fraction = max(0.0, min(1.0, float(cfg.gap_fill_fraction)))
    return stop + fraction * (open_ - stop)


# --------------------------------------------------------------------------- sizing
def risk_multiplier(
    *,
    spread_bps: float,
    max_spread_bps: float,
    atr_pct: float | None,
    profile: dict[str, Any],
    cfg: CostConfig,
) -> float:
    """Shrink the risk budget while the tape is stressed (wider book, fat ATR).

    A stop is more likely to be jumped when the book is thin, so the same 1 % of equity
    is spread over a wider distance instead of being taken at full size.
    """
    # a book at half the cap or wider is already "stressed": below that, trade normally
    mult = 1.0
    if max_spread_bps > 0 and spread_bps > 0:
        stress = max(0.0, min(1.0, (spread_bps / max_spread_bps - 0.5) * 2.0))
        mult += 0.5 * stress
    soft = float(profile.get("atr_soft_cap_pct", 0.0) or 0.0)
    if soft and atr_pct and atr_pct > soft:
        mult += 0.25 * min(1.0, (atr_pct - soft) / soft)
    return round(min(mult, float(cfg.max_risk_multiplier)), 4)


def quantiles(values: Iterable[float], points: Sequence[float]) -> dict[str, float]:
    ordered = sorted(v for v in values if v is not None and math.isfinite(v))
    if not ordered:
        return {f"p{int(p*100)}": 0.0 for p in points}
    out = {}
    for p in points:
        idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
        out[f"p{int(p*100)}"] = round(ordered[idx], 3)
    return out


def atr_series(bars: Sequence[dict[str, Any]], period: int = 14) -> list[float | None]:
    return ind.atr(
        [b["high"] for b in bars], [b["low"] for b in bars], [b["close"] for b in bars], period
    )
