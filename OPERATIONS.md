# Operating SteadyStack

The runbook: what happens each day, what the operator does, how to read what
the system did, and what to do when something goes wrong.

Setup is [`SETUP.md`](SETUP.md); deployment is [`DEPLOY.md`](DEPLOY.md).
On the production box, commands below are run as the app user — the `tx`
helper in [`SETUP.md` §2.1](SETUP.md#21-getting-a-shell) does that. On a
laptop, read `tx` as `uv run python manage.py`.

---

## 1. The trading day

All times IST. Weekdays that the loaded calendar says trade; weekends never
trade whatever a calendar row says.

| Time | What runs | Unit / command | What it does |
|---|---|---|---|
| 09:00 | Session check | `session-check.timer` → `check_kite_session` | Proves the stored Kite session against Kite. None → alarm `no_session` while there is still time to log in. Quiet on a weekend or exchange holiday. Also checks calendar coverage: under 60 days warns, under 30 days (or empty) is an ERROR → `task_failed` |
| 09:05 | Balance check | `balance-check.timer` → `check_balance` | Compares Zerodha's opening balance for the day against what the books held at yesterday's close. See §1.4. No session → prints why and exits quietly; the 09:00 check already alarmed on that |
| 09:45 | Tradesheet | `tradesheet.timer` → `generate_tradesheet` | Ranks names by gap (current weight − target), walks the ranking and plans a line for **every name the cash reaches**, each up to its target. Sized at the ask, or the previous close if nobody is offering. Holds back a reserve when a starved leader is unaffordable. A live day spends no more than Zerodha holds: the budget is capped at Kite's `margins` `live_balance`, or -- if that read fails or the field is missing -- at the books' own Zerodha cash (1100 + 1110 + 1200 + 1210), and the sheet's snapshot (`cash_cap`) and the log name which. The sheet also records the book it was planned in; the day settles into that book's row whatever the mode is by 15:45. No session → holdings cannot be read, so no sheet is written (`task_failed`); a Kite login later, while the market is open, plans the day then |
| — | **Authorize** | You, on Today | Nothing is placed until the latest version is authorized. A regenerated version needs authorizing again |
| 09:45–14:00 | Placing rounds | `ladder.timer` (every 30 s) → `ladder_tick` | See §1.1 |
| 14:00–15:00 | — | — | No new orders; what rests has an hour to fill |
| 15:00–15:25 | The close | `fallback.timer` (every 30 s) → `market_fallback` | See §1.2 |
| 15:45 | End of day | `eod.timer` → `settle_day` | Reconciles with the broker, writes the day's cash-ledger row, carries the unspent cash forward, sets the day's final state. Not caught up after a boot: `settle_day` without `--date` refuses before the close, and a day with no tradesheet is refused with its claim left for a later settle |
| boot | Settle catch-up | `settle-catchup.service` → `settle_day --previous --if-unsettled` | A box that was down at 15:45 settles, once it is back, the latest trading day whose close has passed -- today after its close, else the trading day before; a no-op when that day has its row. When that day has no tradesheet (the 09:45 run was missed too), it settles the trading day before instead if that day has a sheet and no row; an unsettled day further back is named (`settle_day --date <day> --retry`) and the unit fails, so it alarms; with neither, the day is reported and left unclaimed (exit 0). Waits up to two minutes for the order lock rather than giving up, since nothing retries it. It reaches one day only: a box down across two trading days settles the older one by hand (`settle_day --date <day> --retry`). A past live day with orders still open is refused, never levelled -- import its tradebook (§7) |
| 16:15, 17:30, 20:00, 22:30 | Settle retry | `settle-retry.timer` → `settle_day --if-unsettled` | Settles the day unless its row exists, releasing a failed settle's claim first; a no-op once the day has settled. A live day must settle before midnight (§7) |
| 01:00 | Backup | `backup.timer` → `infra/bin/backup.sh` | `pg_dump`, verified, to S3 — every day including weekends |
| always | Tick feed | `steadystack-ticker.service` → `run_ticker` | The Kite websocket. Fills paper orders; resubscribes every 30 s to names ordered since it connected |

Every order-touching command takes the order lock itself
(`/var/lib/steadystack/orders.lock`, `ORDER_LOCK_PATH`), and every run is claimed
in the database on `(trading_date, task, stream, tick_no)`. A round that runs
twice places nothing the second time. On a day with live orders, each round
and each close round first reads Kite's order book, so a lost postback costs
at most thirty seconds, and then Kite's trade book, recording each new fill
with its lot and journal entry -- a live day's fills land while it runs, and
the evening settle only confirms them.

Every trading day has a trace id `ts-YYYY-MM-DD`, stamped on every log line,
audit event and broker call for that day.

### 1.1 Placing rounds (09:45–14:00)

Every 30 seconds, for each name on the authorized tradesheet:

1. **Stale?** Its limit order has rested unfilled for `order_ttl_minutes`
   (default 5) → cancelled, and its shares go back into the schedule. Placed
   again next round at the current price. (Not in the last round before
   14:00.)
2. **Resting?** An order is still out → nothing this round.
3. **Done?** Bought its full quantity → nothing, and no row is written.
4. **Due?** Its schedule — an even spread of its quantity from 09:45 (or from
   authorization, if later) to 14:00 — says it should have more committed by
   now → it owes the difference.
5. **Price:** at the bid while on schedule; toward the ask in proportion to
   how far behind its *fills* are, reaching the ask `catch_up_minutes`
   (default 30) behind; never outside ±0.75% of the last trade; never above
   the ask.
6. **Rate:** at most `max_orders_per_window` (5) orders per
   `order_rate_window_seconds` (30) across the whole system. Most-behind names
   go first; the rest wait a round ("waiting its turn").

Orders are never modified. A stale one is cancelled and a new one placed.

### 1.2 The close (15:00–15:25)

- The first round cancels every limit order still resting and waits for each
  cancellation to be confirmed, then re-reads fills from the broker.
- Each round buys `ceil(remainder ÷ rounds left)` of each name **at market**,
  within the same rate limit, most still-to-buy first.
- A name whose last traded price is more than `fallback_band_pct` (5%) from
  its planned reference is **withheld** — nothing more of it is bought today;
  an alarm fires once.
- Every market order carries `market_protection` = the band; the exchange
  rejects one without it.
- Run by hand before 15:00 (e.g. `run_day.sh --settle` at noon), a close
  round is the only round there will be and buys everything left.

### 1.3 Day states

| State | Meaning |
|---|---|
| `DRAFT` | Planned, nothing placed yet |
| `AWAITING_SESSION` | Planned without a Kite session; parks until login |
| `RUNNING` | Placing |
| `FILLED` / `PARTIAL` / `UNFILLED` | Final, set by the close and confirmed at settlement |
| `HALTED` | Three broker rejections in a row. Terminal: places nothing more, not even at the close |
| `SKIPPED` | Bought nothing, with a reason: weekend, holiday, no session by the cutoff, reserving for a starved leader, nothing affordable, empty universe, the operator, paused, never authorized, blocked by a divergence |

Carry is always computed from actual fills: an unfilled day carries the whole
amount, a partial day carries the remainder.

### 1.4 The morning balance check (09:05)

`check_balance` compares Kite's `margins()` opening balance for the day
against `zerodha_cash` as the books held it at yesterday's close, and writes
one `BalanceCheck` row per day (append-only, never revised — a difference is
recorded and alarmed, never quietly balanced away). The last five checks are
on **Books** (`/books/`). Until the live book is opened (`open_books`, §10)
it holds only part of Zerodha's cash, so the check records nothing and
prints "live book not opened"; Today's funding warnings stay silent too. It
also records nothing on or before the opening entry's own date, which
yesterday's close would leave out -- so `open_books --on` is the last
trading day before go-live, and go-live morning is the first one checked.

The field read is `margins()["equity"]["available"]["opening_balance"]` — the
documented shape, but **unverified**: as of this build no `user.margins` call
has ever been made against the real API, so nobody has checked it against a
live response and a Console statement's own morning balance. Confirm it the
first live morning; if it disagrees, `journal.checks.zerodha_opening_balance`
is the one place to change, to `cash` or `live_balance` instead.

- **Agrees within a rupee** — recorded, nothing more. The rupee tolerance
  exists because the rate card and Zerodha round a day's charges differently
  until the statement's settlement line corrects them.
- **`zerodha balance disagrees with the books`** — the two are more than a
  rupee apart. This is the `task_failed` alarm (it logs an ERROR on
  `steadystack.journal`, like any other task failure). **First thing to do:**
  Books → **Upload a statement**, with the latest Console funds statement,
  then work the queue it leaves — most disagreements turn out to be a
  settlement line, a charge correction or a dividend that has not been
  matched yet. If the queue is clear and the disagreement persists, the
  difference is real money and needs tracing by hand (an external transfer
  made outside this system, or a broker-side adjustment) before the next
  session.

Every trading morning also carries **funding warnings** on Today, computed
rather than logged: money the engine has been allocated but that has not
actually reached Zerodha (`journal.cash.allocate` outran a transfer), and
engine carry that a quarterly settlement swept, or that the Kite Connect fee
was paid from, and that a contribution has not yet restored
(`journal.cash.carry_owed`; only what left after the opening entry counts).
Both read quietly until they are true, so seeing
one is itself the alert — there is no separate alarm for either.

---

## 2. The operator's day

### 2.1 Every trading morning (before 09:45)

1. **Log in to Kite**: Settings → Kite session → Connect. The access token
   lasts until early the next morning; this is daily. If the 09:00 alarm
   fired, this is why.
2. After 09:45, open **Today**. Read the tradesheet: which names, how many,
   at what price, what it will cost, what is held back.
3. **Authorize** it — or adjust or skip first (§4). Authorizing is the only
   other thing an ordinary day needs.

### 2.2 During the day (optional)

- **Orders** (`/orders/today/`): every name's progress (WORKING / COMPLETE /
  DONE), the blotter, and the rounds. Filters keep your scroll position.
