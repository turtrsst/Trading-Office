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
| Account state / limits / instrument conditions | `.account()`, `.limits()`, `.conditions()` | rules come from the venue, not from a docs example |
| Positions and orders | `.snapshot()` → `{orders, positions, account_state}` | one call, one consistent picture |
| Market data | `.candles()` (`bid`, `ask` **or both**) | the spread is measured, not modelled |
| Open / partial close / cancel / modify SL-TP | `.open_position()`, `.close_position()`, `.cancel_order()`, `.modify_order()` | all four are documented operations, not guesses |
| Asynchronous execution | `ExnessBroker.submit()` → `wait_for_final()` | `pending` never becomes a fill; a rejection never invents a price |
| Lot sizing from a risk budget | `lots_for_risk()`, `min_viable_equity()`, `FxInstrument` | rounds **down**, refuses below `volume_min`, sizes to the margin cap |
| Moving the stop | `ExnessBroker.modify_stop()` | SL/TP live on a separate `sltp` order: patched, or created if absent |
| Duplicate protection | `ExnessBroker.seen_ids` + the venue idempotency key | second submit of one `client_id` is refused locally |

There is no "market order" endpoint separate from the position one: `POST …/positions` **is**
immediate execution, and `POST …/orders` only accepts `limit`, `stop` and `sltp`.

## Endpoints used

Only what the public developer portal documents (`exness-api.com`):

| Call | Path | Notes |
| --- | --- | --- |
| Discovery | `GET {gateway}/v1/trading/access-point?account_id=…` | cache the returned `access_point` |
| Account | `GET {ap}/v1/configuration/accounts/{id}/account` | balance, equity, leverage, `trade_mode` |
| Limits | `GET {ap}/v1/configuration/accounts/{id}/limits` | rate limits, candle limit, websocket limits |
| Conditions | `GET {ap}/v1/configuration/accounts/{id}/instruments/{instrument}/conditions` | `volume_min/max/step`, `point_digits`, `contract_size` |
| Snapshot | `GET {ap}/v1/trading/accounts/{id}/snapshot` | `{orders, positions, account_state}` — the state source |
| Candles | `GET {ap}/v1/market-data/accounts/{id}/candles?instrument=…&timeframe=M15&from=…&count=…` | `price_type=bid/ask/both` |
| Open | `POST {ap}/v1/trading/accounts/{id}/positions` | `202` ACK with `operation_id` |
| Close / partial close | `DELETE {ap}/v1/trading/accounts/{id}/positions/{position_id}[?volume=…]` | omit `volume` = full close |
| Pending order | `POST {ap}/v1/trading/accounts/{id}/orders` | `limit`, `stop`, or `sltp` (attach SL/TP) |
| Modify order | `PATCH {ap}/v1/trading/accounts/{id}/orders/{order_id}` | `price`, `stop_loss`, `take_profit` |
| Cancel order | `DELETE {ap}/v1/trading/accounts/{id}/orders/{order_id}` | pending and `sltp` only |
| Status | `GET {ap}/v1/trading/accounts/{id}/operations/{operation_id}` | `pending` → `confirmed` / `rejected` / `failed` |

Two details that were wrong on the first pass, and are now taken from the OpenAPI document:
the operation status value for success is **`confirmed`** (not `filled`), and errors carry
**`code`** (not `error_code`). Both are accepted defensively in the client, but the stub used
by the tests follows the reference exactly so a regression is caught rather than tolerated.

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

## First four commands (the fourth is the one to paste)

```bash
# 4. Nothing is known until the API answers. This prints a redacted report:
#    discovery → account → limits → instrument conditions → snapshot → one gold candle,
#    plus the sizing number for this account. It cannot place an order.
python -m trading_office.cli exness --selftest          # human readable
python -m trading_office.cli --json exness --selftest   # same, for pasting into a chat/issue
```

The report shows credentials as **presence and length only** (`exnsk-de…<23 chars>`,
`<set, 64 chars>`), masks the account id and the host, and never includes a signature. That is
deliberate: a diagnostic you cannot share is a diagnostic nobody uses, and a diagnostic that
can move money is one nobody should share.

Reading the result:

| Verdict | Meaning | What to do |
| --- | --- | --- |
| `not_configured` | a `TO_EXNESS_*` variable is missing | the step names which |
| `bad_private_key` | the blob is not a usable Ed25519 key | re-download it from API Management; it is not the API key |
| `discovery_failed` | the gateway host did not answer | check the DNS-widget host; `1001` = key/signature/clock, `1002` = scope or source IP, a TLS/DNS error means egress |
| `partial` | some reads failed | the failing step carries the error `code` |
| `ok` | every read succeeded | next step is a **demo** account and 20 trades |



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
lot**, and at 1:3 leverage one lot ties up **$88,333 of margin**. The venue minimum of 0.01
lot therefore needs **$883 of margin** and risks `0.01 × 100 × stop_distance`.

