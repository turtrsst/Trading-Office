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
    """Write JSON atomically, encrypting at rest when a key is configured."""
    from . import secure_state

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(secure_state.dump_json(payload))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: Path, default: Any) -> Any:
    from . import secure_state

    if not path.exists():
        return default
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return default
    # StateLocked propagates on purpose: a wrong key must stop the bot, never look empty.
    return secure_state.load_json(raw, default)


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

    @property
    def settings_path(self) -> Path:
        return self.root / "runtime.json"

    @property
    def approvals_path(self) -> Path:
        return self.root / "approvals.json"

    @property
    def snapshot_path(self) -> Path:
        return self.root / "snapshot.json"

    @property
    def reconciled_path(self) -> Path:
        return self.root / "snapshot_reconciled.json"

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

    def stamp_decision(self, symbol: str, timeframe: str, bar_time: str,
                       extra: dict[str, Any]) -> bool:
        """Annotate an existing bar lock (e.g. who approved it). Never creates one."""
        with self._lock:
            data = _read_json(self.signals_path, {})
            key = self.key(symbol, timeframe, bar_time)
            if key not in data:
                return False
            data[key].update(extra)
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

    # ------------------------------------------------------------------ snapshots
    def snapshot_state(self, source: str = "local") -> dict[str, Any]:
        """Everything ``startup_sync`` must be able to put back, in one dict.

        A restart (or a crash mid-partial) must not depend on reconstructing anything from
        the ledger: the exact local truth is written here before the first order and
        updated on every position write.
        """
        return {
            "source": source,
            "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "positions": self.positions(),
            "last_exit_bar": _read_json(self.cooldowns_path, {}),
        }

    def save_snapshot(self, snapshot: dict[str, Any], source: str = "local") -> None:
        with self._lock:
            payload = {
                "source": source,
                "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "positions": snapshot.get("positions") or {},
                "last_exit_bar": snapshot.get("last_exit_bar") or {},
            }
            if source == "local":
                _atomic_write(self.snapshot_path, payload)
            else:
                _atomic_write(self.reconciled_path, payload)
                self.last_source = source

    def load_snapshot(self, prefer_reconciled: bool = True) -> dict[str, Any]:
        """Local truth, with the reconciled venue truth preferred when it is newer."""
        local = _read_json(self.snapshot_path, {}) or {}
        venue = _read_json(self.reconciled_path, {}) if prefer_reconciled else {}
        if not venue:
            return {**local, "source": local.get("source") or "local"}
        if not local:
            return venue
        return venue if str(venue.get("saved_at") or "") >= str(local.get("saved_at") or "") else local

    def restore_positions(self, positions: dict[str, Any]) -> int:
        with self._lock:
            _atomic_write(self.positions_path, positions or {})
        return len(positions or {})

    # ------------------------------------------------------------------ ledger
    def append_ledger(self, event: dict[str, Any]) -> None:
        with self._lock:
            from . import secure_state

            event = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), **event}
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.ledger_path, "ab") as fh:
                fh.write(secure_state.encrypt_bytes(
                    (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")
                ))

    def ledger(self, limit: int = 200) -> list[dict[str, Any]]:
        from . import secure_state

        if not self.ledger_path.exists():
            return []
        raw = secure_state.decrypt_bytes(self.ledger_path.read_bytes())
        lines = raw.decode("utf-8", errors="replace").splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def closed_trades(self) -> list[dict[str, Any]]:
        return [e for e in self.ledger(limit=100_000) if e.get("event") == "trade_closed"]

    # ------------------------------------------------------------------ small settings
    def get_setting(self, key: str) -> Any:
        """Runtime flags that must survive a restart (kill-switch resume, etc.)."""
        with self._lock:
            return _read_json(self.settings_path, {}).get(key)

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            data = _read_json(self.settings_path, {})
            data[key] = value
            _atomic_write(self.settings_path, data)

    # ------------------------------------------------------------------ approvals
    def approvals(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Pending/decided trade requests, newest first (bounded file, see _PRUNE)."""
        with self._lock:
            data = _read_json(self.approvals_path, {}) or {}
            rows = list((data.get("requests") or {}).values())
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def get_approval(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = _read_json(self.approvals_path, {}) or {}
            return (data.get("requests") or {}).get(request_id)

    def add_approval(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = _read_json(self.approvals_path, {}) or {}
            requests = data.setdefault("requests", {})
            requests[request["id"]] = request
            # keep the file bounded: decisions are also in the ledger, this is the queue
            if len(requests) > 200:
                for old in sorted(requests.values(), key=lambda r: str(r.get("created_at") or ""))[:100]:
                    requests.pop(old["id"], None)
            _atomic_write(self.approvals_path, {"requests": requests})
        return request

    def resolve_approval(self, request_id: str, status: str, *, by: str = "",
                         reason: str = "") -> bool:
        with self._lock:
            data = _read_json(self.approvals_path, {}) or {}
            requests = data.get("requests") or {}
            request = requests.get(request_id)
            if not request:
                return False
            if request.get("status") not in ("pending",):
                return False
            request.update({
                "status": status, "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "decided_by": by, "decision_reason": reason,
            })
            requests[request_id] = request
            _atomic_write(self.approvals_path, {"requests": requests})
        return True

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
            for path in (self.signals_path, self.positions_path, self.cooldowns_path,
                         self.account_path, self.settings_path, self.snapshot_path,
                         self.reconciled_path, self.approvals_path):
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
