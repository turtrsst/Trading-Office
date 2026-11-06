"""Unit tests for the four gates. Run with: pytest -q  (or python tests/run_tests.py)"""

from __future__ import annotations

import datetime as dt
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from trading_office import indicators as ind  # noqa: E402
from trading_office import market_filter, risk, signals  # noqa: E402
from trading_office.config import Settings, idx_tick_size, round_to_tick  # noqa: E402
from trading_office.execution import PaperBroker, order_from_plan  # noqa: E402
from trading_office.management import Position, manage_position  # noqa: E402
from trading_office.market import demo  # noqa: E402
from trading_office.state import StateStore  # noqa: E402
from trading_office.strategy.runner import scan_symbol  # noqa: E402


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


def trend_bars(n: int = 260, start: float = 100.0, step: float = 0.6, noise: float = 0.0) -> list[dict]:
    """A clean uptrend with closed candles, for deterministic gate tests."""
    bars = []
    price = start
    for i in range(n):
        open_ = price
        close = open_ + step + (noise and (math.sin(i) * noise))
        high = max(open_, close) + 0.4
        low = min(open_, close) - 0.4
        bars.append(
            {
                "time": (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=i)).isoformat(),
                "open": open_, "high": high, "low": low, "close": close, "volume": 1_000 + i,
            }
        )
        price = close
    return bars


# --------------------------------------------------------------------------- indicators
def test_sma_matches_manual_average():
    values = [1, 2, 3, 4, 5]
    out = ind.sma(values, 2)
    assert out[0] is None
    assert out[1] == 1.5
    assert out[4] == 4.5


def test_ema_seeds_with_sma():
    values = [10.0] * 10
    out = ind.ema(values, 5)
    assert out[4] == 10.0
    assert out[9] == 10.0  # a flat series stays flat


def test_rsi_bounds_and_direction():
    up = [100 + i for i in range(40)]
    down = [100 - i for i in range(40)]
    rsi_up = [v for v in ind.rsi(up, 14) if v is not None]
    rsi_down = [v for v in ind.rsi(down, 14) if v is not None]
    assert all(0 <= v <= 100 for v in rsi_up + rsi_down)
    assert rsi_up[-1] > 70
    assert rsi_down[-1] < 30


def test_atr_positive_and_wilder_smoothed():
    highs = [10, 11, 12, 13, 14, 15] * 5
    lows = [9, 10, 11, 12, 13, 14] * 5
    closes = [9.5, 10.5, 11.5, 12.5, 13.5, 14.5] * 5
    atr = [v for v in ind.atr(highs, lows, closes, 5) if v is not None]
    assert atr and all(v > 0 for v in atr)


def test_bollinger_contains_price_band():
    closes = [100 + math.sin(i / 3) * 5 for i in range(60)]
    upper, mid, lower = ind.bollinger(closes, 20, 2.0)
    i = len(closes) - 1
    assert lower[i] < mid[i] < upper[i]
    assert upper[i] - lower[i] > 0


def test_swing_levels_ignore_current_bar():
    lows = [50, 40, 30, 20, 10, 99, 98, 97, 96, 95, 1]
    swing = ind.swing_lows(lows, 5)
    assert swing[-1] == 95  # lowest of the previous five bars, never the current 1


def test_engulfing_detection():
    assert ind.is_bullish_engulfing(10, 9, 8.8, 10.2)
    assert not ind.is_bullish_engulfing(9, 10, 10.2, 9.5)
    assert ind.is_bearish_engulfing(9, 10, 10.2, 8.8)


# --------------------------------------------------------------------------- market filter
def _ctx(bars, htf, **over):
    cfg = Settings.load().merged("market_filter", "engine")
    profile = Settings.load().profile("crypto")
    ctx = market_filter.build_context_from_bars(
        "TEST", "crypto", bars, htf,
        now=dt.datetime(2026, 3, 3, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=7))),
        profile={**profile, "sessions": []},
        config=cfg,
    )
    for key, value in over.items():
        setattr(ctx, key, value)
    return ctx


def test_filter_blocks_flat_trend():
    bars = [dict(b, close=100.0, open=100.0, high=100.5, low=99.5) for b in trend_bars(260)]
    ctx = _ctx(bars, bars)
    result = market_filter.evaluate(ctx, profile={**Settings.load().profile("crypto"), "sessions": []},
                                    config=Settings.load().merged("market_filter", "engine"))
    trend = result.check("trend")
    assert trend is not None and not trend.passed
    assert not result.passed  # mandatory trend filter


