"""Gate 2 — Signal Generation ("is there a setup on the last closed bar?").

Confluence scoring over 9 points (see docs/STRATEGY_LOGIC.md §2):

=====================================  =======  =======
component                              weight   kind
=====================================  =======  =======
trend alignment (EMA50 vs EMA200)          2     context
trigger (RSI cross out of extreme | BB   2     trigger
  re-entry)
MACD confirmation (line vs signal,
  histogram rising/falling)                 1     confirm
momentum health (RSI in the healthy zone)  1     context
Bollinger mid reclaim / lose               1     trigger
candle structure (engulfing / rejection)   1     confirm
volume expansion                          1     confirm
=====================================  =======  =======

An entry needs ``score >= min_score`` **and** at least one ``trigger`` **and** at least
one ``confirm`` component: pure trend-following never fires an order. Overextension and
two-sided conflict vetoes are applied afterwards.

Every function here works on *closed* candles. The caller is responsible for slicing the
forming bar off — :func:`evaluate` asserts it by never reading beyond the last element.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from . import indicators as ind


@dataclass
class Component:
    name: str
    kind: str  # "context" | "trigger" | "confirm"
    weight: int
    long: bool
    short: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SignalResult:
    symbol: str
    timeframe: str
    bar_time: str  # ISO timestamp of the closed bar the decision belongs to
    action: str  # "LONG" | "SHORT" | "WAIT"
    long_score: int
    short_score: int
    max_score: int
    components: list[Component] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)
    indicators: dict[str, float | None] = field(default_factory=dict)
    atr: float | None = None
    price: float | None = None
    block_code: str = ""  # machine-readable WAIT reason, for the funnel counters

    @property
    def score(self) -> int:
        return max(self.long_score, self.short_score)

    def reasons_for(self, action: str) -> list[str]:
        want_long = action.upper() == "LONG"
        out = []
        for c in self.components:
            if (c.long if want_long else c.short) and c.weight > 0:
                out.append(f"{c.name} +{c.weight}" + (f" ({c.detail})" if c.detail else ""))
        return out

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["score"] = self.score
        data["reasons_long"] = self.reasons_for("LONG")
        data["reasons_short"] = self.reasons_for("SHORT")
        return data


def _fmt(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _bar_time(bar: dict[str, Any]) -> str:
    value = bar.get("time") or bar.get("date") or bar.get("timestamp")
    return str(value or "")


# --------------------------------------------------------------------------- evaluation
def evaluate(
    symbol: str,
    bars: Sequence[dict[str, Any]],
    config: dict[str, Any],
    *,
    bar_time: str | None = None,
) -> SignalResult:
    """Score the latest closed bar in ``bars``.

    ``bars`` must already exclude the forming candle. At least ``ema_slow`` closed bars
    are required for a real reading; with fewer the result is ``WAIT`` with a reason.
    """
    n = len(bars)
    needed = int(config.get("macd_slow", 26)) + int(config.get("macd_signal", 9)) + 5
    result = SignalResult(
        symbol=symbol,
        timeframe=str(config.get("timeframe", "")),
        bar_time=bar_time or (_bar_time(bars[-1]) if n else ""),
        action="WAIT",
        long_score=0,
        short_score=0,
        max_score=9,
    )
    if n < max(needed, 60):
        result.reasons.append(f"not enough closed bars ({n}/{max(needed, 60)})")
        return result

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    opens = [b["open"] for b in bars]
    volumes = [float(b.get("volume") or 0.0) for b in bars]

    fast = int(config.get("ema_fast", 50))
    slow = int(config.get("ema_slow", 200))
    ema_fast = ind.ema(closes, fast)
    ema_slow = ind.ema(closes, slow)
    rsi_p = int(config.get("rsi_period", 14))
    rsi_series = ind.rsi(closes, rsi_p)
    macd_line, macd_sig, macd_hist = ind.macd(
        closes,
        int(config.get("macd_fast", 12)),
        int(config.get("macd_slow", 26)),
        int(config.get("macd_signal", 9)),
    )
    bb_period = int(config.get("bb_period", 20))
    bb_up, bb_mid, bb_low = ind.bollinger(closes, bb_period, float(config.get("bb_std", 2.0)))
    atr_series = ind.atr(highs, lows, closes, int(config.get("atr_period", 14)))
    vol_ma = ind.sma(volumes, int(config.get("volume_ma", 20)))

    i = n - 1  # last closed bar
    p = n - 2  # bar before it
    c, prev = closes[i], closes[p]
    atr_now = atr_series[i]

    result.price = c
    result.atr = atr_now
    result.indicators = {
        "close": c,
        "ema50": ema_fast[i],
        "ema200": ema_slow[i],
        "rsi14": rsi_series[i],
        "macd": macd_line[i],
        "macd_signal": macd_sig[i],
        "macd_hist": macd_hist[i],
        "bb_upper": bb_up[i],
        "bb_mid": bb_mid[i],
        "bb_lower": bb_low[i],
        "atr": atr_now,
        "volume": volumes[i],
        "volume_ma20": vol_ma[i],
        "prev_close": prev,
    }

    comps: list[Component] = []
    long_score = 0
    short_score = 0

    # ------------------------------------------------------------ 1. trend alignment (2)
    trend_long = (
        ema_fast[i] is not None
        and ema_slow[i] is not None
        and c > ema_fast[i] > ema_slow[i]
    )
    trend_short = (
        ema_fast[i] is not None
        and ema_slow[i] is not None
        and c < ema_fast[i] < ema_slow[i]
    )
    comps.append(
        Component(
            "trend_alignment",
            "context",
            2,
            trend_long,
            trend_short,
            f"close {c:.4f} / ema{fast} {_fmt(ema_fast[i])} / ema{slow} {_fmt(ema_slow[i])}",
        )
    )
    long_score += 2 if trend_long else 0
    short_score += 2 if trend_short else 0

    # ------------------------------------------------------------ 2. trigger (2)
    rsi_trigger = float(config.get("rsi_trigger", 35))
    trigger_long_rsi = (
        rsi_series[i] is not None
        and rsi_series[p] is not None
        and rsi_series[p] < rsi_trigger <= rsi_series[i]
    )
    trigger_short_rsi = (
        rsi_series[i] is not None
        and rsi_series[p] is not None
        and rsi_series[p] > 100 - rsi_trigger >= rsi_series[i]
    )
    trigger_long_bb = (
        bb_low[p] is not None
        and bb_low[i] is not None
        and closes[p] < bb_low[p]
        and c > bb_low[i]
        and c > opens[i]
    )
    trigger_short_bb = (
        bb_up[p] is not None
        and bb_up[i] is not None
        and closes[p] > bb_up[p]
        and c < bb_up[i]
        and c < opens[i]
    )
    trigger_long = trigger_long_rsi or trigger_long_bb
    trigger_short = trigger_short_rsi or trigger_short_bb
    trig_detail = []
    if trigger_long_rsi or trigger_short_rsi:
        trig_detail.append(f"RSI {_fmt(rsi_series[p], 1)} -> {_fmt(rsi_series[i], 1)} crossed {rsi_trigger:g}/{100-rsi_trigger:g}")
    if trigger_long_bb or trigger_short_bb:
        trig_detail.append("bar re-entered the Bollinger band")
    comps.append(Component("trigger", "trigger", 2, trigger_long, trigger_short, "; ".join(trig_detail)))
    long_score += 2 if trigger_long else 0
    short_score += 2 if trigger_short else 0

    # ------------------------------------------------------------ 3. MACD confirmation (1)
    macd_long = (
        macd_line[i] is not None
        and macd_sig[i] is not None
        and macd_hist[i] is not None
        and macd_hist[p] is not None
        and macd_line[i] > macd_sig[i]
        and macd_hist[i] > macd_hist[p]
    )
    macd_short = (
        macd_line[i] is not None
        and macd_sig[i] is not None
        and macd_hist[i] is not None
        and macd_hist[p] is not None
        and macd_line[i] < macd_sig[i]
        and macd_hist[i] < macd_hist[p]
    )
    comps.append(
        Component(
            "macd_confirmation",
            "confirm",
            1,
            macd_long,
            macd_short,
            f"hist {_fmt(macd_hist[p], 5)} -> {_fmt(macd_hist[i], 5)}",
        )
    )
    long_score += 1 if macd_long else 0
    short_score += 1 if macd_short else 0

    # ------------------------------------------------------------ 4. momentum health (1)
    ob = float(config.get("rsi_overbought", 75))
    os_ = float(config.get("rsi_oversold", 25))
    healthy_long = rsi_series[i] is not None and 50 < rsi_series[i] < ob
    healthy_short = rsi_series[i] is not None and os_ < rsi_series[i] < 50
    comps.append(
        Component(
            "momentum_health",
            "context",
            1,
            healthy_long,
            healthy_short,
            f"RSI {_fmt(rsi_series[i], 1)}",
        )
    )
    long_score += 1 if healthy_long else 0
    short_score += 1 if healthy_short else 0

    # ------------------------------------------------------------ 5. BB mid reclaim (1)
    mid_long = bb_mid[i] is not None and bb_mid[p] is not None and c > bb_mid[i] and closes[p] <= bb_mid[p]
    mid_short = bb_mid[i] is not None and bb_mid[p] is not None and c < bb_mid[i] and closes[p] >= bb_mid[p]
    comps.append(Component("bb_mid_reclaim", "trigger", 1, mid_long, mid_short, "close crossed the 20-SMA"))
    long_score += 1 if mid_long else 0
    short_score += 1 if mid_short else 0

    # ------------------------------------------------------------ 6. candle structure (1)
    bull_engulf = ind.is_bullish_engulfing(opens[p], closes[p], opens[i], c)
    bear_engulf = ind.is_bearish_engulfing(opens[p], closes[p], opens[i], c)
    bull_rej = ind.bullish_rejection(opens[i], highs[i], lows[i], c)
    bear_rej = ind.bearish_rejection(opens[i], highs[i], lows[i], c)
    comps.append(
        Component(
            "candle_structure",
            "confirm",
            1,
            bull_engulf or bull_rej,
            bear_engulf or bear_rej,
            "engulfing" if (bull_engulf or bear_engulf) else ("rejection wick" if (bull_rej or bear_rej) else ""),
        )
    )
    long_score += 1 if (bull_engulf or bull_rej) else 0
    short_score += 1 if (bear_engulf or bear_rej) else 0

    # ------------------------------------------------------------ 7. volume expansion (1)
    vol_mult = float(config.get("volume_mult", 1.2))
    vol_ok = vol_ma[i] is not None and vol_ma[i] > 0 and volumes[i] > vol_mult * vol_ma[i]
    volume_with_direction = vol_ok and (c > opens[i] or c < opens[i])
    comps.append(
        Component(
            "volume_expansion",
            "confirm",
            1,
            bool(volume_with_direction and c > opens[i]),
            bool(volume_with_direction and c < opens[i]),
            f"volume {volumes[i]:,.0f} vs MA {_fmt(vol_ma[i], 0)}" if vol_ma[i] else "volume MA unavailable",
        )
    )
    long_score += 1 if (volume_with_direction and c > opens[i]) else 0
    short_score += 1 if (volume_with_direction and c < opens[i]) else 0

    result.components = comps
    result.long_score = long_score
    result.short_score = short_score

    # ------------------------------------------------------------ vetoes
    vetoes: list[str] = []
    if rsi_series[i] is not None and rsi_series[i] > ob and bb_up[i] is not None and atr_now:
        if c > bb_up[i] + 0.5 * atr_now:
            vetoes.append("long blocked: price above upper band by > 0.5 ATR (overextended)")
    if rsi_series[i] is not None and rsi_series[i] < os_ and bb_low[i] is not None and atr_now:
        if c < bb_low[i] - 0.5 * atr_now:
            vetoes.append("short blocked: price below lower band by > 0.5 ATR (overextended)")

    min_score = int(config.get("min_score", 5))
    if long_score >= min_score - 1 and short_score >= min_score - 1:
        vetoes.append(f"conflict: long {long_score} vs short {short_score} (indecision)")

    has_trigger_l = any(c_.kind == "trigger" and c_.long for c_ in comps)
    has_confirm_l = any(c_.kind == "confirm" and c_.long for c_ in comps)
    has_trigger_s = any(c_.kind == "trigger" and c_.short for c_ in comps)
    has_confirm_s = any(c_.kind == "confirm" and c_.short for c_ in comps)

    action = "WAIT"
    if long_score >= min_score and long_score > short_score and has_trigger_l and has_confirm_l:
        if not any("long blocked" in v for v in vetoes):
            action = "LONG"
    if short_score >= min_score and short_score > long_score and has_trigger_s and has_confirm_s:
        if not any("short blocked" in v for v in vetoes):
            action = "SHORT"
    if any(v.startswith("conflict") for v in vetoes):
        action = "WAIT"

    result.action = action
    result.vetoes = vetoes
    if action == "WAIT":
        best = max(long_score, short_score)
        missing = []
        if any(v.startswith("conflict") for v in vetoes):
            result.block_code = "conflict"
        elif any("blocked" in v for v in vetoes):
            result.block_code = "overextended"
        elif best < min_score:
            result.block_code = "score_below_min"
            missing.append(f"score {best}/{min_score}")
        elif not (has_trigger_l or has_trigger_s):
            result.block_code = "no_trigger"
            missing.append("no trigger")
        elif not (has_confirm_l or has_confirm_s):
            result.block_code = "no_confirmation"
            missing.append("no confirmation")
        else:
            result.block_code = "other"
        result.reasons.extend(missing + vetoes)
    else:
        result.reasons.extend(result.reasons_for(action))
    return result
