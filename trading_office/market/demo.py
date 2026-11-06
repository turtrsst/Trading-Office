"""Deterministic synthetic market data.

Why this exists: the terminal, the web UI and the tests must work with **no network and
no API keys**, and a demo that changes on every restart cannot be debugged. Every series
here is generated from a seed derived from the symbol, so the same symbol always yields
the same candles, and the numbers look plausible (price scale, tick size, ATR%, volume,
trend regimes, occasional gaps).

Every consumer labels the provider as ``synthetic`` so a demo reading can never be
mistaken for a live quote.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
from typing import Any

try:  # zoneinfo is stdlib from 3.9
    from zoneinfo import ZoneInfo

    WIB = ZoneInfo("Asia/Jakarta")
except Exception:  # pragma: no cover
    WIB = dt.timezone(dt.timedelta(hours=7))


# Reference price scales so a synthetic chart does not look silly next to the real ticker.
IDX_REFERENCE: dict[str, tuple[str, float, float]] = {
    # symbol: (name, price, daily volatility %)
    "BBCA": ("Bank Central Asia", 8_250.0, 1.1),
    "BBRI": ("Bank Rakyat Indonesia", 4_180.0, 1.3),
    "BMRI": ("Bank Mandiri", 5_650.0, 1.3),
    "BBNI": ("Bank Negara Indonesia", 4_950.0, 1.4),
    "TLKM": ("Telkom Indonesia", 2_780.0, 1.2),
    "ASII": ("Astra International", 4_820.0, 1.2),
    "ADRO": ("Alamtri Resources", 2_450.0, 1.9),
    "ANTM": ("Aneka Tambang", 1_585.0, 2.0),
    "MDKA": ("Merdeka Copper Gold", 2_310.0, 2.4),
    "GOTO": ("GoTo Gojek Tokopedia", 62.0, 3.0),
    "ICBP": ("Indofood CBP", 11_250.0, 1.1),
    "UNVR": ("Unilever Indonesia", 1_890.0, 1.6),
    "KLBF": ("Kalbe Farma", 1_465.0, 1.3),
    "PGAS": ("Perusahaan Gas Negara", 1_620.0, 1.5),
    "SMGR": ("Semen Indonesia", 3_100.0, 1.8),
    "INCO": ("Vale Indonesia", 3_750.0, 2.1),
    "TPIA": ("Chandra Asri Pacific", 7_450.0, 2.0),
    "BRPT": ("Barito Pacific", 1_105.0, 2.3),
    "CPIN": ("Charoen Pokphand Indonesia", 4_760.0, 1.5),
    "AMRT": ("Sumber Alfaria Trijaya", 2_870.0, 1.2),
}

CRYPTO_REFERENCE: dict[str, tuple[str, float, float]] = {
    # symbol: (base, price, 15m volatility %)
    "BTC/USDT:USDT": ("BTC", 96_400.0, 0.22),
    "ETH/USDT:USDT": ("ETH", 3_240.0, 0.28),
    "SOL/USDT:USDT": ("SOL", 198.0, 0.42),
    "XRP/USDT:USDT": ("XRP", 2.31, 0.46),
    "BNB/USDT:USDT": ("BNB", 682.0, 0.30),
    "DOGE/USDT:USDT": ("DOGE", 0.372, 0.55),
    "ADA/USDT:USDT": ("ADA", 0.918, 0.50),
}


def _seed(symbol: str) -> int:
    digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _grid(now: dt.datetime, timeframe: str, count: int, *, idx_shape: bool = False) -> list[dt.datetime]:
    """Timestamps of the last ``count`` closed bars, oldest first."""
    step = {
        "1m": dt.timedelta(minutes=1),
        "3m": dt.timedelta(minutes=3),
        "5m": dt.timedelta(minutes=5),
        "15m": dt.timedelta(minutes=15),
        "30m": dt.timedelta(minutes=30),
        "1h": dt.timedelta(hours=1),
        "4h": dt.timedelta(hours=4),
        "1d": dt.timedelta(days=1),
        "1wk": dt.timedelta(days=7),
    }.get(timeframe, dt.timedelta(minutes=15))

    local = now.astimezone(WIB)
    # snap to the grid so the bar times look like a real feed
    minutes = step.total_seconds() / 60
    epoch = int(local.timestamp() // (minutes * 60)) * int(minutes * 60)
    cursor = dt.datetime.fromtimestamp(epoch, WIB)

    out: list[dt.datetime] = []
    while len(out) < count:
        if idx_shape and not _is_session_bar(cursor, timeframe):
            cursor -= step
            continue
        out.append(cursor)
        cursor -= step
    return list(reversed(out))


def _is_session_bar(ts: dt.datetime, timeframe: str) -> bool:
    """Rough IDX session shape for intraday bars; daily bars only on weekdays."""
    if timeframe in ("1d", "1wk"):
        return ts.weekday() < 5
    if timeframe in ("1m", "5m", "15m", "30m", "1h"):
        # intraday: keep a plausible session window, crypto callers ignore this
        return ts.weekday() < 5 and (9 <= ts.hour < 16)
    return True


def _walk(
    symbol: str,
    *,
    count: int,
    price: float,
    vol_pct: float,
    timeframe: str,
    now: dt.datetime,
    drift: float | None = None,
    idx_shape: bool = False,
) -> list[dict[str, Any]]:
    rng = random.Random(_seed(symbol) + count)
    times = _grid(now, timeframe, count, idx_shape=idx_shape)

    # a few regimes so trend filters and MA crossovers actually have something to work on
    regime_len = max(20, count // 9)
    regimes: list[float] = []
    for _ in range(count // regime_len + 2):
        regimes.append(rng.gauss(drift if drift is not None else 0.00035, 0.0011))

    bars: list[dict[str, Any]] = []
    current = price
    for i, ts in enumerate(times):
        regime = regimes[min(i // regime_len, len(regimes) - 1)]
        shock = rng.gauss(0, vol_pct / 100.0)
        # occasional news spike / quiet stretch
        if rng.random() < 0.012:
            shock *= rng.uniform(2.5, 4.5)
        elif rng.random() < 0.04:
            shock *= 0.25
        open_ = current
        close = open_ * (1 + regime + shock)
        span = abs(shock) + vol_pct / 100.0 * rng.uniform(0.4, 1.1)
        high = max(open_, close) * (1 + span * rng.uniform(0.15, 0.7))
        low = min(open_, close) * (1 - span * rng.uniform(0.15, 0.7))
        base_volume = (1_000_000 if price < 1_000 else 200_000) * rng.uniform(0.55, 1.9)
        if timeframe == "1d":
            base_volume *= rng.uniform(4, 12)
        bars.append(
            {
                "time": ts.isoformat(),
                "open": round(open_, 8),
                "high": round(high, 8),
                "low": round(low, 8),
                "close": round(close, 8),
                "volume": round(base_volume, 0),
            }
        )
        current = close

    # scale the series so the last close lands near the reference price
    if bars:
        scale = price / bars[-1]["close"]
        for bar in bars:
            for key in ("open", "high", "low", "close"):
                bar[key] = round(bar[key] * scale, 8)
    return bars


def crypto_bars(
    symbol: str,
    timeframe: str = "15m",
    count: int = 400,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Synthetic perpetual-futures candles (24/7, no session gaps)."""
    base, price, vol = CRYPTO_REFERENCE.get(symbol, (symbol.split("/")[0], 100.0, 0.35))
    now = now or dt.datetime.now(dt.timezone.utc)
    return _walk(symbol, count=count, price=price, vol_pct=vol, timeframe=timeframe, now=now,
                 drift=0.0004, idx_shape=False)


