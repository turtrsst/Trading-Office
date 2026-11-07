"""Analyse → confirm → execute: the button-driven path, tested where it can hurt.

The interesting cases are the refusals. A "buy now" endpoint is easy to write and easy to
get wrong in one direction only: it sends an order the rest of the system was busy
preventing. So most of this file is about what must *not* happen when the operator clicks.

The contract under test: the request carries identifiers only (symbol, side, bar, fingerprint)
and the plan is **recomputed** server-side, so nothing a browser sends can change the size,
the stop or the target.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from trading_office import confirm  # noqa: E402
from trading_office.config import Settings  # noqa: E402
from trading_office.engine import TradingEngine  # noqa: E402

SYMBOL = "TEST/USDT:USDT"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture()
def engine(settings, tmp_path):
    return TradingEngine(market="crypto", settings=settings, symbols=[SYMBOL],
                         state_dir=tmp_path / "confirm")


def _fresh_bar() -> str:
    """The newest closed 15 m bar, so the daily-loss breaker sees today's loss as today's."""
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0).isoformat()


def _scan(settings, *, symbol=SYMBOL, action="LONG", bar_time=None, source="ccxt", plan=True):
    from tests.test_api import _ready_payload

    payload = _ready_payload(settings, symbol=symbol)
    payload["action"] = action
    payload["bar_time"] = bar_time or _fresh_bar()
    payload["source"] = source
    if not plan:
        payload["risk"] = {"approved": False, "plan": None, "rejections": ["rr below 1.5"]}
    return payload


def _evaluate_is(monkeypatch, payload):
    """Every refresh of the analysis returns this payload, whatever the symbol."""
    monkeypatch.setattr(TradingEngine, "evaluate", lambda self, symbol, record=False: payload)


# --------------------------------------------------------------------------- fingerprint
def test_analysis_id_binds_symbol_bar_side_and_size():
    base = confirm.analysis_id("BTC/USDT:USDT", "2026-11-06T10:00:00+07:00", "LONG", 0.5)
    assert base == confirm.analysis_id("BTC/USDT:USDT", "2026-11-06T10:00:00+07:00", "long", 0.5)
    assert base != confirm.analysis_id("BTC/USDT:USDT", "2026-11-06T10:15:00+07:00", "LONG", 0.5)
    assert base != confirm.analysis_id("ETH/USDT:USDT", "2026-11-06T10:00:00+07:00", "LONG", 0.5)
    assert base != confirm.analysis_id("BTC/USDT:USDT", "2026-11-06T10:00:00+07:00", "SHORT", 0.5)
    # a different size is a different trade, not a different spelling of the same one
    assert base != confirm.analysis_id("BTC/USDT:USDT", "2026-11-06T10:00:00+07:00", "LONG", 0.51)
    assert len(base) == 16


# --------------------------------------------------------------------------- refusals
def test_confirm_needs_a_symbol(engine):
    result = confirm.confirm(engine, {})
    assert result["ok"] is False
    assert "symbol is required" in result["reason"]


def test_confirm_refuses_a_symbol_outside_the_watchlist(engine, monkeypatch):
    _evaluate_is(monkeypatch, _scan(engine.settings, symbol="OTHER/USDT:USDT"))
    result = confirm.confirm(engine, {"symbol": "OTHER/USDT:USDT"})
    assert result["ok"] is False
    assert result["block"] == "symbol"
    assert engine.status.orders == 0


def test_confirm_refuses_a_wait_signal(engine, monkeypatch):
    _evaluate_is(monkeypatch, _scan(engine.settings, action="WAIT", plan=False))
    result = confirm.confirm(engine, {"symbol": SYMBOL})
    assert result["ok"] is False
    assert result["block"] in ("wait", "no_plan")
    assert engine.status.orders == 0


def test_confirm_refuses_a_stale_click(engine, monkeypatch):
    """The card the operator read said LONG on the 10:00 bar; the 10:15 bar closed since."""
    shown = "2026-11-06T10:00:00+07:00"
    _evaluate_is(monkeypatch, _scan(engine.settings, bar_time="2026-11-06T10:15:00+07:00"))

    result = confirm.confirm(engine, {"symbol": SYMBOL, "bar_time": shown,
                                      "analysis_id": confirm.analysis_id(
                                          SYMBOL, shown, "LONG",
                                          float(_scan(engine.settings)["risk"]["plan"]["size"]))})

    assert result["ok"] is False
    assert result["block"] == "stale"
    assert "new bar" in result["reason"]
    assert result["bar_now"] == "2026-11-06T10:15:00+07:00"
    assert engine.status.orders == 0


def test_confirm_refuses_a_changed_signal_on_the_same_bar(engine, monkeypatch):
    """The bar is the same, but the setup became WAIT: the card is no longer true."""
    bar = _fresh_bar()
    shown = _scan(engine.settings, action="LONG", bar_time=bar)
    fingerprint = confirm.analysis_id(SYMBOL, bar, "LONG",
                                      float(shown["risk"]["plan"]["size"]))
    _evaluate_is(monkeypatch, _scan(engine.settings, action="WAIT", bar_time=bar, plan=False))

    result = confirm.confirm(engine, {"symbol": SYMBOL, "bar_time": bar,
                                      "analysis_id": fingerprint})
    assert result["ok"] is False
    assert result["block"] in ("wait", "no_plan")
    assert engine.status.orders == 0


def test_confirm_refuses_a_tampered_fingerprint(engine, monkeypatch):
    payload = _scan(engine.settings)
    _evaluate_is(monkeypatch, payload)

    result = confirm.confirm(engine, {"symbol": SYMBOL, "analysis_id": "0000000000000000"})
    assert result["ok"] is False
    assert result["block"] == "stale"
    assert "analyse again" in result["reason"]
    assert engine.status.orders == 0


def test_confirm_refuses_the_wrong_side(engine, monkeypatch):
    _evaluate_is(monkeypatch, _scan(engine.settings, action="LONG"))

    result = confirm.confirm(engine, {"symbol": SYMBOL, "side": "SHORT"})
    assert result["ok"] is False
    assert "the request said SHORT" in result["reason"]


def test_confirm_refuses_while_the_breaker_is_open(engine, monkeypatch):
    _evaluate_is(monkeypatch, _scan(engine.settings))
    engine.state.append_ledger({"event": "trade_closed", "market": "crypto", "symbol": SYMBOL,
                                "pnl": -30.0,
                                "closed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    assert engine.guard_state()["blocked"] is True      # the breaker really is open

    result = confirm.confirm(engine, {"symbol": SYMBOL})
    assert result["ok"] is False
    assert result["block"] == "kill_switch"
    assert "kill switch" in result["reason"]
    assert engine.status.orders == 0


def test_confirm_refuses_a_broken_feed(engine, monkeypatch):
    def boom(self, symbol, record=False):
        raise ConnectionError("provider down")

    monkeypatch.setattr(TradingEngine, "evaluate", boom)
    result = confirm.confirm(engine, {"symbol": SYMBOL})
    assert result["ok"] is False
    assert result["block"] == "error"
    assert "ConnectionError" in result["reason"]


# --------------------------------------------------------------------------- the happy path
def test_confirm_opens_the_position_on_the_paper_book(engine, monkeypatch):
    payload = _scan(engine.settings, action="LONG")
    _evaluate_is(monkeypatch, payload)
    fingerprint = confirm.analysis_id(SYMBOL, payload["bar_time"], "LONG",
                                      float(payload["risk"]["plan"]["size"]))

    result = confirm.confirm(engine, {"symbol": SYMBOL, "analysis_id": fingerprint,
                                      "bar_time": payload["bar_time"]})

    assert result["ok"] is True, result
    assert result["opened"] is True
    assert result["side"] == "LONG"
    assert SYMBOL in engine.positions
    position = engine.positions[SYMBOL]
    assert position.stop > 0                     # every entry carries a hard stop
    assert position.target > position.entry      # and a target that satisfies the RR floor
    assert engine.status.orders == 1
    assert result["position"]["stop"] == position.stop
    assert result["analysis_id"] == fingerprint

    events = [e["event"] for e in engine.state.ledger(30)]
    assert "order" in events and "confirmed_trade" in events


def test_confirm_is_single_use(engine, monkeypatch):
    payload = _scan(engine.settings)
    _evaluate_is(monkeypatch, payload)

    first = confirm.confirm(engine, {"symbol": SYMBOL})
    second = confirm.confirm(engine, {"symbol": SYMBOL})

    assert first["ok"] is True
    assert second["ok"] is False                    # open position / bar lock, never a 2nd order
    assert engine.status.orders == 1


def test_confirm_on_a_live_venue_needs_the_armed_grant(engine, monkeypatch):
    """A browser click is consent inside an armed setup — not a way to create one."""
    from tests.test_autonomy import LiveVenue

    _evaluate_is(monkeypatch, _scan(engine.settings))
    engine.broker = LiveVenue()

    unarmed = confirm.confirm(engine, {"symbol": SYMBOL})
    assert unarmed["ok"] is False
    assert unarmed["block"] == "gate"
    assert engine.broker.orders == []
    assert engine.state.approvals(status="pending") == []   # not even queued


def test_confirm_on_an_armed_live_venue_queues_instead_of_sending(engine, monkeypatch):
    """mode=ask parks the plan for a second, explicit approval — the click says "this setup"."""
    from trading_office import autonomy
    from tests.test_autonomy import LiveVenue

    _evaluate_is(monkeypatch, _scan(engine.settings))
    engine.broker = LiveVenue()
    engine.state.set_setting("live_entry_grant", {
        "armed": True, "fingerprint": autonomy.execution_gate_fingerprint(),
        "limits": autonomy.execution_gate_limits(engine.settings),
        "armed_by": "test", "armed_at": "2026-11-06T00:00:00+00:00",
    })

    result = confirm.confirm(engine, {"symbol": SYMBOL})

    assert result["ok"] is False
    assert "queued for approval" in result["reason"]
    assert engine.broker.orders == []                        # nothing reached the venue
    assert [r["symbol"] for r in engine.state.approvals(status="pending")] == [SYMBOL]


# --------------------------------------------------------------------------- the board
def test_analyse_all_ranks_by_abs_score_and_blocks_what_it_must(engine, monkeypatch):
    engine.symbols = ["A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT", "D/USDT:USDT"]

    def fake_evaluate(self, symbol, record=False):
        base = {"symbol": symbol, "bar_time": "2026-11-06T10:00:00+07:00", "price": 100.0,
                "score": 4, "stage": "ready", "executable": True, "source": "ccxt",
                "reasons": [], "warnings": [], "signal": {},
                "risk": {"plan": {"entry": 100.0, "stop": 99.0, "target": 102.0,
                                  "size": 1.0, "rr": 2.0, "risk_pct": 0.5}}}
        if symbol.startswith("A"):
            return {**base, "action": "LONG", "score": 7}
        if symbol.startswith("B"):
            return {**base, "action": "SHORT", "score": 8}
        if symbol.startswith("C"):
            return {**base, "action": "WAIT", "score": 9}
        return {**base, "action": "LONG", "score": 6, "risk": {"plan": None}}

    monkeypatch.setattr(TradingEngine, "evaluate", fake_evaluate)
    board = confirm.analyse_all(engine)

    assert [r["symbol"] for r in board["rows"]] == ["C/USDT:USDT", "B/USDT:USDT",
                                                    "A/USDT:USDT", "D/USDT:USDT"]
    assert board["tradeable"] == 2                      # C is WAIT, D has no plan
    by_symbol = {r["symbol"]: r for r in board["rows"]}
    assert by_symbol["C/USDT:USDT"]["block"] == "wait"
    assert by_symbol["D/USDT:USDT"]["block"] == "no_plan"
    assert by_symbol["A/USDT:USDT"]["executable"] is True
    assert by_symbol["A/USDT:USDT"]["id"]                # a confirm button needs a target
    assert by_symbol["B/USDT:USDT"]["action"] == "SHORT"
    assert engine.last_analysis is board                 # kept for the UI, never an order


def test_analyse_all_marks_every_row_blocked_after_a_breaker_trip(engine, monkeypatch):
    engine.symbols = ["A/USDT:USDT", "B/USDT:USDT"]
    engine.state.append_ledger({"event": "trade_closed", "market": "crypto", "symbol": "A",
                                "pnl": -30.0,
                                "closed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    _evaluate_is(monkeypatch, _scan(engine.settings))

    board = confirm.analyse_all(engine)
    assert board["guard"]["blocked"] is True
    assert board["tradeable"] == 0
    assert {r["block"] for r in board["rows"]} == {"kill_switch"}


def test_analyse_all_marks_an_open_position(engine, monkeypatch):
    _evaluate_is(monkeypatch, _scan(engine.settings))
    confirm.confirm(engine, {"symbol": SYMBOL})

    board = confirm.analyse_all(engine)
    assert board["rows"][0]["block"] == "position"
    assert board["tradeable"] == 0


def test_analyse_all_tolerates_a_broken_feed(engine, monkeypatch):
    def boom(self, symbol, record=False):
        raise ConnectionError("provider down")

    monkeypatch.setattr(TradingEngine, "evaluate", boom)
    board = confirm.analyse_all(engine)

    assert board["analysed"] == 1
    assert board["tradeable"] == 0
    row = board["rows"][0]
    assert row["action"] == "ERROR"
    assert row["block"] == "error"
    assert "ConnectionError" in row["block_text"]


def test_synthetic_data_is_labelled_and_only_tradable_on_paper(engine, monkeypatch):
    """Demo candles may be rehearsed on the paper book; they are analysis-only on a venue."""
    engine.symbols = ["A/USDT:USDT"]
    _evaluate_is(monkeypatch, _scan(engine.settings, symbol="A/USDT:USDT", source="synthetic"))

    paper = confirm.analyse_all(engine)
    assert paper["paper"] is True
    assert paper["rows"][0]["data_simulated"] is True
    assert paper["rows"][0]["executable"] is True       # rehearsal: allowed, but labelled

    from tests.test_autonomy import LiveVenue

    engine.broker = LiveVenue()
    live = confirm.analyse_all(engine)
    assert live["paper"] is False
    assert live["rows"][0]["block"] == "synthetic"
    assert live["rows"][0]["executable"] is False
