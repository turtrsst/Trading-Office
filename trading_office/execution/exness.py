"""Exness Trader API: signed REST, asynchronous orders, one honest Broker.

Why this is a separate module
-----------------------------
Exness is the first venue this project supports that is **not** a crypto exchange: instruments
are CFDs (XAUUSD, FX majors, indices), volume is in lots with a broker-defined minimum and
step, and — the part that changes the engine's assumptions — **trading is asynchronous**. An
order returns ``status: "accepted"`` with an ``operation_id``; the fill arrives later as a
``transaction_event`` (or by polling the operation). ``PaperBroker`` and ``CcxtBroker`` both
return a priced ``Fill`` synchronously, so an adapter that papered over that difference would
record a position that may never exist.

Design decisions, each with the failure it prevents
---------------------------------------------------
* **Ed25519 over the exact bytes we send.** The signature covers ``EXN-DATA``; the body hash
  covers the exact body. The HTTP layer therefore takes *encoded bytes* and the signer hashes
  those same bytes — no re-serialisation between signing and sending, which is the classic way
  to produce ``AUTH_INVALID_SIGNATURE`` on some requests and not others.
* **Access Point discovered once, then cached.** Discovery is rate limited, and the docs are
  explicit that the Gateway host is not the account's access point.
* **Idempotency key on every mutating call.** A timeout is not a failure: retrying with the
  same key must not open a second position.
* **No fill, no position.** ``submit()`` waits for a final state with a timeout and returns
  ``ok=False`` if it cannot confirm one, instead of pretending the order filled.
* **Volume is refused locally** against ``volume_min``/``volume_max``/``volume_step`` — the
  broker's ``TRADING_RULE_INVALID_VOLUME`` is a 5000 after a round trip, and a silently
  rounded-down lot size is a different risk than the one that was planned.

What is *not* guessed here: the endpoint list. Only the paths documented in the Exness Trader
API guide are used (access point, account configuration, open position, operation status,
close). Anything not documented is absent rather than invented.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

#: documented hash of the empty body (base64url, no padding)
EMPTY_BODY_HASH = "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"

#: error codes named in the public reference, mapped to something an operator can act on.
#: The documented contract is ``code`` + ``error_message``; the hint explains the cause.
ERROR_HINTS: dict[int, str] = {
    1000: "AUTH_INVALID_API_KEY — check EXN-API-KEY, key expiry, and api_key inside EXN-DATA",
    1001: "AUTH_INVALID_SIGNATURE — check EXN-DATA/EXN-SIGN, signed path, body hash, clock skew",
    1002: "AUTH_PERMISSION_DENIED — check key scopes, the source IP allowlist, and the account",
    2000: "ACCOUNT_NOT_FOUND — account_id does not belong to this API key",
    2001: "ACCOUNT_DISABLED — the account cannot trade; nothing to retry",
    2002: "ACCOUNT_CLOSE_ONLY — only closing is allowed; no new exposure",
    3001: "REQUEST_INVALID_PARAMETERS — the request shape is wrong",
    3002: "REQUEST_INVALID_PRICE — price has more digits than point_digits allows",
    3014: "REQUEST_OPERATION_NOT_FOUND — unknown operation_id (24 h retention)",
    4001: "MARKET_TRADE_DISABLED — new positions are disabled for this instrument",
    4002: "MARKET_CLOSE_ONLY — this instrument is close-only right now",
    4003: "MARKET_SESSION_CLOSED — outside the instrument's trading session (gold runs Sun 22:00–Fri 21:00 UTC)",
    4004: "MARKET_NO_QUOTES — no price to execute against; wait for a tick",
    4005: "MARKET_INSTRUMENT_NOT_FOUND — instrument name is wrong for this account",
    5000: "TRADING_RULE_INVALID_VOLUME / VOLUME_TOO_SMALL / VOLUME_TOO_LARGE — check volume_min/max",
    5001: "TRADING_RULE_INVALID_VOLUME_STEP — volume must be a multiple of volume_step",
    5005: "TRADING_RULE_INVALID_PRICE_LEVELS — SL/TP on the wrong side of the market",
    5006: "TRADING_RULE_INSUFFICIENT_MARGIN — free margin is too small for this size",
    5007: "TRADING_RULE_MARGIN_LEVEL_TOO_LOW — the account is near a margin call; no new exposure",
    5010: "TRADING_RULE_TOO_MANY_OPEN_POSITIONS — the venue limit is reached",
    6000: "EXECUTION_REQUOTE — price moved; retry only while the market is open",
    6001: "EXECUTION_REJECTED — wait for a fresh tick; retry only if still intended",
    9000: "SYSTEM_RATE_LIMIT — slow down; check the limits endpoint before retrying",
}

#: documented terminal states for an operation, and the one non-terminal state
FINAL_STATES = {"confirmed": "filled", "rejected": "rejected", "failed": "failed"}
PENDING_STATES = {"pending", "accepted", "processing"}


def b64url(data: bytes) -> str:
    """base64url without ``=`` padding, as the API requires for EXN-DATA and EXN-SIGN."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decimal(value: float) -> str:
    """Encode a number as the decimal string the API wants (no exponent, no trailing zeros)."""
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text or "0"


