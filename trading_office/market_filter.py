"""Gate 1 — Market Filter ("may I trade this symbol at all?").

Four independent filters, each returning a :class:`FilterCheck` with the raw numbers so
the UI can explain *why* a symbol is blocked:

1. ``trend``   — higher-timeframe EMA direction, ATR-relative tolerance.
2. ``volatility`` — ATR% absolute band + ATR regime band (vs its own median).
3. ``session`` — local-time trading windows, weekday guard, news blackout, data freshness.
4. ``spread``  — spread in bps, tick sanity, liquidity (traded value / quote volume).

``min_filters_passed`` decides the tally; the trend filter is mandatory when
``require_trend_filter`` is true, because it is the directional bias.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from . import indicators as ind

FILTER_NAMES = ("trend", "volatility", "session", "spread")


@dataclass
class FilterCheck:
    name: str
    passed: bool
    value: float | None = None
    limit: float | None = None
    detail: str = ""
    estimated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketContext:
    """Everything the filters need, assembled by the caller (live engine or backtest)."""

    symbol: str
    market: str  # "crypto" | "idx"
    timeframe: str
    now: dt.datetime
    price: float
    atr: float
    atr_history: Sequence[float] = field(default_factory=list)
    # higher timeframe (closed bars only)
    htf_close: float | None = None
    htf_ema: float | None = None
    htf_atr: float | None = None
    # spread / liquidity
    bid: float | None = None
    ask: float | None = None
    tick_size: float | None = None
    quote_volume: float | None = None  # median volume over the last N bars (quote ccy)
    daily_value: float | None = None  # IDX: traded value of the last session, IDR
    data_age_s: float | None = None
    stale: bool = False
    demo_data: bool = False  # synthetic series: allowed to trade, but always labelled
    # misc
    is_trading_day: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FilterResult:
    passed: bool
    direction: str | None  # "long" | "short" | None when the bias is flat
    checks: list[FilterCheck]
    passed_count: int
    required: int
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "direction": self.direction,
            "passed_count": self.passed_count,
            "required": self.required,
            "checks": [c.to_dict() for c in self.checks],
            "reasons": list(self.reasons),
        }

    def check(self, name: str) -> FilterCheck | None:
        for c in self.checks:
            if c.name == name:
                return c
        return None


# --------------------------------------------------------------------------- helpers
def spread_bps(ctx: MarketContext) -> tuple[float, bool]:
    """Return (spread in bps, estimated?). Uses the book, else 2 ticks, else slippage."""
    if ctx.bid and ctx.ask and ctx.price > 0 and ctx.ask > ctx.bid:
        return (ctx.ask - ctx.bid) / ctx.price * 10_000.0, False
    tick = ctx.tick_size
    if tick and ctx.price > 0:
        # one tick is the typical quoted spread for a liquid IDX name; two is the
        # pessimistic case for a thin one, so the estimate is one tick plus slippage
        # folded in by the caller. Marked estimated so the UI can say so.
        return 1.0 * tick / ctx.price * 10_000.0, True
    est = float(ctx.extra.get("fallback_spread_bps", 5.0) or 5.0)
    return est, True


def in_sessions(now_local: dt.datetime, sessions: Iterable[Sequence[str]]) -> tuple[bool, str]:
    """Check minute-of-day membership in ``[["09:15","11:30"], ...]`` windows."""
    minute = now_local.hour * 60 + now_local.minute
    for window in sessions:
        start, end = window[0], window[1]
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        start_m, end_m = sh * 60 + sm, eh * 60 + em
        if start_m <= minute < end_m:
            return True, f"{now_local:%H:%M} inside {start}-{end}"
    pretty = ", ".join(f"{w[0]}-{w[1]}" for w in sessions)
    return False, f"{now_local:%H:%M} outside {pretty or 'no window'}"


def in_blackout(now: dt.datetime, blackouts: Iterable[dict[str, Any]]) -> str | None:
    """News blackout check. A blackout entry is either:

    ``{"at": "2026-11-07T19:30:00+07:00", "minutes": 30, "label": "NFP"}`` or
    ``{"weekday": 4, "time": "20:30", "minutes": 45, "label": "US payrolls",
    "tz": "Asia/Jakarta"}``.
    """
    for entry in blackouts or []:
        minutes = int(entry.get("minutes", 30))
        label = entry.get("label", "news")
        if entry.get("at"):
            try:
                at = dt.datetime.fromisoformat(str(entry["at"]))
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=now.tzinfo)
            if abs((now - at).total_seconds()) <= minutes * 60:
                return f"{label} at {at:%Y-%m-%d %H:%M}"
        elif entry.get("time"):
            tz = ZoneInfo(entry.get("tz", "Asia/Jakarta"))
            local = now.astimezone(tz)
            if entry.get("weekday") is not None and int(entry["weekday"]) != local.weekday():
                continue
            hh, mm = (int(x) for x in str(entry["time"]).split(":"))
            target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if abs((local - target).total_seconds()) <= minutes * 60:
                return f"{label} at {target:%Y-%m-%d %H:%M} {tz.key}"
    return None


# --------------------------------------------------------------------------- the gate
def evaluate(
    ctx: MarketContext,
    *,
    profile: dict[str, Any],
    config: dict[str, Any],
) -> FilterResult:
    """Run the four market filters for one symbol.

    ``config`` is the merged ``engine`` + ``market_filter`` sections, ``profile`` the
    market profile (``profiles.crypto`` / ``profiles.idx``).
    """
    checks: list[FilterCheck] = []
    reasons: list[str] = []

    # ---------------------------------------------------------------- 1. trend
    tol_atr = float(config.get("trend_tolerance_atr", 0.25))
    direction: str | None = None
    if ctx.htf_ema is None or ctx.htf_close is None:
        checks.append(
            FilterCheck("trend", False, detail="higher-timeframe EMA unavailable (not enough closed bars)")
        )
        reasons.append("trend: no HTF history")
    else:
        tol = tol_atr * (ctx.htf_atr or 0.0)
        dist = ctx.htf_close - ctx.htf_ema
        if dist > tol:
            direction = "long"
        elif dist < -tol:
            direction = "short"
        passed = direction is not None
        checks.append(
            FilterCheck(
                "trend",
                passed,
                value=round(dist / ctx.htf_close * 100.0, 3) if ctx.htf_close else None,
                limit=round(tol_atr * 100.0, 2),
                detail=(
                    f"HTF close {ctx.htf_close:.4f} vs EMA {ctx.htf_ema:.4f} "
                    f"({dist:+.4f}, tol {tol:.4f}) -> {direction or 'flat'}"
                ),
            )
        )
        if not passed:
            reasons.append("trend: higher timeframe is flat")

    # ---------------------------------------------------------------- 2. volatility
    atr_pct = (ctx.atr / ctx.price * 100.0) if ctx.price else None
    atr_min = float(profile.get("atr_min_pct", 0.25))
    atr_max = float(profile.get("atr_max_pct", 6.0))
    vol_ok = atr_pct is not None and atr_min <= atr_pct <= atr_max
    vol_detail = f"ATR {atr_pct:.3f}% (band {atr_min}-{atr_max}%)" if atr_pct is not None else "ATR unavailable"
    if vol_ok:
        regime_lo, regime_hi = (float(x) for x in config.get("atr_pct_band_mult", [0.6, 2.5]))
        hist = [v / ctx.price * 100.0 for v in ctx.atr_history if v]
        if len(hist) >= 20:
            med = sorted(hist)[len(hist) // 2]
            ratio = atr_pct / med if med else 1.0
            vol_detail += f", {ratio:.2f}x its 100-bar median"
            if not (regime_lo <= ratio <= regime_hi):
                vol_ok = False
                vol_detail += " -> regime out of band"
    checks.append(FilterCheck("volatility", vol_ok, value=_round(atr_pct), limit=atr_max, detail=vol_detail))
    if not vol_ok:
        reasons.append("volatility: " + vol_detail)

    # ---------------------------------------------------------------- 3. session
    tz = ZoneInfo(profile.get("sessions_timezone", config.get("timezone", "Asia/Jakarta")))
    now_local = ctx.now.astimezone(tz)
    daily = str(profile.get("timeframe", "")) in ("1d", "1wk", "1mo")
    if daily:
        # On daily/weekly setups the bar *is* the session: only the weekday/holiday
        # guard applies, the intraday windows would reject every daily bar.
        sess_ok, sess_detail = True, f"{ctx.timeframe} bar: session windows not applicable"
    else:
        sess_ok, sess_detail = in_sessions(now_local, profile.get("sessions") or [])
        if not profile.get("sessions"):
            sess_ok = True  # no window configured = 24/7
    parts = [sess_detail]
    if ctx.market == "idx" and not ctx.is_trading_day:
        sess_ok = False
        parts.append("not an IDX trading day")
    if ctx.market == "crypto" and config.get("block_weekend_crypto") and now_local.weekday() >= 5:
        sess_ok = False
        parts.append("weekend entries disabled")
    blackout = in_blackout(now_local, config.get("news_blackout") or [])
    if blackout:
        sess_ok = False
        parts.append(f"news blackout: {blackout}")
    max_age = float(profile.get("max_tick_age_s", 300))
    if ctx.stale:
        sess_ok = False
        parts.append(f"live data stale ({ctx.data_age_s if ctx.data_age_s is not None else '?'}s)")
    elif ctx.demo_data:
        parts.append("synthetic demo series (no live provider)")
    elif ctx.data_age_s is not None and ctx.data_age_s > max_age:
        sess_ok = False
        parts.append(f"data older than {max_age:.0f}s ({ctx.data_age_s:.0f}s)")
    checks.append(
        FilterCheck(
            "session",
            sess_ok,
            value=round(ctx.data_age_s, 1) if ctx.data_age_s is not None else None,
            limit=max_age,
            detail="; ".join(parts),
        )
    )
    if not sess_ok:
        reasons.append("session: " + "; ".join(parts))

    # ---------------------------------------------------------------- 4. spread / liquidity
    bps, estimated = spread_bps(ctx)
    max_bps = float(profile.get("max_spread_bps", 8.0))
    spread_ok = bps <= max_bps
    sp_parts = [f"spread {bps:.2f} bps{' (estimated)' if estimated else ''} <= {max_bps}"]
    if not spread_ok:
        sp_parts.append("spread too wide")
    liq_ok = True
    if ctx.market == "idx":
        min_value = float(profile.get("min_daily_value_idr", 5_000_000_000))
        if ctx.daily_value is not None:
            liq_ok = ctx.daily_value >= min_value
            sp_parts.append(f"daily value {ctx.daily_value/1e9:.2f} B IDR >= {min_value/1e9:.0f} B")
        if ctx.tick_size and ctx.atr:
            # the stop must be able to sit outside the noise in whole ticks
            ticks = ctx.atr * 1.5 / ctx.tick_size
            if ticks < 3:
                spread_ok = False
                sp_parts.append(f"ATR only {ticks:.1f} ticks wide")
    else:
        if ctx.quote_volume and ctx.quote_volume > 0:
            participation = float(config.get("max_participation_pct", 0.5)) / 100.0
            liq_ok = ctx.quote_volume * participation > 0
            sp_parts.append(f"median bar volume {ctx.quote_volume:,.0f}")
    checks.append(
        FilterCheck(
            "spread",
            bool(spread_ok and liq_ok),
            value=_round(bps, 2),
            limit=max_bps,
            detail="; ".join(sp_parts),
            estimated=estimated,
        )
    )
    if not (spread_ok and liq_ok):
        reasons.append("spread: " + "; ".join(sp_parts))

    # ---------------------------------------------------------------- tally
    passed_count = sum(1 for c in checks if c.passed)
    required = int(config.get("min_filters_passed", 3))
    mandatory_ok = direction is not None or not config.get("require_trend_filter", True)
    overall = passed_count >= required and mandatory_ok
    if overall and direction is None:
        reasons.append("trend filter mandatory but flat")
    return FilterResult(
        passed=overall,
        direction=direction,
        checks=checks,
        passed_count=passed_count,
        required=required,
        reasons=reasons,
    )


def build_context_from_bars(
    symbol: str,
    market: str,
    bars: Sequence[dict[str, Any]],
    htf_bars: Sequence[dict[str, Any]],
    *,
    now: dt.datetime,
    profile: dict[str, Any],
    config: dict[str, Any],
    bid: float | None = None,
    ask: float | None = None,
    stale: bool = False,
    data_age_s: float | None = None,
    is_trading_day: bool = True,
    demo_data: bool = False,
) -> MarketContext:
    """Assemble a :class:`MarketContext` from closed candles only.

    ``bars`` are the trading-timeframe candles with the live bar already removed;
    ``htf_bars`` are the higher-timeframe closed candles.
    """
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    atr_period = int(config.get("atr_period", 14))
    atr_series = ind.atr(highs, lows, closes, atr_period)
    atr_now = ind.last_defined(atr_series) or 0.0

    htf_ema_period = int(config.get("trend_ema", 200))
    htf_closes = [b["close"] for b in htf_bars]
    htf_ema_series = ind.ema(htf_closes, htf_ema_period)
    htf_ema = ind.last_defined(htf_ema_series)
    if htf_ema is None and len(htf_closes) >= 50:
        # not enough weekly bars for a 200 EMA (new listings): fall back to 50 and say so
        htf_ema = ind.last_defined(ind.ema(htf_closes, 50))
    htf_atr = ind.last_defined(
        ind.atr([b["high"] for b in htf_bars], [b["low"] for b in htf_bars], htf_closes, atr_period)
    )

    tick = None
    if profile.get("tick_table") and market == "idx":
        from .config import idx_tick_size

        tick = idx_tick_size(closes[-1])
    quote_vol = None
    if bars:
        recent = [b.get("volume", 0) * b["close"] for b in bars[-20:]]
        recent = [v for v in recent if v]
        if recent:
            quote_vol = sorted(recent)[len(recent) // 2]

    return MarketContext(
        symbol=symbol,
        market=market,
        timeframe=str(profile.get("timeframe", "15m")),
        now=now,
        price=closes[-1],
        atr=atr_now,
        atr_history=[v for v in atr_series[-100:] if v],
        htf_close=htf_closes[-1] if htf_closes else None,
        htf_ema=htf_ema,
        htf_atr=htf_atr,
        bid=bid,
        ask=ask,
        tick_size=tick,
        quote_volume=quote_vol,
        data_age_s=data_age_s,
        stale=stale,
        demo_data=demo_data,
        is_trading_day=is_trading_day,
        extra={"fallback_spread_bps": profile.get("slippage_bps", 5.0)},
    )


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)
