"""One scan of one symbol through the four gates — shared by the API, CLI and screener.

The order is fixed and each gate is recorded, so "why didn't it trade?" always has an
answer:

1. build the closed-bar context (market filter inputs),
2. market filter → may I trade?
3. signal score → is there a setup?
4. risk → size, stop, target, cost sanity,
5. (optional) execute through the broker.

Live-only steps (bar locking, duplicate checks, cooldown, trade management of existing
positions) live in :mod:`trading_office.engine`, because they need persistent state.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .. import market_filter, risk, signals
from ..config import Settings, idx_tick_size


@dataclass
class ScanResult:
    symbol: str
    market: str
    as_of: str
    price: float | None
    action: str  # "LONG" | "SHORT" | "WAIT" | "BLOCKED"
    stage: str  # last gate reached: "filter" | "signal" | "risk" | "ready"
    bars_used: int
    bar_time: str
    filter: dict[str, Any] = field(default_factory=dict)
    signal: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = "synthetic"
    executable: bool = False
    score: int = 0
    indicators: dict[str, Any] = field(default_factory=dict)
    quote: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "as_of": self.as_of,
            "price": self.price,
            "action": self.action,
            "stage": self.stage,
            "bars_used": self.bars_used,
            "bar_time": self.bar_time,
            "score": self.score,
            "filter": self.filter,
            "signal": self.signal,
            "risk": self.risk,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "source": self.source,
            "executable": self.executable,
            "indicators": self.indicators,
            "quote": self.quote,
        }


def scan_symbol(
    *,
    symbol: str,
    market: str,
    bars: Sequence[dict[str, Any]],
    htf_bars: Sequence[dict[str, Any]],
    quote: dict[str, Any] | None = None,
    settings: Settings | None = None,
    equity: float | None = None,
    now: dt.datetime | None = None,
    stale: bool = False,
    data_age_s: float | None = None,
    is_trading_day: bool = True,
    source: str = "synthetic",
    warnings: Sequence[str] | None = None,
    daily_value: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    with_risk: bool = True,
) -> ScanResult:
    """Run the pipeline once for one symbol on already-fetched *closed* bars."""
    settings = settings or Settings.load()
    profile = settings.profile(market)
    filter_cfg = settings.merged("market_filter", "engine")
    signal_cfg = {**settings.section("signal"), "timeframe": profile.get("timeframe")}
    risk_cfg = settings.section("risk")
    equity = float(
        equity
        if equity is not None
        else settings.get("engine.equity_idr" if market == "idx" else "engine.equity_usdt", 1_000.0)
    )
    quote = quote or {}
    now = now or dt.datetime.now(ZoneInfo(profile.get("sessions_timezone", "Asia/Jakarta")))

    # closed candles only: drop a holding bar whose timestamp is still in the future
    bars = list(bars)
    if bars and _is_forming(bars[-1], profile.get("timeframe", "15m"), now):
        bars = bars[:-1]
    if len(bars) < 60:
        return ScanResult(
            symbol=symbol, market=market, as_of=now.isoformat(), price=None, action="BLOCKED",
            stage="filter", bars_used=len(bars), bar_time="",
            reasons=[f"not enough closed bars ({len(bars)})"],
            warnings=list(warnings or []), source=source,
        )

    bar_time = str(bars[-1].get("time") or "")
    price = float(quote.get("price") or bars[-1]["close"])

    demo_data = source == "synthetic"
    ctx = market_filter.build_context_from_bars(
        symbol, market, bars, htf_bars,
        now=now, profile=profile, config=filter_cfg,
        demo_data=demo_data,
        bid=bid if bid is not None else quote.get("bid"),
        ask=ask if ask is not None else quote.get("ask"),
        stale=stale, data_age_s=data_age_s, is_trading_day=is_trading_day,
    )
    ctx.price = price
    ctx.daily_value = daily_value if daily_value is not None else quote.get("value")
    filt = market_filter.evaluate(ctx, profile=profile, config=filter_cfg)

    result = ScanResult(
        symbol=symbol,
        market=market,
        as_of=now.isoformat(),
        price=price,
        action="WAIT",
        stage="filter",
        bars_used=len(bars),
        bar_time=bar_time,
        filter=filt.to_dict(),
        reasons=list(filt.reasons),
        warnings=list(warnings or []),
        source=source,
        quote=quote,
    )
    if not filt.passed:
        result.action = "BLOCKED"
        return result

    sig = signals.evaluate(symbol, bars, signal_cfg, bar_time=bar_time)
    result.stage = "signal"
    result.signal = sig.to_dict()
    result.score = sig.score
    result.indicators = {k: _round(v) for k, v in sig.indicators.items()}
    if sig.action == "WAIT":
        result.action = "WAIT"
        result.reasons = sig.reasons[:4]
        return result
    if market == "idx" and sig.action == "SHORT" and not _short_enabled(profile):
        result.action = "SHORT"
        result.reasons = list(sig.reasons_for("SHORT")) + [
            "short is not executable in the IDX cash market: treat as exit / avoid"
        ]
        return result
    if filt.direction and filt.direction != sig.action.lower():
        result.action = "WAIT"
        result.reasons = [f"signal {sig.action} against the higher-timeframe trend ({filt.direction})"]
        return result

    spread_bps = market_filter.spread_bps(ctx)[0]
    tick = ctx.tick_size
    if market == "idx" and tick is None:
        tick = idx_tick_size(price)
    decision = risk.build_plan(
        symbol=symbol,
        market=market,
        side=sig.action.lower(),
        entry=price,
        atr=float(sig.atr or ctx.atr or 0.0),
        bars=bars,
        equity=equity,
        config=signal_cfg,
        risk_cfg=risk_cfg,
        profile=profile,
        spread_bps=spread_bps,
        tick_size=tick,
        quote_volume=ctx.quote_volume,
    )
    result.stage = "risk"
    result.risk = decision.to_dict()
    result.reasons = sig.reasons_for(sig.action)
    result.warnings.extend(decision.warnings)
    if decision.approved and decision.plan and decision.plan.size > 0:
        result.stage = "ready"
        result.action = sig.action
        result.executable = not (market == "idx" and sig.action == "SHORT")
    else:
        result.action = "WAIT"
        result.reasons = list(decision.rejections) or result.reasons
    return result


def _short_enabled(profile: dict[str, Any]) -> bool:
    """IDX cash equities cannot be shorted by a retail account; crypto perps can."""
    return bool(profile.get("short_enabled", profile.get("name") == "crypto"))


def _is_forming(bar: dict[str, Any], timeframe: str, now: dt.datetime) -> bool:
    """True when the last bar has not closed yet (its close time is in the future)."""
    raw = bar.get("time")
    try:
        stamp = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        return False
    minutes = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60,
        "4h": 240, "1d": 24 * 60, "1wk": 7 * 24 * 60,
    }.get(timeframe, 15)
    return (stamp + dt.timedelta(minutes=minutes)) > now


def _round(value: Any, digits: int = 4) -> Any:
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value