- A name's page (`/orders/<date>/<symbol>/`): depth ladder, its orders, the
  rounds that concerned it, fills, and every broker call.
- **Today** shows where the clock is: "round 44 of 509 · next 10:07:30".

### 2.3 After 15:45

- **Ledger → Cash**: the day's row — opening carry, contribution, spent,
  charges, closing carry — reconciles.
- **Holdings** and **Ledger → Shares** agree with the broker, or there is an
  open divergence to classify (§5.3).

### 2.4 Weekly / monthly / yearly

| When | What |
|---|---|
| Weekly | Glance at alarms, the backup objects in S3, disk use |
| Monthly | **Test a restore** (§9). Check the Kite market-data subscription is paid |
| Monthly | Upload the Console funds statement and work the queue it leaves (§10) |
| Quarterly | After the first-Friday settlement sweeps the account, upload that evening's Console statement and confirm the sweep, then transfer the carry it took back to Zerodha before the next session (§10). A live plan is capped at what the account holds either way, but the books only know of the sweep once its line is confirmed |
| After a Budget, exchange circular or GST change | Check the charge rates in `src/portfolio/charges.py` against a contract note; the tests that pin them will need updating |
| December | Load next year's NSE calendar (`load_calendar`); `check_calendar` / `preflight` warn when coverage is low; under 30 days the 09:00 session check raises `task_failed` every weekday |
| Yearly | Re-confirm the static IP registration and API app settings with Zerodha; review the model portfolio |
| Yearly | Once the chartered accountant signs off a financial year, close it on Books (§10) |

