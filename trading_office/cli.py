"""Command-line terminal: the same engine, for a shell or a script.

    python -m trading_office.cli screen --market idx --limit 20
    python -m trading_office.cli analyse --market idx --symbol BBCA
    python -m trading_office.cli bandar --symbol BBCA --period 1w
    python -m trading_office.cli broker --symbol BBCA
    python -m trading_office.cli foreign --symbol BBCA --days 20
    python -m trading_office.cli ownership --symbol BBCA
    python -m trading_office.cli actions --symbol BBCA
    python -m trading_office.cli crypto --symbols BTC/USDT:USDT,ETH/USDT:USDT
    python -m trading_office.cli backtest --market crypto --symbol BTC/USDT:USDT --oos 0.3
    python -m trading_office.cli serve --port 8000
    python -m trading_office.cli doctor

Every command accepts ``--json`` for machine-readable output (same payload as the REST
API), so a shell pipeline and an agent see identical data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import Settings
from .strategy.screener import Screener

# --------------------------------------------------------------------------- tty helpers
_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    if not _TTY:
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(t: str) -> str:
    return _c(t, "2;37")


def bold(t: str) -> str:
    return _c(t, "1;37")


def green(t: str) -> str:
    return _c(t, "1;32")


def red(t: str) -> str:
    return _c(t, "1;31")


def amber(t: str) -> str:
    return _c(t, "1;33")


def cyan(t: str) -> str:
    return _c(t, "1;36")


LABEL_COLORS = {
    "STRONG_BUY": green, "BUY": green, "WATCH": cyan,
    "HOLD": dim, "REDUCE": amber, "SELL": red,
}


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]], aligns: Sequence[str] | None = None) -> str:
    aligns = list(aligns or ["l"] * len(headers))
    widths = [len(str(h)) for h in headers]
    rendered = []
    for row in rows:
        cells = ["" if c is None else str(c) for c in row]
        rendered.append(cells)
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))
    out = []
    head = "  ".join(
        bold(str(h).ljust(widths[i]) if aligns[i] == "l" else str(h).rjust(widths[i]))
        for i, h in enumerate(headers)
    )
    out.append(head)
    out.append(dim("  ".join("─" * w for w in widths)))
    for cells in rendered:
        out.append(
            "  ".join(
                c.ljust(widths[i]) if aligns[i] == "l" else c.rjust(widths[i])
                for i, c in enumerate(cells)
            )
        )
    return "\n".join(out)


def money(v: Any, digits: int = 2) -> str:
    if v is None:
        return "—"
    v = float(v)
    a = abs(v)
    if a >= 1e12:
        return f"{v/1e12:.{digits}f}T"
    if a >= 1e9:
        return f"{v/1e9:.{digits}f}B"
    if a >= 1e6:
        return f"{v/1e6:.1f}M"
    if a >= 1e3:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def pct(v: Any, digits: int = 2) -> str:
    if v is None:
        return "—"
    s = f"{float(v):+.{digits}f}%"
    return green(s) if float(v) > 0 else red(s) if float(v) < 0 else s


# --------------------------------------------------------------------------- commands
def cmd_screen(args: argparse.Namespace, settings: Settings) -> int:
    screener = Screener(settings)
    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
    data = screener.screen(args.market, symbols=symbols, limit=args.limit, deep_context=not args.fast)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    rows = data["rows"]
    print()
    print(bold(f"  {args.market.upper()} SCREEN") + dim(f"   {data['scanned']} scanned · {data['generated_at'][:19]}"))
    print(dim("  " + "─" * 96))
    body = []
    for r in rows:
        label = r["label"]
        colour = LABEL_COLORS.get(label, dim)
        body.append(
            [
                r["symbol"],
                (r["name"] or "")[:22],
                money(r["price"]),
                pct(r["change_pct"]),
                colour(label.replace("_", " ")),
                f"{r['score']}/9",
                f"{r['conviction']:+.1f}",
                ((r["reasons"] or [""])[0] or "")[:42],
            ]
        )
    print(table(["SYMBOL", "NAME", "PRICE", "CHG", "LABEL", "SCORE", "CONV", "WHY"], body,
                ["l", "l", "r", "r", "l", "r", "r", "l"]))
    counts = data["counts"]
    print()
    print(
        "  " + "  ".join(
            f"{LABEL_COLORS.get(k, dim)(k.replace('_', ' '))} {v}" for k, v in counts.items() if v
        )
    )
    sources = {r["source"] for r in rows}
    print(dim(f"  source: {', '.join(sorted(sources)) or 'n/a'} · synthetic data is simulated, not live"))
    print()
    return 0


def cmd_analyse(args: argparse.Namespace, settings: Settings) -> int:
    screener = Screener(settings)
    data = screener.analyse(args.market, args.symbol)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    scan, row = data["scan"], data["row"]
    print()
    print(bold(f"  {row['symbol']}") + dim(f"  {row['name']}") + "   " +
          LABEL_COLORS.get(row["label"], dim)(row["label"].replace("_", " ")) +
          dim(f"   conviction {row['conviction']:+.1f}/10"))
    print(dim("  " + "─" * 96))
    print(f"  price        {money(scan['price'])}   source: {data.get('source')}")
    print(f"  pipeline     {scan['action']}  score {scan['score']}/9  stage {scan['stage']}")
    print()
    print(bold("  GATE 1 · MARKET FILTER"))
    for check in scan["filter"]["checks"]:
        mark = green("PASS") if check["passed"] else red("BLOCK")
        print(f"   {mark}  {check['name']:<11} {dim(check['detail'])}")
    print()
    print(bold("  GATE 2 · SIGNAL"))
    for comp in (scan["signal"].get("components") or []):
        hit = comp["long"] or comp["short"]
        side = "L" if comp["long"] else "S" if comp["short"] else "·"
        print(f"   {'+' if hit else '·'} {comp['name']:<20} {comp['kind']:<8} {side} {comp['weight']}  {dim(comp['detail'][:60])}")
    print()
    print(bold("  GATE 3 · RISK / PLAN"))
    plan = (scan.get("risk") or {}).get("plan")
    if plan:
        print(f"   side {plan['side'].upper()} · entry {money(plan['entry'])} · stop {money(plan['stop'])} "
              f"({plan['stop_source']}) · target {money(plan['target'])} ({plan['target_source']})")
        print(f"   R:R 1:{plan['rr']:.2f} · size {plan['lots'] if plan['market'] == 'idx' else plan['size']}"
              f"{' lots' if plan['market'] == 'idx' else ' units'} · risk {money(plan['risk_cash'])} "
              f"({plan['risk_pct']:.2f}% of equity)")
        print(f"   costs {plan['costs']['round_trip_cost_pct']:.3f}% round trip · "
              f"expected profit {money(plan['costs']['expected_profit_cash'])}")
    else:
        for reason in (scan.get("risk") or {}).get("rejections", []) or scan["reasons"][:3]:
            print(f"   {red('reject')}  {reason}")
    print()
    if row["context"].get("bandar"):
        b = row["context"]["bandar"]
        f = row["context"].get("foreign") or {}
        print(bold("  CONTEXT"))
        print(f"   bandar {b.get('signal')} ({b.get('phase')}) · top-5 net {b.get('net_pct')}% · "
              f"foreign net {money(f.get('net_value_at_close'))} (streak {f.get('streak')})")
        print()
    print(dim("  not investment advice"))
    print()
    return 0


def cmd_bandar(args: argparse.Namespace, settings: Settings) -> int:
    idx = Screener(settings).idx
    data = idx.bandar(args.symbol.upper(), period=args.period)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    row = data.get("data") or {}
    print()
    print(bold(f"  {row.get('symbol', args.symbol)} · BANDARMOLOGY") + dim(f"  {args.period} · source {data.get('source')}"))
    print(dim("  " + "─" * 96))
    print(f"  signal {bold(str(row.get('signal')))}   phase {row.get('phase')}   "
          f"top-5 net {pct(row.get('net_pct'), 1)} of value   bandar P/L {pct(row.get('bandar_gain_pct'))}")
    print()
    buyers = row.get("buyers") or []
    sellers = row.get("sellers") or []
    rows = []
    for i in range(max(len(buyers), len(sellers))):
        b = buyers[i] if i < len(buyers) else {}
        s = sellers[i] if i < len(sellers) else {}
        rows.append([b.get("rank", ""), b.get("code", ""), money(b.get("value")), b.get("kind", ""),
                     "", s.get("rank", ""), s.get("code", ""), money(s.get("value")), s.get("kind", "")])
    print("  " + bold("TOP BUYERS") + " " * 40 + bold("TOP SELLERS"))
    print(table(["#", "CODE", "VALUE", "KIND", "", "#", "CODE", "VALUE", "KIND"], rows,
                ["r", "l", "r", "l", "l", "r", "l", "r", "l"]))
    for reason in row.get("reasons") or []:
        print(dim(f"  · {reason}"))
    print()
    return 0


def cmd_broker(args: argparse.Namespace, settings: Settings) -> int:
    idx = Screener(settings).idx
    data = idx.broker(args.symbol.upper(), date=args.date or None)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    row = data.get("data") or {}
    print()
    print(bold(f"  {row.get('symbol', args.symbol)} · BROKER SUMMARY")
          + dim(f"  {row.get('from')} → {row.get('to')} · source {data.get('source')}"))
    rows = []
    buyers = row.get("buyers") or []
    sellers = row.get("sellers") or []
    for i in range(max(len(buyers), len(sellers))):
        b = buyers[i] if i < len(buyers) else {}
        s = sellers[i] if i < len(sellers) else {}
        rows.append([b.get("rank", ""), b.get("code", ""), money(b.get("value")), b.get("kind", ""),
                     "", s.get("rank", ""), s.get("code", ""), money(s.get("value")), s.get("kind", "")])
    print(table(["#", "BUYER", "VALUE", "KIND", "", "#", "SELLER", "VALUE", "KIND"], rows,
                ["r", "l", "r", "l", "l", "r", "l", "r", "l"]))
    print(dim(f"  total value {money(row.get('total_value'))} · foreign net {money(row.get('foreign_net_value'))}"))
    print()
    return 0


def cmd_foreign(args: argparse.Namespace, settings: Settings) -> int:
    idx = Screener(settings).idx
    data = idx.foreign(args.symbol.upper(), days=args.days)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    payload = data.get("data") or {}
    print()
    print(bold(f"  {args.symbol.upper()} · FOREIGN FLOW") + dim(f"  {args.days} sessions · source {data.get('source')}"))
    days = payload.get("days") or []
    rows = [[d.get("date"), money(d.get("close")), money(d.get("foreign_buy")), money(d.get("foreign_sell")),
             money(d.get("foreign_net")), money(d.get("foreign_net_value_at_close"))] for d in days]
    print(table(["DATE", "CLOSE", "BUY", "SELL", "NET", "NET@CLOSE"], rows, ["l", "r", "r", "r", "r", "r"]))
    totals = payload.get("totals") or {}
    print(dim(f"  sessions {totals.get('sessions')} · net {money(totals.get('foreign_net'))} · "
              f"streak {totals.get('streak')} · net buy days {totals.get('net_buy_sessions')}"))
    print()
    return 0


def cmd_ownership(args: argparse.Namespace, settings: Settings) -> int:
    idx = Screener(settings).idx
    data = idx.ownership(args.symbol.upper())
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    payload = data.get("data") or {}
    print()
    print(bold(f"  {args.symbol.upper()} · KSEI >1% HOLDERS") + dim(f"  as of {payload.get('as_of')} · source {data.get('source')}"))
    holders = payload.get("holders") or []
    rows = [[h.get("holder", "")[:46], f"{h.get('pct'):.2f}%" if h.get("pct") is not None else "—",
             money(h.get("shares")), pct(h.get("change_pct"))] for h in holders]
    print(table(["HOLDER", "STAKE", "SHARES", "CHG"], rows, ["l", "r", "r", "r"]))
    print(dim(f"  listed {payload.get('listed_pct')}%"))
    print()
    return 0


def cmd_actions(args: argparse.Namespace, settings: Settings) -> int:
    idx = Screener(settings).idx
    data = idx.actions(args.symbol.upper())
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    payload = data.get("data") or {}
    print()
    print(bold(f"  {args.symbol.upper()} · CORPORATE ACTIONS") + dim(f"  source {data.get('source')}"))
    rows = []
    for a in payload.get("actions") or []:
        rows.append([a.get("kind", ""), a.get("label", "")[:28], a.get("amount") or "—",
                     a.get("cum_date") or "—", a.get("ex_date") or "—", a.get("status") or ""])
    print(table(["KIND", "LABEL", "AMOUNT", "CUM", "EX", "STATUS"], rows))
    meetings = payload.get("meetings") or []
    if meetings:
        print()
        print(bold("  RUPS"))
        for m in meetings:
            print(f"   {m.get('date')} {m.get('time') or ''}  {m.get('venue') or ''}  {dim(str(m.get('agenda') or '')[:60])}")
    print()
    return 0


def cmd_crypto(args: argparse.Namespace, settings: Settings) -> int:
    args.market = "crypto"
    args.symbols = args.symbols or ",".join(settings.get("profiles.crypto.symbols") or [])
    args.fast = True
    return cmd_screen(args, settings)


DEFAULT_SYMBOL = {"crypto": "BTC/USDT:USDT", "idx": "BBCA"}


def cmd_backtest(args: argparse.Namespace, settings: Settings) -> int:
    from .backtest import walk_forward
    from .api.server import _backtest_data

    # `--market idx` used to keep the crypto default and quietly report "0 trades" on a
    # symbol IDX does not have; resolve the symbol from the market instead.
    args.symbol = args.symbol or DEFAULT_SYMBOL.get(args.market, "BBCA")
    data = _backtest_data(args.market, args.symbol, args.bars)
    bars = data["bars"][-args.bars :]
    report = walk_forward(
        market=args.market, symbol=args.symbol, bars=bars, settings=settings, oos_fraction=args.oos
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    oos = report["out_of_sample"]
    m = oos["metrics"]
    ins = report["in_sample"]["metrics"]
    print()
    print(bold(f"  {args.symbol} · WALK-FORWARD") + dim(f"  {len(bars)} bars · OOS {int(args.oos*100)}% · source {data.get('source')}"))
    print(dim("  " + "─" * 96))
    print(f"  chosen parameters   {json.dumps(report['chosen_params'])}")
    print()
    rows = [
        ["trades", ins["trades"], m["trades"]],
        ["win rate %", ins["win_rate_pct"], m["win_rate_pct"]],
        ["expectancy R", ins["expectancy_r"], m["expectancy_r"]],
        ["profit factor", ins["profit_factor"], m["profit_factor"]],
        ["return %", ins["return_pct"], m["return_pct"]],
        ["max drawdown %", ins["max_drawdown_pct"], m["max_drawdown_pct"]],
        ["max drawdown % (MTM)", ins.get("max_drawdown_pct_mtm"), m.get("max_drawdown_pct_mtm")],
        ["sharpe", ins["sharpe"], m["sharpe"]],
        ["partials (2R)", ins.get("partials", 0), m.get("partials", 0)],
        ["entry slippage bps", ins.get("avg_entry_slippage_bps"), m.get("avg_entry_slippage_bps")],
        ["exit slippage bps", ins.get("avg_exit_slippage_bps"), m.get("avg_exit_slippage_bps")],
        ["gap-through stops", ins.get("gap_stops", 0), m.get("gap_stops", 0)],
    ]
    print(table(["METRIC", "IN-SAMPLE (used for selection)", "OUT-OF-SAMPLE (unseen)"], rows, ["l", "r", "r"]))
    print()
    print(bold("  LAPORAN KINERJA (out-of-sample)"))
    pf = m.get("profit_factor")
    dd = m.get("max_drawdown_pct") or 0.0
    dd_mtm = m.get("max_drawdown_pct_mtm") or dd
    print(f"   Total trade        {m['trades']:>10}      Profit factor   "
          f"{'∞ (no losing trade)' if pf is None else f'{pf:.2f}'}")
    print(f"   Win rate           {m['win_rate_pct']:>9.1f}%      Max drawdown    {dd:>6.2f}% "
          f"(mark-to-market {dd_mtm:.2f}%)")
    print(f"   Net profit         {m['return_pct']:>9.2f}%      Ending equity   "
          f"{oos.get('ending_equity', 0):>10,.2f}")
    print(f"   Expectancy         {m['expectancy_r']:>8.2f} R      Sharpe          {m.get('sharpe', 0):>6.2f}")
    costs = m.get("costs") or {}
    if costs:
        slip = costs.get("slippage_bps") or {}
        lat = costs.get("latency_adverse_bps") or {}
        print()
        print(bold("  EXECUTION COST (OOS)"))
        print(f"   latency {costs.get('latency_ms', 0):.0f} ms  "
              f"delay cost p50 {lat.get('p50', 0):.2f} bps / p90 {lat.get('p90', 0):.2f} bps")
        print(f"   slippage p50 {slip.get('p50', 0):.2f} bps · p90 {slip.get('p90', 0):.2f} bps · "
              f"worst {slip.get('p100', 0):.2f} bps" +
              (f" · {costs.get('slippage_capped', 0)} fills at the cap" if costs.get("slippage_capped") else ""))
        print(f"   gap guard refused {costs.get('gap_guards', 0)} entries · "
              f"{costs.get('partial_fills', 0)} partials banked "
              f"({costs.get('partial_pnl', 0):,.2f})")
        print(f"   effective k_slip {costs.get('avg_k_slip') or 0:.4f} × ATR per fill · "
              f"{costs.get('shock_fills', 0)} fill(s) in a shock regime · "
              f"{costs.get('entries_cancelled', 0)} entry cancelled after a gap")
        print()
    if m["rejections"] or m["filters_blocked"]:
        print(bold("  WHY TRADES WERE SKIPPED (OOS)"))
        for key, value in list(m["filters_blocked"].items())[:4]:
            print(f"   filter {key:<14} {value}")
        for key, value in list(m["rejections"].items())[:6]:
            print(f"   signal {key:<14} {value}")
        print()
    for t in oos["trades"][-8:]:
        print(f"   {t['side']:<5} {t.get('planned_entry') or t['entry']:>10,.2f} → "
              f"{t.get('planned_stop') or t['stop']:>10,.2f} (stop)  exit {t['exit']:>10,.2f}  "
              f"{t['r_multiple']:>7,.2f} R  {dim(t['reason'][:30])}")
    print()
    return 0


def _security_checks(settings: Settings) -> list[tuple[str, bool, str]]:
    """Credential + at-rest encryption rows for `doctor` (presence only, never values)."""
    from . import secure_state
    from .env import credential_status, load_env

    env_file = load_env()
    key_name = str(settings.get("execution.api_key_env", "TO_API_KEY"))
    secret_name = str(settings.get("execution.api_secret_env", "TO_API_SECRET"))
    creds = credential_status(key_name, secret_name)
    have = sum(1 for entry in creds.values() if entry["set"])
    via = ", ".join(f"{k} via {v['via']}" for k, v in creds.items() if v["set"]) or "none"
    sealing = secure_state.status()
    from .alerts import Alerter

    alerter = Alerter.from_settings(settings)
    return [
        (".env", env_file["found"],
         f"{env_file['file']} · {env_file['loaded']} key(s)" if env_file["found"]
         else "not found — copy .env.example to .env (variables also work without it)"),
        ("credentials", have == len(creds),
         f"{have}/{len(creds)} loaded ({via}); live execution stays off until both exist"),
        ("alerts", alerter.enabled and alerter.configured,
         f"{alerter.channel} ready · {len(alerter.events)} event(s), {alerter.cooldown_s:.0f}s cooldown"
         if alerter.enabled and alerter.configured
         else "off — set alerts.enabled + TO_TELEGRAM_TOKEN + chat_id for 24/7 alerting"),
        ("state at rest", bool(sealing.get("encrypted")),
         f"{sealing.get('algorithm')} via {sealing.get('source')}" if sealing.get("encrypted")
         else "plaintext state/ — set TO_STATE_ENCRYPTION_KEY to seal it"),
    ]


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    from .market import CryptoClient, IdxClient

    idxc = IdxClient(settings.section("idx"))
    crypto = CryptoClient(exchange_id=str(settings.get("execution.exchange_id", "bybit")),
                          symbols=settings.get("profiles.crypto.symbols") or [])
    checks = [
        ("idx CLI", idxc.available(), f"`{idxc.command}` on PATH"),
        ("idx data", idxc.candles("BBCA", period="3mo")["source"] == "idx", "IDX candles from idx.co.id"),
        ("ccxt", crypto.available(), f"{crypto.exchange_id} reachable" if crypto.available() else crypto.last_error or "install ccxt"),
        ("config", settings.path.exists(), str(settings.path)),
        ("paper broker", bool(settings.get("execution.paper", True)), "no capital at risk"),
    ] + _security_checks(settings)
    if args.json:
        print(json.dumps([{"check": c, "ok": ok, "detail": d} for c, ok, d in checks], indent=2))
        return 0
    print()
    for name, ok, detail in checks:
        print(f"  {green('OK  ') if ok else amber('WARN')}  {name:<12} {dim(detail)}")
    print()
    print(dim("  WARN means the app still works: it falls back to deterministic synthetic data"))
    print(dim("  and labels every row with its source."))
    print()
    return 0


def cmd_digest(args: argparse.Namespace, settings: Settings) -> int:
    """Report on the **running** bot (or, failing that, on state rebuilt from disk).

    A digest built from a freshly constructed engine describes the process that just
    started — "cycles 0, equity unchanged" — which is precisely the reassuring lie that
    hides a dead loop. So the server is asked first; only if it cannot be reached does this
    fall back to local state, and the message says which one it used.
    """
    from . import digest as digest_mod

    remote = None if args.offline else digest_mod.fetch_remote(args.api, args.market, args.timeout)
    if remote is None and args.send and not args.offline:
        # Refuse *before* rebuilding a local engine: opening the state store takes the lock
        # and the report would describe this process, not the bot that is supposed to be
        # running. Say what to do instead of printing a confident, wrong number.
        print(f"  {red('not sent')} the API at {args.api} did not answer; "
              f"nothing to report on.")
        print(dim("  start the server (trading-office supervise) or pass --offline to send the "
                  "local snapshot anyway"))
        return 1
    if remote is not None:
        snapshot = digest_mod.snapshot_from_remote(remote, args.market)
        source = f"api {args.api}"
    else:
        engine = _engine_for_cli(args.market, settings)
        snapshot = digest_mod.snapshot_from_engine(engine)
        source = "local state (server not reachable)"

    text = digest_mod.format_digest(snapshot)
    print(digest_mod.plain(text))
    if args.json:
        print(json.dumps({**snapshot, "text": digest_mod.plain(text)}, indent=2, default=str))

    if not args.send:
        print(dim(f"\n  source: {source} · not sent (add --send)"))
        return 0
    outcome = digest_mod.send(settings, text, market=args.market)
    if outcome.get("ok"):
        print(f"  {green('sent')} via {outcome.get('channel')} · source {source}")
        return 0
    print(f"  {red('failed')} {outcome.get('reason') or outcome.get('error') or 'unknown error'}")
    return 1


def cmd_arm_live(args: argparse.Namespace, settings: Settings) -> int:
    """Arm unattended live entries. Refuses without --yes and prints exactly what it covers."""
    from .autonomy import execution_gate_fingerprint, execution_gate_limits
    from .state import StateStore

    engine = _engine_for_cli(args.market, settings)
    limits = execution_gate_limits(settings)
    fingerprint = execution_gate_fingerprint()
    if not args.yes:
        print(f"  {amber('not armed')} this authorises real orders on {args.market}.")
        print(dim("  It would grant:"))
        for name, value in limits.items():
            print(dim(f"    {name:<24} {value}"))
        print(dim(f"  Gate fingerprint {fingerprint} (changes if the gate or these limits change)."))
        print(dim(f"  Re-run with --yes to arm: trading-office arm-live --market {args.market} --yes"))
        return 1
    grant = {"armed": True, "fingerprint": fingerprint, "limits": limits,
             "armed_by": args.by, "armed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    engine.state.set_setting("live_entry_grant", grant)
    engine.state.append_ledger({"event": "live_armed", "market": args.market, **grant})
    gate = engine.execution_gate()["entries"]
    print(f"  {green('armed')} {args.market} · gate {gate['gate']} · {gate['reason']}")
    print(dim(f"  autonomy.mode = {settings.get('autonomy.mode', 'ask')} "
              f"(ask = every entry still waits for a human)"))
    return 0


def cmd_disarm_live(args: argparse.Namespace, settings: Settings) -> int:
    engine = _engine_for_cli(args.market, settings)
    engine.state.set_setting("live_entry_grant", {"armed": False,
                                                  "disarmed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    engine.state.append_ledger({"event": "live_disarmed", "market": args.market, "by": "cli"})
    print(f"  {amber('disarmed')} {args.market} — live entries are refused until re-armed")
    return 0


def _engine_for_cli(market: str, settings: Settings):
    from .engine import TradingEngine

    return TradingEngine(market=market, settings=settings,
                         execute=not bool(settings.get("execution.dry_run", True)),
                         state_dir=settings.get("engine.state_dir", "state"))


def cmd_approvals(args: argparse.Namespace, settings: Settings) -> int:
    from .state import StateStore

    store = StateStore(Path(settings.get("engine.state_dir", "state")) / args.market)
    if args.action == "list":
        rows = store.approvals(status="pending")
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
            return 0
        if not rows:
            print(dim("  no pending approvals"))
            return 0
        print()
        print(table(["ID", "SYMBOL", "SIDE", "ENTRY", "STOP", "TARGET", "SIZE", "EXPIRES"],
                    [[r["id"], r["symbol"], r["side"], r["plan"].get("entry"),
                      r["plan"].get("stop"), r["plan"].get("target"), r["plan"].get("size"),
                      str(r.get("expires_at"))[11:19]] for r in rows],
                    ["l", "l", "l", "r", "r", "r", "r", "l"]))
        print()
        print(dim(f"  approve: trading-office approvals approve <ID> --market {args.market}"))
        return 0
    if not args.request_id:
        print(f"  {red('error')} request id required", file=sys.stderr)
        return 2
    if args.action == "reject":
        ok = store.resolve_approval(args.request_id, "rejected", by=args.by, reason="cli")
        print(green("rejected") if ok else amber("already decided"))
        return 0 if ok else 1
    # approve: execute through the engine so the gates and the bar lock still apply
    engine = _engine_for_cli(args.market, settings)
    result = engine.execute_approved(args.request_id, by=args.by)
    print(json.dumps(result, indent=2, default=str) if args.json else
          (green("approved and sent") if result.get("ok") else red(f"refused: {result.get('reason')}")))
    return 0 if result.get("ok") else 1


def cmd_supervise(args: argparse.Namespace, settings: Settings) -> int:
    """Keep the API + engine loop alive, and restart it when the loop stops cycling."""
    from .supervisor import Supervisor, SupervisorConfig, configure_logging, serve_command

    info = configure_logging(args.log, level="INFO")
    config = SupervisorConfig(
        command=serve_command(host=args.host, port=args.port),
        host="127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host,
        port=args.port, market=args.market, log_file=info["file"],
    )
    print(f"  supervisor: http://{args.host}:{args.port} · log {info['file']} "
          f"(rotating {info['max_bytes'] // (1024*1024)} MB × {info['backups']})")
    print(f"  probes /api/engine/heartbeat every {config.check_interval_s:.0f}s · "
          f"restart after {config.max_stalled_checks} failed beats")
    supervisor = Supervisor(config)
    return supervisor.run(max_restarts=args.max_restarts or None)


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import socket

    import uvicorn

    # A second uvicorn on the same port dies with a bare OSError traceback; check first and
    # say what is actually going on (a stale instance is the usual cause).
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((args.host, args.port))
    except OSError:
        print(f"  {red('port busy')} {args.host}:{args.port} is already in use — another "
              f"`serve` is probably running. Stop it or pick --port {args.port + 1}.",
              file=sys.stderr)
        return 1
    finally:
        probe.close()
    print(f"  Trading Office on http://{args.host}:{args.port}")
    uvicorn.run("trading_office.api.server:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_engine(args: argparse.Namespace, settings: Settings) -> int:
    from .engine import TradingEngine

    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
    engine = TradingEngine(market=args.market, settings=settings, symbols=symbols,
                           execute=not args.no_execute)

    def printer(event: dict[str, Any]) -> None:
        if event.get("event") == "cycle":
            for scan in event.get("scans", []):
                action = scan["action"]
                colour = green if action == "LONG" else red if action == "SHORT" else dim
                reasons = " | ".join(scan.get("reasons", [])[:2])[:100]
                print(
                    f"  {scan['symbol']:<16} {colour(f'{action:<7}')} "
                    f"score {scan['score']}/9  {dim(reasons)}"
                )
        elif event.get("event") in ("trade_opened", "trade_closed", "skip"):
            print("  " + json.dumps(event, default=str)[:180])
        elif event.get("event") == "engine_error":
            print("  " + red("error ") + str(event.get("error"))[:160])

    engine.on_event = printer
    if args.cycles:
        for _ in range(args.cycles):
            engine.cycle()
    else:
        engine.run_forever()
    return 0


def cmd_costs(args: argparse.Namespace, settings: Settings) -> int:
    from . import costs as cost_model

    profile = settings.profile(args.market)
    cfg = cost_model.CostConfig.for_market(args.market, profile)
    payload = {
        "market": args.market,
        "model": cfg.to_dict(),
        "spread_cap_bps": float(profile.get("max_spread_bps", 8.0)),
        "stress_spread_cap_bps": cost_model.risky_spread_cap(profile),
        "management": settings.section("management"),
        "example": cost_model.estimate_slippage(
            price=args.price, atr=args.price * args.atr_pct / 100.0,
            notional=args.notional, quote_volume=args.quote_volume, cfg=cfg,
        ).to_dict(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0
    print(bold(f"execution costs · {args.market}"))
    print(f"  latency      {cfg.latency_ms:.0f} ms")
    print(f"  slippage     base {cfg.slippage_base_bps:.2f} bps + {cfg.atr_mult:.2f}×ATR% "
          f"+ {cfg.impact_coeff:.2f}×√participation, cap {cfg.max_slippage_bps:.0f} bps")
    print(f"  k_slip       {cfg.atr_mult:.3f} × ATR normally, up to "
          f"{cfg.atr_mult * cfg.shock_slippage_mult:.3f} in a shock "
          f"(ATR ≥ {cfg.shock_atr_mult:.1f}× its 50-bar mean)")
    print(f"  gaps         reject at {cfg.gap_reject_atr:.1f} ATR or "
          f"{cfg.gap_reject_spread_mult:.1f}× spread; fill {cfg.gap_fill_fraction:.0%} of the gap")
    print(f"  risk scale   up to {cfg.max_risk_multiplier:.2f}× smaller size when stressed")
    ex = payload["example"]
    print(f"  example      {ex['slippage_bps']:.2f} bps on a "
          f"{args.notional:,.0f} notional at {args.price:,.2f} "
          f"({'capped' if ex['capped'] else 'uncapped'})")
    return 0


def cmd_reconcile(args: argparse.Namespace, settings: Settings) -> int:
    from .engine import TradingEngine

    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
    engine = TradingEngine(market=args.market, settings=settings, symbols=symbols, execute=False)
    report = engine.startup_sync(adopt_untracked=args.adopt, close_orphans=args.close_orphans)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print(bold(f"reconciliation · {args.market} · {report['mode']}"))
    print(f"  venue reachable   {report['venue_available']}")
    for key in ("restored", "adopted", "closed", "untracked", "blocked"):
        rows = report[key]
        print(f"  {key:<16} {len(rows)}" + (f"  {[r['symbol'] for r in rows]}" if rows else ""))
    if report["stops_missing"]:
        print(red(f"  missing stops     {report['stops_missing']}"))
    return 0


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-office",
        description="IDX + crypto trading desk: market filter → signal → risk → execution.",
        epilog="--json is global: put it before the command (trading-office --json screen --market idx).",
    )
    parser.add_argument("--config", default="", help="path to settings.json")
    parser.add_argument("--json", action="store_true", help="emit JSON (same as the REST API)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("screen", help="rank a market with the four-gate pipeline")
    p.add_argument("--market", choices=["idx", "crypto"], default="idx")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--symbols", default="", help="comma-separated tickers")
    p.add_argument("--fast", action="store_true", help="skip ownership/corporate actions")
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("analyse", aliases=["analyze"], help="one symbol, every gate")
    p.add_argument("--market", choices=["idx", "crypto"], default="idx")
    p.add_argument("--symbol", required=True)
    p.set_defaults(func=cmd_analyse)

    p = sub.add_parser("bandar", help="bandarmology: broker accumulation / distribution")
    p.add_argument("--symbol", required=True)
    p.add_argument("--period", choices=["1d", "1w", "1m", "3m"], default="1w")
    p.set_defaults(func=cmd_bandar)

    p = sub.add_parser("broker", help="broker summary: top buyers and sellers")
    p.add_argument("--symbol", required=True)
    p.add_argument("--date", default="")
    p.set_defaults(func=cmd_broker)

    p = sub.add_parser("foreign", help="foreign flow per session")
    p.add_argument("--symbol", required=True)
    p.add_argument("--days", type=int, default=20)
    p.set_defaults(func=cmd_foreign)

    p = sub.add_parser("ownership", help="KSEI holders above 1 percent")
    p.add_argument("--symbol", required=True)
    p.set_defaults(func=cmd_ownership)

    p = sub.add_parser("actions", help="dividends, rights, RUPS")
    p.add_argument("--symbol", required=True)
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser("crypto", help="shortcut for: screen --market crypto")
    p.add_argument("--symbols", default="")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_crypto)

    p = sub.add_parser("backtest", help="walk-forward backtest on closed candles")
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.add_argument("--symbol", default=None,
                   help="defaults to BTC/USDT:USDT (crypto) or BBCA (idx) — never a crypto pair on IDX")
    p.add_argument("--oos", type=float, default=0.3)
    p.add_argument("--bars", type=int, default=900)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("engine", help="run the live engine (paper unless configured)")
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.add_argument("--symbols", default="")
    p.add_argument("--cycles", type=int, default=0)
    p.add_argument("--no-execute", action="store_true")
    p.set_defaults(func=cmd_engine)

    p = sub.add_parser("costs", help="show the execution-cost model (latency, slippage, gap guards)")
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.add_argument("--price", type=float, default=100.0)
    p.add_argument("--atr-pct", dest="atr_pct", type=float, default=1.0)
    p.add_argument("--notional", type=float, default=1500.0)
    p.add_argument("--quote-volume", dest="quote_volume", type=float, default=5_000_000.0)
    p.set_defaults(func=cmd_costs)

    p = sub.add_parser("reconcile", help="compare state/positions.json with the exchange")
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.add_argument("--symbols", default="")
    p.add_argument("--adopt", action="store_true", help="start tracking venue positions we do not know")
    p.add_argument("--close-orphans", dest="close_orphans", action="store_true",
                   help="close venue positions we never authorised")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("serve", help="start the web terminal + REST API")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("digest", help="send (or print) a daily status digest — cron-friendly")
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.add_argument("--send", action="store_true", help="deliver to the configured alert channel")
    p.add_argument("--api", default="http://127.0.0.1:8000",
                   help="running server to report on (default http://127.0.0.1:8000)")
    p.add_argument("--offline", action="store_true",
                   help="do not call the API; rebuild the report from local state")
    p.add_argument("--timeout", type=float, default=5.0, help="API timeout in seconds")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("arm-live", help="authorise unattended LIVE entries (binds the gate fingerprint)")
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.add_argument("--yes", action="store_true", help="required: this authorises real orders")
    p.add_argument("--by", default="cli", help="who armed it (shows up in the audit trail)")
    p.set_defaults(func=cmd_arm_live)

    p = sub.add_parser("disarm-live", help="revoke the live-entry grant immediately")
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.set_defaults(func=cmd_disarm_live)

    p = sub.add_parser("approvals", help="list or decide queued trade requests")
    p.add_argument("action", nargs="?", choices=["list", "approve", "reject"], default="list")
    p.add_argument("request_id", nargs="?", default="")
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.add_argument("--by", default="cli")
    p.set_defaults(func=cmd_approvals)

    p = sub.add_parser("supervise", help="run the API under a restarting, heartbeat-checking supervisor")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.add_argument("--log", default="logs/trading-office.log", help="rotating log file")
    p.add_argument("--max-restarts", dest="max_restarts", type=int, default=0,
                   help="give up after N restarts (0 = never)")
    p.set_defaults(func=cmd_supervise)

    p = sub.add_parser("doctor", help="check providers, config and execution mode")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.load(args.config or None)
    try:
        return int(args.func(args, settings) or 0)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
    except Exception as exc:  # noqa: BLE001 - a terminal should print, not traceback
        if getattr(args, "json", False):
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        else:
            print(f"  {red('error')} {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
