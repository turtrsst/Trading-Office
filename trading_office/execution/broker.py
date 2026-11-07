"""Gate 4b — Order Execution.

Two implementations behind one tiny interface (:class:`Broker`):

* :class:`PaperBroker` — the default. Simulates fills with the spread, slippage and
  commissions of the market profile, keeps margin for crypto, and produces the same
  :class:`Fill` record a real venue returns, so the engine, the ledger and the UI do not
  care which one is wired in.
* :class:`CcxtBroker` — real crypto perpetual futures via ``ccxt`` with exchange-side
  brackets (``stopLoss`` / ``takeProfit`` trigger prices). Testnet by default; it refuses
  to start with live keys unless ``execution.paper`` is explicitly ``false`` and
  ``execution.dry_run`` is ``false``.

Idempotency: every order carries ``client_id = to-{symbol}-{bar_time}-{side}``. The
broker keeps the set of seen ids, so a retry after a network error can never double-fill.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence

from ..config import idx_tick_size, round_to_tick
from ..risk import TradePlan


# --------------------------------------------------------------------------- records
@dataclass
class Order:
    symbol: str
    side: str  # "buy" | "sell"
    quantity: float
    price_hint: float
    stop: float | None = None
    target: float | None = None
    client_id: str = ""
    market: str = "crypto"
    order_type: str = "market"
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Fill:
    ok: bool
    order_id: str
    client_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClosedTrade:
    symbol: str
    market: str
    side: str
    entry: float
    exit: float
    size: float
    pnl: float
    fees: float
    r_multiple: float
    reason: str
    opened_at: str
    closed_at: str
    bar_time: str = ""
    reasons_entry: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Broker(Protocol):
    def submit(self, order: Order) -> Fill: ...
    def close(self, symbol: str, price: float, reason: str) -> ClosedTrade | None: ...
    def equity(self, marks: dict[str, float] | None = None) -> float: ...
    def close_symbol(self, symbol: str) -> dict[str, Any]: ...
    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...
    def cancel_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- paper
class PaperBroker:
    name = "paper"
    """Deterministic paper broker used by the UI, the CLI and the backtester."""

    def __init__(
        self,
        *,
        market: str,
        equity: float,
        profile: dict[str, Any],
        spread_bps: float | None = None,
        leverage: float = 1.0,
    ) -> None:
        self.market = market
        self.starting_equity = float(equity)
        self.cash = float(equity)
        self.profile = profile
        self.spread_bps = float(spread_bps if spread_bps is not None else profile.get("max_spread_bps", 5.0) / 3)
        self.leverage = float(leverage or profile.get("max_leverage", 1) or 1)
        self.positions: dict[str, dict[str, Any]] = {}
        self.closed: list[ClosedTrade] = []
        self.seen_ids: set[str] = set()
        self.orders: list[Fill] = []
        self.locked_margin = 0.0

    # ------------------------------------------------------------------ helpers
    @property
    def contract_size(self) -> float:
        """Units per lot: 1 for spot/perp, 100 oz for XAUUSD, 100 shares for IDX."""
        if self.market == "fx":
            return float(self.profile.get("contract_size", 100.0) or 100.0)
        return 1.0

    def _tick(self, price: float) -> float | None:
        if self.market == "idx" and self.profile.get("tick_table", True):
            return idx_tick_size(price)
        if self.market == "fx":
            return float(self.profile.get("price_step", 0.01) or 0.01)
        return None

    def _slipped_price(self, side: str, price: float) -> float:
        slip = float(self.profile.get("slippage_bps", 2.0)) / 10_000.0
        half_spread = self.spread_bps / 2.0 / 10_000.0
        raw = price * (1 + (half_spread + slip)) if side == "buy" else price * (1 - (half_spread + slip))
        tick = self._tick(raw)
        if tick:
            raw = round_to_tick(raw, "up" if side == "buy" else "down", tick)
        return round(raw, 8)

    def _commission(self, side: str, notional: float) -> float:
        if self.market == "idx":
            pct = float(self.profile.get("commission_buy_pct" if side == "buy" else "commission_sell_pct", 0.15))
            return notional * pct / 100.0
        return notional * float(self.profile.get("commission_bps", 5.5)) / 10_000.0

    # ------------------------------------------------------------------ interface
    def submit(self, order: Order) -> Fill:
        if order.client_id and order.client_id in self.seen_ids:
            return Fill(
                False,
                "",
                order.client_id,
                order.symbol,
                order.side,
                0.0,
                order.price_hint,
                0.0,
                "duplicate client_id refused",
                at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        if order.symbol in self.positions:
            return Fill(
                False, "", order.client_id, order.symbol, order.side, 0.0, order.price_hint, 0.0,
                "position already open for this symbol",
                at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        qty = float(order.quantity)
        if qty <= 0:
            return Fill(False, "", order.client_id, order.symbol, order.side, 0.0, order.price_hint, 0.0,
                        "zero quantity", at=dt.datetime.now(dt.timezone.utc).isoformat())

        if self.market == "fx":
            # a lot is not a unit: 0.05 lot of gold is $13,250 of notional, not $132.50
            step = float(self.profile.get("volume_step", 0.01) or 0.01)
            volume_min = float(self.profile.get("volume_min", step) or step)
            if qty + 1e-9 < volume_min:
                return Fill(False, "", order.client_id, order.symbol, order.side, 0.0, 0.0, 0.0,
                            f"{qty:g} lots is below the venue minimum {volume_min:g}",
                            at=dt.datetime.now(dt.timezone.utc).isoformat())
            qty = round(math.floor(qty / step + 1e-9) * step, 8)
            if qty <= 0:
                return Fill(False, "", order.client_id, order.symbol, order.side, 0.0, 0.0, 0.0,
                            "volume rounds to zero at the venue step",
                            at=dt.datetime.now(dt.timezone.utc).isoformat())

        fill_price = self._slipped_price(order.side, order.price_hint)
        notional = fill_price * qty * self.contract_size
        fee = self._commission(order.side, notional)
        margin = notional / self.leverage if self.market != "idx" else notional
        if margin + fee > self.cash:
            return Fill(False, "", order.client_id, order.symbol, order.side, 0.0, fill_price, 0.0,
                        f"insufficient cash: need {margin + fee:,.2f}, have {self.cash:,.2f}",
                        at=dt.datetime.now(dt.timezone.utc).isoformat())

        self.cash -= margin + fee
        self.locked_margin += margin
        self.seen_ids.add(order.client_id or "")
        self.positions[order.symbol] = {
            "symbol": order.symbol,
            "side": "long" if order.side == "buy" else "short",
            "entry": fill_price,
            "size": qty,
            "stop": order.stop,
            "target": order.target,
            "margin": margin,
            "fees": fee,
            "order": order.to_dict(),
        }
        fill = Fill(
            True,
            f"paper-{uuid.uuid4().hex[:10]}",
            order.client_id,
            order.symbol,
            order.side,
            qty,
            fill_price,
            fee,
            "filled (paper)",
            raw={"margin": margin, "notional": notional, "stop": order.stop, "target": order.target},
            at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        self.orders.append(fill)
        return fill

    def modify_stop(self, symbol: str, new_stop: float) -> bool:
        pos = self.positions.get(symbol)
        if not pos:
            return False
        pos["stop"] = float(new_stop)
        return True

    def open_orders(self) -> list[dict[str, Any]]:
        """Resting brackets. The paper broker fills instantly, so it has none to show."""
        return []

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"ok": False, "reason": "the paper broker has no resting orders"}

    def partial_close(
        self, symbol: str, size: float, price: float, *, reason: str = "partial take profit"
    ) -> dict[str, Any] | None:
        """Reduce a position by ``size`` at ``price``, releasing margin proportionally.

        Returns ``{size, price, pnl, fee, remaining}`` or ``None`` when the symbol has no
        position / the slice is not smaller than the position.
        """
        pos = self.positions.get(symbol)
        if not pos:
            return None
        size = float(size)
        if size <= 0 or size >= float(pos["size"]) - 1e-12:
            return None
        side = "sell" if pos["side"] == "long" else "buy"
        fill = self._slipped_price(side, price)
        notional = fill * size
        fee = self._commission(side, notional)
        direction = 1.0 if pos["side"] == "long" else -1.0
        gross = (fill - pos["entry"]) * size * direction
        share = size / float(pos["size"])
        margin_release = float(pos.get("margin", 0.0)) * share
        pos["size"] = float(pos["size"]) - size
        pos["margin"] = float(pos.get("margin", 0.0)) - margin_release
        pos["fees"] = float(pos.get("fees", 0.0)) + fee
        pos.setdefault("partials", []).append(
            {"size": size, "price": fill, "pnl": gross - fee, "at": dt.datetime.now(dt.timezone.utc).isoformat()}
        )
        self.cash += margin_release + gross - fee
        self.locked_margin = max(0.0, self.locked_margin - margin_release)
        return {
            "size": size,
            "price": fill,
            "pnl": round(gross - fee, 6),
            "fee": round(fee, 6),
            "remaining": round(pos["size"], 12),
            "reason": reason,
        }

    def close(self, symbol: str, price: float, reason: str, *, fees_override: float | None = None) -> ClosedTrade | None:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return None
        side = "sell" if pos["side"] == "long" else "buy"
        exit_price = self._slipped_price(side, price)
        notional = exit_price * pos["size"]
        fee = self._commission(side, notional) if fees_override is None else float(fees_override)
        direction = 1.0 if pos["side"] == "long" else -1.0
        gross = (exit_price - pos["entry"]) * pos["size"] * direction
        total_fees = fee + float(pos.get("fees", 0.0))
        pnl = gross - total_fees
        margin = float(pos.get("margin", 0.0))
        if self.market == "idx":
            # sell proceeds come back to cash
            self.cash += notional - fee
            self.cash -= 0.0
        else:
            self.cash += margin + pnl
            self.locked_margin = max(0.0, self.locked_margin - margin)

        risk_per_unit = 0.0
        try:
            risk_per_unit = abs(pos["entry"] - float(pos["stop"] or pos["entry"]))
        except (TypeError, ValueError):
            risk_per_unit = 0.0
        if not risk_per_unit:
            initial = pos.get("order", {}).get("meta", {}).get("initial_risk")
            risk_per_unit = float(initial or 0.0)
        r_mult = (pnl / (risk_per_unit * pos["size"])) if risk_per_unit and pos["size"] else 0.0

        banked = sum(float(p.get("pnl", 0.0)) for p in pos.get("partials", []))
        trade = ClosedTrade(
            symbol=symbol,
            market=self.market,
            side=pos["side"],
            entry=pos["entry"],
            exit=exit_price,
            size=pos["size"],
            pnl=round(pnl + banked, 4),
            fees=round(total_fees, 4),
            r_multiple=round(r_mult, 4),
            reason=reason,
            opened_at=str(pos["order"].get("meta", {}).get("opened_at", "")),
            closed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            bar_time=str(pos["order"].get("meta", {}).get("bar_time", "")),
            reasons_entry=list(pos["order"].get("meta", {}).get("reasons", [])),
            meta=pos["order"].get("meta", {}),
        )
        self.closed.append(trade)
        return trade

    def equity(self, marks: dict[str, float] | None = None) -> float:
        total = self.cash + self.locked_margin
        marks = marks or {}
        for symbol, pos in self.positions.items():
            mark = marks.get(symbol, pos["entry"])
            direction = 1.0 if pos["side"] == "long" else -1.0
            total += (mark - pos["entry"]) * pos["size"] * direction
        return total

    def unrealised(self, marks: dict[str, float] | None = None) -> dict[str, float]:
        marks = marks or {}
        out = {}
        for symbol, pos in self.positions.items():
            mark = marks.get(symbol, pos["entry"])
            direction = 1.0 if pos["side"] == "long" else -1.0
            out[symbol] = (mark - pos["entry"]) * pos["size"] * direction
        return out

    def snapshot(self, marks: dict[str, float] | None = None) -> dict[str, Any]:
        return {
            "market": self.market,
            "starting_equity": self.starting_equity,
            "cash": round(self.cash, 2),
            "locked_margin": round(self.locked_margin, 2),
            "equity": round(self.equity(marks), 2),
            "positions": list(self.positions.values()),
            "closed_count": len(self.closed),
            "realised_pnl": round(sum(t.pnl for t in self.closed), 2),
        }


# --------------------------------------------------------------------------- ccxt
class CcxtBroker:
    name = "ccxt"
    """Live/testnet perpetual futures through ``ccxt`` (bybit, binance, bitget, okx)."""

    def __init__(
        self,
        *,
        exchange_id: str,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        leverage: int = 3,
        market: str = "crypto",
        profile: dict[str, Any] | None = None,
        symbols: Sequence[str] | None = None,
    ) -> None:
        try:
            import ccxt  # noqa: F401  (imported lazily: optional dependency)
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError("ccxt is not installed: pip install ccxt") from exc
        self.ccxt = ccxt
        self.exchange_id = exchange_id
        self.testnet = testnet
        self.market = market
        self.profile = profile or {}
        self.leverage = leverage
        cls = getattr(ccxt, exchange_id)
        self.exchange = cls(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            }
        )
        if testnet:
            self.exchange.set_sandbox_mode(True)
        self.exchange.load_markets()
        for symbol in symbols or []:
            try:
                self.exchange.set_leverage(leverage, symbol)
            except Exception:  # pragma: no cover - venue-specific
                pass
        self.seen_ids: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def _amount(self, symbol: str, quantity: float) -> float:
        return float(self.exchange.amount_to_precision(symbol, quantity))

    def _price(self, symbol: str, price: float) -> float:
        return float(self.exchange.price_to_precision(symbol, price))

    # ------------------------------------------------------------------ interface
    def submit(self, order: Order) -> Fill:
        if order.client_id in self.seen_ids:
            return Fill(False, "", order.client_id, order.symbol, order.side, 0.0, order.price_hint, 0.0,
                        "duplicate client_id refused")
        params: dict[str, Any] = {}
        if order.stop:
            params["stopLoss"] = {"triggerPrice": self._price(order.symbol, order.stop)}
        if order.target:
            params["takeProfit"] = {"triggerPrice": self._price(order.symbol, order.target)}
        self.seen_ids.add(order.client_id)
        try:
            raw = self.exchange.create_order(
                order.symbol, order.order_type, order.side, self._amount(order.symbol, order.quantity),
                None, params,
            )
            price = float(raw.get("average") or raw.get("price") or order.price_hint)
            fee = float((raw.get("fee") or {}).get("cost") or 0.0)
            return Fill(True, str(raw.get("id", "")), order.client_id, order.symbol, order.side,
                        float(raw.get("amount") or order.quantity), price, fee, "live order accepted", raw=raw)
        except Exception as exc:  # pragma: no cover - network/venue
            return Fill(False, "", order.client_id, order.symbol, order.side, 0.0, order.price_hint, 0.0,
                        f"exchange rejected: {exc}")

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Normalised resting orders (client cards render these, not raw ccxt dicts)."""
        try:  # pragma: no cover - live path
            raw = (
                self.exchange.fetch_open_orders(symbol) if symbol
                else self.exchange.fetch_open_orders()
            )
        except Exception:
            return []
        out = []
        for order in raw or []:
            trigger = (order.get("triggerPrice") or order.get("stopPrice")
                       or (order.get("info") or {}).get("triggerPrice"))
            out.append({
                "id": str(order.get("id") or ""),
                "symbol": order.get("symbol"),
                "type": str(order.get("type") or "order"),
                "side": str(order.get("side") or ""),
                "amount": float(order.get("amount") or 0.0),
                "price": float(order.get("price") or trigger or 0.0),
                "reduce_only": bool(order.get("reduceOnly") or (order.get("info") or {}).get("reduceOnly")),
                "status": order.get("status") or "open",
                "created_at": order.get("datetime"),
            })
        return out

    def cancel_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        """Cancel one resting order. Used by the UI's CANCEL button (and stop replacements)."""
        try:  # pragma: no cover - live path
            if symbol:
                self.exchange.cancel_order(order_id, symbol)
            else:
                self.exchange.cancel_order(order_id)
            return {"ok": True, "order_id": order_id}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "order_id": order_id, "reason": str(exc)}

    def close_symbol(self, symbol: str) -> dict[str, Any]:
        """Market-close one position, reduce-only, with its resting brackets cleaned up.

        There is no "close this symbol" in the engine's own loop, so this lives next to the
        venue: the amount is the venue's own position size (never a guess), the side is the
        opposite one, and the order is reduce-only so it cannot open exposure if the
        position vanished between the read and the send.
        """
        live = [p for p in self.positions() if p.get("symbol") == symbol]
        if not live:
            return {"ok": False, "reason": f"no open position for {symbol}"}
        pos = live[0]
        contracts = float(pos.get("contracts") or 0.0)
        if contracts <= 0:
            return {"ok": False, "reason": "venue reports a zero-size position"}
        side = "sell" if pos.get("side") == "long" else "buy"
        try:  # pragma: no cover - live path
            raw = self.exchange.create_order(
                symbol, "market", side, self._amount(symbol, contracts), None,
                {"reduceOnly": True},
            )
            fill = float(raw.get("average") or raw.get("price") or 0.0)
            entry = float(pos.get("entryPrice") or 0.0)
            direction = 1.0 if pos.get("side") == "long" else -1.0
            fee = float((raw.get("fee") or {}).get("cost") or 0.0)
            cancelled = []
            for order in self.exchange.fetch_open_orders(symbol) or []:
                try:
                    self.exchange.cancel_order(order.get("id"), symbol)
                    cancelled.append(str(order.get("id")))
                except Exception:
                    continue
            return {
                "ok": True,
                "symbol": symbol,
                "side": side,
                "size": contracts,
                "price": fill,
                "pnl": round((fill - entry) * contracts * direction - fee, 6) if fill else 0.0,
                "orders_cancelled": cancelled,
            }
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "reason": str(exc)}

    def amount_step(self, symbol: str) -> float | None:
        """Contract step (precision) for partial sizes."""
        try:
            market = self.exchange.market(symbol)
            precision = (market.get("precision") or {}).get("amount")
            if precision is None:
                return None
            if isinstance(precision, int):
                return 10 ** (-precision)
            return float(precision)
        except Exception:  # pragma: no cover - venue specific
            return None

    def modify_stop(self, symbol: str, new_stop: float) -> dict[str, Any]:
        """Move the stop of the *whole* position by replacing its stop order.

        Exchanges differ: Bybit accepts ``trading-stop`` edits, Binance wants the algo
        order cancelled and re-created. We try the edit first and fall back to cancel +
        re-create, always returning a dict so the engine can ledger what happened.
        """
        try:
            orders = self.exchange.fetch_open_orders(symbol)
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "reason": f"cannot list open orders: {exc}"}
        stops = [
            o for o in orders
            if (o.get("type") in ("stop", "stop_market", "conditional", "trading_stop"))
            or (o.get("info") or {}).get("stopOrderType")
        ]
        if not stops:
            return {"ok": False, "reason": "no resting stop order to move"}
        stop = stops[0]
        try:
            self.exchange.edit_order(stop["id"], symbol, stop.get("type", "stop"), stop.get("side"),
                                     stop.get("amount"), None,
                                     {"triggerPrice": self._price(symbol, new_stop)})
            return {"ok": True, "method": "edit", "order_id": stop.get("id"), "stop": new_stop}
        except Exception as edit_error:  # pragma: no cover - venue specific
            try:
                self.exchange.cancel_order(stop["id"], symbol)
                raw = self.exchange.create_order(
                    symbol, stop.get("type", "stop"), stop.get("side"), stop.get("amount"), None,
                    {"triggerPrice": self._price(symbol, new_stop), "reduceOnly": True},
                )
                return {"ok": True, "method": "recreate", "order_id": raw.get("id"), "stop": new_stop}
            except Exception as exc:
                return {"ok": False, "reason": f"edit failed ({edit_error}); recreate failed ({exc})"}

    def partial_close(self, symbol: str, size: float, price: float, *, reason: str = "") -> dict[str, Any] | None:
        """Reduce-only market order for a slice; the venue's amount precision wins."""
        live = [p for p in self.positions() if p.get("symbol") == symbol]
        if not live:
            return None
        pos = live[0]
        contracts = float(pos.get("contracts") or 0.0)
        step = self.amount_step(symbol)
        qty = min(float(size), contracts)
        if step:
            qty = math.floor(qty / step) * step
        if qty <= 0 or qty >= contracts:
            return None
        side = "sell" if pos.get("side") == "long" else "buy"
        try:  # pragma: no cover - live path
            raw = self.exchange.create_order(
                symbol, "market", side, self._amount(symbol, qty), None, {"reduceOnly": True}
            )
            fill = float(raw.get("average") or raw.get("price") or price)
            entry = float(pos.get("entryPrice") or 0.0)
            direction = 1.0 if pos.get("side") == "long" else -1.0
            fee = float((raw.get("fee") or {}).get("cost") or 0.0)
            return {
                "size": qty,
                "price": fill,
                "pnl": round((fill - entry) * qty * direction - fee, 6),
                "fee": fee,
                "remaining": round(contracts - qty, 12),
                "reason": reason,
            }
        except Exception:  # pragma: no cover
            return None

    def positions(self) -> list[dict[str, Any]]:
        try:
            return [p for p in self.exchange.fetch_positions() if float(p.get("contracts") or 0) > 0]
        except Exception:  # pragma: no cover
            return []

    def close(self, symbol: str, price: float, reason: str) -> ClosedTrade | None:  # pragma: no cover - live
        live = [p for p in self.positions() if p.get("symbol") == symbol]
        if not live:
            return None
        pos = live[0]
        side = "sell" if pos.get("side") == "long" else "buy"
        qty = float(pos.get("contracts") or 0)
        try:
            raw = self.exchange.create_order(symbol, "market", side, self._amount(symbol, qty), None,
                                             {"reduceOnly": True})
        except Exception:
            return None
        exit_price = float(raw.get("average") or raw.get("price") or price)
        entry = float(pos.get("entryPrice") or 0.0)
        direction = 1.0 if pos.get("side") == "long" else -1.0
        pnl = (exit_price - entry) * qty * direction
        return ClosedTrade(
            symbol=symbol, market=self.market, side=str(pos.get("side", "")), entry=entry,
            exit=exit_price, size=qty, pnl=pnl, fees=0.0, r_multiple=0.0, reason=reason,
            opened_at=str(pos.get("timestamp", "")), closed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )

    def equity(self, marks: dict[str, float] | None = None) -> float:  # pragma: no cover - live
        try:
            balance = self.exchange.fetch_balance()
            total = balance.get("total", {}).get("USDT")
            if total is None:
                total = balance.get("USDT", {}).get("total", 0.0)
            return float(total or 0.0)
        except Exception:
            return 0.0