def quote(text: str) -> str:
    """Percent-encode a query value **without** touching what must stay literal.

    The signed path is the transmitted path, so this is the only encoding step: building the
    URL first and letting a library re-encode it afterwards is how signatures break. ``:`` and
    ``+`` are escaped because ``+`` in an ISO timestamp is the UTC offset.
    """
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(ch if ch in safe else "".join(f"%{b:02X}" for b in ch.encode()) for ch in text)


def body_hash(body: bytes | None) -> str:
    """SHA-256 of the **exact** bytes sent, base64url encoded."""
    return b64url(hashlib.sha256(body or b"").digest())


# --------------------------------------------------------------------------- signing
@dataclass
class ExnessSigner:
    """Builds the ``EXN-*`` headers for one request.

    The private key never leaves this object and is never logged; only the public key id is
    echoed in headers (the API requires them to match).
    """

    api_key: str
    private_key: Any                      # cryptography Ed25519PrivateKey
    sign_version: str = "1"

    @classmethod
    def from_pem(cls, api_key: str, pem: str | bytes) -> "ExnessSigner":
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        raw = pem.encode() if isinstance(pem, str) else pem
        return cls(api_key=api_key, private_key=load_pem_private_key(raw, password=None))

    @classmethod
    def from_seed_hex(cls, api_key: str, seed_hex: str) -> "ExnessSigner":
        """For a PA that hands out a 32-byte seed instead of a PEM file."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return cls(api_key=api_key, private_key=Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex)))

    def headers(self, method: str, path: str, body: bytes | None = None,
                idempotency_key: str | None = None, timestamp_ms: int | None = None) -> dict[str, str]:
        """Signed headers for exactly ``method path body`` (query string included in ``path``)."""
        method = method.upper()
        if idempotency_key is None:
            idempotency_key = "" if method == "GET" else f"to-{int(time.time() * 1000)}"
        if method == "GET" and idempotency_key:
            raise ValueError("the API requires an empty EXN-IDEMPOTENCY-KEY for signed GET requests")
        if method != "GET" and not idempotency_key:
            raise ValueError("mutating requests need a non-empty EXN-IDEMPOTENCY-KEY")
        stamp = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
        payload = {
            "api_key": self.api_key,
            "idempotency_key": idempotency_key,
            "timestamp": stamp,
            "sign_version": int(self.sign_version),
            "method": method,
            "path": path,
            "body_hash": body_hash(body),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return {
            "EXN-API-KEY": self.api_key,
            "EXN-IDEMPOTENCY-KEY": idempotency_key,
            "EXN-TIMESTAMP": str(stamp),
            "EXN-SIGN-VERSION": self.sign_version,
            "EXN-DATA": b64url(raw),
            "EXN-SIGN": b64url(self.private_key.sign(raw)),
        }


def verify_headers(public_key: Any, headers: dict[str, str], method: str, path: str,
                   body: bytes | None = None) -> dict[str, Any]:
    """Server-side check (used by the test stub): signature, and that the payload agrees.

    Returns the decoded payload so a stub can also assert the signed path and body hash match
    what actually arrived — the two things that make a signature valid for the wrong request.
    """
    lookup = {k.lower(): v for k, v in headers.items()}
    payload = json.loads(unb64url(lookup["exn-data"]))
    public_key.verify(unb64url(lookup["exn-sign"]), unb64url(lookup["exn-data"]))
    if payload["method"] != method.upper():
        raise ValueError("signed method does not match the request")
    if payload["path"] != path:
        raise ValueError("signed path does not match the request")
    if payload["body_hash"] != body_hash(body):
        raise ValueError("signed body hash does not match the body")
    if payload["timestamp"] != int(lookup["exn-timestamp"]):
        raise ValueError("timestamp header does not match EXN-DATA")
    if payload["api_key"] != lookup["exn-api-key"]:
        raise ValueError("api key header does not match EXN-DATA")
    return payload


# --------------------------------------------------------------------------- instrument
@dataclass
class FxInstrument:
    """One CFD's trading rules, as the account configuration reports them."""

    symbol: str
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    contract_size: float = 100_000.0     # 1 lot = 100k units for FX; 100 oz for XAUUSD
    price_step: float = 0.01
    margin_rate: float = 0.0333          # 1:30 retail on gold, overridden from the venue
    quote_currency: str = "USD"          # gold and FX majors are USD-quoted

    @classmethod
    def from_conditions(cls, symbol: str, data: dict[str, Any]) -> "FxInstrument":
        """Build from ``GET …/instruments/{instrument}/conditions``.

        Every field is optional in that payload, so anything missing keeps the default rather
        than becoming zero: a zero step would make every volume "invalid" in a confusing way.
        """
        known = {f for f in cls.__dataclass_fields__}
        mapped: dict[str, Any] = {}
        aliases = {
            "volume_min": ("volume_min",),
            "volume_max": ("volume_max",),
            "volume_step": ("volume_step",),
            "contract_size": ("contract_size", "lot_size"),
            "price_step": ("price_step", "tick_size", "point"),
            "margin_rate": ("margin_rate",),
            "quote_currency": ("quote_currency", "profit_currency"),
        }
        for field_name, keys in aliases.items():
            if field_name not in known:
                continue
            for key in keys:
                if data.get(key) not in (None, ""):
                    try:
                        mapped[field_name] = float(data[key])
                    except (TypeError, ValueError):
                        pass
                    break
        digits = data.get("point_digits")
        if digits not in (None, "") and "price_step" not in mapped:
            try:
                mapped["price_step"] = 10 ** -int(digits)
            except (TypeError, ValueError):
                pass
        return cls(symbol=symbol, **mapped)

    def price_str(self, value: float) -> str:
        """Round to the instrument's price step and render as a decimal string."""
        step = self.price_step or 0.01
        rounded = round(round(float(value) / step) * step, 10)
        return _decimal(rounded)

    def normalise_volume(self, volume: float) -> tuple[float, str]:
        """Round **down** to the venue step. Returns ``(volume, reason_if_refused)``.

        Rounding down matters: the plan's size came from a risk budget, and rounding up would
        silently exceed it. A size below ``volume_min`` is refused instead of bumped up, because
        the bumped size is a risk the operator never approved.
        """
        if volume <= 0:
            return 0.0, "volume must be positive"
        step = self.volume_step or 0.01
        steps = int(volume / step + 1e-9)
        sized = round(steps * step, 8)
        if sized + 1e-9 < self.volume_min:
            return 0.0, (f"{volume:g} lots is below the venue minimum {self.volume_min:g} "
                         f"(step {step:g})")
        if sized > self.volume_max + 1e-9:
            return 0.0, f"{volume:g} lots is above the venue maximum {self.volume_max:g}"
        return sized, ""