---

## 3. Command reference

| Command | Purpose | Useful flags |
|---|---|---|
| `preflight` | State of the installation; non-zero exit if something blocks a day | `--quiet` (problems only) |
| `check_kite_session` | Is there a usable session | `--offline` (trust stored expiry) |
| `check_balance` | Zerodha's opening balance against the books (§1.4) | |
| `refresh_instruments` | Pull the instrument master from Kite | `--exchange`, `--actor` |
| `generate_tradesheet` | Plan the day | `--date`, `--carry-in`, `--actor`, `--allow-closed-market` (testing), `--skip-holdings` |
| `authorize_tradesheet` | Authorize the latest version | `--actor NAME` (required) |
| `ladder_tick` | One placing round of the daily buy | `--date` (`--stream` exists but only the daily buy is implemented) |
| `market_fallback` | One round of the close | `--date` |
| `rebalance_tick` | One round of every running rebalance batch -- the sell ladder, or from 15:00 the close | `--date` |
| `settle_day` | Sweep what still rests, close the day's batches, settle, write the ledger row, reconcile. Without `--date`, today -- refused before the close | `--date`, `--previous` (the latest trading day whose close has passed: today after its close, else the trading day before; a day with no tradesheet is left unclaimed), `--stream`, `--retry` (release a failed settle's claim; refused once the row exists), `--if-unsettled` (the retry timer's: settle unless the row exists, a no-op once it does), `--trades-only` (with `--date`: only the settlement entry of a day a later row already counted, §7), `--level-unseen --actor NAME` (with `--date` or `--previous`: level a past day's open live orders from their recorded fills, once Console's tradebook shows none traded, §7) |
| `import_tradebook PATH` | Record a past live day's fills from Console's tradebook CSV, when the day did not settle before midnight (§7). Then `settle_day --date <day> --retry` | `--actor` (required), `--dry-run` |
| `snapshot_valuation` | The evening value point for Performance | `--date` |
| `run_ticker` | The tick feed (long-running) | `--once` (connect, report, exit) |
| `stop_trading` | Emergency stop: pause + cancel everything | `--reason`, `--actor` |
| `load_calendar PATH` | Load NSE sessions from CSV | `--actor` |
| `check_calendar` | How far ahead sessions are loaded | |
| `import_opening_positions PATH` | Existing holdings as immutable lots | `--actor` (required), `--source`, `--note`, `--partial`, `--dry-run`, `--empty` (no file: the account held nothing) |
| `open_books` | Post the live book's opening entry (go-live, once — DEPLOY.md §8) | `--cash` (that day's closing funds balance), `--on` (the last trading day before go-live), `--actor` (all required) |
| `enrol_totp USER` | TOTP device + recovery codes | `--name`, `--replace` |
| `seed_demo` | Fake history (DEBUG + paper only) | `--days`, `--reset` (**destructive**) |

Pages that do the same from the browser:

| Page | Path |
|---|---|
| Today | `/` |
| Holdings | `/holdings/` |
| Performance (XIRR vs benchmark, value series) | `/performance/` |
| Orders / a name's day | `/orders/today/`, `/orders/<date>/<symbol>/` |
| Cash ledger (lumpsum form at the top) / Share ledger | `/ledger/cash/`, `/ledger/shares/` |
| Books (trial balance, the journal, statement upload, the morning balance check) | `/books/` |
| Tradesheets (detail, overrides, regenerate, authorize) | `/tradesheets/` |
| Model portfolio | `/model-portfolio/` |
| Configuration, Stop trading / Resume | `/configuration/` |
| Kite session | `/broker/session/` |
| Instruments | `/instruments/` |
| Rebalance sells | `/rebalance/` |
| Divergences, cost basis, corporate actions | `/records/divergences/`, `/records/basis/`, `/records/corporate-actions/` |
| Account security (replace authenticator) | `/accounts/security/` |
| Health (no sign-in) | `/health/` |

---

## 4. Changing the day

Every change below is audited with actor, time and reason.

