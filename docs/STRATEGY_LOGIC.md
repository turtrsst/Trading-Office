# Trading Office — Strategy Logic (read this before the code)

An Expert-Advisor style trading engine with one pipeline, two markets (IDX equities,
crypto perpetual futures) and four gates every order must pass:

```
[ Market Filter ] -> [ Signal Generation ] -> [ Risk Validation ] -> [ Order Execution ]
        (environment)        (setup)                (money)                (routing)
```

Everything below is implemented in `trading_office/strategy/`. Rules are deliberately
conservative: if a gate cannot be evaluated (missing candles, missing ATR, stale
data), the answer is **no trade**, never "assume it's fine".

---

## 0. Data rules that apply to every gate

| Rule | Meaning in code |
| --- | --- |
| **Closed candles only** | Indicators are computed on bars `[:-1]`. The live/forming bar is used only for the *price* handed to the execution layer. Crypto: the last bar of the exchange feed is dropped unless its close time has passed. IDX: the current session's daily bar is dropped while the market is open (`behind_session` / exchange clock). |
| **One decision per bar** | A signal is evaluated once, keyed by `(symbol, timeframe, bar_open_time)`. A state store (`state/signals.json`) refuses a second decision for the same key, so a flickering live candle cannot produce two orders. |
| **Duplicate-trade guard** | Three layers: (1) the bar key above, (2) at most one open position per symbol and per strategy, (3) a cooldown of N bars after a position closes on that symbol. |
| **Unseen data first** | Backtests run on a *locked* candles dataset and the parameter set is validated out-of-sample (walk-forward split). Live scanning only ever sees the same closed-bar pipeline. |

---

## 1. Market Filter — "may I trade this symbol at all?"

Four independent filters. `min_filters` (default **3 of 4**) must pass; the trend
filter is mandatory when `require_trend=true` because it is the directional bias —
without it the entry signals trade against the higher timeframe.

### 1.1 Trend direction (higher timeframe)

- EMA `trend_ema` (default 200) on the **higher timeframe** (`1d` for crypto intraday
  setups, `1wk` for IDX swing setups; configurable per market profile).
- ATR-relative tolerance: price must be more than `0.25 * ATR(14)` away from the EMA,
  so a stock hugging its 200 EMA is "no trend" instead of flipping long/short daily.
- `uptrend` if `close > ema + tol`, `downtrend` if `close < ema - tol`, else `flat`.
- Long entries require `uptrend`, shorts require `downtrend`.

### 1.2 Volatility filter (ATR band)

`atr_pct = ATR(14) / close * 100` on the trading timeframe.

- **Too low** → dead market, spread and commission eat the edge: block when
  `atr_pct < atr_min_pct` (crypto `0.25 %`, IDX `0.8 %`).
- **Too high** → news spike / liquidation cascade: block when
  `atr_pct > atr_max_pct` (crypto `6 %`, IDX `9 %`).
- Additionally `atr_pct` must be inside [`0.6`, `2.5`] × its own 100-bar median, which
  catches regime shifts even when the absolute band still passes.

### 1.3 Time / session window

- Both markets: trading enabled only inside configured windows, expressed in the
  market's local timezone:
  - Crypto filter (default): `07:00–10:00` and `13:00–22:00` **Asia/Jakarta** — the
    London open and the London/New York overlap, i.e. the hours with real volume.
  - IDX: `09:15–11:30` and `13:30–15:45` Asia/Jakarta (session I and II, skipping the
    opening auction noise and the closing auction).
- Weekend/holiday guard: crypto is 24/7 but *entries* can be disabled on
  Saturday/Sunday (thin books, wide spreads); IDX uses the exchange holiday calendar.
- **Auction windows (IDX):** `execution_costs.auction_windows` defaults to
  `08:45–09:15` (pre-opening + opening auction) and `15:45–16:15` (pre-closing +
  closing auction). No entries inside them: the book is indicative only, so the price
  you see is not a price you can trade. Crypto has no auction, only the session
  windows above.
- **News blackout:** a static list of high-impact windows (`news_blackout`, ISO
  timestamps + minutes-of-day rules) is honoured; the live engine also drops any
  symbol whose data is `stale` (IDX provider warning, crypto feed older than
  `max_tick_age_s`).

### 1.4 Spread & liquidity filter

- Crypto: `spread_bps <= max_spread_bps` (default **8 bps**), and depth check
  `order_notional <= 0.5 %` of the last `N` bars' median quote volume.
- IDX: `spread_bps <= max_spread_bps` (default **25 bps** because IDX tick sizes are
  coarse), plus a **tick-size sanity check** so the computed SL/TP are at least
  `min_ticks_away` (default 3) ticks from entry after rounding to the IDX tick table,
  and minimum traded value `min_daily_value_idr` (default 5 B IDR) for liquidity.
