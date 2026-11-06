# Autonomous operation (Telegram approval)

How to run this desk unattended: one process tree, one phone, and an explicit list of what
the bot may decide by itself.

## The three autonomy levels

`config/settings.json` → `autonomy.mode`:

| mode | live entry | who decides |
| --- | --- | --- |
| `ask` (**default**) | parked in a queue, pushed to Telegram + the web dashboard | you, per entry |
| `auto` | sent immediately — **only** when the venue is paper, or the execution gate is armed and its fingerprint still matches | the engine |
| `off` | sent immediately, no gate | nobody — paper only, please |

Paper trading is exempt from the queue on purpose: approvals for simulated fills teach
nothing and train you to tap Approve without reading.

## Arming live money (once, on purpose)

```bash
trading-office arm-live            # shows what would be granted, changes nothing
trading-office arm-live --yes      # grants live entries
trading-office disarm-live         # revokes immediately
```

The grant stores a **fingerprint of the execution gate's source code plus the risk limits
it enforces** (`trading_office/autonomy.py`). Change either one — a relaxed check, a wider
daily-loss limit, a lower minimum filter tally — and the grant stops matching: live entries
are refused until somebody re-arms on purpose. It is a change detector, not a sandbox: it
cannot stop an attacker who can already edit the repository, but it makes "somebody quietly
loosened the gate" impossible to do without leaving a mismatch.

What a live entry still has to pass, in this order:

1. four-gate market filter (trend, session/news blackout, ARA/ARB, spread & liquidity),
2. signal score ≥ `min_score`, risk plan with a hard stop and RR ≥ 1.5,
3. kill switch (daily loss on the **day-open mark**, loss streak),
4. autonomy gate (paper / armed fingerprint / approved),
5. bar lock (one decision per bar), one position per symbol, cooldown after exit,
6. execution-cost check against the frozen plan, venue precision rounding.

None of those steps is skipped by an approval — an approval is step 4, not a bypass.

## Telegram: approve, reject, panic

```env
TO_TELEGRAM_TOKEN=123456:AA...            # from @BotFather
```

```json
"alerts": { "telegram": { "chat_id": "424242" } }
```

`chat_id` is the **owner allowlist**. Only that chat can command the bot; anything else gets
one refusal and no information. Start the bot, send `/help`:

| command | effect |
| --- | --- |
| `/status` | mode, equity, guard, heartbeat, open positions, panic keyboard |
| `/pending` | re-send the approval cards |
| `/panic` | flatten everything (reduce-only), report anything that refused |
| `/resume` | clear today's kill switch |

Buttons arrive with each new entry:

```
Approval needed · SOL/USDT:USDT
side LONG · score 7
entry 198.43 · stop 195.49 · target 203.7
size 1.05 · risk 0.3% of equity · RR 3.0
why: trend_alignment +2, trigger +2, macd_confirmation +1
expires 2026-11-06T17:26:02Z
[✅ Approve] [⛔ Reject]
```

Security properties that are tested, not claimed (`tests/test_telegram_control.py`):

* **callback data is a pointer**, never a payload: `ap:APPROVE:<request id>:<nonce>` — 28
  bytes, so it also fits Telegram's 64-byte limit without ever encoding an amount or price.
  The bot never parses a quantity out of a message, so a clever message cannot become an order.
* **a nonce binds the buttons to the visible text**; an edited or copied card is refused.
* **an approval is single-use**: the request id resolves once (state store refuses a second
  transition), so a double tap or a replayed update cannot send a second order.
* **updates are offset-tracked** (`offset = last_update_id + 1`), so a restart does not
  re-run yesterday's approvals.
* **manual `buy`/`sell` is refused by design.** The bot cannot invent an order; it can only
  decide on plans the strategy produced.
* the token lives in `.env`; `/api/telegram` and `doctor` report presence only.

## Cron: the part autonomy usually forgets

```cron
# after the crypto day / IDX close — one screen you can actually read
15 0 * * *  cd /opt/trading-office && .venv/bin/python -m trading_office.cli digest --send
# daily backup of the encrypted state (the only thing that cannot be regenerated)
30 0 * * *  cd /opt/trading-office && tar czf /var/backups/to-$(date +\%F).tgz state
```

