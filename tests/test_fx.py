"""The ``fx`` market: gold through the Exness adapter, with every guard the other markets have.

These tests are deliberately about the things that *differ* from crypto, because those are the
things that would silently be wrong: a lot is 100 oz (not one unit), the venue minimum can make
a correct risk plan untradable, the week has a weekend, the state source is a snapshot rather
than a position list, and an accepted order is not a filled order.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from trading_office.config import Settings  # noqa: E402
from trading_office.execution.exness import FxInstrument, lots_for_risk, min_viable_equity  # noqa: E402
from trading_office.market import demo  # noqa: E402
from trading_office.market.fx_client import FxClient  # noqa: E402
from trading_office.risk import build_plan  # noqa: E402


def _plan(equity: float, *, atr: float = 3.6, side: str = "long", profile_over: dict | None = None):
    settings = Settings.load()
    profile = settings.profile("fx")
    profile.update(profile_over or {})
    bars = demo.fx_bars("XAUUSD", "15m", 200)
    return build_plan(
        symbol="XAUUSD", market="fx", side=side, entry=bars[-1]["close"], atr=atr, bars=bars,
        equity=equity, config=settings.section("signal"), risk_cfg=settings.section("risk"),
        profile=profile, spread_bps=1.2, tick_size=float(profile.get("price_step", 0.01)),
        leverage=float(profile.get("leverage", 3)),
    )


# --------------------------------------------------------------------------- the lot trap
def test_gold_risk_accounts_for_100_ounces_per_lot():
    """0.16 lot × 100 oz × $60 of stop distance = $960 of risk, i.e. 0.48 % of 200k."""
    gold = FxInstrument("XAUUSD", volume_min=0.01, volume_step=0.01, contract_size=100)
    plan = lots_for_risk(equity=200_000, risk_pct=0.5, entry=2650, stop=2590,
                         instrument=gold, max_margin_pct=8, leverage=3)
    assert plan["ok"] is True
    assert plan["lots"] == 0.16
    assert plan["risk_cash_actual"] == pytest.approx(960.0, abs=0.01)
    # if a lot were one unit, this same size would risk $9.60 — a 100x understatement
    assert plan["risk_cash_actual"] == pytest.approx(plan["lots"] * 100 * 60.0, abs=0.01)


def test_the_margin_cap_shrinks_the_position_instead_of_refusing_it():
    gold = FxInstrument("XAUUSD", volume_min=0.01, volume_step=0.01, contract_size=100)
    plan = lots_for_risk(equity=30_000, risk_pct=0.5, entry=2650, stop=2640,
                         instrument=gold, max_margin_pct=8, leverage=3)
    assert plan["ok"] is True
    assert plan["capped_by"] == "margin"
    assert plan["lots"] == 0.02
    assert plan["margin"] <= plan["margin_cap"] + 1e-6
    assert plan["risk_pct_actual"] < 0.5       # shrinking to the margin cap also cuts the risk


def test_a_small_account_is_refused_with_the_reason_and_the_numbers():
    decision = _plan(equity=3_600)
    assert decision.approved is False
    text = " ".join(decision.rejections)
    assert "below the venue minimum" in text
    assert "0.01 lot" in text


def test_a_funded_account_gets_a_plan_inside_both_budgets():
    decision = _plan(equity=60_000)
    assert decision.approved is True, decision.rejections
    plan = decision.plan
    assert plan.market == "fx"
    assert plan.size == pytest.approx(round(plan.size / 0.01) * 0.01, abs=1e-9)  # on the step
    assert plan.notional == pytest.approx(plan.size * 100 * plan.entry, rel=1e-9)  # 100 oz
    assert plan.risk_cash == pytest.approx(plan.size * 100 * plan.stop_distance, rel=1e-9)
    assert plan.risk_pct <= float(Settings.load().get("risk.risk_per_trade_pct", 1.0)) + 1e-6


def test_the_plan_never_uses_the_venues_500x_leverage():
    decision = _plan(equity=60_000)
    assert decision.plan.leverage == 3.0        # risk.max_leverage, not 500


def test_margin_cap_binds_before_the_risk_budget_on_gold():
    """This is the counter-intuitive part: margin, not the stop, decides if gold is tradable."""
    gold = FxInstrument("XAUUSD", volume_min=0.01, contract_size=100)
    need = min_viable_equity(entry=2650, stop=2632, instrument=gold, risk_pct=0.5,
                             max_margin_pct=8, leverage=3)
    assert need["binding"] == "margin"
    assert need["equity_required"] == pytest.approx(11_041.67, abs=0.01)
    assert need["margin_equity"] > 3 * need["risk_equity"]


def test_a_short_plan_floors_the_lots_and_keeps_the_stop_above_entry():
    # atr 5.0 is the first size where the structural stop clears the RR gate on this sample;
    # a 15m gold ATR of 3.6 inside a 1.5x multiplier does not, and refusing there is correct.
    decision = _plan(equity=60_000, side="short", atr=5.0)
    assert decision.approved is True, decision.rejections
    plan = decision.plan
    assert plan.stop > plan.entry > plan.target
    assert plan.rr >= 1.5


def test_a_plan_whose_stop_is_too_tight_is_refused_not_widened():
    """A 3.6 ATR stop on this tape gives RR 1.39: the answer is "no", not "move the stop"."""
    decision = _plan(equity=60_000, side="short", atr=3.6)
    assert decision.approved is False
    assert any("reward-to-risk" in r for r in decision.rejections)


# --------------------------------------------------------------------------- the weekend
def test_the_synthetic_week_has_a_weekend_gap_and_no_saturday_bars():
    bars = demo.fx_bars("XAUUSD", "15m", 2_000)
    times = [dt.datetime.fromisoformat(b["time"]) for b in bars]
    gaps = [(b - a) for a, b in zip(times, times[1:]) if (b - a) > dt.timedelta(hours=6)]
    assert gaps, "gold does not trade through the weekend: the grid must show the gap"
    closed = [t for t in times if not demo._fx_is_open(t)]
    assert not closed, "no bar may be generated while the market is closed"


def test_fx_hours_are_24_5_in_utc():
    def at(day, hour):
        # 2026-11-06 is a Friday
        return dt.datetime(2026, 11, day, hour, tzinfo=dt.timezone.utc)

    assert demo._fx_is_open(at(6, 20)) is True       # Friday before the close
    assert demo._fx_is_open(at(6, 21)) is False      # Friday close
    assert demo._fx_is_open(at(7, 12)) is False      # Saturday
    assert demo._fx_is_open(at(8, 21)) is False      # Sunday before the open
    assert demo._fx_is_open(at(8, 22)) is True       # Sunday open
    assert demo._fx_is_open(at(9, 3)) is True        # Monday


# --------------------------------------------------------------------------- data source
def test_without_a_key_the_client_is_synthetic_and_says_so():
    client = FxClient(None, ["XAUUSD"])
    bars = client.candles("XAUUSD", "15m", 120)
    assert bars["source"] == "synthetic"
    assert bars["warnings"] and "synthetic" in bars["warnings"][0]
    quote = client.quote("XAUUSD")
    assert quote["source"] == "synthetic"
    assert quote["warnings"]


def test_the_forming_bar_is_dropped():
    """A decision on a half-built candle is a decision on noise."""
    now = dt.datetime(2026, 11, 6, 12, 7, tzinfo=dt.timezone.utc)     # 12:07 → 12:00 M15 is open
    client = FxClient(None, ["XAUUSD"], now=lambda: now)
    rows = [
        {"time": "2026-11-06T11:45:00+00:00", "open": 2650, "high": 2651, "low": 2649,
         "close": 2650.5, "volume": 10},
        {"time": "2026-11-06T12:00:00+00:00", "open": 2650.5, "high": 2652, "low": 2650,
         "close": 2651.5, "volume": 4},
    ]
    bars = client._to_bars(rows, "M15", "bid")
    assert [b["time"] for b in bars] == ["2026-11-06T11:45:00+00:00"]

    later = dt.datetime(2026, 11, 6, 12, 20, tzinfo=dt.timezone.utc)  # 12:00 has closed
    client._now = lambda: later
    bars = client._to_bars(rows, "M15", "bid")
    assert len(bars) == 2


def test_bid_and_ask_candles_give_a_measured_spread():
    now = dt.datetime(2026, 11, 6, 12, 20, tzinfo=dt.timezone.utc)
    client = FxClient(object(), ["XAUUSD"], now=lambda: now)
    rows = [{
        "time": "2026-11-06T12:00:00+00:00", "volume": 4,
        "bid": {"open": 2650.00, "high": 2651, "low": 2649, "close": 2650.90},
        "ask": {"open": 2650.30, "high": 2651.3, "low": 2649.3, "close": 2651.20},
    }]

    class _Stub:
        def candles(self, *a, **k):
            return rows

    client.client = _Stub()
    bars = client._to_bars(rows, "M15", "both")
    assert bars[0]["close"] == 2650.90
    assert bars[0]["ask_close"] == 2651.20


# --------------------------------------------------------------------------- wiring
def test_the_paper_broker_sizes_gold_in_lots_of_100_ounces():
    from trading_office.execution.broker import Order, PaperBroker

    settings = Settings.load()
    broker = PaperBroker(market="fx", equity=60_000, profile=settings.profile("fx"), leverage=3)

    fill = broker.submit(Order(symbol="XAUUSD", side="buy", quantity=0.055, price_hint=2650.0,
                               stop=2632.0, target=2686.0, client_id="paper-fx-1", market="fx"))

    assert fill.ok is True
    assert fill.quantity == 0.05                          # floored to the 0.01 step
    assert fill.raw["notional"] == pytest.approx(13_252.25, rel=1e-6)   # 100 oz per lot
    assert fill.raw["margin"] == pytest.approx(fill.raw["notional"] / 3, rel=1e-6)


def test_the_paper_broker_refuses_a_volume_below_the_venue_minimum():
    from trading_office.execution.broker import Order, PaperBroker

    broker = PaperBroker(market="fx", equity=60_000, profile=Settings.load().profile("fx"))
    fill = broker.submit(Order(symbol="XAUUSD", side="buy", quantity=0.002, price_hint=2650.0,
                               client_id="paper-fx-2", market="fx"))
    assert fill.ok is False
    assert "venue minimum" in fill.message


def test_live_fx_raises_without_credentials_instead_of_trading_paper():
    from trading_office.execution.broker import make_broker

    settings = Settings.load()
    config = json.loads(json.dumps(settings.raw))
    config["execution"]["paper"] = False
    config["execution"]["dry_run"] = False
    for name in ("TO_EXNESS_API_KEY", "TO_EXNESS_PRIVATE_KEY", "TO_EXNESS_PRIVATE_KEY_PATH",
                 "TO_EXNESS_ACCOUNT_ID"):
        os.environ.pop(name, None)

    with pytest.raises(RuntimeError, match="refusing to fall back to paper"):
        make_broker(config, "fx", 60_000, settings.profile("fx"))


def test_the_engine_runs_the_fx_market_end_to_end_on_synthetic_data(tmp_path):
    """No key, no network: the whole pipeline still runs and labels what it used."""
    from trading_office.engine import TradingEngine

    engine = TradingEngine(market="fx", execute=False, state_dir=tmp_path / "fx")
    row = engine.evaluate("XAUUSD")

    assert row["source"] == "synthetic"
    assert any("synthetic" in w for w in row["warnings"])
    assert row["quote"]["bid"] < row["quote"]["last"] < row["quote"]["ask"]
    assert engine.cost_cfg.latency_ms == 250            # the fx cost model, not crypto's


def test_the_api_serves_gold_and_labels_the_source():
    from fastapi.testclient import TestClient

    from trading_office.api.server import app

    client = TestClient(app)
    board = client.get("/api/market/fx")
    assert board.status_code == 200
    assert board.json()["tickers"][0]["symbol"] == "XAUUSD"

    candles = client.get("/api/candles/fx/XAUUSD")
    assert candles.status_code == 200
    assert candles.json()["bars"]

    screen = client.get("/api/screen?market=fx&limit=1")
    assert screen.status_code == 200
    assert screen.json()["rows"][0]["symbol"] == "XAUUSD"


def test_a_scan_without_a_plan_still_says_which_market_it_came_from(tmp_path):
    """The board must not label a gold row "crypto": the UI reads this field."""
    from trading_office.engine import TradingEngine

    engine = TradingEngine(market="fx", execute=False, state_dir=tmp_path / "fx2")
    row = engine.evaluate("XAUUSD")
    assert row["market"] == "fx"
    assert row["source"] == "synthetic"


def test_an_approved_gold_plan_sizes_from_the_paper_equity(tmp_path):
    """Equity sizing is the real end-to-end risk path: plan → order → paper fill."""
    from trading_office.engine import TradingEngine

    settings = Settings.load()
    engine = TradingEngine(market="fx", execute=False, state_dir=tmp_path / "fx3")
    engine.broker = type(engine.broker)(
        market="fx", equity=60_000, profile=settings.profile("fx"), leverage=3)

    # on this synthetic tape the swing high sits 15.52 above a 2650 entry, so only a wide
    # ATR clears the RR gate; the point is the sizing, so drive it directly
    plan = _plan(equity=60_000).plan
    if plan is None or plan.size <= 0:
        plan = _plan(equity=60_000, atr=5.0).plan
    from trading_office.execution.broker import order_from_plan

    order = order_from_plan(plan, "2026-11-07T03:45:00+07:00", ["test"])
    fill = engine.broker.submit(order)

    assert fill.ok is True, fill.message
    assert fill.quantity == plan.size
    assert fill.raw["notional"] == pytest.approx(plan.size * 100 * fill.price, rel=1e-6)

    # the fill is not the plan: the paper broker charges half-spread + slippage, so the real
    # risk at the stop is slightly larger than the plan's. That is the honest direction, and
    # it is bounded by the model rather than by hope.
    assert fill.price > plan.entry                                  # a buy pays the ask
    real_risk = fill.quantity * 100 * abs(fill.price - plan.stop)
    planned_risk = plan.size * 100 * plan.stop_distance
    assert real_risk >= planned_risk
    assert real_risk / planned_risk < 1.10                          # under 10% of extra risk


def test_a_gold_position_reports_risk_in_100_ounce_terms():
    """The portfolio-risk cap reads position risk: 100x wrong there is a silent oversize."""
    from trading_office.management import Position

    pos = Position(symbol="XAUUSD", market="fx", side="long", entry=2650.0, stop=2632.0,
                   target=2686.0, size=0.05, initial_stop=2632.0, opened_at="t0",
                   bar_time="t0", contract_size=100.0)

    assert pos.risk_per_unit == pytest.approx(18.0 * 100)     # $ per lot
    assert pos.to_dict()["risk_cash"] == pytest.approx(90.0)  # 0.05 lot × 100 oz × 18
    # 2668 is +18 from entry: exactly 1R, not 0.01R
    assert pos.unrealised_r(2668.0) == pytest.approx(1.0)

    spot = Position(symbol="BTC/USDT:USDT", market="crypto", side="long", entry=100.0,
                    stop=99.0, target=102.0, size=2.0, initial_stop=99.0, opened_at="t0",
                    bar_time="t0")
    assert spot.to_dict()["risk_cash"] == pytest.approx(2.0)  # unchanged for spot
