"""API, engine and MCP tests. Skipped automatically if FastAPI is not installed."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

fastapi = pytest.importorskip("fastapi", reason="pip install fastapi uvicorn")

from fastapi.testclient import TestClient  # noqa: E402

from trading_office.api.server import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def test_health_reports_providers(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert "idx" in body["providers"] and "crypto" in body["providers"]
    assert body["execution"]["paper"] in (True, False)
    assert body["data_mode"] in ("live", "synthetic")


def test_config_endpoint_exposes_thresholds(client):
    body = client.get("/api/config").json()["config"]
    assert body["risk"]["risk_per_trade_pct"] > 0
    assert body["signal"]["min_score"] >= 1
    assert "crypto" in body["profiles"] and "idx" in body["profiles"]


def test_screen_endpoint_returns_ranked_rows(client):
    body = client.get("/api/screen?market=idx&limit=4&deep=false").json()
    assert body["scanned"] >= 1
    scores = [row["conviction"] for row in body["rows"]]
    assert scores == sorted(scores, reverse=True)
    for row in body["rows"]:
        assert row["label"] in ("STRONG_BUY", "BUY", "WATCH", "HOLD", "REDUCE", "SELL")
        assert row["source"] in ("idx", "synthetic")
        assert "filter_checks" in row["context"]


def test_symbol_detail_has_four_gates(client):
    body = client.get("/api/symbol/idx/BBCA").json()
    checks = body["scan"]["filter"]["checks"]
    assert [c["name"] for c in checks] == ["trend", "volatility", "session", "spread"]
    assert body["candles"], "candles are needed by the chart"
    assert "bandar" in body["context"]


def test_crypto_symbol_with_slash_route(client):
    body = client.get("/api/symbol/crypto/ETH/USDT:USDT")
    assert body.status_code == 200
    assert body.json()["row"]["market"] == "crypto"


def test_candles_series_align_with_bars(client):
    body = client.get("/api/candles/idx/BBCA?limit=150").json()
    assert len(body["bars"]) == 150
    for key, series in body["series"].items():
        assert len(series) == 150, key


def test_backtest_endpoint_reports_oos(client):
    body = client.post(
        "/api/backtest", json={"market": "crypto", "symbol": "XRP/USDT:USDT", "oos": 0.35, "bars": 800}
    ).json()
    assert "out_of_sample" in body and "in_sample" in body
    assert body["chosen_params"]
    metrics = body["out_of_sample"]["metrics"]
    assert "expectancy_r" in metrics and "max_drawdown_pct" in metrics


def test_backtest_reports_execution_costs(client):
    body = client.post(
        "/api/backtest", json={"market": "crypto", "symbol": "SOL/USDT:USDT", "oos": 0.3, "bars": 900}
    ).json()
    metrics = body["out_of_sample"]["metrics"]
    costs = metrics["costs"]
    assert costs["latency_ms"] == 400
    assert costs["slippage_bps"]["p50"] > 0
    assert costs["entries"] >= 0
    assert body["cost_model"]["max_slippage_bps"] == 45.0


def test_cost_model_endpoint_and_reconcile(client):
    model = client.get("/api/cost-model?market=idx").json()
    assert model["model"]["latency_ms"] == 900
    assert model["stress_spread_cap_bps"] > model["spread_cap_bps"]
    report = client.post("/api/engine/reconcile", json={"market": "crypto"}).json()
    assert report["venue_available"] is True
    assert "untracked" in report and "stops_missing" in report
    status = client.get("/api/engine/status?market=crypto").json()
    assert status["status"]["reconciled"]["venue_available"] is True
    assert status["costs"]["latency_ms"] == 400


def test_metrics_endpoint_feeds_the_live_charts(client):
    body = client.get("/api/metrics?market=crypto").json()
    assert body["status"] in ("success", "empty")
    assert len(body["timestamps"]) == len(body["equity"]) == len(body["drawdown"])
    assert body["series"] and {"equity", "peak", "drawdown_pct"} <= set(body["series"][0])
    assert "max_drawdown_pct" in body["performance"]
    perf = client.get("/api/performance?market=crypto").json()
    assert {"profit_factor", "max_drawdown_pct", "net_profit_pct"} <= set(perf)


def test_backtest_payload_has_a_drawdown_curve(client):
    body = client.post(
        "/api/backtest", json={"market": "crypto", "symbol": "SOL/USDT:USDT", "oos": 0.3, "bars": 800}
    ).json()
    assert body["drawdown_curve"]
    point = body["drawdown_curve"][-1]
    assert {"time", "equity", "peak", "drawdown_pct"} <= set(point)
    metrics = body["out_of_sample"]["metrics"]
    assert metrics["max_drawdown_pct_mtm"] >= metrics["max_drawdown_pct"]
    assert metrics["costs"]["avg_k_slip"] is not None


def test_engine_cycle_is_idempotent_on_the_same_bar(client):
    first = client.post("/api/engine/cycle", json={"market": "idx", "symbols": ["BBCA"]}).json()
    second = client.post("/api/engine/cycle", json={"market": "idx", "symbols": ["BBCA"]}).json()
    assert first["scans"][0]["bar_time"] == second["scans"][0]["bar_time"]
    status = client.get("/api/engine/status?market=idx").json()["status"]
    # the bar lock means at most one order per symbol per closed bar
    assert status["orders"] <= status["scans"]


def test_mcp_catalog_lists_tools(client):
    body = client.get("/api/mcp/tools").json()
    names = {tool["name"] for tool in body["tools"]}
    assert {"screen_market", "analyse_symbol", "run_backtest", "idx_bandarmology"} <= names
    assert "claude_code" in body["registration"]


def test_index_page_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Trading Office" in response.text


# --------------------------------------------------------------------------- engine paths
def _ready_payload(settings, symbol="TEST/USDT:USDT"):
    from trading_office import risk
    from trading_office.market import demo

    bars = demo.crypto_bars("BTC/USDT:USDT", "15m", 300)
    plan = risk.build_plan(
        symbol=symbol, market="crypto", side="long", entry=100.0, atr=1.0, bars=bars,
        equity=1000.0, config=settings.section("signal"), risk_cfg=settings.section("risk"),
        profile=settings.profile("crypto"), spread_bps=3.0,
    ).plan
    return {
        "symbol": symbol, "market": "crypto", "action": "LONG", "score": 7, "stage": "ready",
        "bar_time": "2026-11-06T10:00:00+07:00", "price": 100.0, "executable": True,
        "reasons": ["trend_alignment +2", "trigger +2", "macd_confirmation +1"],
        "risk": {"approved": True, "plan": plan.to_dict(), "rejections": []},
        "filter": {"checks": [{"name": "spread", "value": 3.0, "passed": True}]},
        "signal": {"atr": 1.0}, "indicators": {}, "warnings": [], "source": "synthetic",
    }


def test_engine_places_one_order_per_bar(tmp_path):
    from trading_office.config import Settings
    from trading_office.engine import TradingEngine

    settings = Settings.load()
    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path)
    payload = _ready_payload(settings)
    engine._handle_entry("TEST/USDT:USDT", payload)
    assert engine.status.orders == 1
    assert "TEST/USDT:USDT" in engine.positions

    # same bar again -> bar lock / open position guard, never a second order
    second = dict(payload)
    second["execution"] = {}
    engine._handle_entry("TEST/USDT:USDT", dict(payload))
    assert engine.status.orders == 1
    events = [e["event"] for e in engine.state.ledger(20)]
    assert "order" in events and "skip" in events


def test_engine_management_closes_on_stop(tmp_path, monkeypatch):
    from trading_office.config import Settings
    from trading_office.engine import TradingEngine
    from trading_office.market import demo

    settings = Settings.load()
    engine = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    position = engine.positions["TEST/USDT:USDT"]

    bars = demo.crypto_bars("BTC/USDT:USDT", "15m", 200)
    crash = dict(bars[-1], high=position.entry, low=position.stop * 0.9, close=position.stop * 0.95)
    monkeypatch.setattr(
        engine, "fetch",
        lambda symbol: {"bars": bars[:-1] + [crash], "htf_bars": bars, "quote": {"price": crash["close"]},
                        "source": "synthetic", "warnings": [], "stale": False, "data_age_s": 1},
    )
    engine.manage_open_positions()
    assert "TEST/USDT:USDT" not in engine.positions
    closed = engine.state.closed_trades()
    assert closed and closed[-1]["event"] == "trade_closed"
    assert closed[-1]["pnl"] < 0
    assert "stop" in closed[-1]["reason"]


def test_engine_restores_positions_after_restart(tmp_path):
    from trading_office.config import Settings
    from trading_office.engine import TradingEngine

    settings = Settings.load()
    first = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"], state_dir=tmp_path)
    first._handle_entry("TEST/USDT:USDT", _ready_payload(settings))
    again = TradingEngine(market="crypto", settings=settings, symbols=["TEST/USDT:USDT"], state_dir=tmp_path)
    assert "TEST/USDT:USDT" in again.positions
    assert again.positions["TEST/USDT:USDT"].entry == first.positions["TEST/USDT:USDT"].entry
