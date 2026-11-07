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


def test_the_margin_cap_sizes_the_trade_down_instead_of_refusing_it():
    """Margin is a sizing constraint: the position shrinks, the risk falls with it.

    Refusing here would refuse every gold trade on a 1:3 account, because a lot's notional is
    large by construction — the cap that matters is the capital committed, not the notional.
    """
    gold = FxInstrument("XAUUSD", volume_min=0.01, volume_step=0.01, contract_size=100,
                        margin_rate=0.0333)
    # 0.01 lot of gold = 100 oz × 2650 = $265,000 notional = $88,333 of margin at 1:3, so
    # the cap allows 1,600/88,333 = 0.018 lots on $20k: floor to 0.01 and the risk follows
    plan = lots_for_risk(equity=20_000, risk_pct=2.0, entry=2650, stop=2640, instrument=gold,
                         max_margin_pct=8, leverage=3)
    assert plan["ok"] is True
    assert plan["capped_by"] == "margin"
    assert plan["lots"] == 0.01
    assert plan["margin"] == pytest.approx(883.33, abs=0.01)
    assert plan["margin"] <= plan["margin_cap"] + 1e-6

    # and when the cap leaves less than the venue minimum, that *is* a refusal with a reason
    tiny = lots_for_risk(equity=500, risk_pct=2.0, entry=2650, stop=2640, instrument=gold,
                         max_margin_pct=8, leverage=3)
    assert tiny["ok"] is False
    assert "below the venue minimum" in tiny["reason"]


# --------------------------------------------------------------------------- stub venue
class _Venue:
    """A local server that speaks the *documented* Exness contract.

    Status codes, error bodies (``{code, error_message}``), the 202 ACK, the snapshot shape
    and the operation vocabulary (``pending`` / ``confirmed`` / ``rejected``) all follow the
    published OpenAPI document. A stub that only matches what the client happens to send
    would happily pass while the client is wrong.
    """

    def __init__(self, public_key):
        self.public_key = public_key
        self.calls: list[dict] = []
        self.final_status = "confirmed"
        self.final_price = "2651.25"
        self.close_only = False
        self.fail_with: tuple[int, dict] | None = None
        self.positions: list[dict] = [{
            "position_id": "2001", "direction": "buy", "instrument": "XAUUSD",
            "initial_volume": "0.01", "volume": "0.01", "open_price": "2650.00",
            "state": "open", "margin_rate": "0.0333", "commission": "-0.07", "swap": "0",
        }]
        self.sltp: list[dict] = [{
            "order_id": "3001", "order_type": "sltp", "instrument": "XAUUSD",
            "direction": "sell", "volume": "0.01", "stop_loss_price": "2632.00",
            "take_profit_price": "2686.00", "state": "placed",
        }]
        self.candles: list[dict] = []

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
                    return self._reply(401, {"code": 1001, "error_message": "AUTH_INVALID_SIGNATURE"})
                parsed = json.loads(body) if body else None
                venue.calls.append({"path": path, "method": method, "payload": payload,
                                    "body": parsed})
                if venue.fail_with:
                    return self._reply(*venue.fail_with)
                clean, _, query = path.partition("?")

                if clean == "/v1/trading/access-point":
                    return self._reply(200, {"access_point": venue.base, "account_id": "777"})
                if clean.endswith("/account"):
                    return self._reply(200, {"account_id": "777", "balance": "1000.00",
                                             "equity": "1012.50", "currency": "USD",
                                             "leverage": "30", "trade_mode": "full"})
                if clean.endswith("/limits"):
                    return self._reply(200, {"limits": {"candles": {"count": 1000}}})
                if clean.endswith("/conditions"):
                    return self._reply(200, {"volume_min": "0.01", "volume_max": "200",
                                             "volume_step": "0.01", "contract_size": "100",
                                             "point_digits": 2, "margin_rate": "0.0333"})
                if "/candles" in clean:
                    return self._reply(200, {"candles": venue.candles})
                if clean.endswith("/snapshot"):
                    return self._reply(200, {
                        "orders": venue.sltp,
                        "positions": venue.positions,
                        "account_state": {"balance": "1000.00", "equity": "1012.50",
                                          "used_margin": "88.25"},
                    })
                if clean.endswith("/positions"):
                    if method == "GET":
                        return self._reply(200, {"positions": venue.positions})
                    if venue.close_only:
                        return self._reply(403, {"code": 2002, "error_message": "ACCOUNT_CLOSE_ONLY"})
                    return self._reply(202, {"operation_id": "789",
                                             "client_request_id": "x",
                                             "status": "accepted"})
                if "/operations/" in clean:
                    polls = getattr(venue, "_polls", 0)
                    venue._polls = polls + 1
                    if polls < venue.final_after_polls:
                        return self._reply(200, {"operation_id": "789", "status": "pending"})
                    out = {"operation_id": "789", "status": venue.final_status}
                    if venue.final_status == "confirmed":
                        out.update({"price": venue.final_price, "commission": "-0.07"})
                    else:
                        out.update({"error_code": 6001, "error_message": "EXECUTION_REJECTED"})
                    return self._reply(200, out)
                if "/positions/" in clean:            # DELETE = close (or partial close)
                    venue.closed = {"path": path, "volume": query}
                    return self._reply(202, {"operation_id": "790", "status": "accepted"})
                if "/orders/" in clean:                # DELETE = cancel, PATCH = modify
                    venue.order_call = {"method": method, "path": path, "body": parsed}
                    return self._reply(202, {"operation_id": "791", "status": "accepted"})
                if clean.endswith("/orders"):           # POST = new pending / SLTP order
                    venue.order_call = {"method": method, "path": path, "body": parsed}
                    return self._reply(202, {"operation_id": "792", "status": "accepted"})
                return self._reply(404, {"code": 3000, "error_message": "REQUEST_INVALID"})

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_DELETE(self):  # noqa: N802
                self._handle("DELETE")

            def do_PATCH(self):  # noqa: N802
                self._handle("PATCH")

            def log_message(self, *args):
                pass

        return Handler


