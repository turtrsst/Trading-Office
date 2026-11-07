# Gotrade (US shares)

What the API can do, what it cannot, and what that means for a bot on this account.

Gotrade publishes a **real REST API** — the first thing to check was whether "sync with the
app" could mean anything legitimate, and it can. Everything below comes from the published
documentation (`heygotrade.com/api-key/`) plus what the app itself reports about this account.

## Status: adapter built, two account blockers before it can trade

| Check | State | What it means |
| --- | --- | --- |
| REST API documented | yes | orders, positions, balance, cancel, TP/SL on the entry order |
| Adapter + tests | done | `trading_office/execution/gotrade.py`, offline stub, 21 tests |
| **ID document** | **no longer shown as expired** | the banner disappeared from the home screen; record it with `--id-status verified` once it stays gone |
| **API access** | **not enabled** | the docs require requesting it from a Gotrade representative; without it every call is `401`, even with a valid token. Not observable from this repo — only a 401 reveals it |
| API token | not created | Profile → API Key, **1 active token**, valid **1 year**, generating a new one revokes the old |
| Balance | $112.28 | see the fee arithmetic below before expecting this to trade |

The readiness report treats the ID as an **observation, not an assumption**: `id_status` is
`unknown` until someone records what the app shows, because a status page that keeps repeating
a fixed problem is a status page nobody believes.

`trading-office gotrade` prints this list, so the answer is never guesswork.

## The entity (from your screenshots)

The app package is `io.tr8.tradecharlie.android`, and its Legal screen shows **Gotrade**, and
**Alpaca** (the US broker-dealer that executes and clears) with separate customer agreements.
That combination matches **Gotrade Global** ("charlie"), not the Indonesia entity:

```
entity:   global
base URL: https://api.charlie.gotrade.admin.tr8.tech/api/b2c/v1
```

The Indonesia entity uses a different host (`api.indo.gotrade.prod-indo.tr8.tech`). The adapter
takes the base URL from config, so switching is one setting, not a rewrite.

One caveat stated plainly: the *product* you hold may be a PALN contract (derivatives on US
shares, via a registered Indonesian broker) even when the app talks to the Global API. That
affects tax and what you legally own, not the API calls — but it is worth knowing before
scaling up.

## What the API does, exactly

| Call | Path | Notes |
| --- | --- | --- |
| Create order | `POST /stock-orders/create` | `201`, returns `order_id`, status starts `NEW` |
| Order status | `GET /stock-orders/{order_id}` | `NEW` → `FILLED` / `PARTIAL_FILL` / `REJECTED` / `CANCELED` |
| List open orders | `GET /stock-orders` | working orders only |
| Cancel | `DELETE /stock-orders/{order_id}` | only if not fully filled; idempotent |
| Positions | `GET /portfolio/open-positions?is_extended_hours=` | qty, `avg_entry_price`, `unrealized_pl`, `market_value` |
| Balance | `GET /portfolio/account-balance` | `cash_balance` **and** `cash_available_for_trade` |
| Market data | — | the docs list the endpoints but **not their paths** |

Order body: `ticker`, `side`, `order_type`, `quantity` or `amount_cash`, plus optional
`limit_price`, `stop_price`, `is_gtc`, `is_extended_hours`, `take_profit`, `stop_loss`,
`trail_price`, `trail_percent`.

## Four constraints that shape the strategy

**1. Long-only.** `SELL` reduces a holding; the stock schema reports `shortable: false`. There
is no short. The adapter refuses a sell with no position instead of silently inverting it, so
"buy and sell" means *enter long, exit on TP/SL/trailing/time* — never a short.

**2. A stop is not optional, and it belongs on the entry order.** `take_profit` / `stop_loss`
are fields of the create request, which is the only reason a stop survives the bot crashing.
An entry with no stop is refused before the request is sent.

**3. TP/SL are rejected during extended hours** (`400`). So: bracket + regular session, or
extended hours with no protection. The adapter picks the first and says so.

**4. Two cash numbers.** `cash_balance` includes unsettled proceeds; `cash_available_for_trade`
is what can be spent. Sizing on the first is how a bot gets `400 insufficient buying power`
right after an exit. Sizing uses `available`.

## The number that decides everything: the fee

0.3 % per side, **minimum $0.10**. Round trip = 0.6 %, and on small orders the floor dominates:

| Order size | Fee each way | Round trip | Break-even move |
| --- | --- | --- | --- |
| $10 | $0.10 (floor) | $0.20 | **2.00 %** |
| $20 | $0.10 (floor) | $0.20 | **1.00 %** |
| $33 | $0.10 (floor) | $0.20 | 0.61 % |
| $100 | $0.30 | $0.60 | 0.60 % |
| $1,000 | $3.00 | $6.00 | 0.60 % |

On this account the numbers are blunt:

```
$ trading-office gotrade --equity 112.28 --entry 20 --stop 19.4
fee on a $20.00 order: $0.10 each way · round trip $0.20 = 1.000% of price before any profit
sized 1.4035 shares · notional $28.07 · risk $0.84 · fees $0.20 (24% of risk) · binding position_cap
```

