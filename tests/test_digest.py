"""The daily digest: what it says, where it says it from, and how it refuses to lie.

Two failure modes are worth a test each, because both look like success from the outside:

* a digest that describes a **freshly built engine** instead of the running bot
  ("cycles 0, equity unchanged" every morning while the loop is wedged);
* a digest that is **sent** even though the server it claims to describe did not answer.

The rest is formatting: Telegram rejects the whole message if the markup does not parse, so
every externally-sourced value is escaped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from trading_office import cli, digest  # noqa: E402
from trading_office.alerts import Alerter  # noqa: E402
from trading_office.config import Settings  # noqa: E402


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


def _payload(**over):
    """A server payload shaped like the real /api/metrics + /status + /approvals."""
    metrics = {
        "kpi": {"equity": 995.89, "net_profit_pct": -0.41, "win_rate": 42.9,
                "profit_factor": 1.31, "total_trades": 7, "max_drawdown_pct": 2.2},
        "performance": {"trades": 7},
        "execution_gate": {"mode": "paper", "broker": "paper",
                           "entries": {"allowed": True, "reason": "paper broker: simulated fills"}},
        "active_trades": [
            {"symbol": "BTC/USDT:USDT", "side": "long", "size": 0.01, "entry": 60000.0,
             "stop": 59000.0, "target": 62000.0, "pnl": -4.11, "r_multiple": -0.4,
             "partial_done": False},
        ],
        "heartbeat": {"ok": True, "running": True, "silent_s": 4.2, "limit_s": 75.0},
        "drawdown_pct": [0.0, 2.2, 1.1],
        "equity": [1000.0, 1000.5],
        "alerts_enabled": False,
    }
    status = {"status": {"running": True, "mode": "paper", "cycle_count": 812, "orders": 31,
                         "blocked": 4, "last_cycle_at": "2026-11-06T17:10:00+00:00",
                         "started_at": (dt.datetime.now(dt.timezone.utc)
                                        - dt.timedelta(hours=3, minutes=5)).isoformat(),
                         "guard": {}, "skips": ["spread too wide", "spread too wide",
                                                "no trend"]}}
    approvals = {"mode": "ask", "requests": [{"id": "abc123", "symbol": "ETH/USDT:USDT",
                                              "side": "long", "plan": {"entry": 3000.0}}]}
    built = {"metrics": metrics, "status": status, "approvals": approvals,
             "base": "http://127.0.0.1:8000"}
    built.update(over)
    return built


# --------------------------------------------------------------------------- snapshot
def test_snapshot_prefers_the_running_server(monkeypatch):
    monkeypatch.setattr(digest, "_guard_probe", lambda *a, **k: {
        "blocked": True, "reasons": ["daily loss 2.4% >= 2.0%"], "daily_loss_pct": 2.4,
        "consecutive_losses": 3, "max_consecutive_losses": 4, "basis": "day_open_mark",
        "day_start_equity": 1000.0,
    })
    snap = digest.snapshot_from_remote(_payload(), "crypto")

    assert snap["source"].startswith("api ")
    # equity is the mark-to-market number the server computed, not a rebuilt one
    assert snap["equity"] == 995.89
    assert snap["equity_kind"] == "mark-to-market"
    # cycle counters come from the live process
    assert snap["loop"]["cycles"] == 812 and snap["loop"]["orders"] == 31
    assert snap["loop"]["running"] is True
    assert snap["loop"]["uptime_s"] > 3 * 3600
    # an empty guard in the status body is filled from the kill-switch endpoint
    assert snap["guard"]["blocked"] is True
    assert snap["positions"][0]["symbol"] == "BTC/USDT:USDT"
    assert snap["pending"][0]["id"] == "abc123"


def test_snapshot_never_invents_a_guard(monkeypatch):
    monkeypatch.setattr(digest, "_guard_probe", lambda *a, **k: {})
    snap = digest.snapshot_from_remote(_payload(), "crypto")
    assert snap["guard"] == {}          # formatted as "armed", never as a fake green number
    assert "blocked" not in snap["guard"]


# --------------------------------------------------------------------------- rendering
def test_digest_escapes_every_outside_value(monkeypatch):
    monkeypatch.setattr(digest, "_guard_probe", lambda *a, **k: {"blocked": False})
    snap = digest.snapshot_from_remote(_payload(), "crypto")
    snap["positions"][0]["symbol"] = "BAD<&>USDT"
    snap["skips"] = ["no <signal> & nothing"]
    text = digest.format_digest(snap)

    assert "<b>Trading Office" in text
    assert "BAD<&>USDT" not in text          # unescaped angle brackets would break the send
    assert "BAD&lt;&amp;&gt;USDT" in text
    assert "no &lt;signal&gt; &amp; nothing" in text
    assert "Equity &amp; performance" in text
    plain = digest.plain(text)
    assert "<b>" not in plain and "&amp;" not in plain


def test_digest_reports_a_stopped_loop_and_uptime(monkeypatch):
    monkeypatch.setattr(digest, "_guard_probe", lambda *a, **k: {"blocked": False})
    snap = digest.snapshot_from_remote(_payload(), "crypto")
    text = digest.format_digest(snap)
    assert "running · last cycle 4.2s ago" in text
    assert "uptime 3h 5m" in text
    assert "up since" in text

    snap["loop"].update(running=False, ok=False, silent_s=None, limit_s=75.0)
    stopped = digest.format_digest(snap)
    assert "stopped" in stopped


def test_digest_says_unknown_instead_of_zero():
    """A missing number prints as an em dash; a *zero* would read as "nothing happened"."""

    snap = {"market": "crypto", "source": "local state", "loop": {}, "guard": {},
            "positions": [], "pending": [], "skips": []}
    text = digest.format_digest(snap)
    assert "equity —" in text
    assert "0 closed · win —% · PF —" in text.replace("&amp;", "&")


# --------------------------------------------------------------------------- CLI contract
def _args(**over):
    base = dict(market="crypto", send=True, api="http://127.0.0.1:9", offline=False,
                timeout=0.2, json=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_send_refuses_local_state_when_the_api_is_down(monkeypatch, settings, capsys):
    monkeypatch.setattr(digest, "fetch_remote", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_engine_for_cli", lambda *a, **k: pytest.fail("must not send offline"))
    monkeypatch.setattr(digest, "send", lambda *a, **k: pytest.fail("must not send at all"))

    assert cli.cmd_digest(_args(), settings) == 1
    out = capsys.readouterr().out
    assert "not sent" in out
    assert "--offline" in out


def test_offline_flag_allows_the_local_report(monkeypatch, settings, tmp_path, capsys):
    """``--offline`` is the explicit opt-in for state rebuilt on this machine.

    Uses a real engine over a throwaway state dir rather than a stub, so the snapshot code
    is exercised against the object it will actually see at 00:15 on a VPS.
    """
    from trading_office.engine import TradingEngine

    engine = TradingEngine(market="crypto", settings=settings, symbols=[],
                           state_dir=tmp_path / "offline")
    monkeypatch.setattr(digest, "fetch_remote", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_engine_for_cli", lambda *a, **k: engine)
    sent: dict = {}

    def fake_send(_settings, text, market="crypto"):
        sent["text"] = text
        return {"ok": True, "channel": "telegram"}

    monkeypatch.setattr(digest, "send", fake_send)

    assert cli.cmd_digest(_args(offline=True), settings) == 0
    out = capsys.readouterr().out
    assert "local state (server not reachable)" in out   # labelled, never passed off as live
    assert "sent" in out
    assert "Trading Office" in sent["text"]


def test_send_without_credentials_reports_why(settings):
    outcome = digest.send(settings, "<b>hi</b>")
    if outcome.get("ok"):
        pytest.skip("a real Telegram channel is configured in this environment")
    assert "not configured" in outcome["reason"]


# --------------------------------------------------------------------------- transport
def test_telegram_body_carries_the_parse_mode():
    """End to end over real HTTP, against a stub that records what Telegram would receive.

    Mocking ``urlopen`` would have hidden the bug this test exists for: ``deliver`` used to
    pass the chat id to ``_send_telegram(text)``, and the ``TypeError`` was swallowed into
    ``{"ok": False, "error": "TypeError: ..."}`` — a "network failure" that was really a
    broken call signature.
    """
    import http.server
    import threading

    seen: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib naming
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen["path"] = self.path
            seen["body"] = json.loads(body or b"{}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):  # keep the test output clean
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        alerter = Alerter(enabled=True, token="t", chat_id="1", parse_mode="HTML",
                          api_base=base, timeout=5)
        outcome = alerter.deliver("<b>digest</b>")
    finally:
        server.shutdown()
        server.server_close()

    assert outcome["ok"] is True, outcome
    assert seen["path"] == "/bott/sendMessage"
    assert seen["body"]["parse_mode"] == "HTML"     # without it the user sees literal <b>
    assert seen["body"]["text"] == "<b>digest</b>"
    assert seen["body"]["chat_id"] == "1"
    assert seen["body"]["disable_web_page_preview"] is True


def test_alerter_defaults_to_html_from_config(settings):
    alerter = Alerter.from_settings(settings, "crypto", token="t")
    assert alerter.parse_mode == (settings.get("alerts.telegram.parse_mode", "HTML") or "")


def test_alert_text_is_escaped_when_html_is_on():
    """An engine error quoting `Equity < 0` must not turn the whole alert into a 400."""
    sent: list[str] = []
    alerter = Alerter(enabled=True, token="t", chat_id="1", cooldown_s=0, parse_mode="HTML",
                      transport=lambda text, chat: sent.append(text) or {"ok": True})
    outcome = alerter.notify(
        {"event": "engine_error", "market": "crypto", "error": "Equity < 0 & rising"},
        blocking=True)

    assert outcome["ok"] is True
    assert "&lt; 0 &amp; rising" in sent[0]      # Telegram gets something it can parse
    assert "< 0 & rising" not in sent[0]

    plain = Alerter(enabled=True, token="t", chat_id="1", cooldown_s=0,
                    transport=lambda text, chat: sent.append(text) or {"ok": True})
    plain.notify({"event": "engine_error", "market": "crypto", "error": "a < b"}, blocking=True)
    assert "a < b" in sent[1]                    # no parse_mode -> no escaping, no change