@pytest.fixture()
def venue():
    signer = _signer(b"\x02" * 32)
    stub = _Venue(signer.private_key.public_key())
    server = HTTPServer(("127.0.0.1", 0), stub.handler())
    stub.base = f"http://127.0.0.1:{server.server_port}"
    stub.final_after_polls = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield signer, stub
    finally:
        server.shutdown()
        server.server_close()


def _client(signer, stub, **over):
    return ExnessClient(signer=signer, account_id="777", gateway_host=stub.base, timeout=5, **over)


def _gold():
    return FxInstrument("XAUUSD", volume_min=0.01, volume_step=0.01, contract_size=100,
                        margin_rate=0.0333)


# --------------------------------------------------------------------------- client
def test_discovery_is_cached_and_uses_the_gateway_host(venue):
    signer, stub = venue
    client = _client(signer, stub)

    assert client.resolve_access_point() == stub.base == client.resolve_access_point()

    discovery = [c for c in stub.calls if "access-point" in c["path"]]
    assert len(discovery) == 1, "discovery is rate limited: it must be cached"
    assert discovery[0]["payload"]["path"] == "/v1/trading/access-point?account_id=777"


def test_every_call_reaches_the_stub_with_a_valid_signature(venue):
    signer, stub = venue
    client = _client(signer, stub)

    client.account()
    client.snapshot()

    assert all("auth_error" not in c for c in stub.calls), stub.calls
    assert [c["path"].split("?")[0].rsplit("/", 1)[-1] for c in stub.calls] == [
        "access-point", "account", "snapshot"]