| I want to… | Do this | Allowed until |
|---|---|---|
| Replan from fresh prices | Today → **Regenerate** | The first order is out. Clears authorization and overrides |
| Point the rest of a day that has bought at a new plan (e.g. a new model portfolio) | **Stop today**, then Tradesheet → **Replan the rest**, or `manage.py replan_tradesheet --reason "..."` | Paper only. Something has filled, nothing is still resting, before 14:58, not settled. The new version is net of what was bought and spent, and needs authorizing |
| Plan today when the 09:45 timer did not | Today → **Generate today's tradesheet** | Market hours only |
| Buy more or fewer of one name, or skip it | Tradesheet → adjust the line / skip | Raising is limited by the day's cash; lowering cancels the newest orders first; can't go below what filled |
| Skip the whole day | Tradesheet → skip | Before anything has filled |
| Keep one round from placing | Tradesheet → exclude round *n* | The round has not run |
| Add money (bonus, gift) | Ledger → Cash → **Lumpsum** | Lands in today's cash until orders are placed, else tomorrow's. Regenerate to spend it today |
| Change daily amount, clock, pricing, rate limit | Configuration | Any time; applies from the next thing that reads it. Each save is a new version with a reason |
| Change target weights | Model portfolio → upload CSV → review diff → store | Takes effect from the next tradesheet |
| Sell shares | Rebalance → upload `symbol,quantity` (template at `/rebalance/template.csv`) → review → **Execute** within 15 minutes of the upload | Paper only. After the day's 09:45 plan exists, before the close ends, and never for a name the day is buying. It sells through the placement window (at most one order a round, before the buy's), the rest at market from 15:00. When the batch closes (day end, Cancel, or stop) its net proceeds become a `reinvest` allocation to the engine, dated that day. They are the next day's money: the day they arrive, Regenerate, Replan and the cash line leave them out, even when a batch is cancelled or stopped mid-day. Untick *reinvest* on upload to leave them unallocated; the batch page says which it is before Execute. The day's cash-ledger row shows them as sale proceeds. Cancel on the batch page stops it |

### 4.1 Configuration fields that affect execution

| Field | Default | Effect |
|---|---|---|
| `daily_amount` | ₹1,000 | New money per trading day |
| `max_price_per_share` | unset | Names above it leave the universe |
| `reserve_threshold_days` | 5 | Days a leader may be unaffordable before cash is reserved for it |
| `tradesheet_time` | 09:45 | When the day plans; round 0 |
| `placer_interval_seconds` | 30 | Round cadence (the timer must match) |
| `placement_window_close` | 14:00 | Last placing round before this |
| `market_fallback_time` | 15:00 | First close round |
| `close_window_end` | 15:25 | Last close round |
| `slice_quantity` | 1 | Minimum shares per order |
| `catch_up_minutes` | 30 | Behind by this much → pay the ask |
| `order_ttl_minutes` | 5 | Resting this long unfilled → cancel and reschedule |
| `fallback_band_pct` | 5.00 | Sanity band for market orders; also their market protection |
| `max_orders_per_window` / `order_rate_window_seconds` | 5 / 30 | System-wide order rate; the breaker trips above it |
| `order_mode` | paper | `live` is refused unless the static IP is registered and matches |
| `registered_static_ip` | unset | The address registered with Zerodha |
| `ladder_interval_minutes`, `ladder_patience` | 5, 3 | Rebalance sell ladder only |

Changing `placer_interval_seconds` needs the systemd timers (`ladder.timer`,
`fallback.timer`) changed to match.

---

## 5. Reading what happened

### 5.1 Logs

On the box, one JSON object per line, in `/var/log/steadystack/`:

| File | From |
|---|---|
| `web.log` | gunicorn / Django |
| `ticker.log` | the tick feed |
| `tradesheet.log`, `ladder.log`, `fallback.log`, `eod.log` (and `settle-retry`), `session.log`, `books.log`, `backup.log` | each timer's unit |
| `nginx-access.log`, `nginx-error.log` | nginx |

All shipped to CloudWatch log group `/steadystack/app` (365 days). Reconstruct a
day end to end:

```sh
aws logs filter-log-events --region ap-south-1 --log-group-name /steadystack/app \
    --filter-pattern '{ $.trace_id = "ts-2026-09-23" }' --output text
```

Or with Logs Insights:

```
fields @timestamp, logger, message, symbol, quantity, price, reason
| filter trace_id = "ts-2026-09-23"
| sort @timestamp asc
```

systemd's view of a unit: `journalctl -u ladder.service --since today`,
`systemctl list-timers`.

### 5.2 In the database / UI

