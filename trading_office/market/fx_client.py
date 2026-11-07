"""Gold and FX market data through the Exness Trader API.

The whole point of this module is that the strategy never trades on a synthetic chart while
believing it is looking at gold. Exness publishes candle history per account
(``GET /v1/market-data/accounts/{id}/candles``) including a **bid and ask** view, so both the
signal and the spread can come from the venue itself.

Two rules inherited from the crypto client, because they are correctness rules and not taste:

* **Closed bars only.** The last returned candle is dropped while it is still forming. A
  decision made on a half-built bar is a decision on noise, and it is not reproducible.
* **No silent substitution.** If the API is unavailable the caller gets ``source: synthetic``
  and a warning, exactly like the other markets; nothing is labelled live that is not.

The timeframe vocabulary is the venue's own (``M15``, ``H4``, ``D1``…), mapped from the
profile's ``15m`` / ``4h`` style so a profile can stay readable.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from . import demo

#: profile timeframe → Exness candle timeframe
TIMEFRAMES: dict[str, str] = {
    "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
    "1h": "H1", "2h": "H2", "4h": "H4", "1d": "D1", "1wk": "W1", "1mn": "MN",
}

#: duration of one candle, used to decide whether the last bar has closed
STEP: dict[str, dt.timedelta] = {
    "S1": dt.timedelta(seconds=1), "S5": dt.timedelta(seconds=5),
    "S15": dt.timedelta(seconds=15), "M1": dt.timedelta(minutes=1),
    "M3": dt.timedelta(minutes=3), "M5": dt.timedelta(minutes=5),
    "M10": dt.timedelta(minutes=10), "M15": dt.timedelta(minutes=15),
    "M30": dt.timedelta(minutes=30), "H1": dt.timedelta(hours=1),
    "H2": dt.timedelta(hours=2), "H4": dt.timedelta(hours=4),
    "D1": dt.timedelta(days=1), "W1": dt.timedelta(days=7), "MN": dt.timedelta(days=30),
}


class FxUnavailable(RuntimeError):
    """Raised internally when the venue cannot serve a complete series."""


class FxClient:
    """Exness candles + quotes, with a labelled synthetic fallback.

    ``client`` is an :class:`~trading_office.execution.exness.ExnessClient`. It is optional on
    purpose: without credentials the market still works (synthetic), which is what lets the
    whole UI be exercised before an account exists.
    """

    def __init__(self, client: Any | None = None, symbols: list[str] | None = None,
                 *, now: Callable[[], dt.datetime] | None = None) -> None:
        self.client = client
        self.symbols = [s.upper() for s in (symbols or [])]
        self.last_error: str = ""
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    # ------------------------------------------------------------------ helpers
    def available(self) -> bool:
        return self.client is not None

    def status(self) -> dict[str, Any]:
        return {"provider": "exness", "configured": self.available(),
                "last_error": self.last_error, "symbols": self.symbols}

    def _to_bars(self, rows: list[dict[str, Any]], timeframe: str, price_type: str) -> list[dict[str, Any]]:
        """Normalise candles and drop the forming one."""
        if price_type == "both":
            # {time, bid:{open,high,low,close}, ask:{...}, volume}
            bars = []
            for row in rows:
                side = row.get("bid") or {}
                ask = row.get("ask") or {}
                bars.append({
                    "time": str(row.get("time")),
                    "open": float(side.get("open") or 0.0),
                    "high": float(side.get("high") or 0.0),
                    "low": float(side.get("low") or 0.0),
                    "close": float(side.get("close") or 0.0),
                    "volume": float(row.get("volume") or 0.0),
                    "ask_close": float(ask.get("close") or 0.0),
                })
        else:
            bars = [{
                "time": str(row.get("time")),
                "open": float(row.get("open") or 0.0),
                "high": float(row.get("high") or 0.0),
                "low": float(row.get("low") or 0.0),
                "close": float(row.get("close") or 0.0),
                "volume": float(row.get("volume") or 0.0),
            } for row in rows]
        bars = [b for b in bars if b["open"] and b["close"]]
        step = STEP.get(timeframe, dt.timedelta(minutes=15))
        if bars:
            last = dt.datetime.fromisoformat(bars[-1]["time"].replace("Z", "+00:00"))
            if self._now() < last + step:
                bars = bars[:-1]                    # still forming: never used for a decision
        return bars

    # ------------------------------------------------------------------ reads
    def candles(self, symbol: str, timeframe: str = "15m", limit: int = 400,
                price_type: str = "bid") -> dict[str, Any]:
        tf = TIMEFRAMES.get(timeframe, timeframe.upper())
        if self.client is not None:
            try:
                step = STEP.get(tf, dt.timedelta(minutes=15))
                start = self._now() - step * (int(limit) + 1)
                rows = self.client.candles(
                    symbol.upper(), tf,
                    start=start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    count=int(limit),
                    price_type=price_type,
                )
                bars = self._to_bars(rows, tf, price_type)
                if len(bars) < 60:
                    raise FxUnavailable(f"only {len(bars)} closed bars returned for {symbol}")
                return {"bars": bars[-int(limit):], "source": "exness",
                        "timeframe": tf, "warnings": []}
            except Exception as exc:  # noqa: BLE001 - the fallback is the contract
                self.last_error = f"{type(exc).__name__}: {exc}"
        bars = demo.fx_bars(symbol.upper(), "15m", int(limit), now=self._now())
        return {"bars": bars, "source": "synthetic", "timeframe": tf,
                "warnings": [f"{symbol}: synthetic candles ({self.last_error or 'no Exness key'})"]}

    def htf_candles(self, symbol: str, timeframe: str = "4h", limit: int = 260) -> dict[str, Any]:
        """Higher timeframe; resampled from the lower one when the venue cannot serve it."""
        if self.client is not None:
            out = self.candles(symbol, timeframe, limit)
            if out["source"] == "exness":
                return out
        factor = max(1, int(round(
            STEP.get(TIMEFRAMES.get(timeframe, "H4"), dt.timedelta(hours=4))
            / dt.timedelta(minutes=15))))
        return {"bars": demo.htf_bars(symbol.upper(), "fx", factor, int(limit), now=self._now()),
                "source": "synthetic", "timeframe": TIMEFRAMES.get(timeframe, timeframe),
                "warnings": [f"{symbol}: synthetic higher timeframe"]}

    def quote(self, symbol: str) -> dict[str, Any]:
        """Bid/ask from the latest M1 candles (no REST tick endpoint is documented).

        The spread is read, not modelled: it is the only input the spread gate trusts.
        """
        if self.client is not None:
            try:
                block = self.candles(symbol, "1m", 3, price_type="both")
                if block["source"] == "exness" and block["bars"]:
                    last = block["bars"][-1]
                    bid = float(last.get("close") or 0.0)
                    ask = float(last.get("ask_close") or 0.0)
                    if bid and ask and ask >= bid:
                        mid = (bid + ask) / 2
                        return {"symbol": symbol.upper(), "last": round(mid, 5),
                                "bid": bid, "ask": ask,
                                "spread_bps": round((ask - bid) / mid * 10_000, 3),
                                "source": "exness", "timestamp": last["time"]}
                raise FxUnavailable("no bid/ask pair returned")
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
        out = demo.fx_quote(symbol.upper(), now=self._now())
        out["warnings"] = [f"{symbol}: synthetic quote"]
        return out


def from_settings(settings: Any, *, transport: Callable[..., Any] | None = None) -> FxClient:
    """Build a client for the ``fx`` market. Missing credentials are **not** an error here.

    Market data is read-only: without a key the chart is synthetic and labelled as such. It is
    execution that must refuse to start, and that refusal lives in ``exness.from_settings``.
    """
    symbols = list(settings.get("profiles.fx.symbols") or ["XAUUSD"])
    try:
        from ..execution.exness import from_settings as broker_from_settings

        broker = broker_from_settings(settings, transport=transport)
        return FxClient(broker.client, symbols)
    except RuntimeError:
        return FxClient(None, symbols)
    except Exception:  # noqa: BLE001 - a broken key must not break the chart
        return FxClient(None, symbols)
