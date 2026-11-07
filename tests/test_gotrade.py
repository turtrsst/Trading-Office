"""Gotrade (US shares) against a stub that follows the published REST contract.

The rules that matter are not "does it parse JSON": they are long-only, a mandatory bracket, a
fee that can exceed the risk budget, and an order that is ``NEW`` before it is ``FILLED``.
Each of those has a test that fails loudly if the rule is ever relaxed.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from trading_office.execution import gotrade  # noqa: E402
from trading_office.execution.broker import Order  # noqa: E402
from trading_office.execution.gotrade import (  # noqa: E402
    GotradeBroker,
    GotradeClient,
    GotradeError,
    fee_for,
    readiness,
    round_trip_fee,
    shares_for_risk,
)

TOKEN = "eyJhbGciOiJIUzI1NiJ9.test-token-value"


# --------------------------------------------------------------------------- money
def test_the_fee_has_a_floor_that_dominates_small_orders():
    assert fee_for(1000.0) == pytest.approx(3.00)      # 0.3 %
    assert fee_for(20.0) == pytest.approx(0.10)        # floor, not 0.06
    assert round_trip_fee(100.0) == pytest.approx(0.60)


def test_fee_as_a_share_of_risk_is_reported_not_hidden():
    plan = shares_for_risk(equity=112.28, risk_pct=1.0, entry=20.0, stop=19.4,
                          cash_available=112.28)
    assert plan["ok"] is True
    assert plan["binding"] == "position_cap"          # 25 % of equity caps it, not the budget
    # two $0.10 fees against under $1 of risk: a quarter of the budget before the trade exists
    assert plan["fee_round_trip"] == pytest.approx(0.20)
    assert plan["fee_pct_of_risk"] > 20


def test_sizing_is_bounded_by_settled_cash_not_by_balance():
    plan = shares_for_risk(equity=1_000.0, risk_pct=2.0, entry=50.0, stop=48.0,
                          cash_available=100.0)
    assert plan["binding"] == "cash"
    assert plan["shares"] == pytest.approx(2.0)        # $100 / $50, not 10 shares
    assert plan["notional"] == pytest.approx(100.0)


def test_whole_share_accounts_floor_and_refuse_rather_than_round_up():
    # $500 × 25 % = $125 of position at $180 = 0.69 shares → 0 whole shares → refuse
    plan = shares_for_risk(equity=500.0, risk_pct=1.0, entry=180.0, stop=176.0,
                          cash_available=500.0, fractional=False)
    assert plan["ok"] is False
    assert "no size fits" in plan["reason"]

    # enough for one share: floors to 2, never rounds up to 3
    fits = shares_for_risk(equity=2_000.0, risk_pct=1.0, entry=180.0, stop=176.0,
                          cash_available=2_000.0, fractional=False)
    assert fits["ok"] is True
    assert fits["shares"] == 2.0

    # fractional accounts get the exact size instead
    frac = shares_for_risk(equity=500.0, risk_pct=1.0, entry=180.0, stop=176.0,
                          cash_available=500.0, fractional=True)
    assert frac["shares"] > 0 and frac["shares"] < 1


# --------------------------------------------------------------------------- stub venue
class _Venue:
    def __init__(self):
        self.calls: list[dict] = []
        self.status = "FILLED"
        self.fill_price = "178.50"
        self.poll_delay = 0
        self.fail: tuple[int, dict] | None = None
        self.positions = [{
            "ticker": "AAPL", "exchange": "NASDAQ", "currency": "USD", "quantity": "1.5",
            "avg_entry_price": "175.20", "market_value": "267.75", "cost_basis": "262.80",
            "unrealized_pl": "4.95", "unrealized_plpc": "0.0188",
        }]
        self._polls = 0

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
                auth = self.headers.get("Authorization") or ""
                venue.calls.append({"method": method, "path": self.path, "auth": auth,
                                    "body": json.loads(body) if body else None})
                if not auth.startswith("Bearer ") or auth.split(" ", 1)[1] != TOKEN:
                    return self._reply(401, {"status": "error", "message": "Unauthorized"})
                if venue.fail:
                    return self._reply(*venue.fail)
                path = self.path.split("?")[0]
                if path.endswith("/account-balance"):
                    return self._reply(200, {"status": "success", "message": "ok", "data": {
                        "cash_balance": "1000.00", "cash_available_for_trade": "850.00",
                        "currency": "USD"}})
                if path.endswith("/open-positions"):
                    return self._reply(200, {"status": "success", "message": "ok",
                                            "data": venue.positions})
                if path.endswith("/stock-orders/create"):
                    return self._reply(201, {"status": "success", "message": "submitted",
                                             "data": {"order": {
                                                 "order_id": "oid-1", "side": "BUY",
                                                 "type": "MARKET", "status": "NEW",
                                                 "quantity": "1.5",
                                                 "cumulative_quantity": "0"}}})
                if path.endswith("/stock-orders"):
                    return self._reply(200, {"status": "success", "message": "ok", "data": []})
                if "/stock-orders/" in path:
                    if method == "DELETE":
                        return self._reply(200, {"status": "success", "message": "cancelled",
                                                 "data": {"order_id": "oid-1"}})
                    venue._polls += 1
                    pending = venue._polls <= venue.poll_delay
                    return self._reply(200, {"status": "success", "message": "ok", "data": {
                        "stock": {"ticker": "AAPL", "shortable": False, "fractionable": True},
                        "order": {"order_id": "oid-1", "status": "NEW" if pending else venue.status,
                                  "quantity": "1.5",
                                  "cumulative_quantity": "0" if pending else "1.5",
                                  "average_fill_price": "0" if pending else venue.fill_price,
                                  "fees": {"currency": "USD", "value": 0.27}}}})
                return self._reply(404, {"status": "error", "message": "not found"})

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_DELETE(self):  # noqa: N802
                self._handle("DELETE")

            def log_message(self, *args):
                pass

        return Handler


@pytest.fixture()
def venue():
    stub = _Venue()
    server = HTTPServer(("127.0.0.1", 0), stub.handler())
    stub.base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()


def _broker(stub, **over):
    client = GotradeClient(token=TOKEN, base_url=stub.base, timeout=5)
    return GotradeBroker(client=client, **over)


# --------------------------------------------------------------------------- auth + reads
def test_a_missing_or_wrong_token_is_a_401_with_an_actionable_hint(venue):
    client = GotradeClient(token="wrong-token", base_url=venue.base, timeout=5)

    with pytest.raises(GotradeError) as excinfo:
        client.account_balance()

    assert excinfo.value.status == 401
    assert "not enabled for API access" in str(excinfo.value)


def test_balance_exposes_both_cash_numbers(venue):
    broker = _broker(venue)
    assert broker.cash() == {"balance": 1000.0, "available": 850.0}


def test_positions_are_normalised_and_always_long(venue):
    rows = _broker(venue).positions()
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["side"] == "long"
    assert rows[0]["shares"] == 1.5
    assert rows[0]["pnl"] == 4.95


# --------------------------------------------------------------------------- the rules
def test_an_entry_without_a_stop_is_refused_before_any_request(venue):
    broker = _broker(venue)
    fill = broker.submit(Order(symbol="AAPL", side="buy", quantity=1, price_hint=178.5,
                               client_id="us-1", market="us"))

    assert fill.ok is False
    assert "hard stop-loss" in fill.message
    assert not [c for c in venue.calls if c["method"] == "POST"]


def test_a_sell_with_no_position_is_refused_because_this_account_is_long_only(venue):
    broker = _broker(venue)
    fill = broker.submit(Order(symbol="TSLA", side="sell", quantity=1, price_hint=400.0,
                               stop=410.0, client_id="us-2", market="us"))

    assert fill.ok is False
    assert "long-only" in fill.message
    assert not [c for c in venue.calls if c["method"] == "POST"]


def test_a_bracket_is_refused_during_extended_hours(venue):
    broker = _broker(venue, allow_extended_hours=True)
    fill = broker.submit(Order(symbol="AAPL", side="buy", quantity=1, price_hint=178.5,
                               stop=175.0, target=185.0, client_id="us-3", market="us"))

    assert fill.ok is False
    assert "extended hours" in fill.message


def test_a_buy_carries_the_bracket_and_waits_for_filled(venue):
    venue.poll_delay = 2
    broker = _broker(venue, fill_timeout_s=5)

    fill = broker.submit(Order(symbol="aapl", side="buy", quantity=1.5, price_hint=178.5,
                               stop=175.0, target=185.0, client_id="us-4", market="us"))

    assert fill.ok is True, fill.message
    assert fill.price == 178.50
    assert fill.quantity == 1.5
    sent = next(c for c in venue.calls if c["method"] == "POST")
    assert sent["body"]["ticker"] == "AAPL"
    assert sent["body"]["side"] == "BUY"
    assert sent["body"]["stop_loss"] == 175.0
    assert sent["body"]["take_profit"] == 185.0
    assert sent["body"]["is_extended_hours"] is False


def test_new_is_not_a_fill(venue):
    venue.poll_delay = 999
    broker = _broker(venue, fill_timeout_s=0.4)

    fill = broker.submit(Order(symbol="AAPL", side="buy", quantity=1, price_hint=178.5,
                               stop=175.0, client_id="us-5", market="us"))

    assert fill.ok is False
    assert "timeout" in fill.message.lower() or "new" in fill.message.lower()
    assert fill.quantity == 0.0


def test_a_rejected_order_does_not_invent_a_price(venue):
    venue.status = "REJECTED"
    broker = _broker(venue)

    fill = broker.submit(Order(symbol="AAPL", side="buy", quantity=1, price_hint=178.5,
                               stop=175.0, client_id="us-6", market="us"))

    assert fill.ok is False
    assert fill.price == 0.0
    assert "REJECTED" in fill.message


def test_a_partial_fill_is_reported_even_though_the_order_is_still_working(venue):
    """PARTIAL_FILL is not terminal — but the shares exist and must be managed."""
    venue.status = "PARTIAL_FILL"
    broker = _broker(venue, fill_timeout_s=0.5)

    fill = broker.submit(Order(symbol="AAPL", side="buy", quantity=5, price_hint=178.5,
                               stop=175.0, client_id="us-7", market="us"))

    assert fill.ok is True                            # a position exists: never hide it
    assert fill.quantity == 1.5
    assert "partially filled 1.5/5" in fill.message
    assert fill.raw["partial"] is True


def test_a_partial_fill_without_a_price_blocks_instead_of_booking_a_guess(venue):
    venue.status = "PARTIAL_FILL"
    original = venue.handler

    class _NoPrice(type(venue)):
        pass

    broker = _broker(venue, fill_timeout_s=0.5)
    # simulate the venue omitting average_fill_price on a partial fill
    real_wait = broker.client.wait_for_terminal
    broker.client.wait_for_terminal = lambda oid, **kw: {
        "state": "PARTIAL_FILL", "cumulative_quantity": "1.5", "average_fill_price": "0"}

    fill = broker.submit(Order(symbol="AAPL", side="buy", quantity=5, price_hint=178.5,
                               stop=175.0, client_id="us-7b", market="us"))

    assert fill.ok is False
    assert "reconcile" in fill.message
    assert fill.price == 0.0
    broker.client.wait_for_terminal = real_wait


def test_a_whole_share_account_refuses_a_fractional_order(venue):
    broker = _broker(venue, fractional=False)
    fill = broker.submit(Order(symbol="AAPL", side="buy", quantity=1.5, price_hint=178.5,
                               stop=175.0, client_id="us-8", market="us"))
    assert fill.ok is False
    assert "fractional" in fill.message


def test_duplicate_client_id_is_refused(venue):
    broker = _broker(venue)
    order = Order(symbol="AAPL", side="buy", quantity=1, price_hint=178.5, stop=175.0,
                  client_id="us-9", market="us")
    assert broker.submit(order).ok is True
    assert broker.submit(order).ok is False
    assert len([c for c in venue.calls if c["method"] == "POST"]) == 1


def test_closing_uses_a_sell_of_the_held_quantity(venue):
    broker = _broker(venue)
    result = broker.close_symbol("AAPL")
    assert result["ok"] is True
    assert result["shares"] == 1.5
    sent = next(c for c in venue.calls if c["method"] == "POST")
    assert sent["body"]["side"] == "SELL"
    assert sent["body"]["quantity"] == 1.5


def test_live_without_a_token_raises_instead_of_trading_paper():
    class Settings:
        def get(self, dotted, default=None):
            return default

    with pytest.raises(RuntimeError, match="refusing to fall back to paper"):
        gotrade.from_settings(Settings())


def test_readiness_names_the_two_account_blockers_and_the_rules():
    class Settings:
        def get(self, dotted, default=None):
            return default

    out = readiness(Settings())
    assert out["token_present"] is False
    joined = " ".join(out["blocking"])
    assert "ID document" in joined
    assert "API access enabled" in joined
    assert out["constraints"]["long_only"] is True
    assert out["constraints"]["shortable"] is False
    assert out["constraints"]["stop_loss_required"] is True