def lots_for_risk(*, equity: float, risk_pct: float, entry: float, stop: float,
                  instrument: FxInstrument, max_margin_pct: float = 8.0,
                  leverage: float = 1.0 / 0.0333) -> dict[str, Any]:
    """Convert a risk budget into lots, and say when it cannot be expressed.

    This is the FX equivalent of the crypto sizing: risk cash ÷ (price distance × contract
    size). It also refuses when the margin the position needs exceeds the per-trade cap, which
    is the failure mode leverage 1:500 makes easy and the kill switch is too slow to catch.
    """
    distance = abs(float(entry) - float(stop))
    if distance <= 0:
        return {"ok": False, "reason": "stop distance is zero"}
    risk_cash = abs(float(equity)) * abs(float(risk_pct)) / 100.0
    per_lot_risk = distance * instrument.contract_size
    raw_lots = risk_cash / per_lot_risk
    margin_cap = abs(float(equity)) * float(max_margin_pct) / 100.0
    per_lot_margin = instrument.contract_size * float(entry) / (leverage if leverage > 0 else 1.0)
    # the margin cap is a *sizing* constraint, not a rejection: the position shrinks until the
    # capital it ties up fits, and the risk falls with it. Refusing here would refuse every
    # trade on a 1:3 account, because gold's notional is large by construction.
    cap_lots = margin_cap / per_lot_margin if per_lot_margin > 0 else raw_lots
    lots, refused = instrument.normalise_volume(min(raw_lots, cap_lots))
    if not lots:
        return {"ok": False, "reason": refused, "raw_lots": raw_lots, "risk_cash": risk_cash,
                "margin_cap": margin_cap}
    notional = lots * instrument.contract_size * float(entry)
    margin = notional / leverage if leverage > 0 else notional
    capped_by = "margin" if cap_lots < raw_lots else ""
    return {"ok": True, "lots": lots, "risk_cash": risk_cash,
            "risk_cash_actual": round(lots * per_lot_risk, 2),
            "risk_pct_actual": round(lots * per_lot_risk / abs(float(equity)) * 100, 4),
            "margin": margin, "margin_cap": margin_cap, "capped_by": capped_by}


