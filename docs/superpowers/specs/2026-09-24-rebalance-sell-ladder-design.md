# Rebalance sells that actually sell -- design

**Date:** 2026-09-24
**Status:** approved in conversation; spec for review
**Goal:** upload a rebalance sell (`symbol,quantity`) on a trading day, execute
it, and have it sell -- in paper mode -- through that day, closing at market,
with the money going to the next day's buy.

## Why

The sell ladder's placement logic exists (`rebalance/ladder.py`: `run_tick`,
`sell_price_for`, `settle`), and executing a batch marks it RUNNING, but
nothing calls it: no command, not `run_day.sh`, no systemd unit
(`OPERATIONS.md` says so). A batch executed today therefore places nothing,
today or later. And even if it placed, the cash ledger has nowhere for a
sale's money to go (PRD M40a has no proceeds), so the rupees would vanish.

## Decisions (from the operator)

- **D-S1. Sale money is reinvested.** Net proceeds become part of the day's
  closing carry, so the next day's plan spends them on the most underweight
  names -- which is what a rebalance is for.
- **D-S2. Never sell what the day is buying.** CNC nets a same-day buy
  against a same-day sell.

## Design

### 1. Sell rounds

A new command, `rebalance_tick` (in `rebalance/management/commands/`), runs
one sell round for every batch that is RUNNING with today's date:

- claims the round through `idempotency.once(task=LADDER, stream=batch.stream,
  tick_no=round)` and takes the order lock -- the same guard the buy ladder
  uses; the batch's own stream keeps its rounds apart from the buy's (M24);
- re-reads the batch under the lock and skips it unless it is still RUNNING;
- cancels the batch's orders that have rested past `order_ttl_minutes`
  (`orders.ladder.expire_stale`, which is side-agnostic), exactly as the buy
  ladder does, so a sell stranded at a stale ask is replaced rather than left
  to the close;
- reads depth for the batch's names in one quote call and calls
  `rebalance.ladder.run_tick`.

**Sharing the rate limit.** The daily buy places first. `run_tick` gains a
`room` argument -- what `orders_in_window` leaves of the five-per-thirty-
seconds -- and a name beyond it is skipped as `WAITING_TURN` rather than
tripping the rate breaker. Buys are the system's core; a sale waits a round.

**Wiring.** `run_day.sh --watch` runs `rebalance_tick` after `ladder_tick` in
every round; `infra/systemd/ladder.service` gains it as a second `ExecStart`.

### 2. The close (M33)

`market_fallback` runs the daily buy's close as today, then, for each RUNNING
batch of the day, a sell close in the same round and under the same claim
pattern (its own stream): cancel the batch's resting sells, then place a
MARKET sell for each name's unsold remainder, within the room left, paced
across the close rounds as the buy close is. New function:
`rebalance.ladder.close_round(batch, broker, last_prices, room)`.

### 3. Settlement and the money

> **Superseded** by the double-entry journal design
> (`2026-09-24-double-entry-journal-design.md`) as to the money: proceeds are
> no longer summed from sell fills into the ledger row. When a batch closes,
> its net proceeds are posted as a `reinvest` allocation in the journal, and
> the day's `CashLedger.proceeds` is read from those allocations
> (`engine.settlement.day_proceeds`). The balance identity below, with its
> deposit, lumpsum and adjustment columns, no longer exists. The batch
> closing and the D-S2 rules still stand.

- **Batches close.** Day-end settlement (`engine.settlement.settle`, called
  by `settle_day` and `run_day.sh --settle`) calls `rebalance.ladder.settle`
  for every batch of the day, which records sold quantities and moves the
  batch to COMPLETE or PARTIAL once nothing rests.
- **Proceeds.** `CashLedger` gains `proceeds` (Decimal, default 0; one
  migration). It is the day's sale money **net of sell charges**: sum over
  every SELL fill of the day, in any stream, of gross value minus
  (`fill.charges or estimated.total`) -- the same charge rule settlement
  already uses. New `settlement.day_proceeds(day)`; `settle_day` takes
  `proceeds` and the balance becomes
  `opening + deposit + lumpsum + adjustment + proceeds - cash_spent - charges`.
- **The identity.** `CashLedger.available`, `reconciles` and `discrepancy`,
  and the ledger view's `CashLedgerRow` and totals, include `proceeds`.
  `charges` stays buy charges only: sell charges are already inside the net
  proceeds, and counting them in both would double them.
- **Performance is unchanged, deliberately.** XIRR's outflows are
  `cash_spent + charges` and its inflows are realised-gain `net_proceeds`, so
  a sale is an inflow and its reinvestment a new outflow -- already right.
  "Contributed" is `deposit + lumpsum`; proceeds are not a contribution.
- **The daily buy's spend is unchanged**: `day_spend` stays the daily-buy
  stream's buys.

### 4. Never selling what is being bought (D-S2)

Already enforced: an upload refuses a name on today's buy sheet
(`batches._is_being_bought_today`); the daily plan leaves out names in a
running batch (`instruments_with_a_live_sell`); a replan leaves out names
with a sell today.

Added:
- **Executing re-checks.** `batches.start` re-runs the "being bought today"
  check for every row, refusing the whole batch if any name is on the day's
  buy sheet -- a batch uploaded before the 09:45 plan cannot be executed
  after the plan put one of its names to buy.
- **Every sell round re-checks.** `run_tick` skips a name the day's latest
  daily-buy sheet has a line for (quantity > 0), with a stated reason.

### 5. Refusals and edges

- A batch executed after the placement window closes gets no ladder rounds;
  the close (from 15:00) sells it at market.
- Pause (`require_running`) stops sell rounds and the sell close, as it stops
  the buy.
- Stop today stops the daily buy only; a batch is stopped by its own Cancel
  (M32a, already built). The system-wide stop (`stop_trading`) closes running
  batches, as today.
- Live mode is not special-cased: placement is gated by M12a as for buys.
  Only paper is verified before this ships.

## Testing

Tests first, in the style of this morning's work:

- `rebalance_tick`: places for a running batch; skips a non-running one;
  claims each round once; respects `room` after a buy round; re-reads the
  batch under the lock; expires a stale sell.
- Never selling what is bought: `start` refuses a batch whose name is on the
  day's sheet; a round skips such a name.
- The close: cancels resting sells, sells the remainder at market, within room.
- Settlement: `proceeds` equals net sell fills; the row reconciles; the next
  day's opening includes it; charges stay buy-only; a second settle changes
  nothing; the batch closes COMPLETE / PARTIAL.
- End to end (paper): a day with a buy and a sell batch -- rounds, paper
  fills for both sides, the close, settlement -- lots consumed FIFO, one
  ledger row that reconciles with proceeds, XIRR computable.
- Paper-mode check on the real data: an `ASIANPAINT,3` batch dry-run, rolled
  back.

## Documents

PRD: M33 names the wiring and the close; M40a gains proceeds; D-S1 and D-S2
recorded. `OPERATIONS.md`: the "Sell shares" row loses "Not wired yet".
`CLAUDE.md`: the rebalance sell vocabulary entry says proceeds are reinvested.
