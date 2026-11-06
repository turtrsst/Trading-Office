"""Universe screener: run the pipeline over many symbols and rank them.

The screener answers the question the strategy pipeline answers for one symbol, but for
a whole market, and then adds the IDX-flavoured context that the technicals cannot see:

* bandarmology (broker accumulation / distribution),
* foreign flow streak,
* KSEI concentration,
* valuation and growth (P/E, P/B, ROE, earnings growth),
* corporate actions in the next weeks (dividend / rights / RUPS),

and folds them into one transparent **conviction score**. Nothing is hidden: every row
carries the component scores and the plain-language reasons behind the label.

Labels
------
``STRONG_BUY`` / ``BUY``      — pipeline LONG-ready (or nearly) and the context agrees
``WATCH``                     — setup forming, wait for the trigger
``HOLD``                      — no action, position holders may keep it
``REDUCE`` / ``SELL``         — distribution + weak technicals: exit or avoid

For crypto the same scale is used for the tradable side: ``BUY`` = long, ``SELL`` = short.
"""

from __future__ import annotations

import concurrent.futures as futures
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from ..config import Settings
from ..market import CryptoClient, IdxClient
from ..market import demo
from .runner import ScanResult, scan_symbol

LABELS = ("STRONG_BUY", "BUY", "WATCH", "HOLD", "REDUCE", "SELL")


@dataclass
class ScreenRow:
    symbol: str
    name: str
    market: str
    price: float | None
    change_pct: float | None
    label: str
    conviction: float  # -10 .. +10
    action: str  # pipeline action: LONG / SHORT / WAIT / BLOCKED
    score: int
    stage: str
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    plan: dict[str, Any] | None = None
    context: dict[str, Any] = field(default_factory=dict)
    indicators: dict[str, Any] = field(default_factory=dict)
    source: str = "synthetic"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market,
            "price": self.price,
            "change_pct": self.change_pct,
            "label": self.label,
            "conviction": round(self.conviction, 2),
            "action": self.action,
            "score": self.score,
            "stage": self.stage,
            "reasons": self.reasons,
            "components": {k: round(v, 2) for k, v in self.components.items()},
            "plan": self.plan,
            "context": self.context,
            "indicators": self.indicators,
            "source": self.source,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- context
def idx_context(client: IdxClient, symbol: str, *, deep: bool = True) -> dict[str, Any]:
    """Bandarmology, foreign flow, valuation and corporate actions for one IDX name."""
    ctx: dict[str, Any] = {"source": "synthetic", "warnings": []}
    bandar = client.bandar(symbol)
    if bandar.get("data"):
        data = bandar["data"]
        ctx["bandar"] = {
            "signal": data.get("signal"),
            "phase": data.get("phase"),
            "net_pct": data.get("net_pct"),
            "bandar_gain_pct": data.get("bandar_gain_pct"),
            "foreign_net_value": data.get("foreign_net_value"),
            "top_buyers": [b.get("code") for b in (data.get("buyers") or [])][:5],
            "top_sellers": [s.get("code") for s in (data.get("sellers") or [])][:5],
        }
    foreign = client.foreign(symbol, days=20)
    payload = foreign.get("data") or {}
    totals = payload.get("totals") or {}
    if totals:
        ctx["foreign"] = {
            "net_value_at_close": totals.get("foreign_net_value_at_close"),
            "streak": totals.get("streak"),
            "net_buy_sessions": totals.get("net_buy_sessions"),
            "net_sell_sessions": totals.get("net_sell_sessions"),
            "days": [
                {
                    "date": d.get("date"),
                    "foreign_net": d.get("foreign_net"),
                    "foreign_net_value_at_close": d.get("foreign_net_value_at_close"),
                }
                for d in (payload.get("days") or [])[-20:]
            ],
        }
    if deep:
        ownership = client.ownership(symbol)
        holders = (ownership.get("data") or {}).get("holders") or []
        if holders:
            ctx["ownership"] = {
                "as_of": (ownership.get("data") or {}).get("as_of"),
                "listed_pct": (ownership.get("data") or {}).get("listed_pct"),
                "top": [
                    {"holder": h.get("holder"), "pct": h.get("pct"), "change_pct": h.get("change_pct")}
                    for h in holders[:5]
                ],
            }
        actions = client.actions(symbol)
        events = (actions.get("data") or {}).get("actions") or []
        meetings = (actions.get("data") or {}).get("meetings") or []
        ctx["corporate_actions"] = {
            "actions": events[:3],
            "meetings": meetings[:2],
        }
    fundamentals = demo.fundamentals(symbol)
    ctx["fundamentals"] = fundamentals
    ctx["source"] = bandar.get("source", "synthetic")
    ctx["warnings"] = list(dict.fromkeys((bandar.get("warnings") or []) + (foreign.get("warnings") or [])))[:3]
    return ctx