def test_a_tampered_body_is_refused_by_the_venue(venue):
    """Proves the stub really verifies: signing body A and sending body B must 401."""
    signer, stub = venue
    path = "/v1/trading/accounts/777/positions"
    good = signer.headers("POST", path, b'{"volume":"0.01"}', idempotency_key="k1")
    swapped = dict(good)
    swapped["EXN-DATA"] = signer.headers("POST", path, b'{"volume":"9.00"}',
                                         idempotency_key="k1")["EXN-DATA"]

    status, data = exness._default_transport(
        "POST", stub.base + path, swapped, b'{"volume":"0.01"}', 5)

    assert status == 401
    assert data["code"] == 1001


def test_venue_errors_use_the_documented_code_field_and_carry_a_hint(venue):
    signer, stub = venue
    client = _client(signer, stub)
    client.resolve_access_point()
    stub.fail_with = (422, {"code": 5006, "error_message": "TRADING_RULE_INSUFFICIENT_MARGIN"})

    with pytest.raises(ExnessError) as excinfo:
        client.snapshot()

    assert excinfo.value.code == 5006
    assert "INSUFFICIENT_MARGIN" in str(excinfo.value)
    assert excinfo.value.hint


def test_close_only_account_is_reported_not_retried(venue):
    signer, stub = venue
    stub.close_only = True
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    fill = broker.submit(Order(symbol="XAUUSD", side="buy", quantity=0.01, price_hint=2650.0,
                               client_id="to-co", market="fx"))

    assert fill.ok is False
    assert "CLOSE_ONLY" in fill.message or "close" in fill.message.lower()


def test_conditions_override_the_instrument_defaults(venue):
    """volume_min/max/step come from the venue, not from a docs example."""
    signer, stub = venue

    raw = _client(signer, stub).conditions("XAUUSD")
    gold = FxInstrument.from_conditions("XAUUSD", raw)

    assert gold.volume_max == 200.0
    assert gold.contract_size == 100.0
    assert gold.price_step == 0.01          # from point_digits = 2
    assert gold.price_str(2650.567) == "2650.57"


def test_candles_are_asked_for_with_a_signed_query_string(venue):
    signer, stub = venue
    stub.candles = [{"time": "2026-03-16T12:45:00+00:00", "open": 2650.0, "high": 2651.0,
                     "low": 2649.0, "close": 2650.5, "volume": 120}]

    rows = _client(signer, stub).candles("XAUUSD", "M15", start="2026-03-16T12:00:00+00:00",
                                         count=400)

    assert rows[0]["close"] == 2650.5
    call = next(c for c in stub.calls if "/candles" in c["path"])
    assert call["payload"]["path"] == call["path"]      # signed exactly as transmitted
    assert "timeframe=M15" in call["path"]
    assert "instrument=XAUUSD" in call["path"]
    assert "%3A" in call["path"]                        # the ISO timestamp is encoded once


# --------------------------------------------------------------------------- broker
def test_submit_waits_for_confirmed_and_reports_a_real_fill(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold(), equity_value=1000)

    fill = broker.submit(Order(symbol="XAUUSD", side="buy", quantity=0.02, price_hint=2650.0,
                               stop=2632.0, target=2686.0, client_id="to-1", market="fx"))

    assert fill.ok is True, fill.message
    assert fill.price == 2651.25
    assert fill.quantity == 0.02
    assert fill.order_id == "789"
    assert fill.fee == 0.07                                 # commission arrives negative
    sent = next(c for c in stub.calls if c["method"] == "POST")
    assert sent["body"] == {"instrument": "XAUUSD", "side": "buy", "volume": "0.02",
                            "stop_loss": "2632", "take_profit": "2686",
                            "comment": "trading-office"}
    assert sent["payload"]["idempotency_key"] == "to-1"


def test_pending_while_the_venue_decides_becomes_a_fill_once_confirmed(venue):
    signer, stub = venue
    stub.final_after_polls = 2
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold(), fill_timeout_s=5)

    fill = broker.submit(Order(symbol="XAUUSD", side="buy", quantity=0.01, price_hint=2650.0,
                               client_id="to-poll", market="fx"))

    assert fill.ok is True
    polls = [c for c in stub.calls if "/operations/" in c["path"]]
    assert len(polls) == 3