- If the venue gives no book (delayed IDX data), the filter falls back to
  `estimated_spread_bps = 2 * tick_size / price` and marks the result `estimated`.
- **Two-level cap.** The normal cap above blocks entries in a merely quiet book; a
  wider **stress cap** `risky_spread_cap = max_spread_bps × extreme_spread_mult`
  (1.6× crypto, 1.6× IDX) is what the gap guard uses to recognise a shock. Between the
  two, entries are allowed but the *size* is scaled down (see 3.5).

### 1.5 Gap & black-swan tolerance

A stop is a request, not a promise: on a gap the market opens through it and the fill
is worse than the stop, so the realised loss can exceed the 1 % risk budget. Three
defences, all in `trading_office/costs.py`:

| Defence | Rule | Default |
| --- | --- | --- |
| **Gap rejection** | `abs(open − prev_close) / ATR >= gap_reject_atr` → no entry, cooldown | crypto 2.0 ATR, IDX 1.5 ATR |
| **Spread shock** | `spread_bps >= gap_reject_spread_mult × max_spread_bps` → no entry, cooldown | 3.0× crypto, 2.5× IDX |
| **Cooldown** | After a rejection the symbol is blocked for `gap_cooldown_bars` cycles | crypto 3, IDX 2 sessions |
| **Gap through a stop** | If a bar gaps past a resting stop, the exit fills at `stop + f × (open − stop)`, `f = gap_fill_fraction` | 0.5 crypto, 0.75 IDX |

The backtest applies the same table, so a gap loss shows up in the equity curve instead
of being hidden by an optimistic "filled at the stop" assumption, and
`metrics()["gap_stops"]` counts them.

---

## 2. Signal Generation — "is there a setup right now?"

Confluence scoring, evaluated **only on the last closed bar** and only after the
market filter passes. Each component is boolean, has a weight, and records a reason
string that is stored with the trade so it can be audited later.

| Component | Long condition | Short condition | Weight |
| --- | --- | --- | --- |
| Trend alignment | `close > ema50 > ema200` | `close < ema50 < ema200` | 2 |
| Trigger (RSI / BB) | `RSI(14)` crosses up out of `< 35`, **or** previous bar closed below the lower Bollinger band and this bar closes back inside | mirror | 2 |
| Confirmation (MACD) | `macd > signal` and histogram rising for 2 bars | mirror | 1 |
| Momentum health | `50 < RSI < 70` (long) / `30 < RSI < 50` (short) | mirror | 1 |
| Bollinger mid reclaim | close crosses above `bb_mid` | crosses below | 1 |
| Candle structure | bullish engulfing or lower-wick ≥ 2× body | bearish engulfing or upper-wick ≥ 2× body | 1 |
| Volume expansion | `volume > 1.2 × SMA(volume,20)` | same | 1 |

- Entry requires `score >= min_score` (default **5 of 9**) and **no opposing
  component overweight**: the trigger *or* the confirmation must be present, so a
  pure trend-following score can never fire an order.
- Overextension veto: long blocked if `RSI > 75` or close is above the upper band by
  more than `0.5 * ATR`; short mirrored. This kills the "buy the top" failure mode of
  the original script.
- Conflict veto: if long and short scores are both ≥ `min_score - 1`, the bar is
  `WAIT` (indecision), not a coin flip.

### 2.1 State locking

`SignalStateStore.lock(symbol, timeframe, bar_time)` returns `False` the second time
the same bar is evaluated, so a 15 s polling loop cannot re-enter. The lock is
persisted (`state/signals.json`) and survives restarts. The live candle's OHLC can
change as much as it likes; the decision was made on the closed bar and cannot be
revised.

---

## 3. Risk & Position Sizing — "how big, and where do I get out?"

### 3.1 Structural stop loss (mandatory)

Every order carries an SL. Two candidate stops, the **wider** one wins so both
structure and volatility are respected:

1. **Structure**: last `swing_lookback` (default 10) swing low (long) / swing high
   (short) on the trading timeframe.
2. **Volatility**: `entry ∓ atr_sl_mult * ATR(14)` (default **1.5×**).

Then clamp: `sl_distance >= max(spread * 3, 2 ticks, 0.35 * ATR)` — no stop inside
the noise. Rounded to the exchange tick size / price precision.

### 3.2 Take profit and reward-to-risk

- Structural target: recent swing high (long) / swing low (short) or the Bollinger
  outer band, whichever is nearer.