def make_broker(config: dict[str, Any], market: str, equity: float, profile: dict[str, Any]) -> Broker:
    """Build the broker described by the config (paper unless explicitly live)."""
    exec_cfg = config.get("execution", {})
    live = not bool(exec_cfg.get("paper", True)) and not bool(exec_cfg.get("dry_run", True))
    if market == "crypto" and live:
        # resolved through trading_office.env so aliases (CCXT_API_KEY, …) work and the
        # lookup happens in exactly one place
        from ..env import credential

        api_key = credential(exec_cfg.get("api_key_env", "TO_API_KEY"))
        api_secret = credential(exec_cfg.get("api_secret_env", "TO_API_SECRET"))
        if not api_key or not api_secret:
            raise RuntimeError(
                "live trading requested but TO_API_KEY / TO_API_SECRET are not set; "
                "keeping the paper broker instead"
            )
        return CcxtBroker(
            exchange_id=str(exec_cfg.get("exchange_id", "bybit")),
            api_key=api_key,
            api_secret=api_secret,
            testnet=bool(exec_cfg.get("testnet", True)),
            leverage=int(profile.get("leverage", 3)),
            market=market,
            profile=profile,
        )
    if market == "fx" and live:
        # Imported here so the paper/synthetic paths never require the cryptography stack.
        # No fallback: a missing key raises, exactly like the ccxt branch above.
        from .exness import from_settings as exness_from_settings

        return exness_from_settings(config)
    return PaperBroker(
        market=market,
        equity=equity,
        profile=profile,
        leverage=float(profile.get("leverage", profile.get("max_leverage", 1) or 1)),
    )


def order_from_plan(plan: TradePlan, bar_time: str, reasons: Sequence[str], opened_at: str | None = None) -> Order:
    """Translate a :class:`TradePlan` into an order with a deterministic client id."""
    side = "buy" if plan.side == "long" else "sell"
    compact = bar_time.replace(":", "").replace("-", "").replace(" ", "T")[:20]
    return Order(
        symbol=plan.symbol,
        side=side,
        quantity=plan.size,
        price_hint=plan.entry,
        stop=plan.stop,
        target=plan.target,
        client_id=f"to-{plan.symbol.replace('/', '').replace(':','')}-{compact}-{side}",
        market=plan.market,
        meta={
            "reasons": list(reasons),
            "bar_time": bar_time,
            "opened_at": opened_at or dt.datetime.now(dt.timezone.utc).isoformat(),
            "initial_risk": plan.stop_distance,
            "rr": plan.rr,
            "risk_cash": plan.risk_cash,
        },
    )
