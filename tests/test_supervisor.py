"""Supervisor behaviour, tested without spawning servers or sleeping for real."""

from __future__ import annotations

import logging.handlers

import pytest

from trading_office.supervisor import Supervisor, SupervisorConfig, configure_logging


class FakeChild:
    def __init__(self, pid=1, alive=True):
        self.pid = pid
        self.alive = alive
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.alive else (self.returncode if self.returncode is not None else -9)

    def terminate(self):
        self.terminated = True
        self.alive = False
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.alive = False
        self.returncode = -9


def _supervisor(beats, children=None, **over):
    """beats: heartbeat results, consumed in order (the last one repeats).

    ``children`` lists the processes the supervisor *will spawn*; they are returned by the
    spawn spy in order, and the spy always records what it handed out.
    """
    queue = list(beats)
    pending = list(children or [])
    spawned = []

    def probe(_config):
        return queue.pop(0) if len(queue) > 1 else (queue[0] if queue else {"ok": True})

    def spawn(_command):
        child = pending.pop(0) if pending else FakeChild()
        spawned.append(child)
        return child

    cfg = SupervisorConfig(command=["true"], startup_grace_s=0.0, check_interval_s=0.0,
                           max_stalled_checks=3, restart_backoff_s=0.0)
    for key, value in over.items():
        setattr(cfg, key, value)
    sup = Supervisor(cfg, spawn=spawn, probe=probe, sleep=lambda _s: None,
                     clock=lambda: 1000.0)
    return sup, spawned


def test_healthy_child_is_left_alone():
    sup, _ = _supervisor([{"ok": True}])
    child = FakeChild()
    same, _started, healthy = sup.supervise_once(child, started_at=1000.0)
    assert same is child and healthy is True
    assert sup.events == []


def test_exited_child_is_restarted_and_reported():
    sup, spawned = _supervisor([{"ok": True}], children=[FakeChild(pid=2)])
    dead = FakeChild(pid=1, alive=False)
    dead.returncode = 137
    replacement, _started, healthy = sup.supervise_once(dead, started_at=0.0)
    assert healthy is False
    assert replacement is spawned[0]
    assert sup.events[-1]["event"] == "child_exited"
    assert sup.events[-1]["code"] == 137


def test_stalled_loop_is_restarted_only_after_the_configured_number_of_checks():
    sup, spawned = _supervisor([{"ok": False, "silent_s": 900}], children=[FakeChild(pid=2)])
    child = FakeChild()
    sup._stalls = 0
    # the first two failures only warn
    for _ in range(2):
        same, _started, healthy = sup.supervise_once(child, started_at=0.0)
        assert same is child and healthy is True
    same, _started, healthy = sup.supervise_once(child, started_at=0.0)
    assert healthy is False
    assert child.terminated is True
    assert same is spawned[0]
    assert sup.events[-1]["event"] == "restart_stalled_loop"


def test_startup_grace_prevents_restarting_a_slow_boot():
    sup, _ = _supervisor([{"ok": False, "error": "connection refused"}],
                         startup_grace_s=45.0)
    child = FakeChild()
    for _ in range(5):
        same, _started, healthy = sup.supervise_once(child, started_at=990.0)  # 10 s old
        assert same is child and healthy is True
    assert sup.events == []


def test_backoff_is_capped():
    sup, _ = _supervisor([{"ok": True}], restart_backoff_s=2.0, restart_backoff_cap_s=8.0)
    assert sup.backoff(1) == 2.0
    assert sup.backoff(2) == 4.0
    assert sup.backoff(3) == 8.0
    assert sup.backoff(9) == 8.0  # never unbounded


def test_stop_child_terminates_then_kills():
    class Stubborn(FakeChild):
        def terminate(self):
            self.terminated = True  # refuses to die

    sup, _ = _supervisor([{"ok": True}], shutdown_grace_s=0.0)
    stubborn = Stubborn()
    sup.stop_child(stubborn)
    assert stubborn.terminated is True and stubborn.killed is True


def test_restart_notifies_out_of_band():
    messages = []
    sup, spawned = _supervisor([{"ok": True}], children=[FakeChild(pid=2)])
    sup.notifier = messages.append
    sup.supervise_once(FakeChild(alive=False), started_at=0.0)
    assert messages and "child_exited" in messages[0]


def test_logging_rotates_instead_of_growing_without_bound(tmp_path):
    target = tmp_path / "logs" / "x.log"
    info = configure_logging(target, max_bytes=2048, backups=2)
    assert info["file"] == str(target)
    handler = [h for h in logging.getLogger().handlers
               if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert handler and handler[0].maxBytes == 2048 and handler[0].backupCount == 2
    logging.getLogger("trading_office.supervisor").info("hello")
    assert target.exists()
    configure_logging(None)  # leave the global logger clean for other tests