**Fees are 24 % of the risk budget.** With a 1:2 target, the fee eats roughly a fifth of the
reward, so the win rate needed to break even jumps from 33 % to well over 40 %. Against a 40 %
per-position cap on $112, the position that fits is ~$28. That is not a strategy problem, it is
arithmetic: a $112 account with a $0.10 per-side fee cannot day-trade US shares.

Honest options, in order:

1. **Fix the ID first.** Nothing else is possible before that.
2. **Get API access enabled**, then generate the token. Until then the adapter stays paper-only.
3. **Treat US shares as a longer-horizon sleeve**, not a scalping bot: fewer, larger positions
   where 0.6 % round trip is small against a 5–10 % move. The existing time-exit and the cost
   gate already enforce this (`cost_multiple 3.0` rejects setups whose target is inside 3× cost).
4. **Or trade the same idea on a venue where the fee is not the strategy.** Crypto perps on
   Bybit cost ~18 bps round trip; that is where a $112 account can actually rotate.

## An already-invested book: what the numbers say

The account holds four fractional positions and $0.24 of cash. Reviewed through the fee model:

| Position | Value | Weight | P&L | Exit fee | Fee as % of the position |
| --- | --- | --- | --- | --- | --- |
| MSFT | $39.39 | 35.1 % | +23.4 % | $0.12 | 0.30 % |
| Visa | $32.68 | 29.1 % | +31.3 % | $0.10 | 0.31 % |
| Berkshire | $29.87 | 26.6 % | +20.0 % | $0.10 | 0.33 % |
| ExxonMobil | $10.11 | 9.0 % | +5.6 % | $0.10 | **0.99 %** |

```
$ trading-office gotrade --connect
connected  equity $112.29 · invested 99.8% · cash $0.24
  flattening the book costs $0.42 (0.37% of equity)
  no new entry  cash available is $0.24, below the $1 minimum a fractional order needs
  stops  the API has no modify endpoint: take_profit/stop_loss exist only on the create
         request, so an existing position cannot be given a stop
```

Four facts a US bot on this account has to start from:

1. **The book is 99.8 % invested.** There is no cash to enter with, so "buy when the trend
   turns" cannot happen until something is sold. Any bot that ignores this will simply report
   `400 insufficient buying power` forever.
2. **The stops are not there, and cannot be added.** `take_profit` / `stop_loss` exist only on
   the *create* request; there is no modify endpoint. Protecting these four positions means
   selling and re-buying with a bracket, which spends a second round of fees ($0.84).
3. **The XOM position is fee-dominated**: $10.11 with a $0.10 exit is 0.99 %, three times the
   headline rate. Positions under ~$333 always pay the floor rather than 0.3 %.
4. **The upside is real but comes from beta, not from timing.** +20 % to +31 % on mega-caps is
   what a rising market did to a buy-and-hold book; it says nothing about whether a bot can
   reproduce it. Reproducing it with 0.6 % round-trip friction needs a *slower* strategy than
   the one this repo runs for crypto — weekly decisions, not 15-minute ones.

### What a US strategy on this account can honestly be

| Option | Fits? | Why |
| --- | --- | --- |
| Scalping / intraday | no | 0.6 % round trip needs >40 % win rate on tiny moves, and cash is zero |
| Swing with brackets (hold days–weeks) | yes | the fee is amortised over a 5–10 % move; brackets are allowed in the regular session |
| Buy-and-hold rebalancing bot | yes | quarterly trades, fee irrelevant, but it is portfolio management, not "bot trading" |
| Protective stops on the current book | no | the API cannot attach a stop to an existing position |

## Market data

The docs say market data exists (1 price endpoint + 3 market-data endpoints) but do not publish
their paths. Until a representative confirms them, `us` candles come from the repo's synthetic
provider and **every row says `synthetic`**. No live-looking chart is ever produced from fake
data.

## What is wired today

| Piece | State |
| --- | --- |
| `GotradeClient` (bearer auth, both entities) | done, tested against a stub that rejects a wrong token with 401 |
| `GotradeBroker` (submit / positions / close / cancel / equity) | done: refuses short, stopless entry, bracket in extended hours, duplicate `client_id` |
| `PARTIAL_FILL` handling | reported as a fill with `partial: true` — a non-terminal order that bought shares is a position and must be managed |
| `shares_for_risk()` (risk × cash × position cap, fee as a share of risk) | done |
| `readiness()` + `trading-office gotrade` | done: names the blockers, prints the fee arithmetic |
| `profiles.us` market wiring (engine / API / UI) | **not yet** — waiting on the two account blockers, because a US tab that cannot trade is decoration |

## Why the adapter exists before the account

The same reason the Exness one did: the rules (long-only, mandatory bracket, terminal states,
fee floor) are the part that is expensive to get wrong, and they can be proven offline against
a stub. When the ID and the API access are in place, the remaining work is a token in `.env` and
twenty paper trades — not a rewrite.