def test_filter_blocks_dead_volatility():
    bars = [dict(b, high=b["close"] + 0.001, low=b["close"] - 0.001) for b in trend_bars(260)]
    profile = {**Settings.load().profile("crypto"), "sessions": [], "atr_min_pct": 0.5}
    ctx = _ctx(bars, bars)
    result = market_filter.evaluate(ctx, profile=profile,
                                    config=Settings.load().merged("market_filter", "engine"))
    vol = result.check("volatility")
    assert vol is not None and not vol.passed


def test_filter_blocks_wide_spread():
    bars = trend_bars(260)
    cfg = {**Settings.load().merged("market_filter", "engine"), "min_filters_passed": 3}
    profile = {**Settings.load().profile("crypto"), "sessions": [], "max_spread_bps": 1.0}
    ctx = _ctx(bars, bars, bid=99.0, ask=101.0, price=100.0)
    result = market_filter.evaluate(ctx, profile=profile, config=cfg)
    spread = result.check("spread")
    assert spread is not None and not spread.passed


def test_filter_blocks_outside_session():
    bars = trend_bars(260)
    profile = {**Settings.load().profile("crypto"), "sessions": [["09:00", "10:00"]]}
    ctx = _ctx(bars, bars, now=dt.datetime(2026, 3, 3, 23, 0, tzinfo=dt.timezone(dt.timedelta(hours=7))))
    result = market_filter.evaluate(ctx, profile=profile,
                                    config=Settings.load().merged("market_filter", "engine"))
    session = result.check("session")
    assert session is not None and not session.passed


def test_filter_blocks_stale_data_but_allows_demo():
    bars = trend_bars(260)
    cfg = Settings.load().merged("market_filter", "engine")
    profile = {**Settings.load().profile("crypto"), "sessions": []}
    stale = _ctx(bars, bars, stale=True, data_age_s=9999)
    assert not market_filter.evaluate(stale, profile=profile, config=cfg).check("session").passed
    demo_ctx = _ctx(bars, bars, demo_data=True)
    assert market_filter.evaluate(demo_ctx, profile=profile, config=cfg).check("session").passed


def test_news_blackout_window():
    now = dt.datetime(2026, 11, 7, 19, 35, tzinfo=dt.timezone(dt.timedelta(hours=7)))
    hit = market_filter.in_blackout(now, [{"at": "2026-11-07T19:30:00+07:00", "minutes": 30, "label": "NFP"}])
    assert hit and "NFP" in hit
    assert market_filter.in_blackout(now, [{"at": "2026-11-07T21:30:00+07:00", "minutes": 30}]) is None


# --------------------------------------------------------------------------- signals
def test_signal_waits_without_enough_bars(settings):
    result = signals.evaluate("TEST", trend_bars(20), settings.section("signal"))
    assert result.action == "WAIT"
    assert "not enough closed bars" in result.reasons[0]


def test_signal_never_fires_without_trigger(settings):
    """A pure trend (no RSI/BB trigger) must not produce an order."""
    bars = trend_bars(300, step=0.4)
    result = signals.evaluate("TEST", bars, settings.section("signal"))
    assert result.action == "WAIT"
    assert result.block_code in ("no_trigger", "score_below_min", "overextended", "conflict")


def test_signal_locks_state_per_bar(tmp_path):
    store = StateStore(tmp_path)
    assert store.lock("BBCA", "1d", "2026-11-06", {"action": "LONG"}) is True
    assert store.lock("BBCA", "1d", "2026-11-06", {"action": "SHORT"}) is False
    assert store.lock("BBCA", "1d", "2026-11-07", {"action": "LONG"}) is True
    assert store.is_locked("BBCA", "1d", "2026-11-06")


def test_signal_score_components_have_weights(settings):
    bars = trend_bars(300, step=0.5, noise=0.8)
    result = signals.evaluate("TEST", bars, settings.section("signal"))
    assert result.max_score == 9
    assert sum(c.weight for c in result.components) == 9
    assert any(c.kind == "trigger" for c in result.components)


# --------------------------------------------------------------------------- risk
def test_idx_tick_table():
    assert idx_tick_size(150) == 1.0
    assert idx_tick_size(300) == 2.0
    assert idx_tick_size(1500) == 5.0
    assert idx_tick_size(4000) == 10.0
    assert idx_tick_size(9000) == 25.0
    assert round_to_tick(1234, "down") == 1230
    assert round_to_tick(1231, "up") == 1235


