"""Autonomy gate + approval queue: the pieces that decide whether money can move alone."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.test_api import _ready_payload
from trading_office import autonomy
from trading_office.config import Settings
from trading_office.engine import TradingEngine
from trading_office.execution.broker import Fill


class LiveVenue:
    """A venue that records orders instead of filling them locally.

    This is the fixture that makes the autonomy tests meaningful: the paper broker is
    exempt from approval by design, so anything tested against it would prove nothing.
    """

    name = "ccxt"

    def __init__(self, equity: float = 1000.0):
        self._equity = equity
        self.orders: list[Any] = []
        self.closed: list[tuple[str, str]] = []

    def equity(self, marks=None):
        return self._equity

    def submit(self, order):
        self.orders.append(order)
        return Fill(True, f"v{len(self.orders)}", order.client_id, order.symbol, order.side,
                    order.quantity, order.price_hint, 0.0, "filled", {}, "2026-11-06T10:00:00+00:00")

    def close(self, symbol, price, reason):
        from trading_office.execution.broker import ClosedTrade

        self.closed.append((symbol, reason))
        return ClosedTrade(symbol=symbol, market="crypto", side="long", entry=100.0,
                           exit=price or 100.0, size=1.0, pnl=0.0, fees=0.0, r_multiple=0.0,
                           reason=reason, opened_at="", closed_at="2026-11-06T10:00:00+00:00")


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


def _engine(tmp_path, settings, market="crypto", **kwargs):
    return TradingEngine(market=market, settings=settings, symbols=["TEST/USDT:USDT"],
                         state_dir=tmp_path / "auto", **kwargs)


def _live_engine(tmp_path, settings, *, mode: str = "ask", armed: bool = True):
    """An engine wired to a fake live venue, with the autonomy mode under test."""
    raw = {**settings.raw, "autonomy": {"mode": mode, "approval_ttl_s": 1800}}
    engine = _engine(tmp_path, Settings(raw=raw, path=settings.path))
    engine.broker = LiveVenue()
    engine.mode = "live"
    if armed:
        engine.state.set_setting("live_entry_grant", {
            "armed": True,
            "fingerprint": autonomy.execution_gate_fingerprint(),
            "limits": autonomy.execution_gate_limits(engine.settings),
            "armed_by": "test", "armed_at": "2026-11-06T00:00:00+00:00",
        })
    return engine


# --------------------------------------------------------------------------- fingerprint gate
def test_paper_entries_are_always_allowed():
    gate = autonomy.evaluate(is_paper=True, armed={}, limits={})
    assert gate["allowed"] is True and gate["gate"] == "paper"


def test_live_entries_need_an_arm():
    limits = {"max_daily_loss_pct": 2.0}
    gate = autonomy.evaluate(is_paper=False, armed={}, limits=limits)
    assert gate["allowed"] is False and gate["gate"] == "armed"
    assert "arm-live" in gate["reason"]


def test_arm_is_invalidated_by_a_gate_change_and_by_a_limit_change():
    limits = {"max_daily_loss_pct": 2.0}
    fingerprint = autonomy.execution_gate_fingerprint()
    armed = {"armed": True, "fingerprint": fingerprint, "limits": limits, "armed_by": "test"}
    assert autonomy.evaluate(is_paper=False, armed=armed, limits=limits)["allowed"] is True

    changed_gate = {**armed, "fingerprint": "deadbeefdeadbeef"}
    out = autonomy.evaluate(is_paper=False, armed=changed_gate, limits=limits)
    assert out["allowed"] is False and out["gate"] == "fingerprint"

    changed_limits = {**armed, "limits": {"max_daily_loss_pct": 10.0}}
    out = autonomy.evaluate(is_paper=False, armed=changed_limits, limits=limits)
    assert out["allowed"] is False and out["gate"] == "limits"


def test_fingerprint_tracks_the_gate_source_not_the_caller(tmp_path, settings):
    engine = _engine(tmp_path, settings)
    first = autonomy.execution_gate_fingerprint()
    assert engine._remote_entry_allowed()["gate"] == "paper"
    # the gate payload exposes the decision, so the UI/doctor can show it
    entries = engine.execution_gate()["entries"]
    assert entries["allowed"] is True and entries["gate"] == "paper"
    assert autonomy.execution_gate_fingerprint() == first


def test_limits_are_read_from_settings():
    settings = Settings(raw={**Settings.load().raw,
                             "engine": {"max_daily_loss_pct": 1.25, "max_open_positions": 2}})
    limits = autonomy.execution_gate_limits(settings)
    assert limits["max_daily_loss_pct"] == pytest.approx(1.25)
    assert limits["max_open_positions"] == pytest.approx(2)


# --------------------------------------------------------------------------- approval queue
def test_live_entry_is_parked_not_sent(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    payload = _ready_payload(settings)
    engine._handle_entry("TEST/USDT:USDT", payload)

    assert "TEST/USDT:USDT" not in engine.positions
    assert engine.broker.orders == []          # nothing reached the venue
    queued = engine.state.approvals(status="pending")
    assert len(queued) == 1
    request = queued[0]
    assert request["symbol"] == "TEST/USDT:USDT"
    assert request["plan"]["stop"] > 0
    assert request["expires_at"] > request["created_at"]
    assert payload["execution"]["queued_for_approval"]["id"] == request["id"]


def test_approved_request_fills_and_is_not_replayable(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    request = engine.state.approvals(status="pending")[0]

    result = engine.execute_approved(request["id"], by="test")
    assert result["ok"] is True
    assert "TEST/USDT:USDT" in engine.positions
    assert len(engine.broker.orders) == 1
    assert engine.state.get_approval(request["id"])["status"] == "approved"

    again = engine.execute_approved(request["id"], by="test")
    assert again["ok"] is False and "approved" in again["reason"]
    assert len(engine.positions) == 1 and len(engine.broker.orders) == 1


def test_expired_request_refuses_to_execute(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    request = engine.state.approvals(status="pending")[0]
    stored = engine.state.get_approval(request["id"])
    stored["expires_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
    engine.state.add_approval(stored)

    result = engine.execute_approved(request["id"], by="test")
    assert result["ok"] is False and "expired" in result["reason"]
    assert "TEST/USDT:USDT" not in engine.positions
    assert engine.broker.orders == []


def test_rejection_is_final_and_logged(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    request = engine.state.approvals(status="pending")[0]

    assert engine.reject_approval(request["id"], by="test", reason="spread is ugly") is True
    assert engine.state.get_approval(request["id"])["status"] == "rejected"
    assert engine.execute_approved(request["id"])["ok"] is False
    assert engine.reject_approval(request["id"], by="test") is False
    assert engine.broker.orders == []


def test_unarmed_live_venue_is_blocked_before_the_queue(tmp_path, settings):
    engine = _live_engine(tmp_path, settings, armed=False)
    payload = _ready_payload(settings)
    engine._handle_entry("TEST/USDT:USDT", payload)
    assert engine.state.approvals(status="pending") == []
    assert "autonomy gate" in payload["execution"]["skipped"]
    assert engine.broker.orders == []


def test_auto_mode_sends_only_when_armed(tmp_path, settings):
    armed = _live_engine(tmp_path / "a", settings, mode="auto", armed=True)
    armed._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    assert len(armed.broker.orders) == 1 and armed.state.approvals(status="pending") == []

    unarmed = _live_engine(tmp_path / "b", settings, mode="auto", armed=False)
    unarmed._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    assert unarmed.broker.orders == []


def test_paper_mode_never_queues(tmp_path, settings):
    engine = _engine(tmp_path, settings)
    assert engine.approval_required() is False
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    assert "TEST/USDT:USDT" in engine.positions
    assert engine.state.approvals(status="pending") == []
