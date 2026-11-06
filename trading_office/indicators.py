"""Pure-python indicators — no pandas required.

Every function takes plain lists (oldest first) and returns a list of the same length
with ``None`` where the indicator is not yet defined. This keeps the live engine, the
backtester and the tests on exactly the same maths, and means the whole strategy can be
verified without a data-frame library.

The live loop must slice ``bars[:-1]`` (closed candles only) — these helpers never look
ahead, but they cannot know where "now" is.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Iterable, Sequence

Number = float


# ------------------------------------------------------------------ moving averages
def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first ``period`` values (MetaTrader style)."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * alpha + prev * (1.0 - alpha)
        out[i] = prev
    return out


def rolling_std(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 1:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period  # population, like BBs
        out[i] = math.sqrt(var)
    return out


# ------------------------------------------------------------------ oscillators
def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI (the MetaTrader definition, smoothed not simple average)."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None for f, s in zip(ema_fast, ema_slow)
    ]
    # Signal EMA is computed over the defined part only.
    defined = [v for v in line if v is not None]
    sig_defined = ema(defined, signal)
    sig: list[float | None] = [None] * len(line)
    j = 0
    for i, v in enumerate(line):
        if v is None:
            continue
        if j < len(sig_defined):
            sig[i] = sig_defined[j]
        j += 1
    hist: list[float | None] = [
        (l - s) if (l is not None and s is not None) else None for l, s in zip(line, sig)
    ]
    return line, sig, hist


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> list[float | None]:
    """Wilder ATR over true range."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
    first = sum(trs[1 : period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def bollinger(
    values: Sequence[float], period: int = 20, mult: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = sma(values, period)
    sd = rolling_std(values, period)
    upper = [(m + mult * s) if (m is not None and s is not None) else None for m, s in zip(mid, sd)]
    lower = [(m - mult * s) if (m is not None and s is not None) else None for m, s in zip(mid, sd)]
    return upper, mid, lower


# ------------------------------------------------------------------ structure
def swing_lows(lows: Sequence[float], lookback: int = 10) -> list[float | None]:
    """Lowest low of the previous ``lookback`` bars (excluding the current bar)."""
    out: list[float | None] = [None] * len(lows)
    for i in range(len(lows)):
        if i < lookback:
            continue
        window = lows[i - lookback : i]
        out[i] = min(window)
    return out


def swing_highs(highs: Sequence[float], lookback: int = 10) -> list[float | None]:
    out: list[float | None] = [None] * len(highs)
    for i in range(len(highs)):
        if i < lookback:
            continue
        out[i] = max(highs[i - lookback : i])
    return out


def rolling_median(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i + 1 < period:
            continue
        out[i] = median(values[i - period + 1 : i + 1])
    return out


def percentile_rank(values: Iterable[float], value: float) -> float:
    """Share of ``values`` below ``value`` (0..100) — used for the ATR regime check."""
    ordered = sorted(values)
    if not ordered:
        return 50.0
    below = sum(1 for v in ordered if v < value)
    return 100.0 * below / len(ordered)


# ------------------------------------------------------------------ candles
def is_bullish_engulfing(
    prev_open: float, prev_close: float, open_: float, close: float
) -> bool:
    return prev_close < prev_open and close > open_ and open_ <= prev_close and close >= prev_open


def is_bearish_engulfing(
    prev_open: float, prev_close: float, open_: float, close: float
) -> bool:
    return prev_close > prev_open and close < open_ and open_ >= prev_close and close <= prev_open


def wick_ratios(open_: float, high: float, low: float, close: float) -> tuple[float, float, float]:
    body = abs(close - open_)
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    return body, upper, lower


def bullish_rejection(open_: float, high: float, low: float, close: float) -> bool:
    body, _upper, lower = wick_ratios(open_, high, low, close)
    return body > 0 and lower >= 2.0 * body


def bearish_rejection(open_: float, high: float, low: float, close: float) -> bool:
    body, upper, _lower = wick_ratios(open_, high, low, close)
    return body > 0 and upper >= 2.0 * body


# ------------------------------------------------------------------ helpers
def pct_change(new: float, old: float) -> float | None:
    if old == 0:
        return None
    return (new - old) / old * 100.0


def last_defined(values: Sequence[float | None], offset: int = 0) -> float | None:
    """Last non-None value, or look ``offset`` defined values back."""
    seen = 0
    for v in reversed(values):
        if v is None:
            continue
        if seen == offset:
            return v
        seen += 1
    return None