Two constraints bind, and the surprising one wins. With the repo's caps
(`risk_per_trade_pct 0.5`, `max_margin_per_trade_pct 8`, `risk.max_leverage 3`):

| Equity | 0.01 lot margin | Margin cap (8 %) | 0.01 lot risk with an $18 stop | Can trade? |
| --- | --- | --- | --- | --- |
| $1,000 | $883 | $80 | $18 (1.80 %) | no — margin cap leaves 0.0009 lot |
| $10,000 | $883 | $800 | $18 (0.18 %) | no — cap leaves 0.009 lot, under the step |
| $11,042 | $883 | $883 | $18 (0.16 %) | **first equity that fits** |
| $60,000 | $883 | $4,800 | $18 (0.03 %) | yes — 0.05 lot, $300 risk |

`min_viable_equity()` computes that number and says which constraint binds:

```
$ trading-office exness --equity 1000 --risk-pct 0.5 --entry 2650 --stop 2632
cannot size this trade 0.00090566 lots is below the venue minimum 0.01 (step 0.01)
a raw size of 0.0028 lots would risk 5.00 but the venue's smallest step is 0.01 lots
equity needed for the smallest lot: 11,042 (margin constraint = 11,042); 0.01 lot at this stop risks 18.00
```

Three honest ways out, in order of preference: fund the account to the number above; choose an
instrument whose minimum lot is small enough ($1,000 notional on a 0.01 EURUSD lot instead of
$2,650); or deliberately raise `risk_per_trade_pct` and accept that the first stop-out costs
that much. Widening the stop is **not** on the list: it changes the strategy rather than the
sizing.

`lots_for_risk()` rounds **down** to the venue step and refuses below `volume_min` — it never
bumps a size up to the minimum, because the bumped size risks money the operator never
approved. When the margin cap binds it sizes the position *down* to the cap (the risk falls
with it) rather than refusing, which is the only way a 1:3 account can hold gold at all.

## Status and limits

- **Demo first.** The API is identical for demo and live; a demo EXT account can issue keys.
  Run 20 trades in demo before arming live.
- **Asynchronous execution is modelled, not hidden.** `submit()` waits `fill_timeout_s`
  (15 s) for a final state (`confirmed`). A timeout is reported as **not filled** — the
  engine will not manage a position the venue has not confirmed.
- **The stop is a separate order.** Positions do not carry SL/TP; an `sltp` order does. A
  trailing stop therefore *patches* that order, and if the patch fails the engine rolls the
  local stop back instead of believing a stop the venue does not hold.
- **Fees are normalised.** The venue reports commission as a negative amount; the engine
  books it as a positive cost.
- **Market data is live once a key exists.** `GET /v1/market-data/.../candles` supplies gold
  and FX history plus a measured bid/ask spread, so the chart, the screener and the gates are
  looking at the venue instead of at a simulation. Without a key everything still runs and is
  labelled `synthetic` on every row.
- **Backtests are the weak spot.** The candles are real, but the cost model is a profile
  (spread + slippage + swap) rather than measured fills, and there is no stored XAUUSD dataset
  for out-of-sample work. Treat a gold backtest as a sanity check, not as evidence.
- **Averaging, hedging and grid are impossible by construction**: one position per symbol, a
  mandatory stop on every order, and a daily-loss breaker that counts unrealised loss.

## The `fx` market inside the bot

Gold is a first-class market, not a side car:

| Where | What changed |
| --- | --- |
| `config/settings.json` → `profiles.fx` | `XAUUSD`, 15m/4h, London/NY session windows, `volume_min/step 0.01`, `contract_size 100`, `leverage 3`, CFD `execution_costs` |
| `trading_office/market/fx_client.py` | candles (bid/ask/both), closed-bar only, 24/5 week with a weekend gap, synthetic fallback that is labelled |
| `trading_office/risk.py` | lots from `contract_size`, floor to `volume_step`, refuse below `volume_min`, margin cap as a *sizing* cap |
| `trading_office/engine.py` | `--market fx`, snapshot-based reconcile, per-instrument tick rounding for stops |
| `trading_office/api/server.py` | `/api/market/fx`, `/api/candles/fx/XAUUSD`, `/api/screen?market=fx`, `/api/analyse?market=fx`, `providers.fx` in `/api/health` |
| `web/` | a **Gold / FX** tab: gold price with bid/ask and spread, gold plans shown in lots and USD |

What is deliberately **not** automatic: nothing starts trading gold. `execution.paper` stays
`true` until you flip it, and `autonomy.mode` still gates every live entry behind an approval.
