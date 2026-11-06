"""Telegram control surface: authorisation, nonces, replay, and what it refuses to do."""

from __future__ import annotations

import pytest

from tests.test_autonomy import LiveVenue, _live_engine
from tests.test_api import _ready_payload
from trading_office.config import Settings
from trading_office.telegram_bot import TelegramControl, nonce_for, verify_nonce

OWNER = "424242"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


def _control(engine, *, owner=OWNER, sent=None):
    sent = sent if sent is not None else []

    def transport(method, token, chat_id, body):
        sent.append({"method": method, "body": body})
        return {"ok": True, "result": {"message_id": len(sent)}}

    control = TelegramControl(token="t0ken", owner_chat_id=owner, transport=transport,
                              market="crypto", engine_for=lambda _m: engine)
    return control, sent


def _callback(chat_id, data):
    return {"update_id": 7, "callback_query": {
        "id": "cb1", "data": data, "message": {"chat": {"id": chat_id}, "text": "card"}}}


def _message(chat_id, text):
    return {"update_id": 8, "message": {"chat": {"id": chat_id}, "text": text}}


# --------------------------------------------------------------------------- authorisation
def test_strangers_cannot_command_the_bot(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    control, sent = _control(engine)
    out = control.handle_update(_message("999", "/panic"))
    assert out["ok"] is False and out["reason"] == "not authorised"
    assert control.stats["denied"] == 1
    assert engine.broker.orders == [] and engine.positions == {}
    assert sent == []  # nothing is revealed to a stranger, not even an error message


def test_owner_only_allowlist_is_exact():
    control = TelegramControl(token="t", owner_chat_id="42")
    assert control.is_owner(42) is True
    assert control.is_owner("42") is True
    assert control.is_owner("421") is False
    assert control.is_owner("") is False


# --------------------------------------------------------------------------- approval flow
def test_approval_card_round_trip_fills_the_order(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    request = engine.state.approvals(status="pending")[0]
    control, sent = _control(engine)

    card = control.approval_card(request)
    assert "TEST/USDT:USDT" in card.text and "Approval needed" in card.text
    data = card.keyboard[0][0]["callback_data"]
    assert data.startswith(f"ap:APPROVE:{request['id']}:")
    assert len(data.encode()) <= 64          # Telegram's hard limit on callback data

    out = control.handle_update(_callback(OWNER, data))
    assert out["ok"] is True
    assert "TEST/USDT:USDT" in engine.positions
    assert len(engine.broker.orders) == 1
    assert engine.state.get_approval(request["id"])["status"] == "approved"

    # replaying the same keyboard must not send a second order
    again = control.handle_update(_callback(OWNER, data))
    assert again["ok"] is False
    assert len(engine.broker.orders) == 1


def test_a_tampered_card_is_rejected(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    request = engine.state.approvals(status="pending")[0]
    control, _sent = _control(engine)

    forged = f"ap:APPROVE:{request['id']}:000000"
    out = control.handle_update(_callback(OWNER, forged))
    assert out["ok"] is False and "nonce" in out["reason"]
    assert engine.broker.orders == []
    assert engine.state.get_approval(request["id"])["status"] == "pending"


def test_reject_button_closes_the_request(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    request = engine.state.approvals(status="pending")[0]
    control, _sent = _control(engine)
    data = control.approval_card(request).keyboard[0][1]["callback_data"]

    out = control.handle_update(_callback(OWNER, data))
    assert out["ok"] is True and out.get("rejected") is True
    assert engine.state.get_approval(request["id"])["status"] == "rejected"
    assert engine.broker.orders == []


def test_unknown_request_id_is_refused(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    control, _sent = _control(engine)
    out = control.handle_update(_callback(OWNER, "ap:APPROVE:nope:nope"))
    assert out["ok"] is False and "unknown request" in out["reason"]


def test_nonce_helpers_are_deterministic():
    assert verify_nonce("text", "id:approve", nonce_for("text", "id:approve")) is True
    assert verify_nonce("text", "id:approve", nonce_for("other", "id:approve")) is False
    assert verify_nonce("text", "id:approve", "") is False


# --------------------------------------------------------------------------- commands
def test_manual_entries_are_refused_by_design(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    control, sent = _control(engine)
    out = control.handle_update(_message(OWNER, "/buy BTC/USDT:USDT 1"))
    assert "not supported" in sent[-1]["body"]
    assert engine.broker.orders == []
    assert engine.positions == {}


def test_status_reports_the_guard_and_panic_button(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    control, sent = _control(engine)
    control.handle_update(_message(OWNER, "/status"))
    body = sent[-1]["body"]
    assert "guard" in body and "heartbeat" in body and "entries:" in body
    assert "Flatten all" in body


def test_panic_flattens_everything(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    request = engine.state.approvals(status="pending")[0]
    engine.execute_approved(request["id"], by="test")
    assert engine.positions

    control, _sent = _control(engine)
    out = control.handle_update(_message(OWNER, "/panic"))
    assert out["ok"] is True and out["action"] == "FLAT"
    assert engine.positions == {}


def test_update_offset_advances_so_updates_are_not_replayed(tmp_path, settings):
    engine = _live_engine(tmp_path, settings)
    control, _sent = _control(engine)
    control.handle_update({"update_id": 41, "message": {"chat": {"id": OWNER}, "text": "/help"}})
    assert control._offset == 41
    control.handle_update({"update_id": 40, "message": {"chat": {"id": OWNER}, "text": "/help"}})
    assert control._offset == 41  # never moves backwards


def test_unconfigured_control_reports_why_and_sends_nothing():
    control = TelegramControl(token="", owner_chat_id="")
    assert control.configured is False
    assert "TO_TELEGRAM_TOKEN" in control.status()["reason"]