- **Tick rows** (the Ticks table on Orders, a name's *By tick* tab): every round that
  placed, and every change in a name's answer — resting, on schedule, waiting
  its turn, locked book, withheld. A round that finds everything as before
  writes nothing new; the row in force explains it.
- **BrokerCall**: every Kite call with request, response, latency, status,
  credentials redacted — a name's page, *Broker calls* tab.
- **Audit events**: every override, authorization, configuration version,
  stop, resume, expiry of a stale order.
- **Task runs**: every scheduled run and whether it succeeded, keyed by
  `(trading_date, task, stream, tick_no)`.

### 5.3 Divergences

Every evening holdings are compared with the broker. A disagreement opens a
divergence at `/records/divergences/`, to be classified by you — never
absorbed:

| Classification | Use when |
|---|---|
| Bought / sold outside this system | You traded in Kite directly. Record it as an external transaction so lots and cost basis stay right |
| Transferred in or out | Shares moved between demat accounts |
| A corporate action | Split, bonus, merger — record it under corporate actions; lots are superseded, originals kept |
| This system got it wrong | A correction entry supersedes, with a reason |

While a divergence is open, gains for that name are withheld, and a day can
be skipped as "blocked by an unresolved divergence".

The account is the adult's own demat, so it may hold shares that were never
the portfolio's. A holding with no lot that is not in the model portfolio is
not compared: the Divergences page lists it under "Held, but not the
portfolio's". If one of them is the portfolio's, import it
(`import_opening_positions`) or add it to the model portfolio.

In paper mode a classification that records a movement (bought, sold or
transferred outside) is refused: it would post to the live book, and a paper
evening's difference is not the real account's. Leave those open, or
classify one as a movement-free kind, until live resumes (DEPLOY.md §8).

---

### 5.4 Health and monitoring

**`/health/`** (no sign-in) answers **200** when everything that should have
happened by now has, and **503** when something has not. The body says which
check failed — booleans only, nothing about the portfolio:

```json
{"status": "failing",
 "checks": {"session": true, "tradesheet": true, "authorized": true, "rounds": false,
            "close": true, "feed": true, "settled": true},
 "session": true, "tasks_ran_today": true, "last_task_failed": false, "divergences_open": false}
```

| Check | Fails when | Stands down when |
|---|---|---|
| `session` | No valid Kite session after 09:00 on a trading day | Before 09:00, weekend/holiday, system paused |
| `tradesheet` | No tradesheet for today 10 min after the tradesheet time (09:55) | Before then, after the close, weekend/holiday, paused, a session that was not open at the tradesheet time (Muhurat) |
| `authorized` | Today's latest tradesheet is neither authorized nor skipped at 10:30 — a regenerated or replanned version counts afresh | Before 10:30, no tradesheet (the `tradesheet` check says so), otherwise as `tradesheet` |
| `rounds` | No successful placing round in the last 2 min while an authorized day is placing (09:47–14:00) | Not authorized yet, day skipped/halted/filled, no session (the session check says so), outside the window, paused, not a trading day |
| `close` | No successful close round in the last 2 min during 15:02–15:25 | Same as `rounds` |
| `feed` | In market hours: the tick feed's pulse is over 2 min old, it is disconnected, or it is watching names and has heard no tick for 5 min | Outside market hours, no session, paused, not a trading day. Watching nothing and hearing nothing is fine |
| `settled` | The last trading day that had a tradesheet has no cash-ledger row (today counts from 16:15) | That day had no tradesheet |

The tick feed writes its pulse to the database every 15 seconds
(`FeedHeartbeat`). It follows the Kite session: started before the morning
login it waits rather than exiting, and when a new day's login changes the
token it drops the old connection and makes a new one.

**Who watches it:** in production a Route 53 health check polls
`https://<domain>/health/` every 30 seconds from several regions and the
`steadystack-health-failing` alarm (us-east-1) emails when it fails twice running
— the one alarm that covers *silence*: a timer that never fired, a dead
feed, a box nobody can reach. Locally, `curl -s localhost:8000/health/`.

**Alarms**, all to `operator_email`:

| Alarm | Region | Fires on |
|---|---|---|
| `steadystack-health-failing` | us-east-1 | `/health/` 503 or unreachable (§5.4) |
| `steadystack-no_session` | ap-south-1 | The 09:00 check logged "no valid Kite session" |
| `steadystack-order_rejected`, `-day_halted`, `-fallback_withheld`, `-overspend`, `-divergence`, `-orphan_order`, `-unbooked_fill` | ap-south-1 | The matching log line (§7) |
| `steadystack-task_failed` | ap-south-1 | Any ERROR from a `steadystack.*` logger |
| `steadystack-no-settlement` | ap-south-1 | No "day settled" in 24 h (expected at weekends) |
| `steadystack-no-backup` | ap-south-1 | No "backup uploaded" in a day |
| `steadystack-instance-unhealthy` | ap-south-1 | EC2 status checks |
| `steadystack-disk-filling`, `steadystack-memory-high` | ap-south-1 | Disk > 80%, memory > 90% |

Two email subscriptions need confirming after the first apply: one per
region's alert topic.

## 6. Stopping and resuming

**Stop trading and cancel all orders** (Settings, red button, reason
required; or `tx stop_trading --reason "..." --actor <you>`):

1. Pauses the system first — no tradesheet, round or close runs.
2. Tries to cancel every open order, and reports any it could not confirm
   (those are still live — deal with them in Kite).
3. Closes any running rebalance batch (cancelled, or partial if it sold).
4. Fills already taken stand. Today's plan is left as it was.

**Resume trading** (Settings, same place, optional note): lifts the pause.

- Same day, before 14:00: the next round carries on with today's plan and
  authorization. Cancelled shares are owed again; names are now behind
  schedule, so the first rounds price nearer the ask and may place a larger
  catch-up order.
- After 14:00: the close buys what remains from 15:00.
- A closed rebalance batch stays closed; start it again.
- A pause given a resume date lifts itself on that date. The Stop button sets
  none.

There is no command-line resume yet.

---

## 7. When something goes wrong

| Alarm / symptom | What it means | What to do |
|---|---|---|
| `no_session` (09:00) | No valid Kite session | Log in (Settings → Kite session). A day with no sheet yet is planned on login while the market is open, and then needs authorizing; after the close nothing is planned and the amount carries |
| Day not authorized | The 09:45 plan waits | Authorize on Today. Not authorized by the close → skipped, full carry |
| `order_rejected` | The broker refused an order | Read the reason on the name's page (margin, price band, circuit). The day carries on; three in a row halts it |
| `day_halted` | Three consecutive rejections | Terminal for the day: nothing more is placed, not even at the close. Fix the cause (usually margin or a frozen name); tomorrow plans afresh |
| Rate breaker tripped (`task_failed`, "rate breaker tripped") | Something placed faster than the rounds allow | Should not happen — rounds place only what the window allows. Read the task's log; the breaker clears itself when the window passes. Check for a second process running rounds |
| `fallback_withheld` | A name's price moved beyond the sanity band | Intended. That name's remainder carries. Check the name for news / a circuit |
| `overspend` | A day spent more than it was sized for: its row closed with negative carry | The next day's allocation repays it. Check why (price moved between plan and fill) |
| `divergence` | Holdings and lots disagree | Classify it (§5.3) |
| `zerodha balance disagrees with the books` (09:05) | Kite's opening balance and the books are more than a rupee apart | Upload the latest Console statement on Books and work the queue (§1.4) |
| `could not read zerodha's balance to check it` (09:05, `task_failed`) | There was a Kite session, but the `margins()` read failed | Nothing was recorded for the morning. Run `tx check_balance` again once Kite answers; a day's check is recorded once |
| `orphan_order` | A tagged order at the broker with no row here | Look it up by tag in Kite; cancel it there if it should not exist; investigate how it was placed |
| `unbooked_fill` | A sell filled against lots that do not cover it | Stop, look at the lots and the external transactions for that name before the next sell |
| `task_failed` | Any error in a scheduled task | The log line has the task and trace id. Most tasks are safe to re-run by hand; the database refuses a repeat of a round that already ran |
| `instance-unhealthy` | The EC2 status checks fail | Resting orders expire at the close regardless. Recover or replace the instance (DEPLOY.md §6.2); startup reconciliation rebuilds the day by tag |
| `health-failing` | `/health/` is 503 or unreachable | `curl -s https://<domain>/health/` — the `checks` object names the failure. Unreachable: nginx, gunicorn or the box (`systemctl status nginx steadystack-web`). `tradesheet`: `tail /var/log/steadystack/tradesheet.log`, then `generate_tradesheet`. `authorized`: authorize on Today. `rounds`/`close`: `systemctl list-timers ladder* fallback*`, `journalctl -u ladder.service --since -10min` (a round that runs and fails counts as none). `feed`: see the next row. `settled`: run `settle_day --date <day> --retry` (or wait for the retry timer), before midnight on a live day -- see the next row |
| A live day not settled (the 15:45 settle failed: Kite down, the trade book lagging the order book) | Its fills come from Kite's trade book, which holds **today's trades only** | **A live day must settle before midnight IST.** The retry timer tries at 16:15, 17:30, 20:00 and 22:30; by hand, `settle_day --retry`. The rounds already record fills as they happen, so usually only the row is missing. If midnight passes with the day unsettled, **import the tradebook**: Console → Reports → Tradebook for that day, CSV, then `tx import_tradebook <file> --actor <you> --dry-run`, check what it says against the file, run it without `--dry-run`, then `tx settle_day --date <day> --retry` with a Kite session (`--retry` releases the failed settle's claim; a plain `--date` on a failed claim is refused and says so). **Settle day D before D+1's 15:45**: once D+1's row is written, D's can never be, and `settle_day --date D` is refused. Then run `tx settle_day --date D --trades-only`, which posts only D's settlement entry (D+1's row has already counted D's money). The import matches an order by Kite's order id or the exchange's order number -- or, when no order here carries the id, by the day's one open live order of that symbol and side that has no exchange number yet (its cancelled or filled siblings carry their own, and a rejected order never traded), and says so in the dry run and the output; when two or more such orders could take the trade it refuses and lists them -- record the right one's number with `tx record_exchange_order_id --order TAG --exchange-id N --actor <you>` (from Kite's order history or the contract note, audited) and import again -- and levels every other open live order of the day from its fills (audit `order.expired_unseen`) -- Kite no longer holds them to be asked. **`settle_day` never levels a past day's open live orders on its own**: after midnight Kite's books cannot say whether an order filled while the box was down, so the settle (and the boot catch-up) refuses, naming the two ways on: import the day's tradebook, or -- only once Console's tradebook shows none of those orders traded -- `tx settle_day --date <day> --level-unseen --actor <you>` (audited with your name; add `--trades-only` for a day behind a later row). When the evening's settle had already failed, its claim is still held and the refusal says so: `tx settle_day --date <day> --retry --level-unseen --actor <you>` releases it in the same step. The past-date settle logs that the day's orders are missing from the (new day's) order book -- expected, since Kite's book holds only today's. `import_tradebook` refuses a trade whose order is not one of ours, a day that has settled, and any column it does not know (an empty `expiry_date` is accepted): no real Console tradebook has been checked against it yet, so check the first one by hand (DEPLOY.md §8, item 11) |
| Paper orders not filling / `feed` failing | `steadystack-ticker` down, disconnected, or deaf | `systemctl status steadystack-ticker`; `tail /var/log/steadystack/ticker.log` ("waiting for a Kite session" means log in); restart it |
| Everything 500s after a reboot | `/run/steadystack/env` is gone (tmpfs) | Re-run bootstrap to rewrite it (DEPLOY.md §5.2), restart services |
| Backup alarm / no object in S3 | `backup.sh` refused to upload | Read `backup.log`; it never uploads an empty or unparseable dump. Run `systemctl start backup.service` after fixing |

A day that went wrong mid-way:

```sh
tx preflight
tx ladder_tick                  # one round now; refused if this round already ran
tx market_fallback              # after 15:00, or by hand to close early
tx settle_day                   # after the close
tx snapshot_valuation
```

---

## 8. Credentials

| Credential | Where | Rotate |
|---|---|---|
| Kite API key / secret | Secrets Manager `steadystack/kite_api_key`, `kite_api_secret` (local: `.env`) | Regenerate at developers.kite.trade, `put-secret-value`, re-run bootstrap, restart |
| Kite daily access token | Database, encrypted with `KITE_TOKEN_KEY` | Expires daily; log in again |
| `KITE_TOKEN_KEY` | Secrets Manager | Replace; the stored session becomes unreadable — log in to Kite again |
| Django secret key | Secrets Manager | Replace; everyone is signed out |
| Database role passwords | Secrets Manager (the roles follow) | Put the new password in the URL secret, re-run bootstrap -- it sets each role's password from its URL and restarts the services |
| Your TOTP | Your authenticator | Account → Security, or `enrol_totp USER --replace`; recovery codes if the phone is lost |

A leaked credential is fixed by **rotating** it, never by deleting the commit
or the file it leaked in. The pre-commit credential scan exists to stop the
Kite key and TOTP seed reaching git at all.

---

## 9. Backups and restore

- **Nightly dump** 01:00 IST → `s3://steadystack-backups-<account>/YYYY/MM/DD/steadystack-<stamp>.dump`
  (custom format). The box can write and cannot read or delete; the bucket is
  versioned; objects move to IA at 30 days, Glacier IR at 90, expire at 400.
- **EBS snapshots** nightly 01:00 IST by DLM, 14 kept.

Take one now: `sudo systemctl start backup.service`.

**Restore test** (monthly, and before going live), from a machine with read
access to the bucket:

```sh
aws s3 ls s3://steadystack-backups-<account>/2026/09/ --recursive | tail -3
aws s3 cp s3://steadystack-backups-<account>/<key>.dump /tmp/t.dump
createdb steadystack_restore_test
pg_restore --no-owner --dbname steadystack_restore_test /tmp/t.dump
# point DATABASE_URL at steadystack_restore_test, then:
uv run python manage.py shell -c "from engine.ledger import reconcile; print(reconcile())"
dropdb steadystack_restore_test
```

**Real restore** on the box. The box cannot read the bucket, so the dump
reaches it by a link you sign on your own machine:

```sh
# your machine: a link valid for ten minutes
aws s3 presign s3://steadystack-backups-<account>/<key>.dump --expires-in 600
# the box, over SSM
curl -fsSo /tmp/restore.dump '<the signed link>'
```

Then stop trading, stop `steadystack-web`, `steadystack-ticker` and the timers, and
recreate `steadystack` owned by `steadystack_owner`. Grant the runtime role its
default privileges on the new database **before** restoring -- they lived in
the dropped one, and granted after the tables exist they either grant
nothing or hand UPDATE back on the append-only tables:

```sh
sudo -u postgres psql -v ON_ERROR_STOP=1 --dbname steadystack <<'SQL'
GRANT CONNECT ON DATABASE steadystack TO steadystack_app;
GRANT USAGE ON SCHEMA public TO steadystack_app;
ALTER DEFAULT PRIVILEGES FOR ROLE steadystack_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO steadystack_app;
ALTER DEFAULT PRIVILEGES FOR ROLE steadystack_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO steadystack_app;
SQL
```

Then restore as the owner: `sudo -u postgres pg_restore --no-owner --role
steadystack_owner --dbname steadystack /tmp/restore.dump`. Dumps from 24 September
2026 on keep their privileges, so the append-only REVOKEs come back with the
tables; an older dump was taken with `--no-privileges` and needs every
append-only migration's REVOKE re-applied by hand before the runtime role
touches it. Then `txm migrate`, `tx preflight` (as `steadystack_app`; its
"runtime role" row must be clean), start the services, and check the ledger
reconciles and holdings match the broker before resuming.

A backup nobody has restored is a hypothesis.

---

## 10. The books

`/books/` is what a chartered accountant reconciles against Zerodha's funds
statement and the owner's bank statement — the cash ledger (§1.4, §2.3) is a
daily summary of it, not a second copy of the truth.

**Go-live** is [`DEPLOY.md` §8](DEPLOY.md#8-going-live), which is the
authority; this is its books step. On the freshly recreated database, after
`import_opening_positions` and before the switch to live: `tx open_books
--on <the last trading day before go-live> --cash <that day's closing funds
balance> --actor <you>` posts the live book's opening entry. That closing
balance is go-live morning's opening balance, so the first 09:05 check
agrees by construction. Anything the live book already records through
`--on` (there should be nothing on a fresh database) is netted, so the
entry posts only the difference between that and `--cash` -- the live
book's Zerodha cash equals `--cash` afterwards -- plus the cost of every
imported lot the journal does not already hold. The switch to live is
refused until the entry exists, and `preflight` marks it `!` in live mode.
A second call is refused; if the opening was wrong, reverse it and run
`open_books` again: its reversal is dated on the opening's own date, so no
trial balance shows it twice. Once the opening stands, the live book refuses
any real event dated on or before its date ("… is on or before the opening
balance of …; the live books begin there"); and between `open_books` and the switch
to live, record no lumpsum -- in paper mode it would fund the paper engine
(the lumpsum form warns while that window is open).

The opening also sets the engine at nought. Whatever the live engine
accounts collected before go-live -- a carry restore, the engine's share of
a sweep confirmed from a statement -- is moved into unallocated on the same
entry, so the first live day neither spends a paper-period residue nor owes
one back. Each imported lot is its own Investments line carrying the lot, so
Performance dates its cost on the day it was bought, not on go-live, and
"Contributed" includes the opening balance.

**Monthly**: download the funds-ledger statement as CSV from Console → Funds
→ Statement, and **Upload** it on Books (`/books/statement/`). Lines the
journal already explains are matched on the way in; everything else waits
under "Waiting for confirmation" for a kind to be chosen by hand, and a
matched settlement whose amount disagrees appears under "Settlements that
disagree" with **Correct charges**, which spreads the difference across that
trade date's fills and finalises the lots it touches.

- A line dated on or before the opening entry's date is inside `--cash`
  already: it is linked to the opening ("By the opening") and cannot be
  confirmed. Lines uploaded before `open_books` ran are linked when it runs.
  If the statements' balance at the opening date disagrees with `--cash`,
  the Statement page says so; reverse the opening and run `open_books`
  again with the statement's figure.
- A queued line the app already recorded -- a lumpsum whose UPI reference is
  not the one Zerodha prints, two equal lumpsums close together -- is matched
  by hand with **Match to …**, which lists unmatched entries of the line's
  kind within seven days and for the same amount. It posts nothing and is
  audited. **Confirm** instead would post the money a second time.
- **Correct charges** is offered only within rounding: up to ₹5, or 0.1% of
  the date's turnover when that is more. A larger difference is refused and
  shown as too large -- look for a live fill never recorded
  (`import_tradebook`), a sale, or a line matched to the wrong date.
- **Reconciling a settlement by hand.** A difference too large to correct on
  a date already settled cannot be fixed by the app: `import_tradebook`
  refuses a settled date, and a settlement entry is not reversed from Books.
  Reconcile it by hand: take that date's contract note and the Console
  tradebook, and compare them trade by trade with the date's fills (Orders,
  or the journal filtered to the date) and the settlement entry. What
  explains it is one of: a trade Zerodha made that we have no fill for (an
  auction, a trade placed in Kite) -- record it as an external transaction,
  which posts it and moves the difference; charges the rate card does not
  know (an auction penalty, a delivery shortfall) -- record an account
  charge; or a line matched to the wrong date. If what you found posts,
  the difference moves and the line tells you what is left. If nothing
  should post -- the books are right and the difference is Zerodha's to
  explain, or it is already accounted elsewhere -- write what you found in
  **Explain it** beside the line: the reason, with the note and trade
  numbers. The explanation is appended and audited, not an edit; it stops
  the line counting as disagreeing (and blocking the year close) for that
  difference only. The CA sees every one under "Explained settlements".
- Each upload shows its reconciliation: Zerodha's closing balance, ours
  (1100 + 1110 + 1200 + 1210 through the upload's last date) and the
  difference, and how many lines were matched on the way in, matched by
  hand, confirmed, explained by the opening, or are still waiting. **Export
  CSV** gives the CA the same table, with how many of each upload's
  settlement lines still disagree and how many were explained.
  **Settlements CSV** lists every settlement line that differs from ours:
  its date, the difference, the correction bound, and for an explained one
  who explained it, why and when.

**Quarterly**: after the first-Friday running-account settlement sweeps the
account (SEBI's rule, four times a year, keeping back only open
obligations), Today carries a funding warning naming how much of the sweep
was the engine's carry. Transfer that amount back to Zerodha before the next
trading session — the next contribution restores it in the books
automatically, but the account has to hold the cash first, or the day spends
only what is actually there: a live plan is capped at Kite's available cash
(§1). Upload that evening's statement and confirm the sweep rather than
waiting for the month, so the funding warning and carry owed are right from
the next morning. A lumpsum that arrives on a day that has already bought
is the next trading day's money, and so is the carry it restores.

**Yearly**: Books shows any financial year's trial balance (**Financial
year**): income and expense for that year alone, earlier years' brought
forward on one line. Once the chartered accountant has signed off a year,
**Close a year** on Books (`/books/`) — book and the 31 March it ended. A
closed year accepts no entry dated inside it, from then on, so the live book
refuses to close until a statement is held through 31 March (if its last
line falls earlier, upload the next month's too), no line dated in the year
is waiting, and no settlement of the year still disagrees (an explained
one does not count, above). A line of a
closed year that reopens later (its entry reversed) is confirmed on the day
it is confirmed, as a prior-period item: "relates to <date>, after the year
close".

**Which lots the screens read.** In live mode every lot reader -- Today,
weights, Performance, gains, Instruments, the evening divergence scan --
reads the live lots only; paper lots, including those made in a spell back
in paper after go-live, never reach a live figure or a divergence. In paper
mode the screens read every lot, while the paper engine ranks, and a paper
settle sets holdings, from the paper lots alone. The daily value series is
kept per book, and the Performance charts read the current mode's.

**The live benchmark comparison reads "unavailable"** while any opening lot
is held. The benchmark mirrors each of the portfolio's flows at the index
level of its date, and an imported lot's cost is dated on the day it was
bought -- before go-live, when no index level was recorded -- so Performance
shows "No benchmark level recorded for <date>" rather than a comparison that
would be wrong. XIRR itself is unaffected. It stays unavailable until the
last opening lot is sold; benchmarking from the go-live date instead is a
spec decision not yet taken.

Only entries a person records are reversed from the entry page:
contributions, withdrawals, account charges, dividends, the opening and
charge corrections. The engine's own entries -- allocations, fills,
settlements, external trades -- have lots or the daily cash ledger resting
on them, and are put right where they came from.
