"""Crypto market data through ``ccxt`` (public endpoints — no keys needed to read).

Reads are deliberately separated from execution: this module never places an order, it
only gives the engine closed candles, a ticker, a book (for the spread filter) and recent
trades. When ``ccxt`` is missing or the venue is unreachable, it falls back to the
synthetic series in :mod:`trading_office.market.demo` and says so.

Live ticks: :meth:`CryptoClient.recent_trades` is the tick source. On venues where
``ccxt.pro`` is available, ``watch_trades`` streams the tape; otherwise the poller asks
for the last trades every ``poll_seconds``. Either way the *decision* bars come from
closed candles only.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any, Iterable

from . import demo


class CryptoUnavailable(RuntimeError):
    pass


class CryptoClient:
    def __init__(
        self,
        *,
        exchange_id: str = "bybit",
        symbols: Iterable[str] | None = None,
        sandbox: bool = False,
        timeout: float = 20.0,
    ) -> None:
        self.exchange_id = exchange_id
        self.symbols = list(symbols or [])
        self.timeout = timeout
        self.sandbox = sandbox
        self.last_error: str | None = None
        self._exchange: Any = None
        self._lock = threading.RLock()
        self._tick_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ setup
    def exchange(self) -> Any:
        with self._lock:
            if self._exchange is not None:
                return self._exchange
            try:
                import ccxt  # lazy: optional dependency
            except ImportError as exc:
                raise CryptoUnavailable("ccxt is not installed (pip install ccxt)") from exc
            cls = getattr(ccxt, self.exchange_id, None)
            if cls is None:
                raise CryptoUnavailable(f"unknown exchange {self.exchange_id!r}")
            exchange = cls({"enableRateLimit": True, "timeout": int(self.timeout * 1000)})
            if self.sandbox:
                exchange.set_sandbox_mode(True)
            try:
                exchange.load_markets()
            except Exception as exc:  # network / geo block
                raise CryptoUnavailable(f"{self.exchange_id}: {exc}") from exc
            self._exchange = exchange
            return exchange

    def available(self) -> bool:
        try:
            self.exchange()
            return True
        except CryptoUnavailable as exc:
            self.last_error = str(exc)
            return False

    # ------------------------------------------------------------------ candles
    def candles(self, symbol: str, timeframe: str = "15m", limit: int = 400) -> dict[str, Any]:
        """Closed candles only — the forming bar is dropped unless it has closed."""
        try:
            exchange = self.exchange()
            rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            bars = [
                {
                    "time": dt.datetime.fromtimestamp(r[0] / 1000.0, dt.timezone.utc).isoformat(),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                    "time_ms": int(r[0]),
                }
                for r in rows
            ]
            step_ms = exchange.parse_timeframe(timeframe) * 1000
            now_ms = int(time.time() * 1000)
            if bars and now_ms < bars[-1]["time_ms"] + step_ms:
                bars = bars[:-1]  # forming candle: never used for a decision
            if len(bars) < 60:
                raise CryptoUnavailable(f"only {len(bars)} closed bars returned")
            return {"bars": bars, "source": "ccxt", "exchange": self.exchange_id, "warnings": []}
        except Exception as exc:
            self.last_error = str(exc)
            bars = demo.crypto_bars(symbol, timeframe, limit)
            return {
                "bars": bars,
                "source": "synthetic",
                "exchange": self.exchange_id,
                "warnings": [f"{self.exchange_id} unavailable ({exc}); synthetic candles"],
            }

    def htf_candles(self, symbol: str, timeframe: str, limit: int = 300) -> dict[str, Any]:
        return self.candles(symbol, timeframe, limit)

    # ------------------------------------------------------------------ ticks & book
    def ticker(self, symbol: str) -> dict[str, Any]:
        try:
            exchange = self.exchange()
            tick = exchange.fetch_ticker(symbol)
            last = float(tick.get("last") or tick.get("close") or 0.0)
            if last <= 0:
                raise CryptoUnavailable("ticker without a last price")
            out = {
                "symbol": symbol,
                "price": last,
                "bid": float(tick.get("bid") or 0.0) or None,
                "ask": float(tick.get("ask") or 0.0) or None,
                "high": tick.get("high"),
                "low": tick.get("low"),
                "base_volume": tick.get("baseVolume"),
                "quote_volume": tick.get("quoteVolume"),
                "timestamp": tick.get("timestamp"),
                "source": "ccxt",
                "warnings": [],
            }
            self._tick_cache[symbol] = out
            return out
        except Exception as exc:
            self.last_error = str(exc)
            quote = demo.crypto_quote(symbol)
            price = float(quote["price"])
            spread = demo.spread_bps(symbol, price, "crypto") / 10_000.0
            out = {
                "symbol": symbol,
                "price": price,
                "bid": round(price * (1 - spread / 2), 8),
                "ask": round(price * (1 + spread / 2), 8),
                "high": quote["high"],
                "low": quote["low"],
                "quote_volume": quote["volume"] * price,
                "timestamp": None,
                "source": "synthetic",
                "warnings": [f"{self.exchange_id} unavailable ({exc}); synthetic ticker"],
            }
            self._tick_cache[symbol] = out
            return out

    def order_book(self, symbol: str, depth: int = 5) -> dict[str, Any]:
        try:
            exchange = self.exchange()
            book = exchange.fetch_order_book(symbol, depth)
            bid = float(book["bids"][0][0]) if book.get("bids") else None
            ask = float(book["asks"][0][0]) if book.get("asks") else None
            return {"bid": bid, "ask": ask, "source": "ccxt"}
        except Exception as exc:
            self.last_error = str(exc)
            tick = self.ticker(symbol)
            return {"bid": tick.get("bid"), "ask": tick.get("ask"), "source": tick.get("source")}

    def recent_trades(self, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        """The live tape (best effort). Used to time entries inside a closed bar, never
        to form the signal."""
        try:
            exchange = self.exchange()
            trades = exchange.fetch_trades(symbol, limit=limit)
            return [
                {
                    "price": float(t["price"]),
                    "amount": float(t.get("amount") or 0.0),
                    "side": t.get("side"),
                    "timestamp": t.get("timestamp"),
                }
                for t in trades
            ]
        except Exception as exc:
            self.last_error = str(exc)
            price = float(self.ticker(symbol).get("price") or 0.0)
            now = int(time.time() * 1000)
            return [
                {"price": price, "amount": 0.0, "side": "buy" if i % 2 else "sell", "timestamp": now - i}
                for i in range(5)
            ]

    def markets(self, quote: str = "USDT", kind: str = "swap", limit: int = 40) -> list[dict[str, Any]]:
        """Tradable symbols (for the market picker), largest 24h quote volume first."""
        try:
            exchange = self.exchange()
            rows = []
            for symbol, market in exchange.markets.items():
                if market.get("quote") != quote or market.get("type") != kind:
                    continue
                rows.append({"symbol": symbol, "base": market.get("base")})
            return rows[:limit]
        except Exception as exc:
            self.last_error = str(exc)
            return [{"symbol": s, "base": s.split("/")[0]} for s in demo.CRYPTO_REFERENCE][:limit]

    def status(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange_id,
            "available": self.available(),
            "symbols": self.symbols,
            "last_error": self.last_error,
            "install": "pip install ccxt",
        }
