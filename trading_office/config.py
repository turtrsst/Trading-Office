"""Configuration loading.

The engine keeps every threshold in one JSON document (``config/settings.json``) so the
logic in :mod:`trading_office.strategy` never hardcodes a number. Loading is tolerant:
unknown keys are kept (they are simply ignored) and missing sections fall back to the
defaults below, which mirror the documented strategy.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "settings.json"

#: Fallback used when no config file is present (identical to config/settings.json).
DEFAULTS: dict[str, Any] = {
    "engine": {
        "state_dir": "state",
        "timezone": "Asia/Jakarta",
        "require_trend_filter": True,
        "min_filters_passed": 3,
        "max_open_positions": 4,
        "max_portfolio_risk_pct": 4.0,
        "max_daily_loss_pct": 2.0,
        "max_consecutive_losses": 4,
        "cooldown_bars": 3,
        "equity_idr": 100_000_000,
        "equity_usdt": 1_000.0,
    },
    "market_filter": {
        "trend_ema": 200,
        # macro windows in which no entry is allowed (see market_filter.in_blackout):
        # a scheduled release is a gap generator, and a stop cannot protect you across it
        "news_blackout": [
            {"weekday": 0, "time": "20:30", "minutes": 45, "label": "US data", "tz": "Asia/Jakarta"},
            {"weekday": 2, "time": "01:00", "minutes": 45, "label": "FOMC", "tz": "Asia/Jakarta"},
            {"weekday": 4, "time": "19:30", "minutes": 45, "label": "US labour data", "tz": "Asia/Jakarta"},
        ],
        "trend_tolerance_atr": 0.25,
        "atr_period": 14,
        "atr_pct_band_mult": [0.6, 2.5],
        "news_blackout": [],
        "block_weekend_crypto": False,
    },
    "signal": {
        "min_score": 5,
        "rsi_period": 14,
        "rsi_trigger": 35,
        "rsi_overbought": 75,
        "rsi_oversold": 25,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "bb_period": 20,
        "bb_std": 2.0,
        "volume_ma": 20,
        "volume_mult": 1.2,
        "swing_lookback": 10,
    },
    "risk": {
        "risk_per_trade_pct": 1.0,
        "rr_target": 2.0,
        "min_rr": 1.5,
        "atr_sl_mult": 1.5,
        "atr_tp_cap_mult": 6.0,
        "max_leverage": 3,
        "max_margin_per_trade_pct": 8.0,
        "max_symbol_exposure_pct": 20.0,
        "liquidity_participation_pct": 0.5,
        "cost_multiple": 3.0,
    },
    "management": {
        "breakeven_at_r": 1.0,
        "breakeven_buffer_spread": 1.0,
        "partial_tp_r": 2.0,
        "partial_tp_fraction": 0.5,
        "partial_be_at_r": 2.0,
        "trail_atr_mult": 1.5,
        "trail_step_atr": 0.25,
        "trail_start_r": 1.0,
        "max_bars_in_trade_crypto": 96,
        "max_bars_in_trade_idx": 15,
        "exit_on_opposite_signal": True,
        "flat_before_weekend": False,
        "idx_close_before_minutes": 10,
    },
    "profiles": {
        "crypto": {
            "timeframe": "15m",
            "trend_timeframe": "4h",
            "atr_min_pct": 0.25,
            "atr_max_pct": 6.0,
            "max_spread_bps": 8.0,
            "max_margin_per_trade_pct": 8.0,
            "commission_bps": 5.5,
            "slippage_bps": 2.0,
            "sessions": [["07:00", "10:00"], ["13:00", "22:00"]],
            "sessions_timezone": "Asia/Jakarta",
            "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"],
        },
        "idx": {
            "timeframe": "1d",
            "trend_timeframe": "1wk",
            "atr_min_pct": 0.8,
            "atr_max_pct": 9.0,
            "max_spread_bps": 60.0,
            "max_margin_per_trade_pct": 20.0,
            "commission_buy_pct": 0.15,
            "commission_sell_pct": 0.25,
            "slippage_bps": 5.0,
            "tick_table": True,
            "min_daily_value_idr": 5_000_000_000,
            "sessions": [["09:15", "11:30"], ["13:30", "15:45"]],
            "sessions_timezone": "Asia/Jakarta",
            "symbols": ["BBCA", "BBRI", "BMRI", "BBNI", "TLKM", "ASII", "ADRO", "ANTM", "MDKA", "GOTO"],
        },
    },
    "execution": {
        "exchange_id": "bybit",
        "testnet": True,
        "api_key_env": "TO_API_KEY",
        "api_secret_env": "TO_API_SECRET",
        "paper": True,
        "dry_run": False,
        "max_order_retries": 2,
        "poll_seconds": 15,
    },
    "idx": {
        "command": "idx",
        "timeout_seconds": 45,
        "universe_limit": 40,
        "cache_ttl_seconds": 120,
        "offline": False,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class Settings:
    """Dotted-path access to the merged configuration."""

    raw: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Path = DEFAULT_CONFIG_PATH

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Settings":
        # .env first: every entry point (serve, engine, backtest, mcp) goes through here,
        # so one load covers the whole process. Explicit environment variables win.
        from . import env as _env

        _env.load_env()
        target = Path(path) if path else Path(os.environ.get("TO_CONFIG", DEFAULT_CONFIG_PATH))
        raw = copy.deepcopy(DEFAULTS)
        if target.exists():
            with open(target, "r", encoding="utf-8") as fh:
                raw = deep_merge(raw, json.load(fh))
        return cls(raw=raw, path=target)

    # ------------------------------------------------------------------ access
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name, {})
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def profile(self, name: str) -> dict[str, Any]:
        profiles = self.raw.get("profiles", {})
        profile = copy.deepcopy(profiles.get(name, {}))
        profile.setdefault("name", name)
        return profile

    def merged(self, *sections: str) -> dict[str, Any]:
        """Merge several top-level sections into one flat dict (later wins)."""
        out: dict[str, Any] = {}
        for name in sections:
            out.update(self.section(name))
        return out

    def tick_for(self, price: float) -> float:
        """IDX fractional tick table (IDX rules since 2021): returns the tick size."""
        return idx_tick_size(price)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)


# ---------------------------------------------------------------------- IDX ticks
def idx_tick_size(price: float) -> float:
    """IDX tick size for a given price band (IDX regulation II-A, 2021 onwards).

    The exchange quotes rupiah prices with these fractional ticks; rounding to them is
    what makes a simulated IDX order realistic instead of off-tick.
    """
    p = float(price)
    if p < 200:
        return 1.0
    if p < 500:
        return 2.0
    if p < 2000:
        return 5.0
    if p < 5000:
        return 10.0
    return 25.0


def round_to_tick(price: float, side: str = "nearest", tick: float | None = None) -> float:
    """Round ``price`` to a tick boundary. ``side`` is up, down or nearest."""
    t = tick if tick else idx_tick_size(price)
    if t <= 0:
        return float(price)
    units = float(price) / t
    if side == "up":
        return round(int(units) * t + (0 if abs(units - int(units)) < 1e-9 else t), 6)
    if side == "down":
        return round(int(units) * t, 6)
    return round(round(units) * t, 6)
