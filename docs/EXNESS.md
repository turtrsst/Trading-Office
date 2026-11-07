# Exness Trader API

How this repo talks to Exness, what is verified, and what you must do in the Personal Area
before a single order can be sent.

Exness is the first **non-crypto** venue here: the instruments are CFDs (XAUUSD, FX majors,
indices), volume is in **lots** with a broker-defined minimum and step, and — the difference
that matters most — **trading is asynchronous**. An order is *accepted* first and *filled*
later.

## What is implemented

| Piece | Where | Tested |
| --- | --- | --- |
| Signed requests (Ed25519, `EXN-*` headers, body hash, idempotency) | `trading_office/execution/exness.py` → `ExnessSigner` | against a local stub that verifies the signature over the bytes actually received |
| Access Point discovery + cache | `ExnessClient.resolve_access_point()` | discovery is called once, not per request |
| Account / positions reads | `ExnessClient.account()`, `.positions()` | shape normalised for the engine and UI |
| Open / close (asynchronous ACK → final state) | `ExnessClient.open_position()`, `ExnessBroker.submit()` | pending never becomes a fill; a rejection never invents a price |
| Lot sizing from a risk budget | `lots_for_risk()`, `FxInstrument` | rounds **down**, refuses below `volume_min`, enforces a margin cap |
| Duplicate protection | `ExnessBroker.seen_ids` + the venue idempotency key | second submit of one `client_id` is refused locally |

Not implemented on purpose (no documented endpoint was found, and guessing is not acceptable
at the order layer): cancel/modify of a resting order, pending-order listing, and partial
close via the API. `cancel_order()` returns an explicit refusal instead of pretending.

## Endpoints used

Only what the public developer portal documents (`exness-api.com`):

| Call | Path | Notes |
| --- | --- | --- |
| Discovery | `GET {gateway}/v1/trading/access-point?account_id=…` | cache the returned `access_point` |
| Account | `GET {ap}/v1/configuration/accounts/{account_id}/account` | balance, equity, leverage, `trade_mode` |
| Open | `POST {ap}/v1/trading/accounts/{account_id}/positions` | returns `operation_id`, `status: accepted` |
| Status | `GET {ap}/v1/trading/accounts/{account_id}/operations/{operation_id}` | poll until filled / rejected |
| Close | `POST {ap}/v1/trading/accounts/{account_id}/positions/{position_id}/close` | used by flatten / `close_symbol` |

## Signing, in one paragraph

Every request carries `EXN-API-KEY`, `EXN-IDEMPOTENCY-KEY` (empty for GET, non-empty for
mutations), `EXN-TIMESTAMP` (ms), `EXN-SIGN-VERSION: 1`, `EXN-DATA` (base64url, unpadded JSON
of `api_key`, `idempotency_key`, `timestamp`, `sign_version`, `method`, `path`, `body_hash`) and
`EXN-SIGN` (Ed25519 over the **decoded** `EXN-DATA` bytes). `path` includes the query string
verbatim; `body_hash` is SHA-256 of the exact body (`47DEQpj8…` for no body). The adapter signs
the same encoded bytes it sends, so a signature can never describe a different body.

## Setup (Personal Area)

1. **Open an account on the Exness platform (EXT).** MT4/MT5 sub-accounts **cannot** issue API
   keys — that is an Exness rule, not a limitation of this repo.
2. Complete verification (**POI + POA**). It is required for API access.
3. **API Management → New API key**, scope **Trading**. Copy:
   - the **API key** (public, `exnsk-…`),
   - the **Ed25519 private key** (a PEM file, or a 32-byte hex seed depending on what the PA
     gives you — download it once; it cannot be shown again),
   - the **Account ID**.
4. Copy the **Gateway host** from the DNS widget in Key Management. It is *not* the account's
   access point; the adapter uses it once and then caches the access point it returns.
5. Put the values in `.env` (git-ignored), never in chat or in `config/settings.json`:

```bash
TO_EXNESS_API_KEY=exnsk-...
TO_EXNESS_PRIVATE_KEY_PATH=/etc/trading-office/exness.pem   # preferred: outside the repo
# or, if the PA gave you a hex seed:
# TO_EXNESS_PRIVATE_KEY=<64 hex chars>
TO_EXNESS_ACCOUNT_ID=12345678
TO_EXNESS_GATEWAY_HOST=gw-....exness.com
```

## First three commands

```bash
# 1. Sizing only -- no key needed. This is the question that decides whether the
#    account can trade gold at all (see the note below).
python -m trading_office.cli exness --equity 1000 --risk-pct 0.5 --entry 2650 --stop 2632

# 2. Read-only connection: account, equity, leverage, open positions.
python -m trading_office.cli exness --connect

# 3. Same, as JSON (for a script or an agent).
python -m trading_office.cli --json exness --connect
```

`exness --connect` never sends an order: it calls account + positions only, so it works with a
read-limited key and cannot move money.

## The gold sizing trap (read this before depositing)

XAUUSD is quoted per ounce; **1 lot = 100 oz**. At $2,650 that is **$265,000 notional per
lot**. A 0.01 lot (the usual minimum) is $2,650 notional, and an $18 stop on 0.01 lot is
**$18 of risk**.

So on a $1,000 account with a 0.5 % risk budget ($5), the strategy asks for 0.0028 lots —
**below the venue minimum**. `lots_for_risk()` refuses instead of rounding up, because
rounding up to 0.01 lot would risk ~1.8 % per trade: the plan the operator approved said
0.5 %. The CLI says it in one line:

```
cannot size this trade 0.00277778 lots is below the venue minimum 0.01 (step 0.01)
a raw size of 0.0028 lots would risk 5.00 but the venue's smallest step is 0.01 lots
```

Consequences, stated plainly:

| Equity | 0.01 lot risk with an $18 stop | Share of equity |
| --- | --- | --- |
| $500 | $18 | 3.6 % |
| $1,000 | $18 | 1.8 % |
| $3,600 | $18 | 0.5 % (the configured default) |

Three honest options, in order of preference: a **wider** stop is not one of them (it changes
the strategy, not the sizing). Either size the account for the risk you configured, or lower
`risk.risk_per_trade_pct` knowingly and accept that the first stop-out costs that much, or
trade an instrument whose minimum lot is cheap enough to express the plan (some FX minors at
0.01 lot = $1,000 notional). `lots_for_risk()` also enforces `risk.max_margin_per_trade_pct`,
which is what stops leverage 1:500 from turning one bad day into a margin call.

## Status and limits

- **Demo first.** The API is identical for demo and live; a demo EXT account can issue keys.
  Run 20 trades in demo before arming live.
- **Asynchronous execution is modelled, not hidden.** `submit()` waits `fill_timeout_s`
  (15 s) for a final state. A timeout is reported as **not filled** — the engine will not
  manage a position the venue has not confirmed.
- **Fees are normalised.** The venue reports commission as a negative amount; the engine
  books it as a positive cost.
- **`fx` is not a backtestable market yet.** There is no XAUUSD candle dataset here, and the
  crypto/IDX cost models do not apply (spread + swap + weekend gaps instead). Until a real
  dataset exists, backtests say nothing about gold.
- **Averaging, hedging and grid are impossible by construction**: one position per symbol, a
  mandatory stop on every order, and a daily-loss breaker that counts unrealised loss.