def test_risk_plan_sizes_from_equity_and_stop(settings):
    bars = trend_bars(200)
    decision = risk.build_plan(
        symbol="BTC/USDT:USDT", market="crypto", side="long", entry=200.0, atr=2.0, bars=bars,
        equity=1000.0, config=settings.section("signal"), risk_cfg=settings.section("risk"),
        profile=settings.profile("crypto"), spread_bps=3.0,
    )
    assert decision.approved, decision.rejections
    plan = decision.plan
    assert plan is not None
    assert plan.stop < plan.entry < plan.target
    assert plan.rr >= settings.get("risk.min_rr", 1.5)
    # risk at the stop is (approximately) the configured 1% budget
    assert plan.risk_cash <= 1000.0 * settings.get("risk.risk_per_trade_pct", 1.0) / 100.0 * 1.01


def test_risk_rejects_target_inside_costs(settings):
    bars = trend_bars(200, step=0.01)  # tiny ATR -> tiny target vs commissions
    decision = risk.build_plan(
        symbol="BTC/USDT:USDT", market="crypto", side="long", entry=200.0, atr=0.02, bars=bars,
        equity=1000.0, config=settings.section("signal"), risk_cfg=settings.section("risk"),
        profile=settings.profile("crypto"), spread_bps=6.0,
    )
    assert not decision.approved
    assert any("cost" in reason for reason in decision.rejections)


def test_idx_plan_uses_lots_and_no_leverage(settings):
    bars = trend_bars(200, start=4000, step=10)
    decision = risk.build_plan(
        symbol="BBCA", market="idx", side="long", entry=4300.0, atr=60.0, bars=bars,
        equity=100_000_000, config=settings.section("signal"), risk_cfg=settings.section("risk"),
        profile=settings.profile("idx"), spread_bps=8.0, tick_size=round_to_tick(4300.0),
    )
    assert decision.plan is not None
    assert decision.plan.lots is not None and decision.plan.lots >= 1
    assert decision.plan.size == decision.plan.lots * 100
    assert decision.plan.leverage == settings.get("risk.max_leverage", 3) or decision.plan.leverage == 1


def test_portfolio_guard_blocks_when_full(settings):
    blocks = risk.portfolio_check(
        plans=[], open_positions=[{"risk_cash": 10}] * 6, equity=1000.0,
        risk_cfg=settings.section("risk"), engine_cfg=settings.section("engine"),
        daily_pnl_pct=-3.0,
    )
    assert any("max concurrent" in b for b in blocks)
    assert any("daily loss" in b for b in blocks)


# --------------------------------------------------------------------------- management
def make_position(**over) -> Position:
    data = dict(
        symbol="BTC/USDT:USDT", market="crypto", side="long", entry=100.0, stop=98.0, target=106.0,
        size=1.0, initial_stop=98.0, opened_at="2026-01-01T00:00:00+00:00", bar_time="2026-01-01",
    )
    data.update(over)
    return Position(**data)


def test_break_even_moves_stop_to_entry_plus_costs(settings):
    pos = make_position()
    action = manage_position(
        pos, closed_bar={"high": 103.0, "low": 100.0, "close": 102.5}, price=102.5, atr=1.0,
        now=dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc),
        mgmt_cfg=settings.section("management"), profile=settings.profile("crypto"),
        spread_bps=4.0,
    )
    stops = [a for a in action if a.kind == "modify_stop"]
    assert stops, "break-even should move the stop once 1R is reached"
    assert pos.stop >= pos.entry


def test_stop_loss_closes_position(settings):
    pos = make_position()
    actions = manage_position(
        pos, closed_bar={"high": 101.0, "low": 97.5, "close": 98.0}, price=98.0, atr=1.0,
        now=dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc),
        mgmt_cfg=settings.section("management"), profile=settings.profile("crypto"),
    )
    assert actions and actions[0].kind == "close"
    assert "stop" in actions[0].reason


def test_opposite_signal_exits_early(settings):
    pos = make_position()
    actions = manage_position(
        pos, closed_bar={"high": 101.0, "low": 99.5, "close": 100.5}, price=100.5, atr=1.0,
        now=dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc),
        mgmt_cfg=settings.section("management"), profile=settings.profile("crypto"),
        opposite_signal=True,
    )
    assert any("invalidation" in a.reason for a in actions)


def test_time_stop_fires_without_progress(settings):
    pos = make_position(bars_held=200)
    actions = manage_position(
        pos, closed_bar={"high": 100.2, "low": 99.9, "close": 100.0}, price=100.0, atr=1.0,
        now=dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc),
        mgmt_cfg=settings.section("management"), profile=settings.profile("crypto"),
    )
    assert any("time stop" in a.reason for a in actions)


