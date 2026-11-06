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


def test_metrics_payload_carries_the_kpi_block(client):
    body = client.get("/api/metrics?market=crypto").json()
    assert {"win_rate", "profit_factor", "total_trades"} <= set(body["kpi"])
    assert len(body["drawdown"]) == len(body["drawdown_pct"]) == len(body["equity"])


def test_websocket_pushes_metrics_frames(client):
    with client.websocket_connect("/api/ws/metrics?market=crypto") as ws:
        first = ws.receive_json()
        assert first["type"] == "metrics" and first["seq"] == 1
        assert {"equity", "drawdown_pct", "kpi", "series"} <= set(first)
        ws.send_text('{"type":"pong"}')  # the client keeps the connection alive
        second = ws.receive_json()
        assert second["seq"] == 2
        assert second["market"] == "crypto"


def test_websocket_can_switch_market(client):
    with client.websocket_connect("/api/ws/metrics?market=idx") as ws:
        frame = ws.receive_json()
        assert frame["market"] == "idx"
        assert "total_trades" in frame["kpi"]


def test_metrics_payload_carries_the_book(client):
    body = client.get("/api/metrics?market=crypto").json()
    assert "active_trades" in body and "open_orders" in body
    assert body["execution_gate"]["simulated"] is True
    assert body["execution_gate"]["mode"] in ("paper", "dry-run", "live")


def test_close_position_endpoint_refuses_unknown_symbols(client):
    body = client.post("/api/trade/close", json={"market": "crypto", "symbol": "NOPE/USDT:USDT"}).json()
    assert body["ok"] is False
    assert "no open position" in body["reason"]


def test_close_position_endpoint_closes_one_position(tmp_path):
    from fastapi.testclient import TestClient

    from trading_office.api import server as server_module
    from trading_office.config import Settings
    from trading_office.engine import TradingEngine

    engine = TradingEngine(market="crypto", settings=Settings.load(), symbols=["TEST/USDT:USDT"],
                           state_dir=tmp_path / "close", execute=True)
    engine._handle_entry("TEST/USDT:USDT", _ready_payload(Settings.load()))
    engine.fetch = lambda symbol: {"bars": [], "htf_bars": [], "quote": {"price": 101.0},
                                   "source": "synthetic", "warnings": [], "stale": False,
                                   "data_age_s": 1}
    original = server_module._ENGINES.get("crypto")
    server_module._ENGINES["crypto"] = engine
    try:
        client = TestClient(server_module.app)
        body = client.post("/api/trade/close", json={"market": "crypto", "symbol": "TEST/USDT:USDT"}).json()
        assert body["ok"] is True
        assert body["closed"]["symbol"] == "TEST/USDT:USDT"
        assert body["closed"]["pnl"] > 0  # closed at 101 vs entry ~100.03
        assert "TEST/USDT:USDT" not in engine.positions
        events = [e["event"] for e in engine.state.ledger(40)]
        assert "manual_close" in events and "trade_closed" in events
    finally:
        if original is not None:
            server_module._ENGINES["crypto"] = original
        else:
            server_module._ENGINES.pop("crypto", None)


def test_cancel_order_endpoint_is_honest_on_paper(client):
    body = client.post("/api/order/cancel", json={"market": "crypto", "order_id": "abc"}).json()
    assert body["ok"] is False
    assert "paper" in body["reason"]


def test_security_endpoint_reports_presence_not_secrets(client, monkeypatch):
    from trading_office import env as env_module

    for name in ("TO_API_KEY", "CCXT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CCXT_API_KEY", "super-secret-value")

    body = client.get("/api/security").json()
    assert body["credentials"]["TO_API_KEY"]["set"] is True
    assert body["credentials"]["TO_API_KEY"]["via"] == "CCXT_API_KEY"
    assert "super-secret-value" not in str(body)
    assert body["state_encryption"]["encrypted"] in (True, False)
    ignored = {row["prefix"] for row in body["ignored"]}
    assert "MT5_" in ignored


def test_health_reports_encryption_and_secret_presence(client):
    body = client.get("/api/health").json()
    assert "secrets" in body and "state_encryption" in body
    assert "set" in body["secrets"]["TO_API_KEY"]