- `tp_distance = max(structural_distance, rr_target * sl_distance)` with
  `rr_target` default **2.0** (minimum enforced 1.5). If the structural target cannot
  reach 1.5R, the trade is **rejected** — never widened to fit.
- Rejection also if `tp` would sit beyond a round-number free zone / 3×ATR cap, which
  usually means the target is fantasy.

### 3.3 Dynamic lot sizing

```
risk_cash        = equity * risk_per_trade_pct / 100          # 1 % default, hard-capped
risk_per_unit    = |entry - sl|                               # after tick rounding
size_units       = risk_cash / risk_per_unit
notional         = size_units * entry
notional        <= equity * max_leverage * leverage_cap_share # margin guard
notional        <= 0.5 % of rolling median quote volume       # liquidity guard
```

- Crypto: units rounded down to the exchange `amount` precision, minimum notional
  check, margin = notional / leverage; if margin exceeds `max_margin_per_trade_pct`
  of equity, size is scaled down (the original script's fixed `15 USDT × 3x` is
  replaced by this).
- IDX: rounded down to **lots (100 shares)**, and rejected if the minimum lot's risk
  exceeds the risk budget (cash account, no leverage — `max_leverage = 1`).
- Portfolio guard: total open risk ≤ `max_portfolio_risk_pct` (default 4 %), max
  concurrent positions `max_open_positions` (default 4 crypto / 6 IDX), per-symbol
  exposure ≤ `max_symbol_exposure_pct`.

### 3.4 Cost sanity

Before an order is allowed, expected profit must beat costs:
`tp_distance >= (spread + 2 * commission_bps + 2 * slippage_bps) * entry * cost_multiple`
(default `cost_multiple = 3`). A setup whose target is inside the cost envelope is
skipped, not executed — this is the gate that kills most "profitable in theory" bots.

### 3.5 Stress sizing

Volume-weighted risk is not constant through the day. While the book is stressed the
budget is *divided* by a multiplier (distance unchanged, size smaller):

```
stress = clamp(2 × (spread / max_spread − 0.5), 0, 1)        # starts at half the cap
mult   = 1 + 0.5 × stress + 0.25 × (ATR% excess over atr_soft_cap_pct),  clamped
```

`max_risk_multiplier` caps it at 1.75× (crypto) / 1.5× (IDX), and the plan records
`risk_scale_{m}x` in `caps_applied` plus a human `risk_note`, so a shrunken position is
never mistaken for a bug.

### 3.6 Execution-time cost veto

The plan is priced on a closed bar; the fill happens a few hundred milliseconds later.
The live engine re-prices the fill with the same model before submitting and **defers**
the entry (the bar lock prevents a second attempt on that bar) when either

- the spread is above the stress cap, or
- `live cost % > planned round-trip cost % × (1 + execution.max_cost_drift_pct)` (40 %).

Deferrals are written to the ledger as `cost_deferred` with the numbers that caused
them.

---

## 4. Trade Management — "what happens after entry?"

Managed by the engine on each closed bar plus, for trailing, on ticks:

Rule order on every closed bar (first match wins, several can fire in one bar — a partial
and its break-even move are emitted together):

| Rule | Detail |
| --- | --- |
| **1. Hard bracket** | The resting SL/TP are checked first; a gap through the stop uses the gap fill above. |
| **2. Partial take profit (mandatory)** | At `partial_tp_r` (default **2.0R**) close `partial_tp_fraction` (**50 %**) with a **reduce-only** order. `partial_done` is persisted before the next bar, so a restart can never sell the same half twice. Skipped (never rounded up) when half the position is below the venue minimum / `amount_to_precision` step. |
| **3. Break-even + costs** | In the same action list: SL → `entry ± (fees + spread buffer)`, i.e. strictly better than entry. That is what makes the runner risk-free. There is no `breakeven_at_r` trigger any more — the move is part of the partial, not a separate hope. |
| **4. Runner cap (optional)** | If `runner_target_r` is set, the remaining half exits there; left `null` the trade keeps running and the trail decides. |
| **5. ATR trailing (ratchet-only)** | After `trail_start_r` (1R): `HighestHigh − trail_atr_mult × ATR` (long) / `LowestLow + trail_atr_mult × ATR` (short), `trail_atr_mult = 1.5`. The stop only ever moves in the trade's favour, and only when the improvement exceeds `trail_step_atr × ATR` (0.25) so it is not rewritten on every tick. |
| **Time stop** | Force-close after `max_bars_in_trade` (default 96 × 15 m ≈ 24 h crypto; 15 sessions IDX) if the trade never reached +0.5R. |
| **Session / rollover exit** | Crypto: optional flat before the weekend funding window. IDX: flat before the close (`close_before_minutes`) since IDX has no overnight stop protection. |
| **Invalidation** | If an opposite signal reaches `min_score` while the trade is open, exit at market on the next tick (early exit before hard TP/SL). |
| **Kill switch** | Daily loss ≥ `max_daily_loss_pct` (2 %), or `max_consecutive_losses` (4) → no new entries until the next session/day. |