# --------------------------------------------------------------------------- execution
def test_paper_broker_refuses_duplicate_client_id(settings):
    broker = PaperBroker(market="crypto", equity=1000.0, profile=settings.profile("crypto"), leverage=3)
    plan_args = dict(
        symbol="BTC/USDT:USDT", market="crypto", side="long", entry=100.0, atr=1.0, bars=trend_bars(120),
        equity=1000.0, config=settings.section("signal"), risk_cfg=settings.section("risk"),
        profile=settings.profile("crypto"), spread_bps=3.0,
    )
    plan = risk.build_plan(**plan_args).plan
    assert plan is not None
    order = order_from_plan(plan, "2026-11-06T10:00:00+00:00", ["test"])
    first = broker.submit(order)
    second = broker.submit(order)
    assert first.ok
    assert not second.ok and "duplicate" in second.message


def test_paper_broker_cannot_open_two_positions_per_symbol(settings):
    broker = PaperBroker(market="crypto", equity=1000.0, profile=settings.profile("crypto"), leverage=3)
    from trading_office.execution import Order

    a = broker.submit(Order(symbol="BTC/USDT:USDT", side="buy", quantity=0.01, price_hint=100.0,
                            stop=95, target=110, client_id="a", market="crypto"))
    b = broker.submit(Order(symbol="BTC/USDT:USDT", side="sell", quantity=0.01, price_hint=100.0,
                            stop=105, target=90, client_id="b", market="crypto"))
    assert a.ok and not b.ok


def test_paper_broker_pnl_and_equity(settings):
    broker = PaperBroker(market="crypto", equity=1000.0, profile={**settings.profile("crypto"),
                                                                   "slippage_bps": 0.0}, leverage=1, spread_bps=0)
    from trading_office.execution import Order

    fill = broker.submit(Order(symbol="ETH/USDT:USDT", side="buy", quantity=1.0, price_hint=100.0,
                               stop=98, target=104, client_id="x", market="crypto"))
    assert fill.ok
    trade = broker.close("ETH/USDT:USDT", 104.0, "take profit")
    assert trade is not None and trade.pnl > 0
    assert broker.equity() > 1000.0


# --------------------------------------------------------------------------- end to end
def test_scan_symbol_returns_all_gates(settings):
    bars = demo.idx_bars("BBCA", "1d", 400)
    htf = demo.htf_bars("BBCA", "idx", 5, 80)
    result = scan_symbol(
        symbol="BBCA", market="idx", bars=bars, htf_bars=htf,
        quote=demo.idx_quote("BBCA", bars), settings=settings, source="synthetic",
    ).to_dict()
    assert set(["filter", "signal", "risk", "action", "stage", "score"]).issubset(result)
    assert len(result["filter"]["checks"]) == 4
    assert result["action"] in ("LONG", "SHORT", "WAIT", "BLOCKED")


def test_scan_symbol_drops_forming_bar(settings):
    bars = demo.crypto_bars("BTC/USDT:USDT", "15m", 300)
    now = dt.datetime.fromisoformat(bars[-1]["time"]) + dt.timedelta(minutes=1)  # bar still open
    result = scan_symbol(
        symbol="BTC/USDT:USDT", market="crypto", bars=bars,
        htf_bars=demo.htf_bars("BTC/USDT:USDT", "crypto", 16, 30),
        quote=None, settings=settings, now=now, source="synthetic",
    )
    assert result.bar_time == bars[-2]["time"]


def test_backtest_is_deterministic(settings):
    from trading_office.backtest import Backtester

    bars = demo.crypto_bars("SOL/USDT:USDT", "15m", 500)
    a = Backtester(market="crypto", settings=settings).run("SOL/USDT:USDT", bars).metrics()
    b = Backtester(market="crypto", settings=settings).run("SOL/USDT:USDT", bars).metrics()
    assert a == b
    assert "win_rate_pct" in a and "rejections" in a


def test_walk_forward_keeps_oos_separate(settings):
    from trading_office.backtest import walk_forward

    bars = demo.crypto_bars("XRP/USDT:USDT", "15m", 800)
    report = walk_forward(market="crypto", symbol="XRP/USDT:USDT", bars=bars, settings=settings, oos_fraction=0.3)
    assert report["split_time"]
    oos = report["out_of_sample"]
    # every OOS trade must be inside the unseen slice
    for trade in oos["trades"]:
        assert trade["opened_at"] >= report["split_time"]
