"""Gotrade (US stocks) over its documented REST API.

What the documentation actually says, and what that forces here
--------------------------------------------------------------
* **Long-only.** The order schema allows ``side: BUY|SELL`` and marks stocks with
  ``shortable: false``: a SELL reduces a holding, it does not open a short. This adapter
  therefore refuses a sell with no position instead of silently inverting the signal.
* **A terminal, not a stream.** ``POST /stock-orders/create`` returns ``201`` with status
  ``NEW``; the fill arrives later as ``FILLED`` / ``PARTIAL_FILL``. A ``NEW`` order is not a
  position, so ``submit()`` polls to a terminal state and reports "not filled" on timeout —
  the same rule the Exness adapter follows.
* **TP/SL are order fields, not a bracket object.** ``take_profit`` / ``stop_loss`` ride with
  the entry order, which is the only reason a stop survives the bot dying. They are rejected
  during extended hours, so the adapter refuses to arm them there rather than accepting a
  position with no protection.
* **Two cash numbers, not one.** ``cash_balance`` includes unsettled proceeds;
  ``cash_available_for_trade`` is what can actually be spent. Sizing on the first one is how a
  bot gets ``400 insufficient buying power`` after a same-day exit.
* **The fee is the strategy's main constraint.** 0.3 % per side (minimum $0.10) is ~60 bps
  round trip. On a $112 account the $0.10 floor alone is ~9 % of a 1 % risk budget, which is
  why ``shares_for_risk`` reports the fee as a share of risk instead of hiding it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

#: documented base URLs. The mobile app ships as ``io.tr8.tradecharlie.android`` and talks to
#: the "charlie" (Global) entity; the Indonesia entity has its own host.
BASE_URLS = {
    "global": "https://api.charlie.gotrade.admin.tr8.tech/api/b2c/v1",
    "indo": "https://api.indo.gotrade.prod-indo.tr8.tech/api/b2c/v1",
}

#: order states that will not change on their own
TERMINAL = {"FILLED", "REJECTED", "CANCELED"}
#: stated in the docs: whole shares only
WHOLE_SHARE_ONLY = {"stop", "stop_limit", "trailing_stop"}

FEE_PCT = 0.003          # 0.30 % per side
FEE_MIN = 0.10           # USD per side

#: HTTP code → what the operator should do. The body only carries a message, so the status is
#: the contract.
STATUS_HINTS = {
    401: "token missing/expired, or the account is not enabled for API access "
         "(request enablement from your Gotrade representative, then regenerate at Profile > API Key)",
    403: "buying is currently unavailable for this account (KYC/ID validity, restriction)",
    404: "order, stock or ticker not found",
    400: "rejected by a business rule: invalid parameters, TP/SL during extended hours, or "
         "insufficient buying power — check cash_available_for_trade, not cash_balance",
    502: "upstream broker (Alpaca) failed during cancel/status; retrying is safe, it is idempotent",
}


def fee_for(notional: float) -> float:
    """Per-side commission: 0.3 % with a $0.10 floor."""
    return round(max(FEE_MIN, abs(float(notional)) * FEE_PCT), 2)


def round_trip_fee(notional: float) -> float:
    return round(fee_for(notional) * 2, 2)


def shares_for_risk(*, equity: float, risk_pct: float, entry: float, stop: float,
                    cash_available: float, fractional: bool = True,
                    max_position_pct: float = 25.0) -> dict[str, Any]:
    """Risk budget → shares, bounded by what can actually be bought.

    Three limits apply and the smallest wins: the risk budget, the cash that is settled, and a
    per-position share of equity. The fee is *not* treated as free: it is reported as a share of
    the risk budget, because on a small account it is often the largest single number.
    """
    distance = abs(float(entry) - float(stop))
    if distance <= 0:
        return {"ok": False, "reason": "stop distance is zero"}
    risk_cash = abs(float(equity)) * abs(float(risk_pct)) / 100.0
    by_risk = risk_cash / distance
    by_cash = max(0.0, float(cash_available)) / float(entry) if entry else 0.0
    by_position = abs(float(equity)) * float(max_position_pct) / 100.0 / float(entry) if entry else 0.0
    raw = min(by_risk, by_cash, by_position)
    shares = round(raw, 6) if fractional else float(int(raw))
    if shares <= 0:
        return {"ok": False, "reason": (f"no size fits: risk allows {by_risk:.4f} shares, "
                                        f"settled cash {by_cash:.4f}, position cap "
                                        f"{by_position:.4f}"),
                "by_risk": by_risk, "by_cash": by_cash}
    notional = shares * float(entry)
    fee = round_trip_fee(notional)
    return {
        "ok": True,
        "shares": shares,
        "notional": round(notional, 2),
        "risk_cash": round(shares * distance, 2),
        "fee_round_trip": fee,
        "fee_pct_of_risk": round(fee / max(shares * distance, 1e-9) * 100, 2),
        "binding": ("risk" if raw == by_risk and by_risk <= min(by_cash, by_position)
                    else "cash" if by_cash <= by_position else "position_cap"),
    }


@dataclass
class GotradeClient:
    """Bearer-token REST client. One URL per call, no hidden retries on mutations."""

    token: str
    base_url: str = BASE_URLS["global"]
    transport: Callable[..., tuple[int, dict[str, Any]]] | None = None
    timeout: float = 15.0
    audit: list[dict[str, Any]] = field(default_factory=list)

    def _send(self, method: str, path: str, payload: dict[str, Any] | None = None,
              allow: tuple[int, ...] = (200, 201)) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        send = self.transport or _default_transport
        status, data = send(method, self.base_url.rstrip("/") + path, headers, body, self.timeout)
        self.audit.append({"method": method, "path": path, "status": status, "at": time.time()})
        del self.audit[:-100]
        if status not in allow:
            raise GotradeError(status, str(data.get("message") or ""))
        return data.get("data") if isinstance(data, dict) else data

    # ------------------------------------------------------------------ reads
    def account_balance(self) -> dict[str, Any]:
        return self._send("GET", "/portfolio/account-balance") or {}

    def open_positions(self, extended_hours: bool = False) -> list[dict[str, Any]]:
        flag = "true" if extended_hours else "false"
        return list(self._send("GET", f"/portfolio/open-positions?is_extended_hours={flag}") or [])

    def open_orders(self) -> list[dict[str, Any]]:
        return list(self._send("GET", "/stock-orders") or [])

    def order(self, order_id: str) -> dict[str, Any]:
        return self._send("GET", f"/stock-orders/{order_id}") or {}

    # ------------------------------------------------------------------ writes
    def create_order(self, **payload: Any) -> dict[str, Any]:
        return self._send("POST", "/stock-orders/create", payload, allow=(201,))

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._send("DELETE", f"/stock-orders/{order_id}")

    def wait_for_terminal(self, order_id: str, *, timeout_s: float = 20.0,
                          poll_s: float = 1.0) -> dict[str, Any]:
        """Poll until the order stops moving. ``NEW`` is not a fill."""
        deadline = time.time() + max(0.0, timeout_s)
        last: dict[str, Any] = {}
        while time.time() < deadline:
            payload = self.order(order_id)
            # the documented envelope is {data: {stock: {...}, order: {...}}}: the status lives
            # inside `order`, so reading the top level would silently never see a fill
            last = (payload or {}).get("order") or payload or {}
            state = str(last.get("status") or "").upper()
            if state in TERMINAL:
                return {"state": state, **last}       # documented values are UPPERCASE
            time.sleep(max(0.0, poll_s))
        return {"state": "timeout" if last else "unknown", **last}


def _default_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                       timeout: float) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, data=body, method=method,
        headers={**headers, **({"Content-Type": "application/json"} if body else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001
            return exc.code, {"message": f"HTTP {exc.code}"}


class GotradeError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        self.hint = STATUS_HINTS.get(status, "")
        super().__init__(f"HTTP {status}: {message}" + (f" — {self.hint}" if self.hint else ""))


# --------------------------------------------------------------------------- broker
@dataclass
class GotradeBroker:
    """The ``Broker`` the engine drives: long-only US shares with a mandatory bracket.

    Paper and live differ in one place only — the client. Everything the engine relies on
    (refuse a stopless entry, refuse a short, refuse an unconfirmed fill) is enforced here, so
    the paper book rehearses the same rules the live account will impose.
    """

    client: Any
    equity_value: float = 0.0
    market: str = "us"
    name: str = "gotrade"
    fractional: bool = True
    allow_extended_hours: bool = False
    fill_timeout_s: float = 20.0
    seen_ids: set[str] = field(default_factory=set)
    balance: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ state
    def refresh(self) -> dict[str, Any]:
        self.balance = self.client.account_balance() or {}
        return self.balance

    def cash(self) -> dict[str, float]:
        """Both numbers, because they mean different things.

        ``available`` is the only one that can be spent; ``balance`` still includes proceeds
        from a sale that has not settled.
        """
        data = self.balance or self.refresh()
        def _f(key: str) -> float:
            try:
                return float(data.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return {"balance": _f("cash_balance"), "available": _f("cash_available_for_trade")}

    def equity(self, marks: dict[str, float] | None = None) -> float:
        """Cash + market value of what is held. A stale number beats a fabricated one."""
        try:
            self.refresh()
            held = sum(float(p.get("market_value") or 0.0) for p in self.positions())
            self.equity_value = self.cash()["balance"] + held
        except GotradeError:
            pass
        return float(self.equity_value)

    def positions(self) -> list[dict[str, Any]]:
        out = []
        for row in self.client.open_positions(extended_hours=self.allow_extended_hours):
            out.append({
                "id": str(row.get("ticker") or ""),
                "symbol": str(row.get("ticker") or ""),
                "side": "long",                       # the only side the venue allows
                "shares": float(row.get("quantity") or 0.0),
                "entry": float(row.get("avg_entry_price") or 0.0),
                "market_value": float(row.get("market_value") or 0.0),
                "pnl": float(row.get("unrealized_pl") or 0.0),
                "stop": float(row.get("stop_loss") or 0.0) or None,
                "target": float(row.get("take_profit") or 0.0) or None,
                "raw": row,
            })
        return out

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        out = []
        for row in self.client.open_orders():
            order = row.get("order") or row
            stock = row.get("stock") or {}
            ticker = str(stock.get("ticker") or order.get("ticker") or "")
            if symbol and ticker.upper() != symbol.upper():
                continue
            out.append({"id": str(order.get("order_id") or ""), "symbol": ticker,
                        "side": str(order.get("side") or "").lower(),
                        "type": str(order.get("type") or "").lower(),
                        "state": str(order.get("status") or ""),
                        "shares": float(order.get("quantity") or 0.0), "raw": order})
        return out

    def cancel_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        try:
            self.client.cancel_order(order_id)
        except GotradeError as exc:
            return {"ok": False, "order_id": order_id, "reason": str(exc)}
        return {"ok": True, "order_id": order_id}

    # ------------------------------------------------------------------ writes
    def submit(self, order: Any) -> Any:
        """Send one share order and report a fill only when the venue says FILLED.

        Refusals happen before the request, and each has a reason a human can act on:
        a sell that would open a short, an entry with no stop, a bracket during extended hours.
        """
        from .broker import Fill

        def failed(message: str, raw: dict[str, Any] | None = None) -> Fill:
            return Fill(False, "", str(getattr(order, "client_id", "")),
                        str(getattr(order, "symbol", "")), str(getattr(order, "side", "")),
                        0.0, 0.0, 0.0, message, raw=raw or {})

        ticker = str(getattr(order, "symbol", "") or "").upper()
        side = str(getattr(order, "side", "") or "").lower()
        client_id = str(getattr(order, "client_id", "") or "")
        if not ticker:
            return failed("no ticker")
        if client_id and client_id in self.seen_ids:
            return failed("duplicate client_id refused")

        held = {p["symbol"].upper(): p for p in self.positions()}
        if side == "sell" and ticker not in held:
            # the venue reports shortable: false — a sell here is not a short, it is an error
            return failed(f"SELL {ticker} refused: this account is long-only and holds no "
                          f"{ticker}; a short position cannot be opened on Gotrade")
        stop = getattr(order, "stop", None)
        target = getattr(order, "target", None)
        if side == "buy" and not stop:
            return failed("refused: every entry needs a hard stop-loss (repo rule 5); the venue "
                          "accepts stop_loss on the entry order, so there is no excuse to omit it")
        if self.allow_extended_hours and (stop or target):
            return failed("refused: take_profit / stop_loss are rejected by the venue during "
                          "extended hours; trade the regular session or drop the bracket")

        shares = float(getattr(order, "quantity", 0.0) or 0.0)
        if shares <= 0:
            return failed("zero shares")
        if not self.fractional:
            if shares != int(shares):
                return failed(f"fractional shares are disabled in config ({shares} requested)")
            shares = float(int(shares))

        payload: dict[str, Any] = {
            "ticker": ticker,
            "side": "BUY" if side == "buy" else "SELL",
            "order_type": "market",
            "quantity": round(shares, 6),
            "is_gtc": True,
            "is_extended_hours": bool(self.allow_extended_hours),
        }
        if stop:
            payload["stop_loss"] = round(float(stop), 4)
        if target:
            payload["take_profit"] = round(float(target), 4)

        if client_id:
            self.seen_ids.add(client_id)
        try:
            created = self.client.create_order(**payload)
        except GotradeError as exc:
            return failed(str(exc))
        order_body = (created or {}).get("order") or created or {}
        order_id = str(order_body.get("order_id") or "")
        if not order_id:
            return failed(f"venue accepted the request without an order_id: {created}")
        try:
            final = self.client.wait_for_terminal(order_id, timeout_s=self.fill_timeout_s)
        except GotradeError as exc:
            return failed(f"could not read order {order_id}: {exc}", {"created": created})

        # the venue's own vocabulary: NEW | FILLED | PARTIAL_FILL | REJECTED | CANCELED
        state = str(final.get("state") or "").upper()
        filled = float(final.get("cumulative_quantity") or 0.0)
        price = float(final.get("average_fill_price") or 0.0)
        fees = float((final.get("fees") or {}).get("value") or 0.0)

        # A partial fill is not terminal: the order is still working, but *shares were bought*.
        # Reporting "not filled" here would leave a real position unmanaged — worse than a
        # phantom one — so any shares that exist are reported, with the order state attached.
        if filled > 0 and state in ("PARTIAL_FILL", "TIMEOUT", "NEW", ""):
            if price <= 0:
                return failed(
                    f"{filled:g} shares of {ticker} filled but the venue returned no average "
                    f"price; reconcile /portfolio/open-positions before the next decision",
                    {"created": created, "final": final, "unpriced_fill": filled})
            return Fill(True, order_id, client_id, ticker, side, filled, price, fees,
                        f"partially filled {filled:g}/{shares:g} · order {state or 'working'}",
                        raw={"created": created, "final": final, "partial": True})
        if state not in ("FILLED", "PARTIAL_FILL") or filled <= 0:
            return failed(f"order {order_id} ended as {state or 'UNKNOWN'} "
                          f"with {filled:g} shares filled", {"created": created, "final": final})
        message = "filled" if state == "FILLED" else f"partially filled {filled:g}/{shares:g}"
        return Fill(True, order_id, client_id, ticker, side, filled, price, fees, message,
                    raw={"created": created, "final": final})

    def close(self, symbol: str, price: float, reason: str) -> Any:  # pragma: no cover - live
        from .broker import ClosedTrade

        live = [p for p in self.positions() if p["symbol"].upper() == symbol.upper()]
        if not live:
            return None
        row = live[0]
        try:
            self.client.create_order(ticker=row["symbol"], side="SELL", order_type="market",
                                     quantity=row["shares"], is_gtc=True)
        except GotradeError:
            return None
        return ClosedTrade(symbol=row["symbol"], market=self.market, side="long",
                           entry=row["entry"], exit=float(price or row["entry"]),
                           size=row["shares"], pnl=row["pnl"], fees=0.0, r_multiple=0.0,
                           reason=reason, opened_at="",
                           closed_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))

    def close_symbol(self, symbol: str) -> dict[str, Any]:
        live = [p for p in self.positions() if p["symbol"].upper() == symbol.upper()]
        if not live:
            return {"ok": False, "reason": f"no open position for {symbol}"}
        row = live[0]
        try:
            created = self.client.create_order(ticker=row["symbol"], side="SELL",
                                              order_type="market", quantity=row["shares"],
                                              is_gtc=True)
        except GotradeError as exc:
            return {"ok": False, "reason": str(exc)}
        order_id = ((created or {}).get("order") or {}).get("order_id")
        return {"ok": True, "symbol": row["symbol"], "shares": row["shares"], "order_id": order_id}


def from_settings(settings: Any, *, transport: Callable[..., Any] | None = None) -> "GotradeBroker":
    """Build from config + environment. A missing token raises: no silent paper fallback."""
    from ..env import get

    cfg = settings.get("execution.gotrade", {}) or {}
    token = get(str(cfg.get("token_env") or "TO_GOTRADE_TOKEN"))
    base = str(cfg.get("base_url") or BASE_URLS.get(str(cfg.get("entity") or "global"),
                                                    BASE_URLS["global"]))
    if not token:
        raise RuntimeError(
            "Gotrade live requested but TO_GOTRADE_TOKEN is not set; refusing to fall back to "
            "paper. Enable API access first, then generate the token at Profile > API Key."
        )
    return GotradeBroker(
        client=GotradeClient(token=token, base_url=base, transport=transport or None),
        fractional=bool(cfg.get("fractional", True)),
        allow_extended_hours=bool(cfg.get("extended_hours", False)),
        fill_timeout_s=float(cfg.get("fill_timeout_s", 20) or 20),
    )


def readiness(settings: Any) -> dict[str, Any]:
    """What is missing before a US bot could run — no secrets, no network.

    Checks the things the API docs make blocking, so the answer is about accounts and
    permissions rather than about code.
    """
    from ..env import get

    cfg = settings.get("execution.gotrade", {}) or {}
    token = get(str(cfg.get("token_env") or "TO_GOTRADE_TOKEN"))
    entity = str(cfg.get("entity") or "global")
    return {
        "token_present": bool(token),
        "entity": entity,
        "base_url": str(cfg.get("base_url") or BASE_URLS.get(entity, BASE_URLS["global"])),
        "blocking": [
            "a valid/current ID document on the account: the app reports the ID as expired, "
            "and US trading is restricted until it is replaced",
            "API access enabled for the account (requested from a Gotrade representative); "
            "without it every call returns 401 even with a valid token",
            "a token generated at Profile > API Key after enablement (1 active token, 1 year)",
        ],
        "constraints": {
            "long_only": True,
            "shortable": False,
            "fee_pct_per_side": FEE_PCT,
            "fee_min_usd": FEE_MIN,
            "stop_loss_required": True,
            "tp_sl_rejected_in_extended_hours": True,
            "market_data_endpoints_published": False,
        },
    }
