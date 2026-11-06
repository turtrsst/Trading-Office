"""Tests for the execution-cost model, partial take profit, gap guard and reconciliation."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from trading_office import costs, indicators as ind, risk  # noqa: E402
from trading_office.config import Settings  # noqa: E402
from trading_office.engine import TradingEngine  # noqa: E402
from trading_office.execution import Order, PaperBroker  # noqa: E402
from trading_office.management import (  # noqa: E402
    Position,
    breakeven_stop,
    manage_position,
    partial_size,
)
from trading_office.market import demo  # noqa: E402


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


# --------------------------------------------------------------------------- latency
def test_latency_moves_the_fill_with_the_bar():
    bar = {"open": 100.0, "close": 101.0, "high": 101.1, "low": 99.9}
    slow = costs.apply_latency(100.0, bar, latency_ms=900_000, timeframe="15m", side="buy")
    quick = costs.apply_latency(100.0, bar, latency_ms=0, timeframe="15m", side="buy")
    assert quick.fill_price == 100.0
    assert slow.fill_price == pytest.approx(101.0, rel=1e-6)  # a full-bar delay fills at the close
    assert slow.moved_bps > 0  # adverse for a buy in an up bar


def test_latency_is_adverse_only_against_the_side():
    bar = {"open": 100.0, "close": 99.0, "high": 100.1, "low": 98.9}
    buy = costs.apply_latency(100.0, bar, latency_ms=900_000, timeframe="15m", side="buy")
    sell = costs.apply_latency(100.0, bar, latency_ms=900_000, timeframe="15m", side="sell")
    assert buy.moved_bps < 0  # buying into a falling bar is cheap: the adverse move is negative
    assert sell.moved_bps > 0  # selling after a fall is adverse


# --------------------------------------------------------------------------- slippage
def test_slippage_grows_with_volatility_and_size():
    cfg = costs.CostConfig()
    quiet = costs.estimate_slippage(price=100.0, atr=0.2, notional=100.0, quote_volume=1e7, cfg=cfg)
    wild = costs.estimate_slippage(price=100.0, atr=2.0, notional=100.0, quote_volume=1e7, cfg=cfg)
    big = costs.estimate_slippage(price=100.0, atr=0.2, notional=5e5, quote_volume=1e7, cfg=cfg)
    assert wild.slippage_bps > quiet.slippage_bps
    assert big.slippage_bps > quiet.slippage_bps
    assert quiet.components["base"] == cfg.slippage_base_bps


def test_slippage_is_capped_and_reported():
    cfg = costs.CostConfig(max_slippage_bps=10.0)
    est = costs.estimate_slippage(price=100.0, atr=50.0, notional=9e6, quote_volume=1e6, cfg=cfg)
    assert est.capped is True
    assert est.slippage_bps == pytest.approx(10.0)
    assert est.fill_price > 100.0


def test_risk_multiplier_shrinks_the_budget_in_stress():
    cfg = costs.CostConfig(max_risk_multiplier=1.75)
    calm = costs.risk_multiplier(spread_bps=1.0, max_spread_bps=8.0, atr_pct=0.5,
                                 profile={"atr_soft_cap_pct": 2.5}, cfg=cfg)
    stressed = costs.risk_multiplier(spread_bps=8.0, max_spread_bps=8.0, atr_pct=4.0,
                                     profile={"atr_soft_cap_pct": 2.5}, cfg=cfg)
    assert calm == 1.0
    assert 1.0 < stressed <= 1.75
    assert costs.risk_multiplier(spread_bps=5.0, max_spread_bps=8.0, atr_pct=0.5,
                                 profile={"atr_soft_cap_pct": 2.5}, cfg=cfg) > 1.0


def test_plan_sizes_down_when_risk_multiplier_is_set(settings):
    bars = demo.crypto_bars("BTC/USDT:USDT", "15m", 300)
    common = dict(
        symbol="BTC/USDT:USDT", market="crypto", side="long", entry=10.0, atr=0.4, bars=bars,
        equity=10_000.0, config=settings.section("signal"), risk_cfg=settings.section("risk"),
        profile=settings.profile("crypto"), spread_bps=3.0,
    )
    base = risk.build_plan(**common).plan
    scaled = risk.build_plan(**common, risk_multiplier=1.5, risk_note="stressed").plan
    assert base is not None and scaled is not None
    assert base.caps_applied == [] or "risk_scale" not in str(base.caps_applied)
    assert scaled.risk_cash < base.risk_cash
    assert "risk_scale_1.50x" in scaled.caps_applied
    assert scaled.risk_note == "stressed"


# --------------------------------------------------------------------------- gap guard
def test_gap_guard_blocks_after_an_overnight_jump():
    guard = costs.gap_guard(prev_close=100.0, open_price=96.0, atr=1.0, spread_bps=5.0,
                            max_spread_bps=8.0, cfg=costs.CostConfig.for_market("idx"))
    assert guard.cooldown > 0
    assert guard.gap_atr == pytest.approx(4.0)
    ok = costs.gap_guard(prev_close=100.0, open_price=100.2, atr=1.0, spread_bps=5.0,
                         max_spread_bps=8.0, cfg=costs.CostConfig.for_market("idx"))
    assert ok.cooldown == 0


def test_gap_guard_blocks_on_a_blown_out_spread():
    guard = costs.gap_guard(prev_close=100.0, open_price=100.0, atr=1.0, spread_bps=40.0,
                            max_spread_bps=8.0, cfg=costs.CostConfig.for_market("crypto"))
    assert guard.cooldown > 0 and "spread" in guard.reason


def test_gap_through_stop_fills_worse_than_the_stop():
    fill = costs.gap_adjusted_stop(exit_price=100.0, next_open=94.0, side="long",
                                   cfg=costs.CostConfig(gap_fill_fraction=0.5))
    assert 94.0 <= fill < 100.0  # worse than the stop, but not the whole gap
    good = costs.gap_adjusted_stop(exit_price=100.0, next_open=104.0, side="long",
                                   cfg=costs.CostConfig(gap_fill_fraction=0.5))
    assert good == 100.0  # the gap went our way: the resting order fills at its price


def test_no_entry_fills_on_the_wrong_side_of_its_stop(tmp_path):
    """A gap must never turn a stop into a guaranteed profit (or an unfillable bracket)."""
    from trading_office.backtest import Backtester

    settings = Settings.load()
    bars = [dict(b) for b in demo.crypto_bars("SOL/USDT:USDT", "15m", 700)]
    bt = Backtester(market="crypto", settings=settings)
    clean = bt.run("SOL/USDT:USDT", bars, htf_factor=8)
    assert clean.trades

    # crash the bar right after an entry that the clean run took
    opened = {str(b["time"]): i for i, b in enumerate(bars)}
    victim = clean.trades[-1]
    at = opened[str(victim["opened_at"])]
    crash = [dict(b) for b in bars]
    shock = crash[at + 1]
    for key, mult in (("open", 0.92), ("high", 0.93), ("low", 0.91), ("close", 0.92)):
        shock[key] = shock[key] * mult

    result = Backtester(market="crypto", settings=settings).run("SOL/USDT:USDT", crash, htf_factor=8)
    for trade in result.trades:
        long = trade["side"] == "long"
        # the fill must be on the correct side of the stop the plan approved
        assert (trade["entry"] > trade["initial_stop"]) if long else (trade["entry"] < trade["initial_stop"]), trade
    report = result.metrics()["costs"]
    assert report["entries_cancelled"] >= 1
    assert report["model"]["latency_ms"] == 400
    assert result.metrics()["avg_entry_slippage_bps"] is not None


# --------------------------------------------------------------------------- partial TP
def make_position(**over) -> Position:
    data = dict(
        symbol="BTC/USDT:USDT", market="crypto", side="long", entry=100.0, stop=98.0, target=106.0,
        size=1.0, initial_stop=98.0, opened_at="2026-01-01T00:00:00+00:00", bar_time="2026-01-01",
    )
    data.update(over)
    return Position(**data)


def test_partial_tp_fires_at_2r_and_goes_risk_free(settings):
    pos = make_position()
    # price at 2R: 100 + 2*(100-98) = 104, the bar must not have hit the target (106)
    actions = manage_position(
        pos, closed_bar={"high": 104.5, "low": 103.5, "close": 104.2}, price=104.2, atr=1.0,
        now=dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc),
        mgmt_cfg={**settings.section("management"), "partial_tp_r": 2.0}, profile=settings.profile("crypto"),
    )
    partials = [a for a in actions if a.kind == "partial"]
    stops = [a for a in actions if a.kind == "modify_stop"]
    assert partials, actions
    assert partials[0].fraction == pytest.approx(0.5)
    assert partials[0].pnl_target_price == pytest.approx(104.0)
    assert stops and stops[0].new_stop > pos.entry  # break-even **plus** fees, never below entry
    assert pos.partial_done is True


def test_partial_is_skipped_below_the_venue_minimum(settings):
    pos = make_position(size=0.4)  # half = 0.2 units, below a 1-unit step
    actions = manage_position(
        pos, closed_bar={"high": 104.4, "low": 103.6, "close": 104.1}, price=104.1, atr=1.0,
        now=dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc),
        mgmt_cfg={**settings.section("management"), "partial_tp_r": 2.0}, profile=settings.profile("crypto"),
        size_step=1.0,
    )
    assert not [a for a in actions if a.kind == "partial"]
    assert partial_size(pos, 0.5, "crypto", 1.0) == 0.0


def test_breakeven_stop_covers_the_round_trip_costs(settings):
    pos = make_position()
    stop = breakeven_stop(pos, profile=settings.profile("crypto"), spread_bps=4.0)
    assert stop > pos.entry
    assert (stop - pos.entry) / pos.entry < 0.01  # but only by the cost, not by much


def test_runner_is_capped_only_when_configured(settings):
    pos = make_position(partial_done=True, size=0.5, initial_size=1.0)
    capped = manage_position(
        pos, closed_bar={"high": 115.0, "low": 112.0, "close": 114.0}, price=114.0, atr=1.0,
        now=dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc),
        mgmt_cfg={**settings.section("management"), "runner_target_r": 3.0},
        profile=settings.profile("crypto"),
    )
    assert any(a.kind == "close" and "runner" in a.reason for a in capped)
    # without a runner cap the trade keeps running (the ATR trail decides)
    pos2 = make_position(partial_done=True, size=0.5, initial_size=1.0)
    free = manage_position(
        pos2, closed_bar={"high": 200.0, "low": 112.0, "close": 199.0}, price=199.0, atr=1.0,
        now=dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc),
        mgmt_cfg={**settings.section("management"), "runner_target_r": None},
        profile=settings.profile("crypto"),
    )
    assert not [a for a in free if a.kind == "close"]


def test_paper_broker_partial_close_releases_margin(settings):
    broker = PaperBroker(market="crypto", equity=1000.0, leverage=2, spread_bps=0,
                         profile={**settings.profile("crypto"), "slippage_bps": 0.0})
    broker.submit(Order(symbol="ETH/USDT:USDT", side="buy", quantity=1.0, price_hint=100.0,
                        stop=95, target=110, client_id="p1"))
    locked_before = broker.locked_margin
    out = broker.partial_close("ETH/USDT:USDT", 0.5, 105.0)
    assert out and out["remaining"] == pytest.approx(0.5)
    assert broker.locked_margin < locked_before
    trade = broker.close("ETH/USDT:USDT", 110.0, "runner target")
    assert trade is not None
    assert trade.pnl > 0


# --------------------------------------------------------------------------- reconciliation
def test_reconcile_restores_a_matching_position(tmp_path, settings):
    engine = TradingEngine(market="crypto", settings=settings, symbols=["BTC/USDT:USDT"], state_dir=tmp_path)
    engine.positions["BTC/USDT:USDT"] = make_position(symbol="BTC/USDT:USDT")
    engine.broker.positions["BTC/USDT:USDT"] = {
        "symbol": "BTC/USDT:USDT", "side": "long", "entry": 100.0, "size": 1.0,
        "stop": 98.0, "target": 106.0, "margin": 50.0, "fees": 0.0, "order": {},
    }
    report = engine.startup_sync()
    assert [r["symbol"] for r in report["restored"]] == ["BTC/USDT:USDT"]
    assert report["closed"] == [] and report["untracked"] == []


def test_reconcile_closes_state_when_the_venue_is_flat(tmp_path, settings):
    engine = TradingEngine(market="crypto", settings=settings, symbols=["BTC/USDT:USDT"], state_dir=tmp_path)
    engine.positions["BTC/USDT:USDT"] = make_position(symbol="BTC/USDT:USDT")
    report = engine.startup_sync()
    assert report["closed"] and report["closed"][0]["symbol"] == "BTC/USDT:USDT"
    assert "BTC/USDT:USDT" not in engine.positions


def test_reconcile_reports_untracked_venue_positions(tmp_path, settings):
    engine = TradingEngine(market="crypto", settings=settings, symbols=["BTC/USDT:USDT"], state_dir=tmp_path)
    engine.broker.positions["BTC/USDT:USDT"] = {
        "symbol": "BTC/USDT:USDT", "side": "short", "entry": 100.0, "size": 1.0,
        "stop": 102.0, "target": 90.0, "margin": 50.0, "fees": 0.0, "order": {},
    }
    report = engine.startup_sync()
    assert report["untracked"] and report["untracked"][0]["symbol"] == "BTC/USDT:USDT"
    # and the symbol is blocked from new entries until a human looks
    assert engine.gap_cooldowns.get("BTC/USDT:USDT", 0) > 0

    engine2 = TradingEngine(market="crypto", settings=settings, symbols=["BTC/USDT:USDT"],
                            state_dir=tmp_path / "adopt")
    engine2.broker.positions["BTC/USDT:USDT"] = {
        "symbol": "BTC/USDT:USDT", "side": "short", "entry": 100.0, "size": 1.0,
        "stop": 102.0, "target": 90.0, "margin": 50.0, "fees": 0.0, "order": {},
    }
    adopted = engine2.startup_sync(adopt_untracked=True)
    assert adopted["adopted"] and "BTC/USDT:USDT" in engine2.positions


def test_reconcile_blocks_when_the_venue_cannot_answer(tmp_path, settings):
    engine = TradingEngine(market="crypto", settings=settings, symbols=["BTC/USDT:USDT"], state_dir=tmp_path)
    engine.positions["BTC/USDT:USDT"] = make_position(symbol="BTC/USDT:USDT")
    engine._venue_positions = lambda: None  # type: ignore[assignment]
    report = engine.startup_sync()
    assert report["blocked"] and not report["restored"]
    assert engine.gap_cooldowns.get("BTC/USDT:USDT", 0) > 0


def test_reconcile_runs_once_before_the_first_cycle(tmp_path, settings):
    engine = TradingEngine(market="crypto", settings=settings, symbols=["BTC/USDT:USDT"], state_dir=tmp_path)
    engine.cycle()
    assert engine.reconciled, "the first cycle must reconcile before scanning"
    assert engine.status.reconciled.get("venue_available") is True


def test_cost_check_defers_an_entry_when_the_spread_blows_out(tmp_path, settings):
    engine = TradingEngine(market="crypto", settings=settings, symbols=["BTC/USDT:USDT"], state_dir=tmp_path)
    bars = demo.crypto_bars("BTC/USDT:USDT", "15m", 300)
    engine.last_scans["BTC/USDT:USDT"] = {
        "filter": {"checks": [{"name": "spread", "value": 400.0, "passed": False}]},
        "signal": {"atr": 50.0},
        "quote": {"price": 96_000.0},
    }
    plan = risk.build_plan(
        symbol="BTC/USDT:USDT", market="crypto", side="long", entry=96_000.0, atr=50.0, bars=bars,
        equity=1000.0, config=settings.section("signal"), risk_cfg=settings.section("risk"),
        profile=settings.profile("crypto"), spread_bps=3.0,
    ).plan
    verdict = engine.cost_check("BTC/USDT:USDT", plan)
    assert verdict["ok"] is False
    assert "spread" in verdict["reason"]


# --------------------------------------------------------------------------- live partials
def test_live_partial_is_booked_once_and_never_repeats(tmp_path, settings):
    """The 2R partial must fire through the broker, persist `partial_done`, and not repeat."""
    from tests.test_api import _ready_payload
    from trading_office.market import demo

    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path / "partials")
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    position = engine.positions["TEST/USDT:USDT"]
    initial_size = position.size
    assert not position.partial_done

    bars = demo.crypto_bars("BTC/USDT:USDT", "15m", 300)
    two_r = position.entry + 2 * (position.entry - position.stop)
    at_2r = dict(bars[-2], high=two_r + position.entry * 0.001, low=position.entry,
                 close=two_r + position.entry * 0.0005)
    feed = {"bars": bars[:-1] + [at_2r], "htf_bars": bars, "quote": {"price": at_2r["close"]},
            "source": "synthetic", "warnings": [], "stale": False, "data_age_s": 1}
    engine.fetch = lambda symbol: feed  # type: ignore[assignment]

    engine.manage_open_positions()
    assert engine.status.partials == 1
    position = engine.positions["TEST/USDT:USDT"]
    assert position.partial_done is True
    assert position.size == pytest.approx(initial_size / 2, rel=0.05)
    assert position.stop > position.entry  # break-even + costs, so the runner is free
    saved = engine.state.positions().get("TEST/USDT:USDT", {})
    assert saved.get("partial_done") is True

    events = [e["event"] for e in engine.state.ledger(50)]
    assert "partial" in events
    # a second management pass can never re-sell the same half
    engine.manage_open_positions()
    assert engine.status.partials == 1


def test_closed_trade_includes_banked_partial_pnl(tmp_path, settings):
    from tests.test_api import _ready_payload

    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path / "banked")
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    position = engine.positions["TEST/USDT:USDT"]
    banked = engine.broker.partial_close("TEST/USDT:USDT", position.size / 2,
                                         position.entry + 2.0, reason="2R partial")
    assert banked
    position.size -= banked["size"]
    position.realised_pnl += banked["pnl"]
    trade = engine.broker.close("TEST/USDT:USDT", position.entry, "trailing stop hit (profit locked)")
    engine._record_close("TEST/USDT:USDT", trade)
    closed = engine.state.closed_trades()[-1]
    assert closed["pnl"] == pytest.approx(trade.pnl, abs=1e-6)
    assert closed["pnl"] > 0  # the partial alone paid for the flat runner


def test_engine_does_not_fake_a_stop_move_the_venue_refused(tmp_path, settings):
    """If the venue refuses to move the stop, the local book must not pretend it moved."""
    from tests.test_api import _ready_payload
    from trading_office.market import demo

    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path / "stopmove")
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    position = engine.positions["TEST/USDT:USDT"]
    engine.broker.modify_stop = lambda symbol, stop: {"ok": False, "reason": "no resting stop"}  # type: ignore

    bars = demo.crypto_bars("BTC/USDT:USDT", "15m", 300)
    runner_bar = dict(bars[-2], high=position.entry + 2.2, low=position.entry + 1.5,
                      close=position.entry + 2.0)
    feed = {"bars": bars[:-1] + [runner_bar], "htf_bars": bars, "quote": {"price": runner_bar["close"]},
            "source": "synthetic", "warnings": [], "stale": False, "data_age_s": 1}
    engine.fetch = lambda symbol: feed  # type: ignore[assignment]

    before = position.stop
    engine.manage_open_positions()
    # the bar only reaches ~1.3R, so this is a trailing move, not the partial
    assert engine.status.partials == 0
    # the venue refused the move, so the local stop stayed honest
    assert position.stop == before
    events = [e["event"] for e in engine.state.ledger(60)]
    assert "stop_move_failed" in events
    assert any("stop not moved" in err for err in engine.status.last_errors)


# --------------------------------------------------------------------------- kill switch
def _close_trade(engine, pnl: float, when: str) -> None:
    engine.state.append_ledger(
        {"event": "trade_closed", "market": "crypto", "symbol": "TEST/USDT:USDT", "side": "long",
         "entry": 100.0, "exit": 100.0 + pnl, "size": 1.0, "pnl": pnl, "closed_at": when}
    )


def test_kill_switch_blocks_after_the_daily_loss_limit(tmp_path, settings):
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path / "guard")
    _close_trade(engine, -30.0, f"{today}T10:00:00+00:00")  # -3 % of a 1000 USDT book
    guard = engine.guard_state()
    assert guard["blocked"] is True
    assert any("daily loss" in r for r in guard["reasons"])
    assert guard["daily_loss_pct"] >= 2.0

    from tests.test_api import _ready_payload

    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    assert "TEST/USDT:USDT" not in engine.positions
    assert engine.status.orders == 0
    assert any("kill switch" in (e.get("reason") or "") for e in engine.state.ledger(30))

    # a human override re-opens the day, and it survives a restart (same day only)
    engine.resume()
    assert engine.guard_state()["blocked"] is False
    restarted = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                             state_dir=tmp_path / "guard")
    assert restarted.guard_state()["blocked"] is False


def test_kill_switch_counts_consecutive_losses(tmp_path, settings):
    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path / "streak")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    for _ in range(4):
        _close_trade(engine, -0.5, f"{today}T11:00:00+00:00")
    guard = engine.guard_state()
    assert guard["consecutive_losses"] >= 4
    assert guard["blocked"] is True
    assert any("consecutive losses" in r for r in guard["reasons"])


# --------------------------------------------------------------------------- k_slip / shock
def test_slippage_coefficient_is_atr_units_and_ramps_in_a_shock():
    cfg = costs.CostConfig(atr_mult=0.06, shock_atr_mult=3.0, shock_slippage_mult=3.0)
    calm_k, stress = costs.slippage_coefficient(atr=700, atr_baseline=700, cfg=cfg)
    assert calm_k == pytest.approx(0.06) and stress == pytest.approx(1.0)
    # 3x its own 50-bar mean ATR = a shock bar: the coefficient triples
    shock_k, stress = costs.slippage_coefficient(atr=2100, atr_baseline=700, cfg=cfg)
    assert shock_k == pytest.approx(0.18) and stress == pytest.approx(3.0)
    # between the two it ramps (no cliff), never below the base coefficient
    mid_k, _ = costs.slippage_coefficient(atr=1400, atr_baseline=700, cfg=cfg)
    assert 0.06 < mid_k < 0.18
    assert costs.slippage_coefficient(atr=0, atr_baseline=700, cfg=cfg)[0] == pytest.approx(0.06)


def test_fill_offset_equals_k_slip_times_atr_in_price_terms():
    """The ATR-fraction model (open ± k·ATR) is what the bps model computes."""
    cfg = costs.CostConfig(slippage_base_bps=0.0, atr_mult=0.1, impact_coeff=0.0,
                           max_slippage_bps=10_000, shock_atr_mult=100.0)
    price, atr = 100.0, 2.0
    est = costs.estimate_slippage(price=price, atr=atr, notional=0.0, quote_volume=None, cfg=cfg)
    assert est.fill_price - price == pytest.approx(0.1 * atr, rel=1e-9)
    short_fill = price - (est.fill_price - price)
    assert short_fill == pytest.approx(price - 0.1 * atr)


def test_shock_bar_pays_more_than_the_same_bar_in_a_quiet_tape():
    cfg = costs.CostConfig()
    quiet = costs.estimate_slippage(price=96_400.0, atr=700.0, notional=1500.0,
                                    quote_volume=5e7, cfg=cfg, atr_baseline=700.0)
    shock = costs.estimate_slippage(price=96_400.0, atr=2100.0, notional=1500.0,
                                    quote_volume=5e7, cfg=cfg, atr_baseline=700.0)
    assert shock.slippage_bps > quiet.slippage_bps
    assert shock.components["k_slip"] > quiet.components["k_slip"]
    assert shock.components["atr_stress"] >= 2.9


def test_backtest_reports_the_effective_slippage_coefficient():
    from trading_office.backtest import Backtester

    settings = Settings.load()
    bt = Backtester(market="crypto", settings=settings)
    result = bt.run("SOL/USDT:USDT", demo.crypto_bars("SOL/USDT:USDT", "15m", 800))
    report = result.metrics()["costs"]
    if report["entries"]:
        assert report["avg_k_slip"] and report["avg_k_slip"] > 0
        assert "shock_fills" in report


# --------------------------------------------------------------------------- metrics
def test_backtest_metrics_include_drawdown_curve_and_mtm_drawdown():
    from trading_office.backtest import Backtester

    settings = Settings.load()
    result = Backtester(market="crypto", settings=settings).run(
        "SOL/USDT:USDT", demo.crypto_bars("SOL/USDT:USDT", "15m", 1200)
    )
    metrics = result.metrics()
    payload = result.to_dict()
    curve = payload["equity_curve"]
    assert curve and {"time", "equity", "peak", "drawdown_pct"} <= set(curve[0])
    assert metrics["max_drawdown_pct"] >= 0
    # the mark-to-market drawdown counts open excursions, so it can only be worse
    assert metrics["max_drawdown_pct_mtm"] >= metrics["max_drawdown_pct"]
    assert metrics["avg_mae_r"] >= 0
    if metrics["trades"]:
        worst_curve = max(p["drawdown_pct"] for p in curve)
        assert worst_curve >= metrics["max_drawdown_pct"] - 0.01


def test_live_performance_matches_the_ledger(tmp_path, settings):
    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path / "perf")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    for pnl in (12.0, -4.0, 8.0):
        _close_trade(engine, pnl, f"{today}T10:00:00+00:00")
    perf = engine.performance()
    assert perf["trades"] == 3
    assert perf["gross_profit"] == pytest.approx(20.0)
    assert perf["gross_loss"] == pytest.approx(4.0)
    assert perf["profit_factor"] == pytest.approx(5.0)
    assert perf["net_pnl"] == pytest.approx(16.0)
    assert perf["net_profit_pct"] == pytest.approx(1.6, rel=0.01)
    assert perf["max_drawdown_pct"] > 0  # the -4 after +12 is a real trough
    curve = engine._equity_curve()
    assert {"peak", "drawdown_pct"} <= set(curve[-1])


def test_atr_baseline_is_a_rolling_mean_not_a_fixed_percentage(settings):
    from trading_office.backtest import Backtester

    bt = Backtester(market="crypto", settings=settings)
    bars = demo.crypto_bars("BTC/USDT:USDT", "15m", 200)
    baseline = bt._atr_baseline(bars)
    assert baseline and baseline > 0
    assert bt._atr_baseline(bars[:10]) is None  # too little history to call anything a shock

    # double every bar's range: ATR and its own mean move together, so the *stress ratio*
    # stays ~1 and the guard does not fire on a permanently volatile instrument
    wide = [dict(b, high=(b["high"] + b["low"]) / 2 + (b["high"] - (b["high"] + b["low"]) / 2) * 2,
                 low=(b["high"] + b["low"]) / 2 - ((b["high"] + b["low"]) / 2 - b["low"]) * 2)
            for b in bars]
    wide_baseline = bt._atr_baseline(wide)
    ratio = wide_baseline / baseline
    assert 1.8 < ratio < 2.2


def test_a_flash_crash_bar_makes_the_fill_coefficient_climb(settings):
    from trading_office.backtest import Backtester

    bt = Backtester(market="crypto", settings=settings)
    bars = [dict(b) for b in demo.crypto_bars("BTC/USDT:USDT", "15m", 300)]
    crash = dict(bars[-1])
    mid = crash["close"]
    crash.update(open=mid * 0.97, high=mid * 1.06, low=mid * 0.90, close=mid * 0.93)
    bars[-1] = crash
    baseline = bt._atr_baseline(bars[:-1])
    atr_now = ind.atr([b["high"] for b in bars], [b["low"] for b in bars],
                      [b["close"] for b in bars], 14)[-1]
    k, stress = costs.slippage_coefficient(atr=atr_now, atr_baseline=baseline, cfg=bt.cost_cfg)
    assert stress > 1.5
    assert k > bt.cost_cfg.atr_mult


# --------------------------------------------------------------------------- crash restore
def test_startup_sync_restores_from_the_snapshot_when_positions_json_is_lost(tmp_path, settings):
    """A crash between two writes must not lose the position: the snapshot + venue win."""
    from tests.test_api import _ready_payload

    root = tmp_path / "crash"
    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"], state_dir=root)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    size = engine.positions["TEST/USDT:USDT"].size
    state_dir = engine.state.root
    assert (state_dir / "snapshot.json").exists()

    (state_dir / "positions.json").unlink()  # the crash lost the last write

    fresh = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"], state_dir=root)
    fresh.broker.positions["TEST/USDT:USDT"] = {
        "symbol": "TEST/USDT:USDT", "side": "long", "entry": 100.05, "size": size,
        "stop": 98.5, "target": 104.5, "margin": 100.0, "fees": 0.0, "order": {},
    }
    report = fresh.startup_sync()
    assert report["restored_from_disk"] == 1
    assert [r["symbol"] for r in report["restored"]] == ["TEST/USDT:USDT"]
    assert report["closed"] == [] and report["untracked"] == []
    # the venue's entry wins over the stale local one
    assert fresh.positions["TEST/USDT:USDT"].entry == pytest.approx(100.05)
    # and the reconciled truth becomes the baseline for the next restart
    assert (state_dir / "snapshot_reconciled.json").exists()


def test_startup_sync_still_closes_ghosts_that_the_snapshot_remembers(tmp_path, settings):
    from tests.test_api import _ready_payload

    root = tmp_path / "ghost"
    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"], state_dir=root)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    state_dir = engine.state.root
    (state_dir / "positions.json").unlink()

    fresh = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"], state_dir=root)
    report = fresh.startup_sync()  # venue is flat: the restored ghost must be dropped
    assert report["restored_from_disk"] == 1
    assert [r["symbol"] for r in report["closed"]] == ["TEST/USDT:USDT"]
    assert fresh.positions == {}


def test_snapshot_is_written_on_every_position_change(tmp_path, settings):
    import json

    from tests.test_api import _ready_payload

    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path / "snap")
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    path = engine.state.snapshot_path
    saved = json.loads(path.read_text())
    assert "TEST/USDT:USDT" in saved["positions"]
    engine.broker.modify_stop("TEST/USDT:USDT", 99.0)
    engine.positions["TEST/USDT:USDT"].stop = 99.0
    engine._persist_snapshot()
    assert json.loads(path.read_text())["positions"]["TEST/USDT:USDT"]["stop"] == 99.0


# --------------------------------------------------------------------------- manual controls
def test_active_trades_rows_carry_the_live_numbers(tmp_path, settings):
    from tests.test_api import _ready_payload

    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path / "rows")
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    rows = engine.active_trades({"TEST/USDT:USDT": 102.0})
    assert len(rows) == 1
    row = rows[0]
    assert {"symbol", "side", "size", "entry", "stop", "target", "mark", "pnl", "r_multiple"} <= set(row)
    assert row["side"] == "LONG"
    assert row["pnl"] > 0
    assert row["r_multiple"] == pytest.approx(row["pnl"] / (engine.positions["TEST/USDT:USDT"].risk_per_unit
                                                           * row["size"]), rel=0.02)
    # no marks -> the entry is the mark and the row is flat, never an error
    assert engine.active_trades()[0]["pnl"] == pytest.approx(0.0, abs=1e-9)


def test_close_position_only_touches_its_own_symbol(tmp_path, settings):
    from tests.test_api import _ready_payload

    engine = TradingEngine(market="crypto", settings=settings, symbols=["A/USDT:USDT", "B/USDT:USDT"],
                          state_dir=tmp_path / "multi")
    engine._handle_entry("A/USDT:USDT", _ready_payload(settings, symbol="A/USDT:USDT"))
    other = _ready_payload(settings, symbol="B/USDT:USDT")
    other["bar_time"] = "2026-11-06T11:00:00+07:00"
    engine._handle_entry("B/USDT:USDT", other)
    assert len(engine.positions) == 2

    result = engine.close_position("A/USDT:USDT", reason="test")
    assert result["ok"] is True
    assert "A/USDT:USDT" not in engine.positions
    assert "B/USDT:USDT" in engine.positions
    assert engine.close_position("A/USDT:USDT")["ok"] is False  # idempotent, not an exception


def test_execution_gate_is_explicit_about_simulation(tmp_path, settings):
    paper = TradingEngine(market="crypto", settings=settings, symbols=["T/USDT:USDT"],
                          state_dir=tmp_path / "g1")
    gate = paper.execution_gate()
    assert gate["simulated"] is True and gate["live"] is False
    assert gate["broker"] == "paper" and "simulated" in gate["note"]

    dry = TradingEngine(market="crypto", settings=settings, symbols=["T/USDT:USDT"],
                        state_dir=tmp_path / "g2", execute=False)
    assert dry.execution_gate()["simulated"] is True
    assert "dry-run" in dry.execution_gate()["note"]