def test_an_order_that_never_confirms_is_not_reported_as_a_fill(venue):
    signer, stub = venue
    stub.final_status = "pending"
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold(), fill_timeout_s=0.6)

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


def test_positions_are_normalised_from_the_snapshot(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    rows = broker.positions()

    assert rows[0]["symbol"] == "XAUUSD"
    assert rows[0]["side"] == "buy"            # the venue says "direction"
    assert rows[0]["lots"] == 0.01
    assert rows[0]["entry"] == 2650.0
    assert rows[0]["pnl"] == 0.0               # not in the schema → 0.0, never a guess
    assert rows[0]["swap"] == 0.0


def test_the_stop_lives_on_an_sltp_order_not_on_the_position(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    orders = broker.open_orders("XAUUSD")

    assert orders[0]["type"] == "sltp"
    assert orders[0]["id"] == "3001"


def test_equity_comes_from_the_venue_not_from_a_default(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold(), equity_value=1.0)
    assert broker.equity() == 1012.50


def test_close_symbol_uses_delete_on_the_position(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    result = broker.close_symbol("XAUUSD")

    assert result["ok"] is True
    assert result["operation_id"] == "790"
    assert stub.closed["path"].startswith("/v1/trading/accounts/777/positions/2001")


def test_modify_stop_patches_the_sltp_order(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    moved = broker.modify_stop("XAUUSD", 2640.0)

    assert moved["ok"] is True
    assert stub.order_call["method"] == "PATCH"
    assert stub.order_call["path"].startswith("/v1/trading/accounts/777/orders/3001")
    assert stub.order_call["body"] == {"stop_loss": "2640"}


def test_a_stop_that_cannot_move_says_so(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())
    stub.positions = []

    moved = broker.modify_stop("XAUUSD", 2640.0)

    assert moved["ok"] is False
    assert "no open position" in moved["reason"]


def test_cancel_order_is_documented_and_not_a_stub(venue):
    signer, stub = venue
    broker = ExnessBroker(client=_client(signer, stub), instrument=_gold())

    out = broker.cancel_order("3001")

    assert out["ok"] is True
    assert stub.order_call["method"] == "DELETE"


def test_min_viable_equity_names_the_binding_constraint():
    """On gold the margin cap binds long before the risk budget does."""
    gold = FxInstrument("XAUUSD", volume_min=0.01, contract_size=100)

    need = exness.min_viable_equity(entry=2650, stop=2632, instrument=gold,
                                    risk_pct=0.5, max_margin_pct=8.0, leverage=3)

    assert need["binding"] == "margin"
    assert need["equity_required"] == pytest.approx(11041.67, abs=0.01)
    assert need["risk_at_minimum_lot"] == 18.0


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

    # 10k is still too small: the margin cap leaves 0.009 lots, below the venue step
    args = argparse.Namespace(equity=10_000, risk_pct=0.5, entry=2650, stop=2632,
                              json=False, connect=False)
    assert cli.cmd_exness(args, Settings.load()) == 1
    assert "cannot size this trade" in capsys.readouterr().out

    # 60k trades, and the risk comes out below the budget the operator set
    args = argparse.Namespace(equity=60_000, risk_pct=0.5, entry=2650, stop=2632,
                              json=False, connect=False)
    assert cli.cmd_exness(args, Settings.load()) == 0
    assert "sized XAUUSD" in capsys.readouterr().out


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


def test_the_cli_names_the_equity_the_smallest_lot_needs(capsys):
    """The one number a small account needs: what it takes to trade gold at all."""
    import argparse

    from trading_office import cli
    from trading_office.config import Settings

    args = argparse.Namespace(equity=1_000, risk_pct=0.5, entry=2650, stop=2632,
                              json=False, connect=False)
    assert cli.cmd_exness(args, Settings.load()) == 1
    out = capsys.readouterr().out
    assert "below the venue minimum" in out
    assert "11,04" in out               # equity required, thousands separator
    assert "margin" in out              # and which constraint binds