def min_viable_equity(*, entry: float, stop: float, instrument: FxInstrument,
                      risk_pct: float = 0.5, max_margin_pct: float = 8.0,
                      leverage: float = 3.0) -> dict[str, Any]:
    """Smallest equity at which the venue minimum lot still fits **both** budgets.

    Two independent constraints bind, and the larger one wins:

    * **risk** — the smallest lot must not exceed the per-trade risk budget, so
      ``equity ≥ volume_min × stop × contract ÷ (risk_pct/100)``
    * **margin** — the smallest lot's margin must stay under the per-trade cap, so
      ``equity ≥ volume_min × contract × entry ÷ (leverage × margin_pct/100)``

    On gold the margin constraint dominates by an order of magnitude, which is the
    counter-intuitive part: the stop distance is what feels risky, the lot size is what
    actually decides whether the trade can exist.
    """
    distance = abs(float(entry) - float(stop))
    contract = instrument.contract_size
    volume_min = instrument.volume_min
    risk_equity = (volume_min * distance * contract * 100.0 / abs(risk_pct)) if risk_pct else 0.0
    margin_equity = (volume_min * contract * float(entry)
                     / (max(leverage, 1e-9) * max(max_margin_pct, 1e-9) / 100.0))
    required = max(risk_equity, margin_equity)
    return {
        "equity_required": round(required, 2),
        "binding": "margin" if margin_equity >= risk_equity else "risk",
        "risk_equity": round(risk_equity, 2),
        "margin_equity": round(margin_equity, 2),
        "volume_min": volume_min,
        "risk_at_minimum_lot": round(volume_min * distance * contract, 2),
        "risk_at_minimum_lot_pct": round(
            volume_min * distance * contract / required * 100, 4) if required else 0.0,
    }


# --------------------------------------------------------------------------- transport
def _default_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                       timeout: float) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001 - a non-JSON error page is still an error
            return exc.code, {"error_message": f"HTTP {exc.code}"}


