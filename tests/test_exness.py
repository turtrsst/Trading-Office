"""Exness Trader API adapter, tested against a stub that actually verifies the signature.

The stub is the point of this file. A signer tested against its own output proves nothing;
these tests stand up a local HTTP server that rebuilds the signed payload, verifies the
Ed25519 signature over `EXN-DATA`, and checks that the signed path and body hash match the
request that arrived. That is the only way to catch the classic failure: a signature that is
valid for a *different* body than the one sent.

No network, no credentials. The private key is generated in the test.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from trading_office.execution import exness  # noqa: E402
from trading_office.execution.broker import Order  # noqa: E402
from trading_office.execution.exness import (  # noqa: E402
    EMPTY_BODY_HASH,
    ExnessBroker,
    ExnessClient,
    ExnessError,
    ExnessSigner,
    FxInstrument,
    body_hash,
    lots_for_risk,
    verify_headers,
)


def _signer(seed: bytes = b"\x01" * 32) -> ExnessSigner:
    return ExnessSigner(api_key="exnsk-public-test",
                        private_key=Ed25519PrivateKey.from_private_bytes(seed))


# --------------------------------------------------------------------------- signing
def test_empty_body_hash_matches_the_documented_constant():
    assert body_hash(None) == EMPTY_BODY_HASH
    assert body_hash(b"") == EMPTY_BODY_HASH


def test_headers_round_trip_and_agree_with_each_other():
    signer = _signer()
    path = "/v1/trading/access-point?account_id=123456"
    headers = signer.headers("GET", path, timestamp_ms=1773656070123)

    payload = verify_headers(signer.private_key.public_key(), headers, "GET", path)

    assert payload["path"] == path                      # query string signed verbatim
    assert payload["body_hash"] == EMPTY_BODY_HASH
    assert headers["EXN-IDEMPOTENCY-KEY"] == ""         # required, and must be empty for GET
    assert headers["EXN-SIGN-VERSION"] == "1"
    assert "=" not in headers["EXN-DATA"] and "=" not in headers["EXN-SIGN"]


def test_a_signature_is_bound_to_its_own_body_and_path():
    signer = _signer()
    path = "/v1/trading/accounts/1/positions"
    body = json.dumps({"instrument": "XAUUSD", "side": "buy", "volume": "0.01"}).encode()
    headers = signer.headers("POST", path, body, idempotency_key="order-1")

    with pytest.raises(ValueError, match="body hash"):
        verify_headers(signer.private_key.public_key(), headers, "POST", path,
                       body=json.dumps({"instrument": "XAUUSD", "side": "buy", "volume": "1.00"}).encode())
    with pytest.raises(ValueError, match="path"):
        verify_headers(signer.private_key.public_key(), headers, "POST", path + "?x=1", body=body)


def test_get_needs_an_empty_idempotency_key_and_post_needs_a_non_empty_one():
    signer = _signer()
    with pytest.raises(ValueError, match="empty EXN-IDEMPOTENCY-KEY"):
        signer.headers("GET", "/v1/x", idempotency_key="nope")
    with pytest.raises(ValueError, match="non-empty"):
        signer.headers("POST", "/v1/x", b"{}", idempotency_key="")


def test_mutating_requests_default_to_a_usable_idempotency_key():
    headers = _signer().headers("POST", "/v1/x", b"{}")
    assert headers["EXN-IDEMPOTENCY-KEY"]                   # otherwise the venue rejects it


def test_from_seed_hex_builds_the_same_signature_as_from_bytes():
    seed = "11" * 32
    pem = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
    signer = ExnessSigner.from_seed_hex("k", seed)
    assert isinstance(signer.private_key, type(pem))
    headers = signer.headers("GET", "/v1/x", timestamp_ms=1)
    verify_headers(pem.public_key(), headers, "GET", "/v1/x")


# --------------------------------------------------------------------------- instrument rules
def test_volume_is_rounded_down_to_the_step_never_up():
    gold = FxInstrument("XAUUSD", volume_min=0.01, volume_step=0.01, contract_size=100)
    assert gold.normalise_volume(0.087) == (0.08, "")        # down, not 0.09
    lots, reason = gold.normalise_volume(0.004)
    assert lots == 0.0 and "below the venue minimum" in reason
    assert gold.normalise_volume(250)[0] == 0.0              # above max is refused, not capped


def test_risk_sizing_refuses_instead_of_bumping_to_the_minimum_lot():
    """The real constraint: a small account cannot express a small risk on gold."""
    gold = FxInstrument("XAUUSD", volume_min=0.01, volume_step=0.01, contract_size=100)
    plan = lots_for_risk(equity=1000, risk_pct=0.5, entry=2650, stop=2632, instrument=gold)
    assert plan["ok"] is False
    assert "below the venue minimum" in plan["reason"]

    # the same stop on a bigger account fits the 0.01 step, and reports the real risk %
    bigger = lots_for_risk(equity=10_000, risk_pct=0.5, entry=2650, stop=2632, instrument=gold)
    assert bigger["ok"] is True
    assert bigger["lots"] == 0.02
    assert bigger["risk_pct_actual"] <= 0.5 + 1e-9


def test_margin_cap_blocks_a_plan_that_fits_the_lot_step_but_not_the_budget():
    gold = FxInstrument("XAUUSD", volume_min=0.01, volume_step=0.01, contract_size=100,
                        margin_rate=0.0333)
    plan = lots_for_risk(equity=500, risk_pct=2.0, entry=2650, stop=2640, instrument=gold,
                         max_margin_pct=8)
    assert plan["ok"] is False
    assert "margin" in plan["reason"]


# --------------------------------------------------------------------------- stub venue
class _Venue:
    """A local Exness-shaped server that verifies what it receives."""

    def __init__(self, public_key):
        self.public_key = public_key
        self.calls: list[dict] = []
        self.final_status = "filled"
        self.final_price = 2651.25
        self.fail_with: tuple[int, dict] | None = None
        self.final_after_polls = 0

    def handler(self):
        venue = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                path = self.path
                try:
                    payload = verify_headers(venue.public_key, dict(self.headers), method, path, body)
                except Exception as exc:  # noqa: BLE001 - the point is to report it
                    venue.calls.append({"path": path, "auth_error": str(exc)})
                    return self._reply(401, {"error_code": 1001, "error_message": f"AUTH_INVALID_SIGNATURE: {exc}"})
                venue.calls.append({"path": path, "method": method, "payload": payload,
                                    "body": json.loads(body) if body else None})
                if venue.fail_with:
                    return self._reply(*venue.fail_with)
                if path.startswith("/v1/trading/access-point"):
                    return self._reply(200, {"access_point": venue.base, "account_id": "777"})
                if path.endswith("/account"):
                    return self._reply(200, {"account_id": "777", "balance": "1000.00",
                                             "equity": "1012.50", "currency": "USD",
                                             "leverage": "30", "trade_mode": "full"})
                if path.endswith("/positions") and method == "GET":
                    return self._reply(200, {"positions": [
                        {"position_id": "p1", "instrument": "XAUUSD", "side": "buy",
                         "volume": "0.01", "open_price": "2650.00", "profit": "1.25",
                         "stop_loss": "2632.00", "take_profit": "2686.00"}]})
                if path.endswith("/positions") and method == "POST":
                    return self._reply(200, {"operation_id": "789",
                                             "client_request_id": (json.loads(body) or {}).get("x", "order"),
                                             "status": "accepted"})
                if "/operations/" in path:
                    polls = getattr(venue, "_polls", 0)
                    venue._polls = polls + 1
                    if polls < venue.final_after_polls:
                        return self._reply(200, {"operation_id": "789", "status": "pending"})
                    return self._reply(200, {"operation_id": "789", "status": venue.final_status,
                                             "price": venue.final_price, "commission": "-0.07"})
                if path.endswith("/close"):
                    return self._reply(200, {"operation_id": "790", "status": "accepted"})
                return self._reply(404, {"error_message": "no such route"})

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def log_message(self, *args):
                pass

        return Handler


@pytest.fixture()
def venue():
    signer = _signer(b"\x02" * 32)
    stub = _Venue(signer.private_key.public_key())
    server = HTTPServer(("127.0.0.1", 0), stub.handler())
    stub.base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield signer, stub
    finally:
        server.shutdown()
        server.server_close()


def _client(signer, stub, **over):
    client = ExnessClient(signer=signer, account_id="777", gateway_host=stub.base, timeout=5, **over)
    return client


def _gold():
    return FxInstrument("XAUUSD", volume_min=0.01, volume_step=0.01, contract_size=100,
                        margin_rate=0.0333)


# --------------------------------------------------------------------------- client
def test_discovery_is_cached_and_uses_the_gateway_host(venue):
    signer, stub = venue
    client = _client(signer, stub)

    first = client.resolve_access_point()
    second = client.resolve_access_point()

    assert first == stub.base == second
    discovery_calls = [c for c in stub.calls if "access-point" in c["path"]]
    assert len(discovery_calls) == 1, "discovery is rate limited: it must be cached"
    assert discovery_calls[0]["payload"]["path"] == "/v1/trading/access-point?account_id=777"


def test_every_call_reaches_the_stub_with_a_valid_signature(venue):
    signer, stub = venue
    client = _client(signer, stub)

    client.account()
    client.positions()

    assert all("auth_error" not in c for c in stub.calls), stub.calls
    assert [c["path"].split("?")[0].rsplit("/", 1)[-1] for c in stub.calls] == [
        "access-point", "account", "positions"]


def test_a_tampered_body_is_refused_by_the_venue(venue):
    """Proves the stub really verifies: signing body A and sending body B must 401."""
    signer, stub = venue
    client = _client(signer, stub)
    client.resolve_access_point()

    original = signer.headers("POST", "/v1/trading/accounts/777/positions", b'{"volume":"0.01"}',
                              idempotency_key="k1")
    swapped = dict(original)
    swapped["EXN-DATA"] = signer.headers("POST", "/v1/trading/accounts/777/positions",
                                         b'{"volume":"9.00"}', idempotency_key="k1")["EXN-DATA"]
    status, data = exness._default_transport(
        "POST", stub.base + "/v1/trading/accounts/777/positions", swapped, b'{"volume":"0.01"}', 5)

    assert status == 401
    assert data["error_code"] == 1001


def test_venue_errors_carry_an_actionable_hint(venue):
    signer, stub = venue
    client = _client(signer, stub)
    client.resolve_access_point()
    stub.fail_with = (400, {"error_code": 5000, "error_message": "invalid volume"})

    with pytest.raises(ExnessError) as excinfo:
        client.positions()

    assert "volume_min" in str(excinfo.value)
    assert excinfo.value.code == 5000


# --------------------------------------------------------------------------- broker
def test_submit_waits_for_the_final_state_and_reports_a_real_fill(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold(), equity_value=1000)

    fill = broker.submit(Order(symbol="XAUUSD", side="buy", quantity=0.02, price_hint=2650.0,
                               stop=2632.0, target=2686.0, client_id="to-1", market="fx"))

    assert fill.ok is True, fill.message
    assert fill.price == 2651.25
    assert fill.quantity == 0.02
    assert fill.order_id == "789"
    assert fill.fee == 0.07
    sent = next(c for c in stub.calls if c["method"] == "POST")
    assert sent["body"]["instrument"] == "XAUUSD"
    assert sent["body"]["volume"] == "0.02"
    assert sent["payload"]["idempotency_key"] == "to-1"
    assert sent["body"]["stop_loss"] == 2632.0        # the hard stop travels with the order


def test_an_accepted_order_that_never_finalises_is_not_reported_as_a_fill(venue):
    """The ACK is not the result: 'pending' must never become a phantom position."""
    signer, stub = venue
    stub.final_status = "pending"
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    fill = broker.submit(Order(symbol="XAUUSD", side="buy", quantity=0.01, price_hint=2650.0,
                               client_id="to-2", market="fx"))

    assert fill.ok is False
    assert "timeout" in fill.message or "pending" in fill.message


def test_a_rejected_order_does_not_invent_a_price(venue):
    signer, stub = venue
    stub.final_status = "rejected"
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    fill = broker.submit(Order(symbol="XAUUSD", side="sell", quantity=0.01, price_hint=2650.0,
                               client_id="to-3", market="fx"))

    assert fill.ok is False
    assert fill.price == 0.0
    assert "rejected" in fill.message


def test_volume_is_refused_before_anything_is_signed(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    fill = broker.submit(Order(symbol="XAUUSD", side="buy", quantity=0.004, price_hint=2650.0,
                               client_id="to-4", market="fx"))

    assert fill.ok is False
    assert "below the venue minimum" in fill.message
    assert not [c for c in stub.calls if c["method"] == "POST"], "no request should have been sent"


def test_duplicate_client_id_is_refused_at_the_adapter(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())
    order = Order(symbol="XAUUSD", side="buy", quantity=0.01, price_hint=2650.0,
                  client_id="to-5", market="fx")

    assert broker.submit(order).ok is True
    second = broker.submit(order)

    assert second.ok is False
    assert "duplicate" in second.message
    assert len([c for c in stub.calls if c["method"] == "POST"]) == 1


def test_an_unconfigured_venue_raises_instead_of_falling_back_to_paper():
    class Settings:
        def get(self, dotted, default=None):
            return default

    with pytest.raises(RuntimeError, match="refusing to fall back to paper"):
        exness.from_settings(Settings())


def test_positions_are_normalised_for_the_engine(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    rows = broker.positions()

    assert rows[0]["symbol"] == "XAUUSD"
    assert rows[0]["lots"] == 0.01
    assert rows[0]["stop"] == 2632.0
    assert rows[0]["target"] == 2686.0


def test_equity_comes_from_the_venue_not_from_a_default(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold(), equity_value=1.0)
    assert broker.equity() == 1012.50


def test_close_symbol_reports_what_it_actually_closed(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    result = broker.close_symbol("XAUUSD")

    assert result["ok"] is True
    assert result["volume"] == 0.01
    assert result["operation_id"] == "790"


def test_base64url_helpers_are_padding_free():
    text = exness.b64url(b"any bytes at all")
    assert "=" not in text
    assert exness.unb64url(text) == b"any bytes at all"
    assert base64.urlsafe_b64decode(text + "==") == b"any bytes at all"


# --------------------------------------------------------------------------- doctor + cli
def test_doctor_reports_presence_never_the_secret(monkeypatch):
    from trading_office import cli
    from trading_office.config import Settings

    settings = Settings.load()
    for name in ("TO_EXNESS_API_KEY", "TO_EXNESS_PRIVATE_KEY", "TO_EXNESS_ACCOUNT_ID",
                 "TO_EXNESS_GATEWAY_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TO_EXNESS_API_KEY", "exnsk-very-secret-public-id")
    name, ok, detail = cli._exness_check(settings)
    assert name == "exness"
    assert ok is False                                  # incomplete is a WARN, not a pass
    assert "exnsk-very-secret-public-id" not in detail  # never echo the whole key
    assert "PRIVATE_KEY" in detail


def test_doctor_is_happy_when_all_four_are_present(monkeypatch):
    from trading_office import cli
    from trading_office.config import Settings

    settings = Settings.load()
    monkeypatch.setenv("TO_EXNESS_API_KEY", "exnsk-abcdef123456")
    monkeypatch.setenv("TO_EXNESS_PRIVATE_KEY", "00" * 32)
    monkeypatch.setenv("TO_EXNESS_ACCOUNT_ID", "123456")
    monkeypatch.setenv("TO_EXNESS_GATEWAY_HOST", "gw.example")
    name, ok, detail = cli._exness_check(settings)
    assert ok is True
    assert "123456" in detail
    assert "abcdef123456" not in detail                  # only the first 8 chars


def test_cli_sizing_check_exits_non_zero_when_the_plan_cannot_be_expressed(capsys):
    """The gold trap, as an exit code a script can act on."""
    import argparse

    from trading_office import cli
    from trading_office.config import Settings

    args = argparse.Namespace(equity=1000, risk_pct=0.5, entry=2650, stop=2632,
                              json=False, connect=False)
    assert cli.cmd_exness(args, Settings.load()) == 1
    out = capsys.readouterr().out
    assert "cannot size this trade" in out
    assert "below the venue minimum" in out

    args = argparse.Namespace(equity=10_000, risk_pct=0.5, entry=2650, stop=2632,
                              json=False, connect=False)
    assert cli.cmd_exness(args, Settings.load()) == 0
    assert "0.02 lots" in capsys.readouterr().out


def test_cli_connect_without_credentials_says_what_is_missing(monkeypatch, capsys):
    import argparse

    from trading_office import cli
    from trading_office.config import Settings

    for name in ("TO_EXNESS_API_KEY", "TO_EXNESS_PRIVATE_KEY", "TO_EXNESS_PRIVATE_KEY_PATH",
                 "TO_EXNESS_ACCOUNT_ID", "TO_EXNESS_GATEWAY_HOST"):
        monkeypatch.delenv(name, raising=False)
    args = argparse.Namespace(equity=0, risk_pct=0.5, entry=0, stop=0, json=False, connect=True)
    assert cli.cmd_exness(args, Settings.load()) == 1
    out = capsys.readouterr().out
    assert "not configured" in out
    assert "refusing to fall back to paper" in out
