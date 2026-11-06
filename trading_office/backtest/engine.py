"""Event-driven backtester for the four-gate pipeline.

Rules that keep the simulation honest:

* Signals are evaluated on **closed bars only**; a fill happens at the **next bar's
  open** (plus the spread/slippage of the profile), never at the signal bar's close.
* Intrabar order of events per bar: gaps are respected — if the bar opens beyond the
  stop, the stop fills at the open, not at the stop price.
* Stops and targets are checked against the bar's high/low, with the pessimistic
  assumption that the stop is hit first when both are inside the same bar.
* Commissions, spread and slippage come from the market profile **and from the cost
  model in :mod:`trading_office.costs`**: slippage scales with ATR% and with the size's
  share of the bar's volume, a ``latency_ms`` delay moves the fill off the open, and a
  partial take profit fills at its target price but still pays the taker fee.
* Gaps are not free: a stop that is jumped fills at the open plus
  ``gap_fill_fraction`` of the jump, and a gap wider than ``gap_reject_atr`` blocks new
  entries for ``gap_cooldown_bars`` bars — the same guard the live engine applies.
* The market filter, signal scoring, risk sizing and trade management are the *same*
  functions the live engine calls — there is no parallel "test" logic.

Usage::

    python -m trading_office.backtest --market crypto --data data/crypto_BTCUSDT_15m.json
    python -m trading_office.backtest --market idx --data data/idx_swing.json --oos 0.3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import costs, indicators as ind
from .. import market_filter, risk, signals
from ..config import Settings, idx_tick_size
from ..management import Position, manage_position


# --------------------------------------------------------------------------- helpers
def _as_dt(value: Any, fallback_tz: str = "Asia/Jakarta") -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(fallback_tz)
    except Exception:  # pragma: no cover
        tz = dt.timezone.utc
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value) / 1000.0, dt.timezone.utc)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return dt.datetime.now(dt.timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    return dt.datetime.now(dt.timezone.utc)


def resample(bars: Sequence[dict[str, Any]], factor: int) -> list[dict[str, Any]]:
    """Aggregate ``factor`` consecutive bars into one (simple OHLCV resampling)."""
    if factor <= 1:
        return list(bars)
    out: list[dict[str, Any]] = []
    for i in range(0, len(bars), factor):
        chunk = bars[i : i + factor]
        if len(chunk) < factor and out:
            break
        out.append(
            {
                "time": chunk[0]["time"],
                "open": chunk[0]["open"],
                "high": max(b["high"] for b in chunk),
                "low": min(b["low"] for b in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(float(b.get("volume") or 0) for b in chunk),
            }
        )
    return out


def align_htf(
    ltf_bars: Sequence[dict[str, Any]],
    htf_bars: Sequence[dict[str, Any]],
    factor: int,
) -> list[list[dict[str, Any]]]:
    """For each LTF bar, the list of *closed* HTF bars available at that moment.

    ``htf`` is built by resampling the LTF series, so an HTF bar only becomes visible
    once its final LTF member has closed. That removes the look-ahead trap of using a
    weekly EMA while the week is still forming.
    """
    out: list[list[dict[str, Any]]] = []
    htf_index = 0
    for i in range(len(ltf_bars)):
        # HTF bar that contains bar i finishes at the end of its group
        completed = (i + 1) // factor
        if completed != htf_index:
            htf_index = completed
        out.append(htf_bars[:htf_index])
    return out


# --------------------------------------------------------------------------- result
@dataclass
class BacktestResult:
    market: str
    symbol: str
    timeframe: str
    bars: int
    start: str
    end: str
    starting_equity: float
    ending_equity: float
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    filters_blocked: dict[str, int] = field(default_factory=dict)
    signals_by_bar: list[dict[str, Any]] = field(default_factory=list)
    cost_report: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ metrics
    @property
    def wins(self) -> list[dict[str, Any]]:
        return [t for t in self.trades if t["pnl"] > 0]

    @property
    def losses(self) -> list[dict[str, Any]]:
        return [t for t in self.trades if t["pnl"] <= 0]

    def metrics(self) -> dict[str, Any]:
        n = len(self.trades)
        pnls = [float(t["pnl"]) for t in self.trades]
        r_multiples = [float(t["r_multiple"]) for t in self.trades]
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)
        # closed-trade view of the drawdown: every trade worst case is its exit P&L
        peak = self.starting_equity
        max_dd = 0.0
        for _ts, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100.0)
        # mark-to-market view: the worst *open* excursion counts too, which is what can
        # actually stop a live account out (a paper DD that ignores it flatters the run)
        peak = self.starting_equity
        max_dd_mtm = max_dd
        running = self.starting_equity
        for trade in self.trades:
            if trade.get("worst_equity") is not None:
                running = float(trade["worst_equity"])
                peak = max(peak, running)
                max_dd_mtm = max(max_dd_mtm, (peak - running) / peak * 100.0 if peak > 0 else 0.0)
            running += float(trade["pnl"])
            peak = max(peak, running)
            max_dd_mtm = max(max_dd_mtm, (peak - running) / peak * 100.0 if peak > 0 else 0.0)
        returns = []
        for i in range(1, len(self.equity_curve)):
            prev = self.equity_curve[i - 1][1]
            if prev:
                returns.append((self.equity_curve[i][1] - prev) / prev)
        sharpe = 0.0
        if len(returns) > 2:
            sd = statistics.pstdev(returns)
            if sd > 0:
                sharpe = statistics.mean(returns) / sd * math.sqrt(252)
        return {
            "trades": n,
            "wins": len(self.wins),
            "losses": len(self.losses),
            "win_rate_pct": round(len(self.wins) / n * 100.0, 2) if n else 0.0,
            "net_pnl": round(sum(pnls), 2),
            "return_pct": round((self.ending_equity / self.starting_equity - 1) * 100.0, 2)
            if self.starting_equity
            else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "expectancy_cash": round(sum(pnls) / n, 2) if n else 0.0,
            "expectancy_r": round(sum(r_multiples) / n, 3) if n else 0.0,
            "avg_win": round(gross_win / len(self.wins), 2) if self.wins else 0.0,
            "avg_loss": round(-gross_loss / len(self.losses), 2) if self.losses else 0.0,
            "max_drawdown_pct": round(max_dd, 2),
            "max_drawdown_pct_mtm": round(max_dd_mtm, 2),
            "avg_mae_r": round(
                statistics.mean([float(t.get("mae_r") or 0.0) for t in self.trades]), 3
            ) if self.trades else 0.0,
            "sharpe": round(sharpe, 2),
            "long_trades": sum(1 for t in self.trades if t["side"] == "long"),
            "short_trades": sum(1 for t in self.trades if t["side"] == "short"),
            "partials": sum(int(t.get("partials") or 0) for t in self.trades),
            "gap_stops": sum(1 for t in self.trades if "gap" in str(t.get("reason", ""))),
            "avg_entry_slippage_bps": round(
                statistics.mean(
                    [float(t["entry_slippage_bps"]) for t in self.trades
                     if t.get("entry_slippage_bps") is not None]
                ), 3,
            ) if any(t.get("entry_slippage_bps") is not None for t in self.trades) else None,
            "avg_exit_slippage_bps": round(
                statistics.mean([float(t.get("exit_slippage_bps") or 0.0) for t in self.trades]), 3
            ) if self.trades else None,
            "avg_bars_held": round(statistics.mean([t["bars_held"] for t in self.trades]), 1)
            if self.trades
            else 0.0,
            "rejections": dict(sorted(self.rejections.items(), key=lambda kv: -kv[1])),
            "filters_blocked": dict(sorted(self.filters_blocked.items(), key=lambda kv: -kv[1])),
            "costs": self.cost_report,
        }

    def _curve_with_drawdown(self) -> list[dict[str, Any]]:
        """``[time, equity]`` -> points carrying the running peak and drawdown %.

        The engine's live charts and any UI reading this payload can then plot the
        drawdown band without re-deriving it (and without disagreeing with the metrics).
        """
        peak = self.starting_equity
        out: list[dict[str, Any]] = []
        for ts, eq in self.equity_curve:
            peak = max(peak, float(eq))
            dd = ((peak - float(eq)) / peak * 100.0) if peak > 0 else 0.0
            out.append({"time": ts, "equity": round(float(eq), 2),
                        "peak": round(peak, 2), "drawdown_pct": round(dd, 3)})
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "start": self.start,
            "end": self.end,
            "starting_equity": self.starting_equity,
            "ending_equity": round(self.ending_equity, 2),
            "metrics": self.metrics(),
            "trades": self.trades,
            "equity_curve": self._curve_with_drawdown(),
            "signals_by_bar": self.signals_by_bar,
            "cost_model": self.cost_report.get("model"),
        }


# --------------------------------------------------------------------------- engine
class Backtester:
    def __init__(
        self,
        *,
        market: str,
        settings: Settings | None = None,
        risk_override: dict[str, Any] | None = None,
        signal_override: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings or Settings.load()
        self.market = market
        self.profile = self.settings.profile(market)
        self.filter_cfg = self.settings.merged("market_filter", "engine")
        self.signal_cfg = {
            **self.settings.section("signal"),
            "timeframe": self.profile.get("timeframe"),
            **(signal_override or {}),
        }
        self.risk_cfg = {**self.settings.section("risk"), **(risk_override or {})}
        self.risk_cfg.setdefault("max_leverage", self.profile.get("leverage", 1) if market == "idx" else self.risk_cfg.get("max_leverage", 3))
        self.mgmt_cfg = self.settings.section("management")
        self.engine_cfg = self.settings.section("engine")
        self.cost_cfg = costs.CostConfig.for_market(market, self.profile)
        self.cost_events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers
    def _equity_start(self) -> float:
        return float(
            self.engine_cfg.get("equity_idr" if self.market == "idx" else "equity_usdt", 1_000.0)
        )

    def _commission(self, side: str, notional: float) -> float:
        p = self.profile
        if self.market == "idx":
            pct = float(p.get("commission_buy_pct" if side == "buy" else "commission_sell_pct", 0.15))
            return notional * pct / 100.0
        return notional * float(p.get("commission_bps", 5.5)) / 10_000.0

    def _quote_volume(self, bars: Sequence[dict[str, Any]], window: int = 20) -> float | None:
        recent = [float(b.get("volume") or 0) * float(b["close"]) for b in bars[-window:]]
        recent = [v for v in recent if v > 0]
        if not recent:
            return None
        return sorted(recent)[len(recent) // 2]

    def _slippage(self, *, price: float, atr: float, notional: float,
                  quote_volume: float | None,
                  atr_baseline: float | None = None) -> costs.SlippageEstimate:
        estimate = costs.estimate_slippage(
            price=price, atr=atr, notional=notional, quote_volume=quote_volume,
            cfg=self.cost_cfg, atr_baseline=atr_baseline,
        )
        self.cost_events.append(
            {"kind": "slippage", **estimate.to_dict(), "notional": round(notional, 2)}
        )
        return estimate

    def _exit_slippage_bps(self, *, bar: dict[str, Any], atr: float,
                          notional: float) -> float:
        """Slippage charged on the way out.

        Entry slippage alone flatters a backtest: the stop would always fill exactly at
        the stop price, which is the one fill a real book is most likely to miss. The same
        cost model runs on exits, but **liquidity-seeking exits are cheaper**: a resting
        take-profit is a limit fill, and a stop is a market order into a book that is
        already moving. Both are represented instead of assuming the best case.
        """
        estimate = costs.estimate_slippage(
            price=float(bar.get("close") or 0.0) or 1.0,
            atr=atr, notional=notional, quote_volume=bar.get("quote_volume"),
            cfg=self.cost_cfg, atr_baseline=None,
        )
        self.cost_events.append({"kind": "slippage", **estimate.to_dict(), "side": "exit"})
        return estimate.slippage_bps

    @staticmethod
    def _exit_kind_factor(reason: str) -> float:
        """How much of the modelled slippage a given exit actually pays."""
        text = (reason or "").lower()
        if "take profit" in text or "partial" in text or "target" in text:
            return 0.0  # resting limit order: filled at its own price or not at all
        if "gap" in text:
            return 0.0  # gap openings are already priced by gap_adjusted_stop
        return 0.6  # stop / time / invalidation exits are market orders

    def _atr_baseline(self, closed: Sequence[dict[str, Any]]) -> float | None:
        """Mean ATR over the last ``shock_baseline_bars`` bars — the shock reference.

        Comparing today's ATR with a *rolling mean of the same series* (rather than a
        fixed percentage) keeps the guard meaningful in both a quiet BTC tape and a
        position with a permanently fat ATR.
        """
        window = int(getattr(self, "shock_baseline_bars", 50) or 50)
        if len(closed) < 20:
            return None
        period = int(self.filter_cfg.get("atr_period", 14))
        highs = [b["high"] for b in closed]
        lows = [b["low"] for b in closed]
        closes = [b["close"] for b in closed]
        series = ind.atr(highs, lows, closes, period)
        tail = [v for v in series[-window:] if v is not None]
        if not tail:
            return None
        return float(statistics.mean(tail))

    @staticmethod
    def _open_pnl(position: Position, mark: float) -> float:
        direction = 1.0 if position.side == "long" else -1.0
        return (float(mark) - position.entry) * position.size * direction

    def _spread_bps(self, bar: dict[str, Any], tick: float | None) -> float:
        if tick and bar["close"]:
            return 2.0 * tick / bar["close"] * 10_000.0
        return float(self.profile.get("slippage_bps", 2.0))

    # ------------------------------------------------------------------ cost report
    def _cost_report(self) -> dict[str, Any]:
        """What execution actually cost: slippage distribution, latency drag, gap events."""
        slips = [e["slippage_bps"] for e in self.cost_events if e.get("kind") == "slippage"]
        latencies = [
            abs(e["latency"]["moved_bps"]) for e in self.cost_events
            if e.get("kind") == "entry" and e.get("latency")
        ]
        gaps = [e for e in self.cost_events if e.get("kind") == "gap_guard"]
        cancelled = [e for e in self.cost_events if e.get("kind") == "entry_cancelled"]
        partials = [e for e in self.cost_events if e.get("kind") == "partial"]
        return {
            "model": self.cost_cfg.to_dict(),
            "entries": sum(1 for e in self.cost_events if e.get("kind") == "entry"),
            "slippage_bps": costs.quantiles(slips, (0.5, 0.9, 1.0)) if slips else {},
            "slippage_capped": sum(
                1 for e in self.cost_events if e.get("kind") == "slippage" and e.get("capped")
            ),
            "shock_fills": sum(
                1 for e in self.cost_events
                if e.get("kind") == "slippage"
                and float((e.get("components") or {}).get("atr_stress") or 0) >= 1.5
            ),
            "avg_k_slip": round(
                statistics.mean(
                    [float((e.get("components") or {}).get("k_slip") or 0.0)
                     for e in self.cost_events if e.get("kind") == "slippage"]
                ), 4,
            ) if any(e.get("kind") == "slippage" for e in self.cost_events) else None,
            "latency_adverse_bps": costs.quantiles(latencies, (0.5, 0.9, 1.0)) if latencies else {},
            "latency_ms": self.cost_cfg.latency_ms,
            "gap_guards": len(gaps),
            "gap_examples": gaps[:3],
            "entries_cancelled": len(cancelled),
            "cancelled_examples": cancelled[:3],
            "partial_fills": len(partials),
            "partial_pnl": round(sum(float(e.get("pnl", 0.0)) for e in partials), 2),
        }

    # ------------------------------------------------------------------ main loop
    def run(
        self,
        symbol: str,
        bars: Sequence[dict[str, Any]],
        *,
        htf_factor: int | None = None,
        start_equity: float | None = None,
        collect_signals: bool = True,
        window: tuple[str | None, str | None] = (None, None),
    ) -> BacktestResult:
        bars = [b for b in bars if b.get("close") and b.get("high") and b.get("low")]
        if len(bars) < 120:
            raise ValueError(f"need at least 120 bars, got {len(bars)}")
        self.cost_events = []
        factor = int(htf_factor or self.profile.get("htf_factor", 8 if self.market == "crypto" else 5))
        htf_bars = resample(bars, factor)
        htf_view = align_htf(bars, htf_bars, factor)
        tick = self.profile.get("tick_table") and self.market == "idx"

        equity = float(start_equity if start_equity is not None else self._equity_start())
        result = BacktestResult(
            market=self.market,
            symbol=symbol,
            timeframe=str(self.profile.get("timeframe", "")),
            bars=len(bars),
            start=str(bars[0]["time"]),
            end=str(bars[-1]["time"]),
            starting_equity=equity,
            ending_equity=equity,
        )
        result.equity_curve.append((str(bars[0]["time"]), equity))

        # Walk-forward windows: indicators warm up on the whole series, but entries are
        # only allowed inside the window. This is what makes an out-of-sample slice fair
        # (it is not handicapped by a missing EMA warm-up).
        win_start, win_end = window
        first_i = 60
        last_i = len(bars) - 1
        if win_start:
            first_i = max(first_i, next((j for j, b in enumerate(bars) if str(b["time"]) >= win_start), len(bars) - 2))
        if win_end:
            last_i = min(last_i, next((j for j, b in enumerate(bars) if str(b["time"]) >= win_end), len(bars) - 1))

        position: Position | None = None
        pending: dict[str, Any] | None = None
        closed_today: list[dict[str, Any]] = []
        current_day: str | None = None
        last_exit_bar: str | None = None
        cooldown = int(self.engine_cfg.get("cooldown_bars", 3))
        gap_cooldown_until = -1  # bar index until which new entries are blocked (gap shock)

        for i in range(first_i, last_i):
            bar = bars[i]
            next_bar = bars[i + 1]
            now = _as_dt(bar["time"], str(self.profile.get("sessions_timezone", "Asia/Jakarta")))
            today = now.strftime("%Y-%m-%d")
            if today != current_day:
                closed_today = []
                current_day = today

            closed = bars[: i + 1]
            htf_closed = htf_view[i]

            # ------------------------------------------------------ fill the pending order
            if pending is not None:
                plan = pending["plan"]
                fill_price = float(next_bar["open"]) if i + 1 < len(bars) else float(next_bar["close"])
                # latency: the order does not arrive instantly, so it fills along the bar's drift
                latency = costs.apply_latency(
                    fill_price, bar, latency_ms=self.cost_cfg.latency_ms,
                    timeframe=str(self.profile.get("timeframe", "15m")),
                    side="buy" if plan.side == "long" else "sell",
                )
                fill_price = latency.fill_price
                # black-swan veto: if the fill bar already opened through the planned
                # stop, the setup is invalid — entering here would mean a long whose
                # stop sits above the market (a guaranteed "profit" on a gap down).
                through = (
                    float(next_bar["open"]) <= plan.stop if plan.side == "long"
                    else float(next_bar["open"]) >= plan.stop
                )
                if through:
                    self.cost_events.append(
                        {
                            "kind": "entry_cancelled", "time": str(bar["time"]),
                            "reason": "open gapped through the planned stop",
                            "open": round(float(next_bar["open"]), 8), "stop": round(plan.stop, 8),
                            "side": plan.side,
                        }
                    )
                    result.rejections["entry_gapped_through_stop"] = (
                        result.rejections.get("entry_gapped_through_stop", 0) + 1
                    )
                    pending = None
                    continue
                # dynamic slippage: volatility + size against the book
                spread = self._spread_bps(pending["bar"], pending.get("tick"))
                notional = fill_price * plan.size
                slip_est = self._slippage(
                    price=fill_price, atr=float(pending.get("atr") or 0.0), notional=notional,
                    quote_volume=pending.get("quote_volume"),
                    atr_baseline=pending.get("atr_baseline"),
                )
                direction = 1.0 if plan.side == "long" else -1.0
                fill_price = fill_price * (1 + direction * (spread / 2 / 10_000.0))
                fill_price = (slip_est.fill_price / max(fill_price, 1e-12)) * fill_price
                fee = self._commission("buy" if plan.side == "long" else "sell", abs(fill_price * plan.size))
                self.cost_events.append(
                    {
                        "kind": "entry", "time": str(bar["time"]), "latency": latency.to_dict(),
                        "slippage_bps": round(slip_est.slippage_bps, 3), "spread_bps": round(spread, 3),
                        "fill": round(fill_price, 8), "signal_bar_open": round(float(next_bar["open"]), 8),
                    }
                )
                position = Position(
                    symbol=symbol,
                    market=self.market,
                    side=plan.side,
                    entry=fill_price,
                    stop=plan.stop,
                    target=plan.target,
                    size=plan.size,
                    initial_stop=plan.stop,
                    opened_at=str(bar["time"]),
                    bar_time=str(pending["bar_time"]),
                    initial_size=plan.size,
                    meta={
                        "reasons": pending["reasons"],
                        "fees": fee,
                        "plan": plan.to_dict(),
                        "planned_entry": plan.entry,
                        "planned_stop": plan.stop,
                        "slippage_bps": round(slip_est.slippage_bps, 3),
                        "latency_ms": self.cost_cfg.latency_ms,
                    },
                )
                pending = None

            # ------------------------------------------------------ manage the open position
            if position is not None:
                bar_high = float(bar["high"])
                bar_low = float(bar["low"])
                spread_bps_now = self._spread_bps(bar, tick if tick else None)
                atr_now = ind.last_defined(
                    ind.atr(
                        [b["high"] for b in closed], [b["low"] for b in closed],
                        [b["close"] for b in closed], int(self.filter_cfg.get("atr_period", 14)),
                    )
                ) or 0.0

                # gap shock between sessions -> cooldown for new entries (black-swan guard)
                guard = costs.gap_guard(
                    prev_close=float(bars[i - 1]["close"]),
                    open_price=float(bar["open"]),
                    atr=atr_now,
                    spread_bps=spread_bps_now,
                    max_spread_bps=float(self.profile.get("max_spread_bps", 8.0)),
                    cfg=self.cost_cfg,
                )
                if guard.cooldown and gap_cooldown_until < i:
                    gap_cooldown_until = i + int(guard.cooldown)
                    self.cost_events.append(
                        {"kind": "gap_guard", "time": str(bar["time"]), **guard.to_dict()}
                    )

                # gap through the stop: the bracket cannot protect a jumped price
                exit_price = None
                exit_reason = ""
                long = position.side == "long"
                jumped = (
                    float(bar["open"]) <= position.stop if long
                    else float(bar["open"]) >= position.stop
                )
                if jumped:
                    exit_price = costs.gap_adjusted_stop(
                        exit_price=position.stop, next_open=float(bar["open"]),
                        side=position.side, cfg=self.cost_cfg, atr=atr_now,
                    )
                    exit_reason = "stop loss (gap through)"

                # worst open excursion (MAE): the mark-to-market drawdown a live account
                # would have had to survive *before* this bar resolved
                bar_worst = bar_low if long else bar_high
                worst_px = float(position.meta.get("worst_px") or position.entry)
                worst_px = min(worst_px, bar_worst) if long else max(worst_px, bar_worst)
                position.meta["worst_px"] = worst_px

                if exit_price is None:
                    actions = manage_position(
                        position,
                        closed_bar=bar,
                        price=float(bar["close"]),
                        atr=atr_now,
                        now=now,
                        mgmt_cfg=self.mgmt_cfg,
                        profile=self.profile,
                        spread_bps=spread_bps_now,
                        opposite_signal=False,
                        tick_size=idx_tick_size(bar["close"]) if tick else None,
                        size_step=self.profile.get("size_step"),
                    )
                    for action in actions:
                        if action.kind == "close":
                            exit_price, exit_reason = action.price or float(bar["close"]), action.reason
                        elif action.kind == "modify_stop" and action.new_stop:
                            position.stop = action.new_stop
                        elif action.kind == "partial":
                            # a limit take profit fills at its own price, fees still apply
                            fill = action.pnl_target_price or action.price or float(
                                bar["high"] if long else bar["low"]
                            )
                            slice_size = position.size * float(action.fraction or 0.5)
                            if slice_size <= 0:
                                continue
                            gross = (fill - position.entry) * slice_size * (1.0 if long else -1.0)
                            fee = self._commission("sell" if long else "buy", abs(fill * slice_size))
                            position.realised_pnl += gross - fee
                            position.size = round(position.size - slice_size, 12)
                            position.meta["partials"] = position.meta.get("partials", []) + [
                                {
                                    "fraction": action.fraction,
                                    "size": round(slice_size, 8),
                                    "fill": round(fill, 8),
                                    "pnl": round(gross - fee, 2),
                                    "at": str(bar["time"]),
                                }
                            ]
                            # banked profit counts towards equity immediately
                            equity += gross - fee
                            self.cost_events.append(
                                {
                                    "kind": "partial", "time": str(bar["time"]),
                                    "size": round(slice_size, 8), "fill": round(fill, 8),
                                    "pnl": round(gross - fee, 2), "fee": round(fee, 2),
                                }
                            )
                            if position.size <= 0:
                                exit_price, exit_reason = fill, "partial closed the whole position"

                if exit_price is not None:
                    # exits pay slippage too (see _exit_kind_factor for the per-reason share)
                    exit_slip_bps = self._exit_slippage_bps(
                        bar=bar, atr=atr_now, notional=abs(exit_price * position.size)
                    ) * self._exit_kind_factor(exit_reason)
                    if exit_slip_bps:
                        exit_price *= 1 - (1 if long else -1) * exit_slip_bps / 10_000.0
                    fee = self._commission("sell" if long else "buy", abs(exit_price * position.size))
                    direction = 1.0 if long else -1.0
                    gross = (exit_price - position.entry) * position.size * direction
                    total_fee = fee + float(position.meta.get("fees", 0.0))
                    pnl = gross - total_fee
                    equity += pnl
                    rpu = max(position.risk_per_unit, 1e-12)
                    initial_size = max(position.initial_size or position.size, 1e-12)
                    trade = {
                        "symbol": symbol,
                        "side": position.side,
                        # the plan's numbers, not the slip-adjusted fill: the R maths must
                        # stay anchored to the geometry the risk gate actually approved
                        "entry": round(position.entry, 6),
                        "planned_entry": round(float(position.meta.get("planned_entry") or position.entry), 6),
                        "planned_stop": round(float(position.meta.get("planned_stop") or position.initial_stop), 6),
                        "initial_stop": round(position.initial_stop, 6),
                        "exit": round(exit_price, 6),
                        "stop": round(position.initial_stop, 6),
                        "target": round(position.target, 6),
                        "size": round(initial_size, 6),
                        "size_left": round(position.size, 6),
                        "pnl": round(pnl + position.realised_pnl, 2),
                        "pnl_runner": round(pnl, 2),
                        "pnl_banked": round(position.realised_pnl, 2),
                        "fees": round(total_fee, 2),
                        "partials": len(position.meta.get("partials", [])),
                        "r_multiple": round((pnl + position.realised_pnl) / (rpu * initial_size), 3),
                        "reason": exit_reason,
                        "gap_risk": exit_reason == "stop loss (gap through)",
                        "worst_equity": round(equity + self._open_pnl(position, worst_px), 2),
                        "mae_r": round(
                            ((position.entry - worst_px) if long else (worst_px - position.entry))
                            / max(position.risk_per_unit, 1e-12), 3
                        ),
                        "entry_reason": " | ".join(position.meta.get("reasons", [])),
                        "opened_at": position.opened_at,
                        "closed_at": str(bar["time"]),
                        "bars_held": position.bars_held,
                        "rr_planned": round(abs(position.target - position.entry) / rpu, 2),
                        "entry_slippage_bps": position.meta.get("slippage_bps"),
                        "exit_slippage_bps": round(exit_slip_bps, 3),
                    }
                    result.trades.append(trade)
                    closed_today.append(trade)
                    last_exit_bar = str(bar["time"])
                    position = None
                    result.equity_curve.append((str(bar["time"]), equity))
                    continue

            result.equity_curve.append((str(bar["time"]), equity))

            # ------------------------------------------------------ new entries
            if position is not None or pending is not None or last_exit_bar == str(bar["time"]):
                continue
            if last_exit_bar and cooldown:
                idx_exit = next((j for j in range(i, -1, -1) if str(bars[j]["time"]) == last_exit_bar), None)
                if idx_exit is not None and i - idx_exit < cooldown:
                    continue

            # cooldown after deployment of capital: one position at a time in the test
            ctx = market_filter.build_context_from_bars(
                symbol,
                self.market,
                closed,
                htf_closed,
                now=now,
                profile=self.profile,
                config=self.filter_cfg,
                stale=False,
                data_age_s=0,
                is_trading_day=now.weekday() < 5,
                gap_cooldown_bars=max(0, gap_cooldown_until - i),
            )
            if tick:
                ctx.tick_size = idx_tick_size(bar["close"])
            filt = market_filter.evaluate(ctx, profile=self.profile, config=self.filter_cfg)
            if not filt.passed:
                for check in filt.checks:
                    if not check.passed:
                        result.filters_blocked[check.name] = result.filters_blocked.get(check.name, 0) + 1
                continue

            sig = signals.evaluate(symbol, closed, self.signal_cfg, bar_time=str(bar["time"]))
            if collect_signals:
                result.signals_by_bar.append(
                    {
                        "time": str(bar["time"]),
                        "action": sig.action,
                        "long": sig.long_score,
                        "short": sig.short_score,
                    }
                )
            if sig.action == "WAIT":
                key = sig.block_code or "wait"
                result.rejections[key] = result.rejections.get(key, 0) + 1
                continue
            if filt.direction and filt.direction != sig.action.lower():
                result.rejections["signal against HTF trend"] = (
                    result.rejections.get("signal against HTF trend", 0) + 1
                )
                continue

            entry = float(bar["close"])
            spread_bps = self._spread_bps(bar, idx_tick_size(entry) if tick else None)
            atr_pct_now = (float(sig.atr or 0.0) / entry * 100.0) if entry else None
            risk_mult = costs.risk_multiplier(
                spread_bps=spread_bps,
                max_spread_bps=float(self.profile.get("max_spread_bps", 8.0)),
                atr_pct=atr_pct_now,
                profile=self.profile,
                cfg=self.cost_cfg,
            )
            decision = risk.build_plan(
                symbol=symbol,
                market=self.market,
                side=sig.action.lower(),
                entry=entry,
                atr=float(sig.atr or 0.0),
                bars=closed,
                equity=equity,
                config=self.signal_cfg,
                risk_cfg=self.risk_cfg,
                profile=self.profile,
                spread_bps=spread_bps,
                tick_size=idx_tick_size(entry) if tick else None,
                quote_volume=ctx.quote_volume,
                risk_multiplier=risk_mult,
                risk_note=(
                    f"risk scaled {1/risk_mult:.2f}x down: spread {spread_bps:.1f} bps, "
                    f"ATR {atr_pct_now:.2f}%" if risk_mult != 1.0 else ""
                ),
            )
            if not decision.approved:
                for reason in decision.rejections:
                    key = reason.split("(")[0].split(":")[0].strip()
                    result.rejections[key] = result.rejections.get(key, 0) + 1
                continue
            plan = decision.plan
            if plan is None or plan.size <= 0:
                continue
            pending = {
                "plan": plan,
                "bar": bar,
                "bar_time": str(bar["time"]),
                "reasons": sig.reasons_for(sig.action),
                "tick": idx_tick_size(entry) if tick else None,
                "atr": float(sig.atr or 0.0),
                "atr_baseline": self._atr_baseline(closed),
                "quote_volume": ctx.quote_volume,
            }
            result.equity_curve.append((str(bar["time"]) + "+signal", equity))

        # force-close at the last bar of the window so the metrics include the open trade
        if position is not None:
            last = bars[min(last_i, len(bars) - 1)]
            exit_price = float(last["close"])
            long = position.side == "long"
            # a forced exit is a market exit: it pays the same slippage a live one would
            exit_slip_bps = self._exit_slippage_bps(
                bar=last, atr=atr_now if "atr_now" in locals() else 0.0,
                notional=abs(exit_price * position.size),
            ) * self._exit_kind_factor("end of data")
            if exit_slip_bps:
                exit_price *= 1 - (1 if long else -1) * exit_slip_bps / 10_000.0
            fee = self._commission("sell" if long else "buy", abs(exit_price * position.size))
            gross = (exit_price - position.entry) * position.size * (1 if long else -1)
            pnl = gross - fee - float(position.meta.get("fees", 0.0))
            equity += pnl
            rpu = max(position.risk_per_unit, 1e-12)
            initial_size = max(position.initial_size or position.size, 1e-12)
            result.trades.append(
                {
                    "symbol": symbol,
                    "side": position.side,
                    "entry": round(position.entry, 6),
                    "exit": round(exit_price, 6),
                    "stop": round(position.initial_stop, 6),
                    "target": round(position.target, 6),
                    "size": round(initial_size, 6),
                    "size_left": round(position.size, 6),
                    "pnl": round(pnl + position.realised_pnl, 2),
                    "pnl_runner": round(pnl, 2),
                    "pnl_banked": round(position.realised_pnl, 2),
                    "fees": round(fee, 2),
                    "partials": len(position.meta.get("partials", [])),
                    "r_multiple": round((pnl + position.realised_pnl) / (rpu * initial_size), 3)
                    if initial_size
                    else 0.0,
                    "reason": "end of data",
                    "entry_slippage_bps": position.meta.get("slippage_bps"),
                    "exit_slippage_bps": round(exit_slip_bps, 3),
                    "entry_reason": " | ".join(position.meta.get("reasons", [])),
                    "opened_at": position.opened_at,
                    "closed_at": str(last["time"]),
                    "bars_held": position.bars_held,
                    "rr_planned": round(abs(position.target - position.entry) / rpu, 2),
                }
            )
            result.equity_curve.append((str(last["time"]), equity))

        result.ending_equity = equity
        result.cost_report = self._cost_report()
        return result


# --------------------------------------------------------------------------- walk-forward
def walk_forward(
    *,
    market: str,
    symbol: str,
    bars: Sequence[dict[str, Any]],
    settings: Settings | None = None,
    oos_fraction: float = 0.3,
    param_grid: Iterable[dict[str, Any]] | None = None,
    htf_factor: int | None = None,
) -> dict[str, Any]:
    """Pick parameters in-sample, then report the untouched out-of-sample slice.

    ``param_grid`` yields overrides like ``{"signal": {"min_score": 5},
    "risk": {"rr_target": 2.0}}``. The best in-sample set is chosen by expectancy R with
    a minimum trade count, then re-run once on the OOS slice.
    """
    settings = settings or Settings.load()
    split = int(len(bars) * (1.0 - oos_fraction))
    split_time = str(bars[split]["time"])
    grid = list(param_grid or [
        {"signal": {"min_score": 5}},
        {"signal": {"min_score": 6}},
        {"signal": {"min_score": 7}},
        {"signal": {"min_score": 5}, "risk": {"rr_target": 1.5}},
        {"signal": {"min_score": 5}, "risk": {"rr_target": 3.0}},
    ])

    rows = []
    best = None
    for params in grid:
        bt = Backtester(
            market=market,
            settings=settings,
            signal_override=params.get("signal"),
            risk_override=params.get("risk"),
        )
        res = bt.run(symbol, bars, htf_factor=htf_factor, collect_signals=False,
                     window=(None, split_time))
        metrics = res.metrics()
        rows.append({"params": params, "metrics": metrics})
        score = metrics["expectancy_r"] if metrics["trades"] >= 5 else -999.0
        if best is None or score > best[0]:
            best = (score, params, res)

    chosen = best[1] if best else {}
    bt_oos = Backtester(
        market=market,
        settings=settings,
        signal_override=chosen.get("signal"),
        risk_override=chosen.get("risk"),
    )
    res_oos = bt_oos.run(symbol, bars, htf_factor=htf_factor, collect_signals=False,
                         window=(split_time, None))
    return {
        "symbol": symbol,
        "market": market,
        "split_index": split,
        "split_time": split_time,
        "oos_start": res_oos.start,
        "oos_note": (
            "the out-of-sample run starts at the first bar after the split where the "
            "indicators of the trading timeframe are warm (they warm up on the full series)"
        ),
        "chosen_params": chosen,
        "in_sample": best[2].to_dict() if best else None,
        "out_of_sample": res_oos.to_dict(),
        # the execution model is a property of the run, not of the slice: surface it at
        # the top level too so a caller never has to dig into the OOS block for it
        "cost_model": bt_oos.cost_cfg.to_dict(),
        "grid": rows,
        "note": (
            "The out-of-sample slice was never used for selection; indicators are warmed up "
            "on the full history so the slice is not handicapped, but no entry is taken "
            "before the split. A positive in-sample and a negative OOS result means the "
            "parameters are curve-fitted."
        ),
    }


# --------------------------------------------------------------------------- CLI
def load_bars(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        for key in ("candles", "bars", "data", "ohlcv"):
            if key in payload:
                payload = payload[key]
                break
        else:
            if "quote" in payload and "candles" not in payload:
                raise ValueError("file holds a quote, not candles")
    bars = []
    for row in payload:
        if isinstance(row, (list, tuple)):
            bars.append({"time": row[0], "open": row[1], "high": row[2], "low": row[3],
                         "close": row[4], "volume": row[5] if len(row) > 5 else 0})
        elif isinstance(row, dict):
            bars.append(row)
    return bars


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest the Trading Office pipeline on closed candles.")
    parser.add_argument("--market", choices=["crypto", "idx"], required=True)
    parser.add_argument("--data", required=True, help="JSON file with candles (idx shape or ccxt rows)")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--oos", type=float, default=0.0, help="out-of-sample fraction, e.g. 0.3")
    parser.add_argument("--htf-factor", type=int, default=None, help="LTF bars per HTF bar")
    parser.add_argument("--out", default="", help="write the report JSON here")
    args = parser.parse_args(argv)

    bars = load_bars(args.data)
    symbol = args.symbol or Path(args.data).stem
    settings = Settings.load()
    if args.oos:
        report = walk_forward(
            market=args.market, symbol=symbol, bars=bars, settings=settings,
            oos_fraction=args.oos, htf_factor=args.htf_factor,
        )
    else:
        bt = Backtester(market=args.market, settings=settings)
        report = bt.run(symbol, bars, htf_factor=args.htf_factor).to_dict()

    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    metrics = report.get("metrics") or (report.get("out_of_sample") or {}).get("metrics") or {}
    if metrics:
        print(
            f"\n{symbol}: {metrics['trades']} trades · win {metrics['win_rate_pct']}% · "
            f"expectancy {metrics['expectancy_r']}R · PF {metrics['profit_factor']} · "
            f"maxDD {metrics['max_drawdown_pct']}%",
            flush=True,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