@dataclass
class ExnessClient:
    """Minimal, testable Trader API client: one URL per call, signed at the edge."""

    signer: ExnessSigner
    account_id: str
    gateway_host: str = ""                 # from the DNS widget; used once, for discovery
    access_point: str = ""                 # cached per account
    transport: Callable[..., tuple[int, dict[str, Any]]] | None = None
    timeout: float = 10.0
    audit: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ plumbing
    def _send(self, method: str, base: str, path: str, payload: dict[str, Any] | None = None,
              idempotency_key: str | None = None) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        headers = self.signer.headers(method, path, body, idempotency_key=idempotency_key)
        send = self.transport or _default_transport
        status, data = send(method, base.rstrip("/") + path, headers, body, self.timeout)
        record = {"method": method, "path": path, "status": status,
                  "error_code": data.get("code", data.get("error_code")), "at": time.time()}
        self.audit.append(record)
        del self.audit[:-100]
        if status >= 400:
            # the documented contract is {code, error_message}; error_code was assumed
            # before the OpenAPI reference was read, so both are accepted here
            code = data.get("code", data.get("error_code"))
            raise ExnessError(status, code, str(data.get("error_message") or ""), record)
        return data

    def resolve_access_point(self) -> str:
        """Discovery, cached. The Gateway host is *not* an account access point."""
        if self.access_point:
            return self.access_point
        if not self.gateway_host:
            raise ExnessError(0, None, "gateway_host is required for access-point discovery", {})
        path = f"/v1/trading/access-point?account_id={self.account_id}"
        data = self._send("GET", self.gateway_host, path)
        point = str(data.get("access_point") or "")
        if not point:
            raise ExnessError(0, None, "discovery returned no access_point", {"body": data})
        self.access_point = point
        return point

    def _ap(self) -> str:
        return self.resolve_access_point()

    # ------------------------------------------------------------------ reads
    def account(self) -> dict[str, Any]:
        return self._send("GET", self._ap(), f"/v1/configuration/accounts/{self.account_id}/account")

    def limits(self) -> dict[str, Any]:
        """Rate limits, candle limits and websocket limits: read before spending quota."""
        return self._send("GET", self._ap(), f"/v1/configuration/accounts/{self.account_id}/limits")

    def conditions(self, instrument: str) -> dict[str, Any]:
        """The venue's own volume_min/max/step, point_digits, contract size and margin rate.

        This is the authority for instrument rules: guessing them from a docs example is how
        an order becomes TRADING_RULE_INVALID_VOLUME after a round trip.
        """
        return self._send("GET", self._ap(),
                          f"/v1/configuration/accounts/{self.account_id}"
                          f"/instruments/{instrument}/conditions")

    def snapshot(self) -> dict[str, Any]:
        """Current trading state: ``{orders, positions, account_state}``."""
        return self._send("GET", self._ap(),
                          f"/v1/trading/accounts/{self.account_id}/snapshot")

    def positions(self) -> list[dict[str, Any]]:
        return list(self.snapshot().get("positions") or [])

    def candles(self, instrument: str, timeframe: str, *, start: str, count: int = 400,
                price_type: str = "bid") -> list[dict[str, Any]]:
        """Historical candles (``{time, open, high, low, close, volume}``), oldest first.

        ``start`` is ISO-8601 UTC. ``count`` may be negative to walk backwards, which is what
        a backtest wants: the most recent N bars without knowing the end time.
        """
        path = (f"/v1/market-data/accounts/{self.account_id}/candles"
                f"?instrument={instrument}&timeframe={timeframe}"
                f"&price_type={price_type}&from={quote(start)}&count={int(count)}")
        return list(self._send("GET", self._ap(), path).get("candles") or [])

    def operation(self, operation_id: str) -> dict[str, Any]:
        """Final state of an accepted operation (the ACK is not the result)."""
        return self._send("GET", self._ap(),
                          f"/v1/trading/accounts/{self.account_id}/operations/{operation_id}")

    # ------------------------------------------------------------------ writes
    def open_position(self, *, instrument: str, side: str, volume: float,
                      stop_loss: float | None = None, take_profit: float | None = None,
                      client_request_id: str = "", comment: str = "") -> dict[str, Any]:
        # field names and the decimal-string encoding come from OpenPositionRequest in the
        # published OpenAPI document: stop_loss (not stop_loss_price) was my earlier guess
        payload: dict[str, Any] = {"instrument": instrument, "side": side.lower(),
                                   "volume": _decimal(volume)}
        if stop_loss:
            payload["stop_loss"] = _decimal(stop_loss)
        if take_profit:
            payload["take_profit"] = _decimal(take_profit)
        if comment:
            payload["comment"] = comment[:31]        # MT-style comment limit, kept short
        return self._send("POST", self._ap(), f"/v1/trading/accounts/{self.account_id}/positions",
                          payload, idempotency_key=client_request_id or None)

    def close_position(self, position_id: str, *, volume: float | None = None,
                       client_request_id: str = "") -> dict[str, Any]:
        """Close (or partially close) a position. Documented as ``DELETE`` on the position,
        with the partial volume as a query parameter — not a ``POST .../close`` body."""
        path = f"/v1/trading/accounts/{self.account_id}/positions/{position_id}"
        if volume:
            path += f"?volume={_decimal(volume)}"
        return self._send("DELETE", self._ap(), path,
                          idempotency_key=client_request_id or None)

    def cancel_order(self, order_id: str, *, client_request_id: str = "") -> dict[str, Any]:
        return self._send("DELETE", self._ap(),
                          f"/v1/trading/accounts/{self.account_id}/orders/{order_id}",
                          idempotency_key=client_request_id or None)

    def modify_order(self, order_id: str, *, stop_loss: float | None = None,
                     take_profit: float | None = None, price: float | None = None,
                     client_request_id: str = "") -> dict[str, Any]:
        """Change SL/TP or price on a pending order. ``None`` omits (unchanged)."""
        payload: dict[str, Any] = {}
        if price is not None:
            payload["price"] = _decimal(price)
        if stop_loss is not None:
            payload["stop_loss"] = _decimal(stop_loss)
        if take_profit is not None:
            payload["take_profit"] = _decimal(take_profit)
        if not payload:
            raise ValueError("modify_order needs at least one of price / stop_loss / take_profit")
        return self._send("PATCH", self._ap(),
                          f"/v1/trading/accounts/{self.account_id}/orders/{order_id}",
                          payload, idempotency_key=client_request_id or None)

    def wait_for_final(self, operation_id: str, *, timeout_s: float = 15.0,
                       poll_s: float = 0.5) -> dict[str, Any]:
        """Poll until the operation stops being pending, or give up honestly.

        Returns ``{"state": "filled"|"rejected"|"pending"|"timeout", ...}``. ``pending`` is a
        real answer: the caller must not invent a price for an order the venue has not priced.
        """
        deadline = time.time() + max(0.0, timeout_s)
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.operation(operation_id)
            state = str(last.get("status") or "").lower()
            if state in FINAL_STATES:
                return {"state": FINAL_STATES[state], **last}
            if state and state not in PENDING_STATES:      # an unknown state is not a fill
                return {"state": "unknown", **last}
            time.sleep(max(0.0, poll_s))
        return {"state": "timeout" if last else "pending", **last}


