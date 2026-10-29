---
title: "feat(rebalance): sell an executed batch through the day, proceeds reinvested"
type: feat
status: completed
date: 2026-09-24
deepened: 2026-09-24
completed: 2026-09-24
origin: docs/superpowers/specs/2026-09-24-rebalance-sell-ladder-design.md
---

# Sell an executed rebalance batch through the day, proceeds reinvested

## Enhancement summary

**Deepened on:** 2026-09-24. **Reviews:** data integrity, concurrency,
simplicity, Python/Django, architecture -- each checked against `src/`.

### What changed from the first draft
1. **Sells go first, one order a round.** `rebalance_tick` runs *before* the
   buy round with `room = min(1, room)`; the buy round's own
   `orders_in_window` count then leaves it `max - 1`. No rebalance code in
   `orders.ladder` (the draft's `sells_owed` hook was an import cycle).
2. **The close lives in `rebalance_tick`** from `market_fallback_time`;
   `market_fallback` is untouched (its early returns would have skipped it).
3. **Phases reordered** so nothing places a sell until settlement can close a
   batch and book its money ("switch on" is last).
4. **Settlement sweeps and closes first, outside its transaction**, in the
   `settle_day` command; a failed sweep refuses the settle; `--retry` releases
   a failed claim. The draft's "the next settle closes it" was false.
5. **Proceeds are net of the charges booking stored**, exclude the daily-buy
   stream (already netted there), and reach only the balance: `available`,
   `net_in` and "contributed" keep their meaning.
6. **Cancel and stop take the order lock**, and sell rounds, `start` and the
   close refuse once the day's ledger row exists.
7. **Live refused at `start`** until live sell reconciliation exists.
8. **Dropped:** the lock change (Regenerate stays refused while a sale rests),
   four placement helpers (one `stream_orders` and one extracted `expire`),
   the buy-side reserve, net proceeds on the batch page.

## Overview

A rebalance sell can be uploaded and executed, but nothing places it
(`OPERATIONS.md`: "Not wired yet"). This plan wires it so that, in paper mode,
the operator uploads `ASIANPAINT,3` after the 09:45 plan on 25 Sep, executes
it, and it sells through the day, closes at market from 15:00, settles into
the day's ledger row, and the next morning's plan spends the money
(see spec: `docs/superpowers/specs/2026-09-24-rebalance-sell-ladder-design.md`).

- **D-S1 -- sale money is reinvested.** Net proceeds join the day's closing
  carry; the next plan spends them.
- **D-S2 -- never sell what the day is buying.** CNC nets a same-day buy
  against a same-day sell.

## Problem statement

| # | Gap | Where |
|---|-----|-------|
| G1 | `run_tick` paces on the 5-minute `tick_number` and places every call without looking for a resting order | `rebalance/ladder.py:63-151` |
| G2 | Expiry and the close look orders up by sheet version; a sell attaches to whichever version was latest | `orders/ladder.py:266`, `rebalance/ladder.py:167` |
| G3 | A batch stays RUNNING while any order is open, and `settle_day` is claimed once a day -- a failed settle is never retried | `rebalance/ladder.py:221`, `orders/idempotency.py:108-165` |
| G4 | A sell filling after the ledger row is lost; nothing refuses selling after settlement | `engine/ledger.py:206` |
| G5 | `start` checks no trading day, settlement, plan, D-S2, lots across batches, or mode | `rebalance/batches.py:316-379` |
| G6 | A cancelled batch reopens its name for Generate/Regenerate | `rebalance/batches.py:419-434` |
| G7 | Cancel and `stop_trading` take row locks only; a sell round can place after them | `rebalance/batches.py:381`, `orders/halt.py:91` |
| G8 | No ledger column for sale money | `engine/models.py:241` and readers |
| G9 | Paper holdings refreshed only for daily-buy names with net fills > 0 | `engine/settlement.py:266-296` |
| G10 | Cancel/stop decide CANCELLED vs PARTIAL from `row.sold_quantity`, set only at settle | `rebalance/batches.py:382`, `orders/halt.py:158` |
| G11 | `seed_demo` sells after that day's row is written | `engine/management/commands/seed_demo.py:416,427-498` |
| G12 | Live sell fills are recorded only for the daily-buy stream | `orders/reconcile.py:61,232` |

## Decisions

| # | Decision |
|---|----------|
| S1 | **Round model for sells.** Each name runs to an even schedule from `max(window open, batch.started_at)` to `placement_window_close` (`orders.schedule.progress`/`scheduled`, lookahead one round). With a sell resting: RESTING. Otherwise place what the schedule owes, priced by `schedule.sell_price` (below). A resting sell past `order_ttl_minutes` is expired and rescheduled. `run_tick` takes `quotes` shaped as `run_round`'s (`quote_parts`, for `last_price`). `sell_price_for` is removed. |
| S2 | **Sell orders are found by stream.** `placement.stream_orders(trading_date, *, stream, instrument=None)`; open = `.exclude(state__in=TERMINAL)`. `orders.ladder.expire_stale` is split into a public `expire(orders, *, moment, config, broker)` it calls itself; the sell path uses `expire` on `stream_orders`. |
| S3 | **Sells first, one a round.** `rebalance_tick` runs before `ladder_tick` (and before `market_fallback` in the close) with `room = min(1, placement.room(config))`, recomputed per batch. `placement.room` replaces the private `_room` copies in `orders/ladder.py:528` and `orders/fallback.py:297`. Batches oldest `started_at` first. |
| S4 | **The close.** From `market_fallback_time` to `close_window_end`, `rebalance_tick` runs `close_round`: cancel a name's resting non-fallback sells (unconfirmed cancel: skip the name), then one MARKET SELL for the whole remainder (`is_fallback=True`, `market_protection=config.fallback_band_pct`), within room. No band withholding: the operator confirmed the sale. Between the placement close and the close window it does nothing. |
| S5 | **Settlement sweeps, closes, then books.** The `settle_day` command, before claiming: cancels every still-open order of the day in **every** stream, each committed on its own (paper cancels re-read under a row lock, so nothing fills after); any unconfirmed cancel refuses the settle (nothing claimed). Then `rebalance.ladder.close_batches(day)` moves every batch of the day out of RUNNING from its fills (COMPLETE / PARTIAL / CANCELLED "day ended"). Then the existing claimed `settlement.settle`. `settle_day --retry` releases a *failed* SETTLE claim when no ledger row exists. |
| S6 | **`CashLedger.proceeds`** (migration `0010_cashledger_proceeds`: `default=0, db_default=0`; reverse refuses while any row has proceeds). `settlement.day_proceeds(day)` = SELL fills of the day in streams other than the daily buy, gross minus `fill.charges` (stored by booking, DP once per scrip per day), falling back to the estimate only if unset; it logs a warning if it differs from that day's rebalance `RealisedGain.net_proceeds` by more than ₹0.01 a fill (an unbooked fill). `settle_day(proceeds=)`: balance adds it. `available` and `net_in` unchanged; `reconciles`, `discrepancy` and the identity docstring include it; `reconcile()` returns a separate `proceeds` total; the ledger view rows and totals, CSV, `cash_ledger.html`, `engine/detail.html` and the integrity card ("SALE PROCEEDS") show it. `DaySpend.remaining` does not include it (tomorrow's money). XIRR, contributed, invested unchanged. |
| S7 | **`start` refuses** (in order): not a trading day; paused; live mode (until G12); the day's ledger row exists; after `close_window_end`; no daily-buy sheet today (execute after the 09:45 plan); a name on today's buy sheet (D-S2); a name whose lots less other RUNNING batches' unsold fall short. `start(moment=None)` stamps `started_at` from it and re-stamps `trace_id` to the trading day. After the placement close the confirmation says it sells at market in the close. |
| S8 | **One "sold today" rule for every plan.** `rebalance.batches.names_sold_today(day)` -- a sell order resting or filled today in any stream; `_rank` leaves those out for generate, regenerate and replan (replan's own helper goes). |
| S9 | **Cancel and stop are serialised with rounds.** `batches.cancel` and `halt.stop_trading` run under `order_lock(wait_seconds=STOP_WAIT_SECONDS)`; `cancel` refuses anything not RUNNING; both then call `close_batch` (fill-based sold). `rebalance_tick`, the close and `start` refuse once the day's ledger row exists. |
| S10 | **Holdings follow every fill.** Paper `Holding` is set from lots for every name with a fill today in any stream, either side (0 when fully sold; the row is kept). |
| S11 | **Own task names.** Sell rounds claim `idempotency.REBALANCE` / `REBALANCE_CLOSE`, so `web/deck.py`'s ladder counts and timeline stay the buy's. |
| S12 | **Executing is the batch's authorisation.** A sell does not wait for the day's buy to be authorised (M15f); Stop today stops the buy only; a batch stops by Cancel (M32a) or `stop_trading`. Regenerate stays refused while a sale rests -- cancel the batch first. |
| S13 | **The batch page shows sold so far** from `rebalance.ladder.sold_by_row(batch)` (the same count `close_batch` uses). |

Deliberately not done: live sells (G12); T+1 delivery of lots bought the
previous day; Tick rows for sell rounds (outcomes in the TaskRun detail and
log); net proceeds on the batch page; narrowing the lock to daily-buy orders
(`why_locked` also decides where a lumpsum lands).

## Technical approach

### Phase 1 -- pacing and finding sell orders (inert)

- `orders/schedule.py`:

```python
def sell_price(*, bid: Decimal, ask: Decimal, urgency: Decimal, tick_size: Decimal,
               last_price: Decimal | None = None) -> Decimal:
    """The limit for a sell: the ask, lowered toward the bid by *urgency*."""
    price = min(_ceil(ask - (ask - bid) * urgency, tick_size), ask)
    if last_price and last_price > 0:
        ceiling = _floor(last_price * (1 + OTR_EXEMPT_BAND), tick_size)
        floor = _ceil(last_price * (1 - OTR_EXEMPT_BAND), tick_size)
        price = min(max(price, floor), ceiling)
    return max(price, bid)   # never under the bid, even if the band says so
```

- `orders/placement.py`: `stream_orders(...)`; `room(config)` (public, one copy).
- `orders/ladder.py`: `expire(orders, ...)` extracted from `expire_stale`.
- `rebalance/ladder.py`: `run_tick(*, batch, quotes, broker, moment, room)`
  on `round_number` (`BatchTick.round_no`); per row: skip a name on today's
  buy sheet; expire stale; RESTING; owed from the schedule; WAITING_TURN past
  room; place SELL at `sell_price`. `sold_by_row(batch)`; `close_batch(batch,
  *, reason)`; `close_batches(day)`; `close_round(...)` (Phase 3 wires it).
- Tests: `orders/tests/test_schedule.py` (sell_price: steps, bid floor, band,
  tick rounding up); `rebalance/tests/test_ladder.py` (new; helpers `depth()`,
  `at()`, `a_running_batch()` moved to `rebalance/tests/conftest.py`,
  `a_running_batch` also creating the day's sheet): pacing, resting, expiry,
  room, D-S2 skip, a v1-attached sell found after a replan. The sell-ladder
  tests at `test_batches.py:428-560` move here and are rewritten to the round
  model.

### Phase 2 -- settlement and the money (inert with no sells)

- Migration `engine/0010_cashledger_proceeds`; `CashLedger` field, identity.
- `engine/ledger.py`: `settle_day(..., proceeds=ZERO)`; `reconcile()` proceeds.
- `engine/settlement.py`: `day_proceeds`; `settle` passes proceeds and
  refreshes holdings for every name filled today.
- `orders/management/commands/settle_day.py`: sweep (every stream) -> refuse
  on unconfirmed -> `close_batches` -> claimed settle; `--retry`.
- Readers: `performance/ledgers.py` rows and totals, `web/views.py` CSV,
  `templates/web/cash_ledger.html`, `templates/engine/detail.html`,
  `web/deck.py` integrity card. `npm run build` if classes change.
- `seed_demo`: the sale inside `_run_days` before that day's settle, with
  proceeds.
- Tests (`engine/tests/test_settlement.py`, `test_ledger.py`,
  `orders/tests/test_settle_command.py` new): proceeds = net sell fills,
  excluding the daily stream; charges buy-only; row reconciles; next opening
  includes proceeds; DP charged once across slices; an unbooked sell fill is
  counted and warned; an open sell at settle is cancelled and its batch
  closed; an unconfirmed cancel refuses and claims nothing; `--retry`; a
  second settle changes nothing; Holding equals lots after a full sale and the
  divergence scan is clean; every seeded row's proceeds equals `day_proceeds`.

### Phase 3 -- execute, cancel, plan around a sale, the close

- `rebalance/batches.py`: `start` guards and `moment` (S7); `names_sold_today`
  (S8); `cancel` under the lock, RUNNING only, `close_batch` (S9).
- `orders/halt.py`: `stop_trading` under the lock; `_close_running_batches`
  uses `close_batch`.
- `engine/tradesheet.py` `_rank`; `engine/replan.py` uses the shared helper.
- `rebalance/views.py` + `detail.html`: sold so far (S13); the close wording.
- Tests (`rebalance/tests/test_batches.py`): every `start` refusal; trace and
  `started_at`; lots across two batches; cancel of a filled batch is PARTIAL,
  of a COMPLETE one refused; a cancelled batch's name stays out of Generate
  and Regenerate; `close_round` cancels resting sells and sells the remainder
  at market once, never beyond the row quantity.

### Phase 4 -- switch on

- `rebalance/management/commands/rebalance_tick.py`: trading day, pause,
  RUNNING batches of the date (oldest first), session, not before the
  tradesheet time, refuse after the ledger row, bind the trading-day trace;
  per batch: placement window -> `once(REBALANCE, stream, round_number)`,
  close window -> `once(REBALANCE_CLOSE, stream, fallback.round_number)`;
  re-read the batch under the lock; one quote call; `run_tick` or
  `close_round` with `room=min(1, placement.room(config))`.
  `AlreadyRan`/`Busy` are "not my turn"; broker errors a `CommandError`.
- `orders/idempotency.py`: `REBALANCE`, `REBALANCE_CLOSE`.
- `ops/local/run_day.sh`: `rebalance_tick` before `ladder_tick` in each round
  and before `market_fallback` in each close round, `|| note`.
- `infra/systemd/ladder.service` and `fallback.service`: a first
  `ExecStart=-/usr/bin/flock -n /run/steadystack/orders.lock ... rebalance_tick`
  (the `-` so a sell failure never stops the buy).
- Tests (`rebalance/tests/test_tick_command.py`, following
  `orders/tests/test_ladder_command.py`): places for a running batch, skips
  others, one claim per round per batch, own task name, room one, refused
  after the ledger row, `Busy` is not a traceback, close window runs the close.

### Phase 5 -- end to end, documents, paper check

- `engine/tests/test_full_day.py`: a day with a buy and a sell batch -- sells
  one a round before buys, paper fills both sides, the close, the settle
  command -- lots FIFO, realised gains, one row reconciling with proceeds,
  the next day's opening including them, XIRR computable.
- PRD: M33 (wiring, pacing, close), M40a (proceeds), D-S1, D-S2, S12.
  `OPERATIONS.md` "Sell shares" row and `settle_day --retry`. `CLAUDE.md`
  vocabulary.
- Paper check on the real data: an `ASIANPAINT,3` batch dry-run in a rolled
  back transaction.

## System-wide impact

- **Interaction graph.** Execute -> `start` guards -> RUNNING. Each round:
  `rebalance_tick` (at most one sell) -> `ladder_tick` (buys, room counts the
  sell) -> `run_ticker` fills either side -> `booking.book` consumes lots,
  writes realised gains. 15:00-15:25: `rebalance_tick` close, then
  `market_fallback`. Day end: `settle_day` sweep -> close batches -> settle
  (proceeds, holdings) -> ledger row.
- **Error propagation.** Quote failure: that batch places nothing that round.
  Unconfirmed cancel: that name places nothing; at settle, the settle refuses.
  `RateBreakerTripped` cannot fire by construction; if it does, CommandError.
- **State lifecycle.** A round's claim is kept on a crash; the schedule is
  clock-based, so the next round catches up. A failed settle leaves its claim
  failed; `--retry` releases it when no row exists. A batch never outlives
  its day: the sweep closes it.
- **API surface parity.** Web execute and cancel, `stop_trading`, run_day,
  systemd. The phone has no rebalance screen.
- **Invariants.** One ledger row per day (proceeds on it); append-only (DDL
  add, guarded reverse); idempotent rounds (batch stream + own task); FIFO
  lots; trace_id bound for every sell round, stamped at execute.

## Acceptance criteria

- [x] A batch executed after 09:45 sells in paper through the day, closes at market, and ends COMPLETE, PARTIAL or CANCELLED from its fills -- never RUNNING after settle.
- [x] No plan (generate, regenerate, replan) buys a name with a sell resting or filled today; no batch starts or places for a name on today's buy sheet.
- [x] Sells get a slot every round they owe one; buy plus sell never exceeds the rate limit.
- [x] The ledger row carries net proceeds; it reconciles; `available`, `net_in`, contributed and XIRR method unchanged; the next day spends the proceeds.
- [x] Every `start` refusal has a test; cancel and stop are serialised with rounds; nothing places after the ledger row.
- [x] Holdings, lots and the divergence scan agree after a sale; seed_demo's rows reconcile with proceeds.
- [x] Migration applies; `makemigrations --check` clean; full suite passes; paper dry-run on the real data.
- [x] PRD, `OPERATIONS.md`, `CLAUDE.md` updated.

## Implementation notes (2026-09-24)

- **Test runs use a private order lock** (`config/settings/test.py`): the
  suite used the real `/tmp` lock, so today's trading loop made tests report
  `Busy` -- and a test could have blocked a real round. Found while building
  this, fixed first.
- **The system-wide stop never refuses for want of the lock**: it waits for a
  round in flight and, if one will not finish, stops anyway (the pause still
  keeps every later round from placing).
- **`preview` shows sold-so-far from the fills** once a batch has a stream,
  which is the batch page's live figure (S13).
- **`ladder_interval_minutes` and `ladder_patience`** belonged to the old sell
  pacing and are now unused; they stay until a migration removes them.
- **Deploying:** `run_day.sh` changes. A running `run_day.sh` reads its file as
  it goes, so merge only after the day's loop has settled, or restart it.

## Risks

- **systemd lock files.** The units' `flock` uses `/run/steadystack/orders.lock`;
  Python's lock defaults to `/tmp`, private per unit under `PrivateTmp=true`,
  so web actions are not serialised with timers on the box. Pre-existing
  (recorded this morning); must be fixed before Phase 4 of the system plan.
  The laptop shares `/tmp`, so tomorrow is unaffected.
- **Paper MARKET fills** take the next traded price; a thin name can fill far
  from the last price. Accepted (S4).
- **A sell starts only after the plan exists** (S7): an operator who uploads
  early executes after 09:45 (the preview token lasts 15 minutes, so upload
  again if it lapses).

## Sources

- Spec: `docs/superpowers/specs/2026-09-24-rebalance-sell-ladder-design.md`.
- Research and five reviews (this session); history `2c8e123`, `0147de5`,
  `bfc8a49`.
- `rebalance/ladder.py:49-231`, `rebalance/batches.py:144-434`,
  `rebalance/models.py:21-118`, `orders/schedule.py:95-126`,
  `orders/ladder.py:191,221,266,504,528`, `orders/placement.py:95,134-277,412`,
  `orders/fallback.py:105,164,297`, `orders/booking.py:55-167`,
  `orders/idempotency.py:40,108-165`, `orders/halt.py:91,158`,
  `orders/reconcile.py:61,232`, `engine/settlement.py:107-296`,
  `engine/ledger.py:206-424`, `engine/models.py:241-330`,
  `engine/migrations/0003_ledger_append_only.py`,
  `engine/management/commands/seed_demo.py:66,416,427-498`,
  `performance/ledgers.py:20-88`, `performance/returns.py:71-87`,
  `web/views.py:538`, `web/deck.py:140,151,567`, `ops/local/run_day.sh:148-168`,
  `infra/systemd/ladder.service`, `rebalance/tests/test_batches.py:404-560`.