---

## 5. Execution

- Crypto: `ccxt` (bybit/binance/bitget/okx), `swap` market orders with attached
  `stopLoss` / `takeProfit` trigger prices (exchange-side brackets, so a dead bot
  cannot leave a naked position). Testnet is the default (`MODE_TESTNET = true`).
- IDX: paper broker only (`execution/idx_broker.py`) — the IDX data path is delayed
  and there is no public retail order API. Orders are simulated against the last
  closed price with tick rounding, spread crossing and commission (`0.15 %` buy,
  `0.25 %` sell incl. 0.1 % sales tax).
- Every attempt is written to an audit ledger (`state/ledger.jsonl`) with the score,
  reasons, gate results and the sizing math, so "why did it buy?" is answerable.
- Idempotency: client order IDs are `to-{symbol}-{bar_time}-{side}`; a retried order
  with the same ID is refused by the executor and by the exchange where supported.
- **Partials are reduce-only** market orders sized through `amount_to_precision()`
  (`broker.amount_step()`), and the remaining stop is moved with `edit_order`, falling
  back to cancel-and-recreate when the venue does not support edits.
- **Startup reconciliation** (`TradingEngine.startup_sync()`, also
  `POST /api/engine/reconcile`, `python -m trading_office.cli reconcile`, MCP
  `reconcile_state`) runs before the first cycle and compares `state/positions.json`
  with `exchange.fetch_positions()`:

  | Situation | Action |
  | --- | --- |
  | State and venue agree | `restore` — venue entry/size win, `initial_stop`, `partial_done` and the trail high-water mark are kept |
  | Venue flat, state open | `close` — the trade left without us; it is recorded and cleared (no phantom position) |
  | Venue open, state missing | `untracked` — reported, and the symbol is blocked from new entries; `--adopt` re-attaches tracking, `--close-orphans` flattens instead |
  | Venue unreachable | `blocked` — no new entries until it answers |
  | Resting stop missing | `stops_missing` — flagged for a human/automated re-bracket |

  The report is persisted in the ledger and exposed in `GET /api/engine/status`
  (`status.reconciled`) and on the Live Engine tab.

---

## 6. Backtest, screening and "unseen data"

- `backtest/engine.py` walks closed bars bar-by-bar, feeds fills from the **next bar's
  open** (no look-ahead), and emits trades with R-multiples. It reports win rate,
  expectancy, profit factor, max drawdown, Sharpe, exposure, partials, gap-stops and the
  per-filter rejection counts (why trades were skipped).
- **Fill model** (identical to the live engine, `trading_office/costs.py`):

  ```
  latency_drift   = (open − signal_close) / close × (latency_ms / bar_ms)
  fill            = open × (1 + latency_drift)
  slippage_bps    = base + atr_mult × (ATR/price × 10⁴) + impact_coeff × √(participation %)
                    capped at max_slippage_bps
  participation % = notional / median(quote volume, 20 bars)
  ```

  Crypto: 400 ms latency, 1.5 bps base, 0.06 × ATR%, 1.5 × √impact, 45 bps cap.
  IDX: 900 ms, 2 bps, 0.05, 2.5, 80 bps. Every fill emits a `cost_event`, which the
  result aggregates into `metrics()["costs"]`:

  | Key | Meaning |
  | --- | --- |
  | `slippage_bps.p50 / p90 / p100` | realised slippage distribution |
  | `slippage_capped` | fills that hit `max_slippage_bps` (a size warning) |
  | `latency_adverse_bps` | how much the 400/900 ms delay moved fills against us |
  | `gap_guards`, `gap_examples` | entries refused by the gap/spread guard |
  | `partial_fills`, `partial_pnl` | banked 2R partials |

  `POST /api/backtest` returns the same block, so the numbers are inspectable, not
  folklore.
- Walk-forward: parameters are chosen on the first 70 % of the locked dataset and
  reported on the last 30 % (`--oos`), which is the only number that counts.
- The IDX screener (`market/idx_client.py` + `strategy/screener.py`) runs the same
  pipeline over a universe and produces ranked **BUY / WATCH / SELL** lists with the
  reasons, plus bandarmology, broker summary, foreign flow, KSEI ownership and
  corporate actions pulled through the `idx` CLI/MCP server.

> This is engineering software, not financial advice. The paper/backtest numbers are
> simulations and past performance does not imply future returns.