`trading-office digest` prints risk, book, guard, skipped reasons and pending approvals, and
`--send` delivers it to the same Telegram channel. An autonomous bot nobody reads is
indistinguishable from one that stopped working three days ago.

### The digest talks to the running bot, not to a new process

`digest` asks `http://127.0.0.1:8000` (`/api/metrics`, `/api/engine/status`,
`/api/engine/kill-switch`, `/api/approvals`) for the numbers. That distinction is the whole
point: an engine constructed inside the digest process reports `cycles 0` and an equity
frozen at its own starting balance, because it is describing *itself*. A digest that is
wrong in the reassuring direction every morning is worse than none.

| Situation | What `digest --send` does |
| --- | --- |
| server answering | reports the live book, cycles, uptime, guard and approvals; sends |
| server down, no `--offline` | prints why and exits **1**; sends nothing |
| server down, `--offline` | reports local state, labelled `local state (server not reachable)`; sends |
| no token / chat id | prints `alert channel not configured` and exits **1** |

| Flag | Default | Use |
| --- | --- | --- |
| `--market crypto\|idx` | `crypto` | which book to report |
| `--api URL` | `http://127.0.0.1:8000` | server to ask (useful when it runs on another port) |
| `--send` | off | deliver to Telegram; without it the message only goes to the terminal |
| `--offline` | off | allow a local-state report while the server is unreachable |
| `--timeout` | `5` | give up on a wedged server instead of hanging a scheduled task |

Exit codes are cron-friendly on purpose: **0** printed/sent, **1** nothing was sent. A cron
mail on a non-zero exit is your second alerting channel, and it needs no credentials.

Windows Task Scheduler equivalent (on the Windows box that runs the bot). The scheduler
starts in `C:\Windows\system32` and reads no shell profile, so `cd` into the repo first —
both `config/settings.json` and `state/` are resolved relative to it:

```bat
schtasks /Create /TN "TradingOffice digest" /SC DAILY /ST 00:15 /RU "%USERNAME%" ^
  /TR "cmd /c cd /d C:\Trading-Office && .venv\Scripts\python.exe -m trading_office.cli digest --send"

:: 0 = the digest was delivered; anything else = it was not (see the table above)
schtasks /Query /TN "TradingOffice digest" /V /FO LIST | findstr /i "result"
:: run it right now instead of waiting until 00:15
schtasks /Run /TN "TradingOffice digest"
```

The token (`TO_TELEGRAM_TOKEN`, alias `TELEGRAM_BOT_TOKEN`) must be a **user** environment
variable of the account the task runs as — `setx TO_TELEGRAM_TOKEN "123:abc"` once, then sign
out and back in — not something exported only in the terminal you happened to use. GUI route:
`taskschd.msc` → *Create Task* → *Actions* → **Start in** = `C:\Trading-Office`, and tick
*Run whether user is logged on or not*.

## What still needs a human

| Situation | Why it is not automated |
| --- | --- |
| Arming live entries | the one irreversible decision; bound to a fingerprint and a CLI flag |
| Changing risk limits | invalidates the arm, by construction |
| A strategy that failed OOS | no code can decide "this edge is gone"; the digest is the trigger to look |
| Corrupt/unreadable state | `StateLocked` stops the bot on purpose — do not add a fallback |
| An approval older than its TTL | the price it was computed at no longer exists |

## First-week checklist

1. `trading-office doctor` — providers, credentials, alerts, state encryption: all OK or
   knowingly accepted.
2. `trading-office backtest --market crypto --oos 0.35` — positive OOS on the symbols you
   intend to trade, with exit costs included.
3. `trading-office supervise` under the service manager; confirm the badge says **Healthy**
   and a `kill -9` of the child is followed by an automatic restart.
4. Live-in-paper for a week: `autonomy.mode=ask` still queues nothing because paper is
   exempt — so read the digest daily instead and compare it with the dashboard.
5. Only then: `arm-live --yes`, `mode=auto`, and start with the smallest size the venue
   allows. Re-arm after every deploy (the fingerprint will usually have moved).
