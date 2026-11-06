"""Bridge to the ``idx`` CLI / MCP tooling (github.com/fzn0x/idx).

Everything the IDX side of the app knows comes through here, in one place:

===============  ==========================================  ======================================
feature          idx command                                 shape used by the app
===============  ==========================================  ======================================
candles          ``stocks history SYM --period 1y -o json``  ``data.candles[]``
quote            ``stocks quote SYM -o json``                ``data[0]``
brief            ``stocks brief SYM -o json``                fundamentals + technical + news
bandarmology     ``bandar detect SYM --period 1w -o json``   ``data[0]`` (signal, phase, net_pct…)
broker summary   ``broker SYM -o json``                     ``data[0]`` (buyers/sellers)
foreign flow     ``stocks foreign SYM --days 20 -o json``   ``data.totals`` / ``days[]``
KSEI ownership   ``ownership ticker SYM -o json``           holder rows
corp. actions    ``stocks actions SYM -o json``             dividends / rights / RUPS
screen           ``stocks screen -f ... -o json``           ``data.rows[]``
indices          ``market indices -o json``                 IHSG, LQ45 …
universe         ``stocks list -o json``                    instruments
===============  ==========================================  ======================================

Design rules

* **One subprocess per request**, cached for ``idx.cache_ttl_seconds`` so a dashboard
  refresh does not hammer idx.co.id (and does not trip its bot protection).
* **Failure never breaks the app**: a missing binary, a non-zero exit or a JSON parse
  error returns synthetic data with ``source="synthetic"`` and a human-readable warning
  that is surfaced in the UI. Nothing is silently faked as live.
* ``idx``'s own envelope (``schema/provider/fetched_at/stale/warnings/data``) is passed
  through where useful so ``stale`` badges stay honest.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import demo


class IdxUnavailable(RuntimeError):
    """Raised when the idx binary cannot be executed at all."""


class _TTLCache:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            stamp, value = hit
            if time.time() - stamp > self.ttl:
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class IdxClient:
    """Thin, cached, failure-tolerant wrapper around the ``idx`` binary."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.command = str(cfg.get("command", "idx"))
        self.timeout = float(cfg.get("timeout_seconds", 45))
        self.offline = bool(cfg.get("offline", False))
        self.cache = _TTLCache(float(cfg.get("cache_ttl_seconds", 120)))
        self.last_error: str | None = None

    # ------------------------------------------------------------------ plumbing
    def available(self) -> bool:
        return shutil.which(self.command) is not None or Path(self.command).exists()

    def _run(self, *args: str) -> dict[str, Any]:
        cmd = [self.command, *args, "-o", "json"]
        if self.offline:
            cmd.append("--offline")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IdxUnavailable(str(exc)) from exc
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = message[-1] if message else f"exit {proc.returncode}"
            raise IdxUnavailable(f"idx {args[0]} failed: {detail}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise IdxUnavailable(f"idx returned non-JSON: {proc.stdout[:120]!r}") from exc

    def _cached(self, key: str, fetch: Callable[[], Any]) -> Any:
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        value = fetch()
        self.cache.put(key, value)
        return value

    def _envelope_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": payload.get("provider"),
            "fetched_at": payload.get("fetched_at"),
            "stale": payload.get("stale", False),
            "warnings": payload.get("warnings") or [],
        }

    # ------------------------------------------------------------------ prices
    def candles(
        self, symbol: str, *, period: str = "1y", interval: str = "1d"
    ) -> dict[str, Any]:
        """Daily (or weekly) candles. Returns ``{bars, source, note, warnings}``."""
        symbol = symbol.upper().replace(".JK", "")

        def fetch() -> dict[str, Any]:
            payload = self._run("stocks", "history", symbol, "--period", period, "--interval", interval)
            candles = ((payload.get("data") or {}).get("candles")) or []
            bars = [
                {
                    "time": c.get("time"),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": float(c.get("volume") or 0),
                }
                for c in candles
                if c.get("open") is not None
            ]
            if not bars:
                raise IdxUnavailable("idx returned no candles")
            return {
                "bars": bars,
                "source": "idx",
                "note": self._envelope_note(payload),
                "warnings": payload.get("warnings") or [],
            }

        try:
            return self._cached(f"candles:{symbol}:{period}:{interval}", fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            # ask the synthetic provider for what was requested: the backtest needs the
            # full window (walk-forward warms EMA200 on it), not a hard-coded 260 bars
            count = {"1y": 260, "2y": 520, "5y": 1250, "10y": 1500}.get(period, 260)
            bars = demo.idx_bars(
                symbol,
                interval if interval in ("1d", "1wk") else "1d",
                min(count * 5, 1500) if interval == "1wk" else count,
            )
            return {
                "bars": bars,
                "source": "synthetic",
                "note": {"provider": "synthetic"},
                "warnings": [f"idx unavailable ({exc}); showing synthetic candles"],
            }

    def quote(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper().replace(".JK", "")

        def fetch() -> dict[str, Any]:
            payload = self._run("stocks", "quote", symbol)
            rows = payload.get("data") or []
            if not rows:
                raise IdxUnavailable("no quote row")
            row = rows[0]
            return {"quote": row, "source": "idx", "note": self._envelope_note(payload),
                    "warnings": payload.get("warnings") or []}

        try:
            return self._cached(f"quote:{symbol}", fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            quote = demo.idx_quote(symbol)
            return {"quote": quote, "source": "synthetic", "note": {"provider": "synthetic"},
                    "warnings": [f"idx unavailable ({exc}); showing a synthetic quote"]}

    def screen(self, filters: str | None = None, limit: int = 40) -> dict[str, Any]:
        def fetch() -> dict[str, Any]:
            args = ["stocks", "screen", "-n", str(limit)]
            if filters:
                args += ["-f", filters]
            payload = self._run(*args)
            rows = ((payload.get("data") or {}).get("rows")) or []
            return {"rows": rows, "source": "idx", "note": self._envelope_note(payload),
                    "warnings": payload.get("warnings") or []}

        try:
            return self._cached(f"screen:{filters}:{limit}", fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            return {"rows": [], "source": "synthetic", "note": {"provider": "synthetic"},
                    "warnings": [f"idx unavailable ({exc}); using the bundled universe"]}

    def indices(self) -> dict[str, Any]:
        def fetch() -> dict[str, Any]:
            payload = self._run("market", "indices")
            data = payload.get("data") or {}
            return {"data": data, "source": "idx", "note": self._envelope_note(payload),
                    "warnings": payload.get("warnings") or []}

        try:
            return self._cached("indices", fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            return {"data": {"date": None, "indices": []}, "source": "synthetic",
                    "note": {"provider": "synthetic"},
                    "warnings": [f"idx unavailable ({exc})"]}

    def universe(self, limit: int = 60) -> list[str]:
        """Most liquid names to scan: the screen's largest rows, else the bundled list."""
        rows = self.screen(limit=limit).get("rows") or []
        symbols = [str(r.get("symbol", "")).upper() for r in rows if r.get("symbol")]
        if symbols:
            return symbols[:limit]
        return list(demo.IDX_REFERENCE.keys())[:limit]

    # ------------------------------------------------------------------ analysis
    def brief(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()

        def fetch() -> dict[str, Any]:
            payload = self._run("stocks", "brief", symbol)
            return {"data": payload.get("data") or {}, "source": "idx",
                    "note": self._envelope_note(payload), "warnings": payload.get("warnings") or []}

        try:
            return self._cached(f"brief:{symbol}", fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            return {"data": {}, "source": "synthetic", "note": {"provider": "synthetic"},
                    "warnings": [f"idx unavailable ({exc})"]}

    def bandar(self, symbol: str, period: str = "1w") -> dict[str, Any]:
        """Bandarmology reading: top-N broker accumulation/distribution."""
        symbol = symbol.upper()

        def fetch() -> dict[str, Any]:
            payload = self._run("bandar", "detect", symbol, "--period", period)
            rows = payload.get("data") or []
            if isinstance(rows, dict):
                rows = [rows]
            if not rows:
                raise IdxUnavailable("no bandar reading")
            return {"data": rows[0], "source": "idx", "note": self._envelope_note(payload),
                    "warnings": payload.get("warnings") or []}

        try:
            return self._cached(f"bandar:{symbol}:{period}", fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            return {"data": _demo_bandar(symbol), "source": "synthetic",
                    "note": {"provider": "synthetic"},
                    "warnings": [f"idx unavailable ({exc}); synthetic bandarmology"]}

    def broker(self, symbol: str, date: str | None = None) -> dict[str, Any]:
        symbol = symbol.upper()

        def fetch() -> dict[str, Any]:
            args = ["broker", symbol]
            if date:
                args += ["--date", date]
            payload = self._run(*args)
            rows = payload.get("data") or []
            if not rows:
                raise IdxUnavailable("no broker summary")
            return {"data": rows[0], "source": "idx", "note": self._envelope_note(payload),
                    "warnings": payload.get("warnings") or []}

        key = f"broker:{symbol}:{date or 'latest'}"
        try:
            return self._cached(key, fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            return {"data": _demo_broker(symbol), "source": "synthetic",
                    "note": {"provider": "synthetic"},
                    "warnings": [f"idx unavailable ({exc}); synthetic broker summary"]}

    def foreign(self, symbol: str, days: int = 20) -> dict[str, Any]:
        symbol = symbol.upper()

        def fetch() -> dict[str, Any]:
            payload = self._run("stocks", "foreign", symbol, "--days", str(days))
            return {"data": payload.get("data") or {}, "source": "idx",
                    "note": self._envelope_note(payload), "warnings": payload.get("warnings") or []}

        try:
            return self._cached(f"foreign:{symbol}:{days}", fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            return {"data": _demo_foreign(symbol, days), "source": "synthetic",
                    "note": {"provider": "synthetic"},
                    "warnings": [f"idx unavailable ({exc}); synthetic foreign flow"]}

    def ownership(self, symbol: str) -> dict[str, Any]:
        """KSEI 1%+ holders for one ticker (local database, no network)."""
        symbol = symbol.upper()

        def fetch() -> dict[str, Any]:
            payload = self._run("ownership", "ticker", symbol)
            return {"data": payload.get("data") or {}, "source": "idx",
                    "note": self._envelope_note(payload), "warnings": payload.get("warnings") or []}

        try:
            return self._cached(f"ownership:{symbol}", fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            return {"data": _demo_ownership(symbol), "source": "synthetic",
                    "note": {"provider": "synthetic"},
                    "warnings": [f"idx unavailable ({exc}); synthetic KSEI register"]}

    def actions(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()

        def fetch() -> dict[str, Any]:
            payload = self._run("stocks", "actions", symbol)
            return {"data": payload.get("data") or {}, "source": "idx",
                    "note": self._envelope_note(payload), "warnings": payload.get("warnings") or []}

        try:
            return self._cached(f"actions:{symbol}", fetch)
        except IdxUnavailable as exc:
            self.last_error = str(exc)
            return {"data": _demo_actions(symbol), "source": "synthetic",
                    "note": {"provider": "synthetic"},
                    "warnings": [f"idx unavailable ({exc}); synthetic corporate actions"]}

    # ------------------------------------------------------------------ health
    def status(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "available": self.available(),
            "offline": self.offline,
            "cache_ttl_s": self.cache.ttl,
            "last_error": self.last_error,
            "install": (
                "npm install -g github:fzn0x/idx  ·  curl -fsSL "
                "https://raw.githubusercontent.com/fzn0x/idx/main/install.sh | sh  ·  "
                "cargo install --locked ihsg-cli"
            ),
        }


# --------------------------------------------------------------------------- synthetic fallbacks
def _demo_bandar(symbol: str) -> dict[str, Any]:
    """Coherent synthetic bandarmology: the signal is *derived* from the broker lists."""
    import random

    rng = random.Random(symbol + "bandar")
    codes = ["YP", "CC", "AK", "BK", "RX", "MG", "DX", "NI", "OD", "RF"]
    picks = rng.sample(codes, 10)
    buyers = [
        {"rank": i + 1, "code": picks[i], "kind": rng.choice(["local", "foreign", "state_owned"]),
         "lots": 0, "value": float(rng.randint(20, 120) * 1e9), "average_price": None}
        for i in range(5)
    ]
    sellers = [
        {"rank": i + 1, "code": picks[i + 5], "kind": rng.choice(["local", "foreign"]),
         "lots": 0, "value": float(rng.randint(20, 120) * 1e9), "average_price": None}
        for i in range(5)
    ]
    buy_value = sum(b["value"] for b in buyers)
    sell_value = sum(s["value"] for s in sellers)
    total_value = buy_value + sell_value + rng.uniform(0.3, 0.8) * (buy_value + sell_value)
    net_value = buy_value - sell_value
    net_pct = net_value / total_value * 100.0 if total_value else 0.0
    for side in (buyers, sellers):
        for row in side:
            row["lots"] = int(row["value"] / 1e6)
    signal = (
        "big_accumulation" if net_pct >= 15 else "accumulation" if net_pct >= 5
        else "big_distribution" if net_pct <= -15 else "distribution" if net_pct <= -5 else "neutral"
    )
    price_change = round(rng.gauss(0, 3), 2)
    if signal in ("big_accumulation", "accumulation"):
        phase = "markup" if price_change > 2 else "quiet_accumulation"
    elif signal in ("big_distribution", "distribution"):
        phase = "markdown" if price_change < -2 else "quiet_distribution"
    else:
        phase = "neutral"
    name = demo.IDX_REFERENCE.get(symbol, (symbol,))[0]
    return {
        "symbol": symbol,
        "name": name,
        "period": "1w",
        "signal": signal,
        "phase": phase,
        "net_value": round(net_value, 0),
        "net_pct": round(net_pct, 2),
        "price_change_pct": price_change,
        "bandar_gain_pct": round(rng.gauss(0, 4), 2),
        "foreign_net_value": round(rng.gauss(0, 300e9), 0),
        "total_value": round(total_value, 0),
        "top": 5,
        "buyers": buyers,
        "sellers": sellers,
        "reasons": ["synthetic demo data (idx CLI not reachable)"],
        "synthetic": True,
    }


def _demo_broker(symbol: str) -> dict[str, Any]:
    import random

    rng = random.Random(symbol + "broker")
    codes = ["YP", "CC", "AK", "BK", "RX", "MG", "DX", "NI", "OD", "RF", "SQ", "CP"]
    pick = rng.sample(codes, 10)
    buyers = [
        {"rank": i + 1, "code": pick[i], "kind": rng.choice(["local", "foreign"]),
         "lots": rng.randint(100_000, 1_200_000), "value": int(rng.uniform(3e9, 120e9)),
         "average_price": None}
        for i in range(5)
    ]
    sellers = [
        {"rank": i + 1, "code": pick[i + 5], "kind": rng.choice(["local", "foreign"]),
         "lots": rng.randint(90_000, 1_000_000), "value": int(rng.uniform(2e9, 100e9)),
         "average_price": None}
        for i in range(5)
    ]
    return {
        "symbol": symbol,
        "name": demo.IDX_REFERENCE.get(symbol, (symbol,))[0],
        "from": dt.date.today().isoformat(),
        "to": dt.date.today().isoformat(),
        "investor": "all",
        "board": "all",
        "buyers": buyers,
        "sellers": sellers,
        "total_value": round(sum(b["value"] for b in buyers) + sum(s["value"] for s in sellers), 0),
        "foreign_net_value": round(rng.gauss(0, 200e9), 0),
        "synthetic": True,
    }


def _demo_foreign(symbol: str, days: int) -> dict[str, Any]:
    """Synthetic foreign flow in the shape of ``idx.foreign.v1`` (shares + net at close)."""
    import random

    rng = random.Random(symbol + "foreign")
    price = demo.IDX_REFERENCE.get(symbol, (symbol, 1_500.0, 1.5))[1]
    out = []
    streak = 0
    for i in range(days):
        day = dt.date.today() - dt.timedelta(days=days - i)
        bought = float(rng.randint(20_000, 400_000))
        sold = float(rng.randint(20_000, 400_000))
        net = bought - sold
        streak = streak + 1 if net > 0 else 0
        out.append(
            {
                "date": day.isoformat(),
                "close": round(price, 2),
                "volume": int((bought + sold) * 1.4),
                "foreign_buy": bought,
                "foreign_sell": sold,
                "foreign_net": net,
                "foreign_net_value_at_close": round(net * price, 0),
            }
        )
    tail = out[-1]["foreign_net"] if out else 0
    return {
        "symbol": symbol,
        "days": out,
        "totals": {
            "sessions": len(out),
            "foreign_buy": sum(d["foreign_buy"] for d in out),
            "foreign_sell": sum(d["foreign_sell"] for d in out),
            "foreign_net": round(sum(d["foreign_net"] for d in out), 0),
            "foreign_net_value_at_close": round(
                sum(d["foreign_net_value_at_close"] for d in out), 0
            ),
            "net_buy_sessions": sum(1 for d in out if d["foreign_net"] > 0),
            "net_sell_sessions": sum(1 for d in out if d["foreign_net"] < 0),
            "streak": streak if tail > 0 else (0 if tail == 0 else -1),
        },
        "synthetic": True,
    }


def _demo_ownership(symbol: str) -> dict[str, Any]:
    import random

    rng = random.Random(symbol + "ksei")
    names = [
        "PT BANK CENTRAL ASIA TBK", "CITIBANK N.A. NEW YORK", "JPMORGAN CHASE BANK",
        "PT MANDIRI SEKURITAS", "GOVERNMENT OF SINGAPORE", "VANGUARD GROUP INC",
    ]
    holders = []
    remaining = 100.0
    for i, name in enumerate(names):
        pct = round(min(remaining, rng.uniform(1.2, 22.0)), 2)
        remaining = max(0.0, remaining - pct)
        holders.append(
            {
                "holder": name,
                "pct": pct,
                "shares": int(pct / 100.0 * 120_000_000_000),
                "change_pct": round(rng.gauss(0, 0.4), 2),
                "origin": "foreign" if rng.random() > 0.5 else "local",
            }
        )
    return {
        "symbol": symbol,
        "as_of": dt.date.today().replace(day=1).isoformat(),
        "holders": holders,
        "listed_pct": round(sum(h["pct"] for h in holders), 2),
        "synthetic": True,
    }


def _demo_actions(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "actions": [
            {
                "kind": "cash_dividend",
                "label": "Cash dividend",
                "amount": 42.0,
                "cum_date": (dt.date.today() + dt.timedelta(days=12)).isoformat(),
                "ex_date": (dt.date.today() + dt.timedelta(days=13)).isoformat(),
                "status": "announced",
            }
        ],
        "meetings": [
            {
                "date": (dt.date.today() + dt.timedelta(days=30)).isoformat(),
                "time": "10:00",
                "venue": "Jakarta",
                "status": "planned",
                "agenda": "Annual general meeting (RUPS Tahunan)",
            }
        ],
        "synthetic": True,
    }


def mcp_registration_snippets(binary: str = "idx") -> dict[str, str]:
    """Copy-paste registrations for the MCP server, used by the docs tab."""
    return {
        "claude_code": f"claude mcp add --scope user idx -- {binary} mcp",
        "claude_desktop": json.dumps(
            {"mcpServers": {"idx": {"command": binary, "args": ["mcp"]}}}, indent=2
        ),
        "codex": f"codex mcp add idx -- {binary} mcp",
        "cursor": json.dumps({"mcpServers": {"idx": {"command": binary, "args": ["mcp"]}}}, indent=2),
    }
