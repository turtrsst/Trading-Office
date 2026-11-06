"""Persistent state: bar locks, open positions, the trade ledger and the audit trail.

Everything is JSON so a human can read (and diff) it. Writes are atomic (tmp + replace)
and the ledger is append-only JSONL.

State layout (under ``engine.state_dir``, default ``state/``)::

    state/
      signals.json     # {symbol|timeframe|bar_time: decision} — one decision per bar
      positions.json   # open positions, keyed by symbol
      cooldowns.json   # {symbol: last_exit_bar_time}
      ledger.jsonl     # every fill, close, stop move and rejection
      account.json     # equity snapshots / daily P&L
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


@dataclass
class StateStore:
    """Thread-safe JSON state for one engine instance."""

    root: Path
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    @property
    def signals_path(self) -> Path:
        return self.root / "signals.json"

    @property
    def positions_path(self) -> Path:
        return self.root / "positions.json"

    @property
    def cooldowns_path(self) -> Path:
        return self.root / "cooldowns.json"

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.jsonl"

    @property
    def account_path(self) -> Path:
        return self.root / "account.json"

    # ------------------------------------------------------------------ bar locks
    @staticmethod
    def key(symbol: str, timeframe: str, bar_time: str) -> str:
        return f"{symbol}|{timeframe}|{bar_time}"

    def is_locked(self, symbol: str, timeframe: str, bar_time: str) -> bool:
        with self._lock:
            data = _read_json(self.signals_path, {})
            return self.key(symbol, timeframe, bar_time) in data

    def lock(self, symbol: str, timeframe: str, bar_time: str, decision: dict[str, Any]) -> bool:
        """Record the decision for a bar. Returns False if the bar was already decided."""
        with self._lock:
            data = _read_json(self.signals_path, {})
            k = self.key(symbol, timeframe, bar_time)
            if k in data:
                return False
            data[k] = {**decision, "decided_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            # keep the file bounded: last 5000 decisions
            if len(data) > 5000:
                for old_key in list(data.keys())[: len(data) - 5000]:
                    data.pop(old_key, None)
            _atomic_write(self.signals_path, data)
            return True

    # ------------------------------------------------------------------ positions
    def positions(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return _read_json(self.positions_path, {})

    def save_position(self, symbol: str, position: dict[str, Any]) -> None:
        with self._lock:
            data = self.positions()
            data[symbol] = position
            _atomic_write(self.positions_path, data)

    def drop_position(self, symbol: str, exit_bar: str | None = None) -> None:
        with self._lock:
            data = self.positions()
            data.pop(symbol, None)
            _atomic_write(self.positions_path, data)
            if exit_bar:
                cooldowns = _read_json(self.cooldowns_path, {})
                cooldowns[symbol] = exit_bar
                _atomic_write(self.cooldowns_path, cooldowns)

    def last_exit_bar(self, symbol: str) -> str | None:
        with self._lock:
            return _read_json(self.cooldowns_path, {}).get(symbol)

    # ------------------------------------------------------------------ ledger
    def append_ledger(self, event: dict[str, Any]) -> None:
        with self._lock:
            event = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), **event}
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.ledger_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def ledger(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def closed_trades(self) -> list[dict[str, Any]]:
        return [e for e in self.ledger(limit=100_000) if e.get("event") == "trade_closed"]

    # ------------------------------------------------------------------ account
    def account(self) -> dict[str, Any]:
        with self._lock:
            return _read_json(self.account_path, {})

    def save_account(self, account: dict[str, Any]) -> None:
        with self._lock:
            _atomic_write(self.account_path, account)

    # ------------------------------------------------------------------ helpers
    def reset(self, *, keep_ledger: bool = True) -> None:
        with self._lock:
            for path in (self.signals_path, self.positions_path, self.cooldowns_path, self.account_path):
                if path.exists():
                    path.unlink()
            if not keep_ledger and self.ledger_path.exists():
                self.ledger_path.unlink()


def bar_key_of(bar: dict[str, Any]) -> str:
    """ISO string identity of a closed bar (works for ccxt and index candles)."""
    value = bar.get("time") or bar.get("date") or bar.get("timestamp")
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value) / 1000.0, dt.timezone.utc).isoformat()
    return str(value or "")


def bars_between(a: str | None, b: str | None) -> int | None:
    """Number of bars between two ISO timestamps — used for the cooldown check."""
    if not a or not b:
        return None
    try:
        ta = dt.datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = dt.datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int(abs((tb - ta).total_seconds())))


def tail(iterable: Iterable[Any], n: int) -> list[Any]:
    values = list(iterable)
    return values[-n:] if n > 0 else values
