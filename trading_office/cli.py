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
import json
import sys
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


def cmd_backtest(args: argparse.Namespace, settings: Settings) -> int:
    from .backtest import walk_forward
    from .api.server import _backtest_data

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
        ["sharpe", ins["sharpe"], m["sharpe"]],
    ]
    print(table(["METRIC", "IN-SAMPLE (used for selection)", "OUT-OF-SAMPLE (unseen)"], rows, ["l", "r", "r"]))
    print()
    if m["rejections"] or m["filters_blocked"]:
        print(bold("  WHY TRADES WERE SKIPPED (OOS)"))
        for key, value in list(m["filters_blocked"].items())[:4]:
            print(f"   filter {key:<14} {value}")
        for key, value in list(m["rejections"].items())[:6]:
            print(f"   signal {key:<14} {value}")
        print()
    for t in oos["trades"][-8:]:
        print(f"   {t['side']:<5} {t['entry']:>10,.2f} → {t['exit']:>10,.2f}  "
              f"{pct(t['r_multiple']*100, 2):>10} R  {dim(t['reason'][:34])}")
    print()
    return 0


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
    ]
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


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

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


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-office",
        description="IDX + crypto trading desk: market filter → signal → risk → execution.",
        epilog="Every command takes --json for machine-readable output.",
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

    p = sub.add_parser("ownership", help="KSEI holders above 1%")
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
    p.add_argument("--symbol", default="BTC/USDT:USDT")
    p.add_argument("--oos", type=float, default=0.3)
    p.add_argument("--bars", type=int, default=900)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("engine", help="run the live engine (paper unless configured)")
    p.add_argument("--market", choices=["idx", "crypto"], default="crypto")
    p.add_argument("--symbols", default="")
    p.add_argument("--cycles", type=int, default=0)
    p.add_argument("--no-execute", action="store_true")
    p.set_defaults(func=cmd_engine)

    p = sub.add_parser("serve", help="start the web terminal + REST API")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

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