class ExnessError(RuntimeError):
    """A venue error, already translated into something an operator can act on."""

    def __init__(self, status: int, code: Any, message: str, record: dict[str, Any]):
        self.status = status
        self.code = code
        try:
            hint = ERROR_HINTS.get(int(code))       # type: ignore[arg-type]
        except (TypeError, ValueError):
            hint = None
        self.hint = hint or ""
        detail = f"HTTP {status}" + (f" · {code}" if code is not None else "")
        super().__init__(f"{detail}: {message}" + (f" — {hint}" if hint else "") + f" ({record.get('path', '')})")


# --------------------------------------------------------------------------- broker
@dataclass
class ExnessBroker:
    """The ``Broker`` implementation the engine drives, over the signed REST API.

    Mirrors :class:`~trading_office.execution.broker.CcxtBroker`'s surface (``submit``,
    ``close``, ``equity``, ``positions``, ``close_symbol``) so nothing in the engine has to
    learn a new shape — the only conceptual difference is that ``submit`` waits for the venue
    to *finish* the order before reporting a fill.

    Sizing note: ``Order.quantity`` for this venue is **lots**, and it is validated locally
    against the instrument rules before a single byte is signed.
    """

    client: ExnessClient
    instrument: FxInstrument
    equity_value: float = 0.0
    market: str = "fx"
    name: str = "exness"
    fill_timeout_s: float = 15.0
    seen_ids: set[str] = field(default_factory=set)
    account_snapshot: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ state
    def refresh(self) -> dict[str, Any]:
        """Read the account once: balance/equity plus the rules this broker enforces."""
        data = self.client.account()
        self.account_snapshot = data
        for key in ("equity", "balance", "margin_free", "currency", "leverage", "trade_mode"):
            if key in data:
                self.account_snapshot.setdefault(key, data[key])
        equity = data.get("equity") or data.get("balance")
        if equity is not None:
            try:
                self.equity_value = float(equity)
            except (TypeError, ValueError):
                pass
        return data

    def equity(self, marks: dict[str, float] | None = None) -> float:
        """Equity as the venue reports it. A stale value is better than a fabricated one."""
        try:
            self.refresh()
        except ExnessError:
            pass
        return float(self.equity_value)

    def positions(self) -> list[dict[str, Any]]:
        """Open positions, normalised to the keys the engine and UI already read.

        The venue's position object carries ``direction`` (not ``side``) and no per-position
        profit, so P&L is left at 0.0 rather than invented: the caller has a mark and can
        compute it. SL/TP live on separate SLTP orders, not on the position payload.
        """
        out = []
        for row in self.client.positions():
            out.append({
                "id": str(row.get("position_id") or row.get("id") or ""),
                "symbol": str(row.get("instrument") or row.get("symbol") or ""),
                "side": str(row.get("direction") or row.get("side") or "").lower(),
                "lots": float(row.get("volume") or 0.0),
                "entry": float(row.get("open_price") or row.get("price") or 0.0),
                "stop": float(row.get("stop_loss") or row.get("stop_loss_price") or 0.0) or None,
                "target": float(row.get("take_profit") or row.get("take_profit_price") or 0.0) or None,
                "pnl": float(row.get("profit") or 0.0),   # not in the schema → 0.0, not a guess
                "swap": float(row.get("swap") or 0.0),
                "commission": float(row.get("commission") or 0.0),
                "raw": row,
            })
        return out

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Pending and SLTP orders from the snapshot."""
        out = []
        for row in self.client.snapshot().get("orders") or []:
            if symbol and str(row.get("instrument", "")).upper() != symbol.upper():
                continue
            out.append({"id": str(row.get("order_id", "")),
                        "symbol": str(row.get("instrument", "")),
                        "side": str(row.get("direction", "")).lower(),
                        "type": str(row.get("order_type", "")),
                        "price": float(row.get("price") or 0.0),
                        "lots": float(row.get("volume") or 0.0),
                        "state": str(row.get("state", "")),
                        "raw": row})
        return out

    def cancel_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        """Cancel a pending or SLTP order (documented: DELETE …/orders/{order_id})."""
        try:
            ack = self.client.cancel_order(order_id, client_request_id=f"cancel-{order_id}")
        except ExnessError as exc:
            return {"ok": False, "order_id": order_id, "reason": str(exc)}
        return {"ok": True, "order_id": order_id, "operation_id": ack.get("operation_id")}

    # ------------------------------------------------------------------ writes
    def submit(self, order: Any) -> Any:
        """Send one order, wait for the venue's final state, and only then report a fill.

        Returns the same :class:`Fill` dataclass the rest of the engine expects. A ``False``
        here means *nothing is known to be open*, which is the only safe thing to tell the
        engine when a timeout happens — the alternative records a phantom position that no
        venue holds, and the next cycle would manage a position that does not exist.
        """
        from .broker import Fill

        def failed(message: str, raw: dict[str, Any] | None = None) -> Fill:
            # price 0.0, not the hint: nothing was filled, and a non-zero price here is how a
            # refused order turns into a position at a price nobody paid
            return Fill(False, "", str(getattr(order, "client_id", "")), str(getattr(order, "symbol", "")),
                        str(getattr(order, "side", "")), 0.0, 0.0, 0.0, message, raw=raw or {})

        symbol = str(getattr(order, "symbol", "") or self.instrument.symbol)
        if symbol.upper() != self.instrument.symbol.upper():
            return failed(f"{symbol} is not the configured instrument ({self.instrument.symbol})")
        client_id = str(getattr(order, "client_id", "") or "")
        if client_id and client_id in self.seen_ids:
            return failed("duplicate client_id refused")
        lots, refused = self.instrument.normalise_volume(float(getattr(order, "quantity", 0.0) or 0.0))
        if not lots:
            return failed(f"volume refused locally: {refused}")
        side = str(getattr(order, "side", "")).lower()
        if side not in ("buy", "sell"):
            return failed(f"unknown side {side!r}")

        if client_id:
            self.seen_ids.add(client_id)
        try:
            ack = self.client.open_position(
                instrument=self.instrument.symbol, side=side, volume=lots,
                stop_loss=getattr(order, "stop", None), take_profit=getattr(order, "target", None),
                client_request_id=client_id, comment="trading-office",
            )
        except ExnessError as exc:
            return failed(str(exc))

        operation_id = str(ack.get("operation_id") or "")
        if not operation_id:
            return failed(f"venue ACK without operation_id: {ack}", ack)
        try:
            final = self.client.wait_for_final(operation_id, timeout_s=self.fill_timeout_s)
        except ExnessError as exc:
            return failed(f"could not read operation {operation_id}: {exc}", {"ack": ack})
        state = final.get("state")
        if state != "filled":
            return failed(f"order {operation_id} ended as {state}: "
                          f"{final.get('reason') or final.get('error_message') or 'no final state'}",
                          {"ack": ack, "final": final})

        price = float(final.get("price") or final.get("open_price") or 0.0) or float(
            getattr(order, "price_hint", 0.0) or 0.0)
        # the venue reports commission as a negative amount (a debit); the engine books fees
        # as a positive cost, so the sign is normalised here rather than at every call site
        fee = abs(float(final.get("commission") or final.get("fee") or 0.0))
        return Fill(True, operation_id, client_id, symbol, side, lots, price, fee,
                    "filled", raw={"ack": ack, "final": final})

    def close(self, symbol: str, price: float, reason: str) -> Any:  # pragma: no cover - depends on venue
        """Close the venue's own position for ``symbol``; no size is invented here."""
        from .broker import ClosedTrade

        live = [p for p in self.positions() if p["symbol"] == symbol]
        if not live:
            return None
        row = live[0]
        try:
            self.client.close_position(row["id"], client_request_id=f"close-{row['id']}-{int(time.time())}")
        except ExnessError:
            return None
        return ClosedTrade(symbol=symbol, market=self.market, side=row["side"], entry=row["entry"],
                           exit=float(price or row["entry"]), size=row["lots"], pnl=row["pnl"],
                           fees=0.0, r_multiple=0.0, reason=reason, opened_at="",
                           closed_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))

    def modify_stop(self, symbol: str, new_stop: float) -> dict[str, Any]:
        """Move the stop for ``symbol``, or say why it could not be moved.

        Exness keeps SL/TP on SLTP orders attached to a position, not on the position, so this
        finds the SLTP order for our position and patches it; if there is none it creates one.
        A ``False``/``ok: False`` here is important: the caller rolls the local stop back
        instead of believing a stop that the venue does not hold.
        """
        live = [p for p in self.positions() if p["symbol"] == symbol]
        if not live:
            return {"ok": False, "reason": f"no open position for {symbol}"}
        position = live[0]
        existing = next((o for o in self.open_orders(symbol) if o["type"] == "sltp"), None)
        price = self.instrument.price_str(new_stop)
        try:
            if existing:
                ack = self.client.modify_order(existing["id"], stop_loss=float(price),
                                               client_request_id=f"sl-{existing['id']}-{int(time.time())}")
            else:
                ack = self.client._send(
                    "POST", self.client._ap(),
                    f"/v1/trading/accounts/{self.client.account_id}/orders",
                    {"position_id": position["id"], "type": "sltp", "stop_loss": price},
                    idempotency_key=f"sltp-{position['id']}-{int(time.time())}",
                )
        except ExnessError as exc:
            return {"ok": False, "reason": str(exc), "stop": new_stop}
        return {"ok": True, "stop": new_stop, "operation_id": ack.get("operation_id"),
                "position_id": position["id"]}

    def close_symbol(self, symbol: str) -> dict[str, Any]:
        live = [p for p in self.positions() if p["symbol"] == symbol]
        if not live:
            return {"ok": False, "reason": f"no open position for {symbol}"}
        try:
            ack = self.client.close_position(live[0]["id"], client_request_id=f"ui-close-{live[0]['id']}")
        except ExnessError as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "operation_id": ack.get("operation_id"), "symbol": symbol,
                "volume": live[0]["lots"]}


