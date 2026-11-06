#!/usr/bin/env python3
"""Write the demo datasets used by the backtester and the docs.

    python scripts/make_demo_data.py            # synthetic closed candles
    python scripts/make_demo_data.py --live     # try ccxt / idx first, fall back to synthetic

Files written into ``data/``:

    crypto_<SYMBOL>_15m.json   900 closed perpetual-futures candles
    idx_<SYMBOL>_1d.json       900 closed daily candles (5y of IDX history)

Each file is a plain JSON object::

    {"symbol": ..., "market": ..., "timeframe": ..., "source": ..., "candles": [...], "quote": {...}}

so it can be fed straight to::

    python -m trading_office.backtest --market crypto --data data/crypto_BTCUSDT_15m.json --oos 0.3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trading_office.config import Settings  # noqa: E402
from trading_office.market import demo  # noqa: E402

CRYPTO = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"]
IDX = ["BBCA", "BBRI", "BMRI", "TLKM", "ANTM", "GOTO"]


def safe(symbol: str) -> str:
    return symbol.replace("/", "").replace(":", "").replace("USDTUSDT", "USDT")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="prefer the live providers")
    parser.add_argument("--bars", type=int, default=900)
    args = parser.parse_args()

    settings = Settings.load()
    out_dir = ROOT / "data"
    out_dir.mkdir(exist_ok=True)

    crypto_client = idx_client = None
    if args.live:
        try:
            from trading_office.market import CryptoClient

            crypto_client = CryptoClient(exchange_id=str(settings.get("execution.exchange_id", "bybit")))
        except Exception as exc:  # noqa: BLE001
            print(f"  crypto provider unavailable ({exc})")
        try:
            from trading_office.market import IdxClient

            idx_client = IdxClient(settings.section("idx"))
        except Exception as exc:  # noqa: BLE001
            print(f"  idx provider unavailable ({exc})")

    written = []
    for symbol in CRYPTO:
        bars, source = None, "synthetic"
        if crypto_client is not None:
            payload = crypto_client.candles(symbol, "15m", max(args.bars, 400))
            bars, source = payload["bars"], payload["source"]
        if not bars:
            bars = demo.crypto_bars(symbol, "15m", args.bars)
        path = out_dir / f"crypto_{safe(symbol)}_15m.json"
        path.write_text(
            json.dumps(
                {"symbol": symbol, "market": "crypto", "timeframe": "15m",
                 "source": source, "candles": bars,
                 "quote": demo.crypto_quote(symbol, bars)},
                indent=None,
            ),
            encoding="utf-8",
        )
        written.append((path.name, len(bars), source))

    for symbol in IDX:
        bars, source, quote = None, "synthetic", None
        if idx_client is not None:
            payload = idx_client.candles(symbol, period="5y", interval="1d")
            bars, source = payload["bars"], payload["source"]
            quote = (idx_client.quote(symbol) or {}).get("quote")
        if not bars:
            bars = demo.idx_bars(symbol, "1d", args.bars)
        quote = quote or demo.idx_quote(symbol, bars)
        path = out_dir / f"idx_{symbol}_1d.json"
        path.write_text(
            json.dumps(
                {"symbol": symbol, "market": "idx", "timeframe": "1d",
                 "source": source, "candles": bars, "quote": quote},
                indent=None,
            ),
            encoding="utf-8",
        )
        written.append((path.name, len(bars), source))

    for name, count, source in written:
        print(f"  {name:<28} {count:>5} bars   [{source}]")
    print(f"\n  {len(written)} files in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
