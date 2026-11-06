"""Process supervisor: keeps the web terminal + engine loop alive, and proves it.

Why not just ``systemd``/NSSM
----------------------------
Both are fine at "restart the process if it dies". Neither notices a process that is
*alive but not cycling* — the classic 24/7 bot failure: the web server answers, the badge
is green, and no stop has been managed for an hour. This supervisor adds the missing layer:

1. **Restart with backoff** when the child exits (crash, OOM, bad deploy), capped so a
   crash loop cannot spin the CPU or hammer the exchange on every restart.
2. **Liveness probe**, not just process liveness: it polls ``/api/engine/heartbeat`` and,
   after N consecutive failures, kills and restarts the child. The engine's own watchdog
   knows what a stale loop looks like; this process is what acts on it.
3. **Bounded logs.** RotatingFileHandler instead of an ever-growing redirect, because the
   first thing a 24/7 deployment runs out of is disk, not CPU.
4. **Out-of-band alert on restart.** A restart is an incident; if nobody is told, a bot
   that has been restarting every 10 minutes for a day looks "fine" in the morning.

Everything is injectable (clock, sleeper, process factory, probe) so the offline suite can
test the decision logic without spawning servers.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

log = logging.getLogger("trading_office.supervisor")


def configure_logging(path: str | os.PathLike[str] | None, *, level: str = "INFO",
                      max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> dict[str, Any]:
    """Rotating file + stdout. Returns what was configured (for the CLI report)."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    info: dict[str, Any] = {"file": None, "max_bytes": max_bytes, "backups": backups}
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
        info["file"] = str(target)
    return info


@dataclass
class SupervisorConfig:
    command: Sequence[str]
    host: str = "127.0.0.1"
    port: int = 8000
    market: str = "crypto"
    #: consecutive failed heartbeats before the child is killed and restarted
    max_stalled_checks: int = 3
    check_interval_s: float = 20.0
    restart_backoff_s: float = 2.0
    restart_backoff_cap_s: float = 120.0
    #: give the child this long to answer its first heartbeat after a (re)start
    startup_grace_s: float = 45.0
    shutdown_grace_s: float = 10.0
    log_file: str | None = None


@dataclass
class Supervisor:
    config: SupervisorConfig
    # injectables (tests pass fakes; production uses the defaults below)
    spawn: Callable[[Sequence[str]], Any] | None = None
    probe: Callable[[SupervisorConfig], dict[str, Any]] | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    notifier: Callable[[str], Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ probes
    def heartbeat(self) -> dict[str, Any]:
        if self.probe is not None:
            return self.probe(self.config)
        return _http_heartbeat(self.config)

    def _record(self, event: str, **detail: Any) -> None:
        row = {"event": event, "at": self.clock(), **detail}
        self.events.append(row)
        log.warning("supervisor: %s %s", event, detail or "")
        if self.notifier is not None:
            try:
                self.notifier(f"[SUPERVISOR] {event}: {detail}")
            except Exception:  # pragma: no cover - alerting must never crash the supervisor
                pass

    # ------------------------------------------------------------------ one cycle
    def supervise_once(self, child: Any, *, started_at: float) -> tuple[Any, float, bool]:
        """One supervision step.

        Returns ``(child, started_at, healthy)``. A restarted child is a new object, so the
        caller replaces what it holds. ``healthy`` is False when the child was replaced.
        """
        if child is None or child.poll() is not None:
            code = None if child is None else child.returncode
            self._record("child_exited", code=code)
            return self.spawn_child(), self.clock(), False

        beat = self.heartbeat()
        # a child that has not finished starting up is not stalled yet
        if not beat.get("ok", True):
            if self.clock() - started_at < self.config.startup_grace_s:
                return child, started_at, True
            self._stalls = getattr(self, "_stalls", 0) + 1
            if self._stalls < self.config.max_stalled_checks:
                log.warning("heartbeat failed (%s/%s): %s", self._stalls,
                            self.config.max_stalled_checks, beat)
                return child, started_at, True
            self._record("restart_stalled_loop", heartbeat=beat,
                         checks=self._stalls)
            self.stop_child(child)
            self._stalls = 0
            return self.spawn_child(), self.clock(), False

        self._stalls = 0
        return child, started_at, True

    # ------------------------------------------------------------------ process control
    def spawn_child(self) -> Any:
        factory = self.spawn or _spawn_subprocess
        child = factory(self.config.command)
        log.info("supervisor: started %s", " ".join(self.config.command))
        return child

    def stop_child(self, child: Any) -> None:
        if child is None:
            return
        try:
            if child.poll() is None:
                child.terminate()
                deadline = self.clock() + self.config.shutdown_grace_s
                while child.poll() is None and self.clock() < deadline:
                    self.sleep(0.2)
                if child.poll() is None:
                    child.kill()
        except Exception as exc:  # pragma: no cover
            log.error("supervisor: could not stop child cleanly: %s", exc)

    def backoff(self, restarts: int) -> float:
        return min(self.config.restart_backoff_cap_s,
                   self.config.restart_backoff_s * (2 ** max(0, restarts - 1)))

    # ------------------------------------------------------------------ main loop
    def run(self, *, max_restarts: int | None = None) -> int:
        child = self.spawn_child()
        started_at = self.clock()
        restarts = 0
        stopping = {"flag": False}

        def handle(signum, _frame):  # pragma: no cover - signal path
            stopping["flag"] = True
            self._record("signal", signal=signum)

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)
        try:
            while not stopping["flag"]:
                child, started_at, healthy = self.supervise_once(child, started_at=started_at)
                if not healthy:
                    restarts += 1
                    if max_restarts is not None and restarts > max_restarts:
                        self._record("giving_up", restarts=restarts)
                        return 1
                    delay = self.backoff(restarts)
                    log.info("supervisor: restart #%s in %.1fs", restarts, delay)
                    self.sleep(delay)
                else:
                    self.sleep(self.config.check_interval_s)
        finally:
            self.stop_child(child)
        return 0


# ---------------------------------------------------------------------- defaults
def _spawn_subprocess(command: Sequence[str]) -> Any:
    return subprocess.Popen(list(command))  # noqa: S603 - the command comes from our own CLI


def _http_heartbeat(config: SupervisorConfig) -> dict[str, Any]:
    """Poll the API. Any answer that is not ok=True counts as a failed beat."""
    import json
    import urllib.error
    import urllib.request

    url = f"http://{config.host}:{config.port}/api/engine/heartbeat?market={config.market}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def serve_command(*, python: str | None = None, host: str, port: int,
                  log_level: str = "info") -> list[str]:
    return [python or sys.executable, "-m", "uvicorn", "trading_office.api.server:app",
            "--host", host, "--port", str(port), "--log-level", log_level]