def from_settings(settings: Any, *, transport: Callable[..., Any] | None = None) -> ExnessBroker:
    """Build the broker from config + environment. Keys are read through ``trading_office.env``.

    Nothing here falls back to paper silently: a missing key raises, exactly like
    ``make_broker`` does for ccxt — a live bot that quietly does nothing looks identical to a
    live bot that is working.
    """
    from ..env import credential, get

    cfg = settings.get("execution.exness", {}) or {}
    api_key = credential(str(cfg.get("api_key_env") or "TO_EXNESS_API_KEY"))
    private_key = get(str(cfg.get("private_key_env") or "TO_EXNESS_PRIVATE_KEY"))
    private_key_path = get(str(cfg.get("private_key_path_env") or "TO_EXNESS_PRIVATE_KEY_PATH"))
    account_id = str(get(str(cfg.get("account_id_env") or "TO_EXNESS_ACCOUNT_ID")) or "")
    gateway = str(get(str(cfg.get("gateway_env") or "TO_EXNESS_GATEWAY_HOST")) or "")
    if not api_key or not (private_key or private_key_path) or not account_id:
        raise RuntimeError(
            "Exness live requested but TO_EXNESS_API_KEY / TO_EXNESS_PRIVATE_KEY(_PATH) / "
            "TO_EXNESS_ACCOUNT_ID are not set; refusing to fall back to paper"
        )
    if private_key_path:
        from pathlib import Path

        pem = Path(private_key_path).expanduser().read_bytes()
        signer = ExnessSigner.from_pem(api_key, pem)
    else:
        signer = ExnessSigner.from_seed_hex(api_key, private_key.strip())
    client = ExnessClient(signer=signer, account_id=account_id, gateway_host=gateway,
                          transport=transport or None,
                          timeout=float(cfg.get("timeout_s", 10) or 10))
    rules = cfg.get("instrument", {}) or {}
    instrument = FxInstrument(
        symbol=str(rules.get("symbol", "XAUUSD")),
        volume_min=float(rules.get("volume_min", 0.01)),
        volume_max=float(rules.get("volume_max", 100)),
        volume_step=float(rules.get("volume_step", 0.01)),
        contract_size=float(rules.get("contract_size", 100)),
        price_step=float(rules.get("price_step", 0.01)),
        margin_rate=float(rules.get("margin_rate", 0.0333)),
    )
    return ExnessBroker(client=client, instrument=instrument,
                        market=str(cfg.get("market", "fx")),
                        fill_timeout_s=float(cfg.get("fill_timeout_s", 15) or 15))