def test_engine_autostart_is_off_by_default_and_starts_when_configured():
    """A supervised deployment should not need somebody to press Start."""
    from trading_office.api import server as server_module

    # default: the API must come up idle, not start trading as a side effect of a request.
    # The shipped config file is the contract (this checkout may have it switched on for
    # the supervised deployment, which is exactly the point of the flag).
    from trading_office.config import DEFAULTS

    assert DEFAULTS["engine"].get("auto_start_engine", False) is False

    from trading_office.config import Settings

    saved = server_module._SETTINGS_OVERRIDE
    raw = {**server_module.SETTINGS.raw,
           "engine": {**server_module.SETTINGS.raw.get("engine", {}),
                      "auto_start_engine": True, "auto_start_markets": ["crypto"]}}
    server_module._SETTINGS_OVERRIDE = Settings(raw=raw, path=server_module.SETTINGS.path)
    try:
        server_module._ENGINES.pop("crypto", None)
        with TestClient(server_module.app):
            engine = server_module._ENGINES.get("crypto")
            assert engine is not None and engine.status.running is True
        # leaving the context stops the loop (no thread survives the server)
        assert server_module._ENGINES["crypto"].status.running is False
    finally:
        server_module._SETTINGS_OVERRIDE = saved
        server_module._ENGINES.pop("crypto", None)


# --------------------------------------------------------------------------- autonomy surface
def test_approvals_endpoint_lists_and_decides(client):
    from trading_office.api import server as server_module

    engine = server_module._engine("crypto")
    request = {
        "id": "testreq01", "market": "crypto", "symbol": "BTC/USDT:USDT", "side": "LONG",
        "score": 7, "reasons": ["trend_alignment +2"], "plan": {"entry": 68000, "stop": 67000,
        "target": 70000, "size": 0.01, "risk_pct": 0.3, "rr": 2.0},
        "bar_time": "2026-11-06T10:00:00+07:00",
        "created_at": server_module._iso_now(), "expires_at": server_module._iso_now(),
        "status": "pending",
    }
    engine.state.add_approval(request)

    listed = client.get("/api/approvals?market=crypto").json()
    assert any(row["id"] == "testreq01" for row in listed["requests"])
    assert listed["mode"] in ("ask", "auto", "off")
    assert "allowed" in listed["entries_gate"]

    rejected = client.post("/api/approvals/reject",
                           json={"market": "crypto", "request_id": "testreq01"}).json()
    assert rejected["ok"] is True
    assert engine.state.get_approval("testreq01")["status"] == "rejected"
    engine.state.approvals_path.unlink(missing_ok=True)


def test_arm_live_requires_confirmation_and_rotates_with_the_gate(client):
    from trading_office.api import server as server_module

    engine = server_module._engine("crypto")
    refused = client.post("/api/autonomy/arm", json={"market": "crypto"}).json()
    assert refused["ok"] is False and "confirm" in refused["reason"]

    armed = client.post("/api/autonomy/arm", json={"market": "crypto", "confirm": True}).json()
    assert armed["ok"] is True and armed["grant"]["armed"] is True
    assert len(armed["grant"]["fingerprint"]) == 16
    # paper is still paper: arming never turns a simulated venue into a live one
    assert armed["entries"]["gate"] == "paper"

    disarmed = client.post("/api/autonomy/disarm",
                           json={"market": "crypto", "by": "test"}).json()
    assert disarmed["entries"]["allowed"] is True and disarmed["entries"]["gate"] == "paper"
    engine.state.set_setting("live_entry_grant", {})


# --------------------------------------------------------------------------- analyse → confirm
def test_analyse_endpoint_is_read_only_and_ranked(client):
    body = client.get("/api/analyse?market=crypto").json()
    assert body["analysed"] >= 1
    assert body["market"] == "crypto"
    assert body["paper"] in (True, False)
    assert "entries_gate" in body and "guard" in body
    scores = [abs(row.get("score") or 0) for row in body["rows"]]
    assert scores == sorted(scores, reverse=True)
    for row in body["rows"]:
        assert row["action"] in ("LONG", "SHORT", "WAIT", "BLOCKED", "ERROR")
        assert "executable" in row and "block_text" in row
        if row["executable"]:
            assert row["id"], "an executable row must carry a confirm fingerprint"
            assert row["plan"]["stop"] > 0, "no plan without a hard stop"


def test_confirm_endpoint_refuses_a_symbol_it_cannot_see(client):
    body = client.post("/api/analyse/confirm",
                       json={"market": "crypto", "symbol": "NOT/REAL", "analysis_id": "x"}).json()
    assert body["ok"] is False
    assert body["block"] == "symbol"
    assert "watchlist" in body["reason"]


def test_confirm_endpoint_refuses_a_tampered_fingerprint(client):
    board = client.get("/api/analyse?market=crypto").json()
    executable = next((r for r in board["rows"] if r["executable"]), None)
    if executable is None:                       # quiet market: nothing to confirm, which is fine
        pytest.skip("no executable setup in this market snapshot")
    body = client.post("/api/analyse/confirm",
                       json={"market": "crypto", "symbol": executable["symbol"],
                             "analysis_id": "0" * 16}).json()
    assert body["ok"] is False
    assert "analyse again" in body["reason"]