# --------------------------------------------------------------------------- scoring
def conviction(row: ScreenRow, *, market: str) -> tuple[float, dict[str, float], list[str]]:
    """Combine the pipeline reading with the market context into -10..+10."""
    parts: dict[str, float] = {}
    notes: list[str] = []

    # 1. technical / pipeline (max ±4)
    if row.action == "LONG":
        parts["pipeline"] = min(4.0, 2.0 + row.score * 0.22)
        notes.append(f"pipeline LONG, score {row.score}/9")
    elif row.action == "SHORT":
        parts["pipeline"] = -min(4.0, 2.0 + row.score * 0.22)
        notes.append(f"pipeline SHORT, score {row.score}/9")
    elif row.stage == "signal":
        drift = (row.score - 4) * 0.4
        parts["pipeline"] = max(-1.5, min(1.5, drift))
        notes.append(f"no entry yet (score {row.score}/9)")
    else:
        parts["pipeline"] = 0.0

    # 2. trend + volatility filter reading (max ±1.5)
    checks = row.context.get("filter_checks") or {}
    trend = checks.get("trend") or {}
    direction = trend.get("detail", "")
    if "-> long" in direction:
        parts["trend_filter"] = 1.0
        notes.append("higher timeframe up")
    elif "-> short" in direction:
        parts["trend_filter"] = -1.0
        notes.append("higher timeframe down")
    else:
        parts["trend_filter"] = 0.0

    # 3. bandarmology (max ±2.5)
    bandar = row.context.get("bandar") or {}
    signal = bandar.get("signal")
    bandar_map = {
        "big_accumulation": 2.5, "accumulation": 1.3, "neutral": 0.0,
        "distribution": -1.3, "big_distribution": -2.5,
    }
    parts["bandar"] = bandar_map.get(str(signal), 0.0)
    if signal:
        notes.append(f"bandar {signal.replace('_', ' ')}" + (
            f" ({bandar.get('net_pct'):+.1f}% of value)" if isinstance(bandar.get("net_pct"), (int, float)) else ""
        ))
    phase = bandar.get("phase")
    if phase in ("quiet_accumulation", "markup"):
        parts["bandar"] += 0.5
    elif phase in ("quiet_distribution", "markdown"):
        parts["bandar"] -= 0.5

    # 4. foreign flow (max ±1.5)
    foreign = row.context.get("foreign") or {}
    streak = foreign.get("streak") or 0
    net_value = foreign.get("net_value_at_close") or 0
    fscore = 0.0
    if isinstance(streak, (int, float)) and streak:
        fscore += 0.5 if streak > 0 else -0.5
    if isinstance(net_value, (int, float)) and net_value:
        fscore += 0.5 if net_value > 0 else -0.5
    parts["foreign_flow"] = max(-1.5, min(1.5, fscore))
    if fscore:
        notes.append(
            f"foreign {'net buy' if net_value > 0 else 'net sell'}"
            + (f", {streak:+d} sessions" if streak else "")
        )

    # 5. valuation / quality (max ±1.0), only for the IDX side
    fund = row.context.get("fundamentals") or {}
    vscore = 0.0
    if market == "idx" and fund:
        pe = fund.get("pe")
        pb = fund.get("pb")
        roe = fund.get("roe_pct")
        growth = fund.get("earnings_growth_pct")
        if isinstance(pe, (int, float)) and 0 < pe < 12:
            vscore += 0.3
        if isinstance(pb, (int, float)) and 0 < pb < 1.5:
            vscore += 0.2
        if isinstance(roe, (int, float)) and roe > 15:
            vscore += 0.25
        if isinstance(growth, (int, float)) and growth > 10:
            vscore += 0.25
        parts["valuation"] = vscore
        if vscore:
            notes.append(f"valuation +{vscore:.2f}")

    total = sum(parts.values())
    return max(-10.0, min(10.0, total)), parts, notes