def idx_bars(
    symbol: str,
    timeframe: str = "1d",
    count: int = 260,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Synthetic IDX daily candles (weekdays only, IDX tick sizes on the close)."""
    from ..config import round_to_tick

    name, price, vol = IDX_REFERENCE.get(symbol.upper(), (symbol.upper(), 1_500.0, 1.6))
    now = now or dt.datetime.now(dt.timezone.utc)
    bars = _walk(symbol.upper(), count=count, price=price, vol_pct=vol, timeframe=timeframe, now=now,
                 drift=0.0002, idx_shape=True)
    for bar in bars:
        for key in ("open", "high", "low", "close"):
            bar[key] = round_to_tick(bar[key], "nearest")
            bar[key] = max(bar[key], 50.0)
    return bars


def idx_quote(symbol: str, bars: list[dict[str, Any]] | None = None, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """A quote row in the shape the IDX client returns."""
    symbol = symbol.upper()
    bars = bars or idx_bars(symbol, "1d", 260, now=now)
    last, prev = bars[-1], bars[-2]
    name, _price, _vol = IDX_REFERENCE.get(symbol, (symbol, last["close"], 1.5))
    change = last["close"] - prev["close"]
    return {
        "symbol": symbol,
        "name": name,
        "price": last["close"],
        "previous_close": prev["close"],
        "change": round(change, 2),
        "change_pct": round(change / prev["close"] * 100.0, 2) if prev["close"] else 0.0,
        "open": last["open"],
        "day_high": last["high"],
        "day_low": last["low"],
        "volume": last["volume"],
        "value": round(last["close"] * last["volume"], 0),
        "market_time": last["time"],
    }


def crypto_quote(symbol: str, bars: list[dict[str, Any]] | None = None, *, now: dt.datetime | None = None) -> dict[str, Any]:
    bars = bars or crypto_bars(symbol, "15m", 400, now=now)
    last, prev = bars[-1], bars[-2]
    return {
        "symbol": symbol,
        "price": last["close"],
        "previous_close": prev["close"],
        "change_pct": round((last["close"] / prev["close"] - 1) * 100.0, 3),
        "high": last["high"],
        "low": last["low"],
        "volume": last["volume"],
        "time": last["time"],
    }


def spread_bps(symbol: str, price: float, market: str) -> float:
    """A stable, plausible spread so the spread filter is not a coin flip."""
    rng = random.Random(_seed(symbol) + int(price) % 997)
    if market == "idx":
        from ..config import idx_tick_size

        tick = idx_tick_size(price)
        return round(2 * tick / price * 10_000.0 * rng.uniform(0.8, 2.2), 2)
    return round(rng.uniform(1.5, 7.0), 2)


def htf_bars(symbol: str, market: str, factor: int, count: int, *, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Higher-timeframe series (resampled, so it is consistent with the LTF series)."""
    if market == "idx":
        ltf = idx_bars(symbol, "1d", count * factor, now=now)
    else:
        ltf = crypto_bars(symbol, "15m", count * factor, now=now)
    out = []
    for i in range(0, len(ltf), factor):
        chunk = ltf[i : i + factor]
        out.append(
            {
                "time": chunk[0]["time"],
                "open": chunk[0]["open"],
                "high": max(b["high"] for b in chunk),
                "low": min(b["low"] for b in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(float(b.get("volume") or 0) for b in chunk),
            }
        )
    return out


def fundamentals(symbol: str) -> dict[str, Any]:
    """Deterministic but plausible fundamentals for the screener (clearly synthetic)."""
    rng = random.Random(_seed(symbol) + 7)
    symbol = symbol.upper()
    sector_map = {
        "BBCA": "Financials", "BBRI": "Financials", "BMRI": "Financials", "BBNI": "Financials",
        "TLKM": "Infrastructure", "ASII": "Industrials", "ADRO": "Energy", "ANTM": "Basic Materials",
        "MDKA": "Basic Materials", "GOTO": "Technology", "ICBP": "Consumer Non-Cyclicals",
        "UNVR": "Consumer Non-Cyclicals", "KLBF": "Healthcare", "PGAS": "Energy",
        "SMGR": "Basic Materials", "INCO": "Basic Materials", "TPIA": "Basic Materials",
        "BRPT": "Basic Materials", "CPIN": "Consumer Non-Cyclicals", "AMRT": "Consumer Non-Cyclicals",
    }
    return {
        "sector": sector_map.get(symbol, "Industrials"),
        "pe": round(rng.uniform(6, 22), 2),
        "pb": round(rng.uniform(0.7, 4.2), 2),
        "roe_pct": round(rng.uniform(6, 28), 2),
        "der": round(rng.uniform(0.1, 2.4), 2),
        "dividend_yield_pct": round(rng.uniform(0, 7.5), 2),
        "revenue_growth_pct": round(rng.uniform(-6, 22), 2),
        "earnings_growth_pct": round(rng.uniform(-15, 35), 2),
        "foreign_net_idr_bn": round(rng.gauss(0, 220), 1),
        "bandar_net_pct": round(rng.gauss(0, 12), 1),
        "ksei_top1_pct": round(rng.uniform(28, 78), 1),
    }