def label_for(conv: float, action: str, market: str, executable: bool = True) -> str:
    """Map the conviction score to a label.

    The label follows the *conviction*, not the wish: a setup only reads BUY/STRONG_BUY
    when the pipeline actually produced an executable entry and the context agrees.
    """
    if not executable and action == "SHORT":
        return "SELL"  # IDX cash market: a short reading is an exit signal
    if market == "crypto":
        if action == "LONG" and conv >= 6.0:
            return "STRONG_BUY"
        if action == "LONG" and conv >= 2.5:
            return "BUY"
        if action == "SHORT" and conv <= -6.0:
            return "SELL"
        if action == "SHORT" and conv <= -2.5:
            return "REDUCE"
        if conv >= 6.0:
            return "WATCH"
        if conv <= -6.0:
            return "REDUCE"
        return "WATCH" if abs(conv) > 1.0 else "HOLD"
    # IDX: long-only execution, so a negative reading always means reduce/exit
    if conv >= 6.0:
        return "STRONG_BUY" if action == "LONG" else "WATCH"
    if conv >= 2.5:
        return "BUY" if action == "LONG" else "WATCH"
    if conv >= 1.0:
        return "WATCH"
    if conv > -1.5:
        return "HOLD"
    if conv > -3.5:
        return "REDUCE"
    return "SELL"


# --------------------------------------------------------------------------- screening
class Screener:
    def __init__(self, settings: Settings | None = None, *, workers: int = 4) -> None:
        self.settings = settings or Settings.load()
        self.workers = workers
        self.idx = IdxClient(self.settings.section("idx"))
        self.crypto = CryptoClient(
            exchange_id=str(self.settings.get("execution.exchange_id", "bybit")),
            symbols=self.settings.get("profiles.crypto.symbols") or [],
        )

    # ------------------------------------------------------------------ data
    def _idx_symbol_data(self, symbol: str) -> dict[str, Any]:
        candles = self.idx.candles(symbol, period="1y", interval="1d")
        htf = self.idx.candles(symbol, period="2y", interval="1wk")
        quote_payload = self.idx.quote(symbol)
        return {
            "bars": candles["bars"],
            "htf_bars": htf["bars"],
            "quote": quote_payload.get("quote") or {},
            "source": candles.get("source", "synthetic"),
            "warnings": list(candles.get("warnings") or []),
        }

    def _crypto_symbol_data(self, symbol: str) -> dict[str, Any]:
        profile = self.settings.profile("crypto")
        candles = self.crypto.candles(symbol, str(profile.get("timeframe", "15m")), 400)
        htf = self.crypto.candles(symbol, str(profile.get("trend_timeframe", "4h")), 260)
        tick = self.crypto.ticker(symbol)
        return {
            "bars": candles["bars"],
            "htf_bars": htf["bars"],
            "quote": tick,
            "source": candles.get("source", "synthetic"),
            "warnings": list(candles.get("warnings") or []),
        }

    # ------------------------------------------------------------------ public
    def universe(self, market: str, limit: int = 20, symbols: Sequence[str] | None = None) -> list[str]:
        if symbols:
            return [s.upper() if market == "idx" else s for s in symbols][:limit]
        if market == "idx":
            return self.idx.universe(limit=limit)
        return list(self.settings.get("profiles.crypto.symbols") or [])[:limit]

    def screen(
        self,
        market: str,
        *,
        symbols: Sequence[str] | None = None,
        limit: int = 20,
        deep_context: bool = True,
        with_risk: bool = True,
    ) -> dict[str, Any]:
        market = market.lower()
        targets = self.universe(market, limit=limit, symbols=symbols)
        fetch = self._idx_symbol_data if market == "idx" else self._crypto_symbol_data
        rows: list[ScreenRow] = []
        failures: list[str] = []

        def work(symbol: str) -> ScreenRow | None:
            data = fetch(symbol)
            scan: ScanResult = scan_symbol(
                symbol=symbol,
                market=market,
                bars=data["bars"],
                htf_bars=data["htf_bars"],
                quote=data["quote"],
                settings=self.settings,
                stale=False,
                source=str(data.get("source")),
                warnings=data.get("warnings") or [],
                with_risk=with_risk,
            )
            quote = data["quote"]
            name = quote.get("name") or (demo.IDX_REFERENCE.get(symbol, (symbol,))[0] if market == "idx" else symbol)
            context: dict[str, Any] = {
                "filter_checks": {
                    c["name"]: c for c in (scan.filter.get("checks") or [])
                }
            }
            if market == "idx":
                context.update(idx_context(self.idx, symbol, deep=deep_context))
            else:
                context["fundamentals"] = {}
            row = ScreenRow(
                symbol=symbol,
                name=str(name),
                market=market,
                price=scan.price,
                change_pct=quote.get("change_pct"),
                label="HOLD",
                conviction=0.0,
                action=scan.action,
                score=scan.score,
                stage=scan.stage,
                reasons=list(scan.reasons),
                plan=(scan.risk or {}).get("plan"),
                context=context,
                indicators=scan.indicators,
                source=scan.source,
                warnings=scan.warnings,
            )
            conv, parts, notes = conviction(row, market=market)
            row.conviction = conv
            row.components = parts
            row.label = label_for(conv, row.action, market, bool(scan.executable or row.action == "WAIT"))
            row.reasons = (scan.reasons or [])[:3] + notes
            return row

        with futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            for symbol, result in zip(targets, pool.map(_safe(work), targets)):
                if isinstance(result, ScreenRow):
                    rows.append(result)
                else:
                    failures.append(f"{symbol}: {result}")

        rows.sort(key=lambda r: r.conviction, reverse=True)
        return {
            "market": market,
            "generated_at": dt.datetime.now(ZoneInfo("Asia/Jakarta")).isoformat(),
            "universe": targets,
            "scanned": len(rows),
            "failures": failures,
            "rows": [r.to_dict() for r in rows],
            "counts": _counts(rows),
            "disclaimer": (
                "Screening output computed from the documented pipeline. Synthetic data is "
                "used whenever the live provider is unavailable — check the 'source' field "
                "on every row. Not investment advice."
            ),
        }

    # ------------------------------------------------------------------ single symbol
    def analyse(self, market: str, symbol: str) -> dict[str, Any]:
        market = market.lower()
        data = self._idx_symbol_data(symbol) if market == "idx" else self._crypto_symbol_data(symbol)
        scan = scan_symbol(
            symbol=symbol,
            market=market,
            bars=data["bars"],
            htf_bars=data["htf_bars"],
            quote=data["quote"],
            settings=self.settings,
            stale=False,
            source=str(data.get("source")),
            warnings=data.get("warnings") or [],
        )
        context: dict[str, Any] = {}
        if market == "idx":
            context = idx_context(self.idx, symbol, deep=True)
            context["broker"] = self.idx.broker(symbol).get("data") or {}
            context["brief"] = (self.idx.brief(symbol).get("data") or {})
        row = ScreenRow(
            symbol=symbol,
            name=str(data["quote"].get("name") or symbol),
            market=market,
            price=scan.price,
            change_pct=data["quote"].get("change_pct"),
            label="HOLD",
            conviction=0.0,
            action=scan.action,
            score=scan.score,
            stage=scan.stage,
            reasons=list(scan.reasons),
            plan=(scan.risk or {}).get("plan"),
            context={**context, "filter_checks": {c["name"]: c for c in (scan.filter.get("checks") or [])}},
            indicators=scan.indicators,
            source=scan.source,
            warnings=scan.warnings,
        )
        conv, parts, notes = conviction(row, market=market)
        row.conviction = conv
        row.components = parts
        row.label = label_for(conv, row.action, market, bool(scan.executable or row.action == "WAIT"))
        row.reasons = (scan.reasons or [])[:3] + notes
        return {
            "scan": scan.to_dict(),
            "row": row.to_dict(),
            "candles": data["bars"][-260:],
            "htf_candles": data["htf_bars"][-120:],
            "quote": data["quote"],
            "context": context,
            "source": data.get("source"),
            "warnings": data.get("warnings") or [],
        }


def _counts(rows: Iterable[ScreenRow]) -> dict[str, int]:
    out = {label: 0 for label in LABELS}
    for row in rows:
        out[row.label] = out.get(row.label, 0) + 1
    return out


def _safe(fn):
    def wrapper(symbol: str):
        try:
            return fn(symbol)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the screen
            return f"{type(exc).__name__}: {exc}"

    return wrapper
