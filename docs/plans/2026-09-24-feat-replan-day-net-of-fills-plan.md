---
title: "feat(engine): replan the rest of a day, net of what it already bought"
type: feat
status: completed
date: 2026-09-24
deepened: 2026-09-24
completed: 2026-09-24
---

# Replan the rest of a day, net of what it already bought

## Enhancement summary

**Deepened on:** 2026-09-24. **Reviews run:** data integrity, architecture,
simplicity, Python/Django, migrations, concurrency, and external research
(Kite Connect holdings semantics; Django/Postgres locking). Every finding was
checked against `src/`.

### What changed from the first draft
1. **Replan no longer cancels orders itself.** The operator presses **Stop
   today** first; replan requires a stopped day with nothing resting and makes
   no broker cancel calls. Most double-buy risk in the first draft lived in
   replan's own cancel loop (fills racing cancels, a live cancel "confirmed"
   while still CANCEL PENDING, a Stop today pressed mid-replan being overwritten).
2. **Paper only.** In live mode `Fill` rows are written only at settlement
   (`orders/reconcile.py:232-279`, called from `engine/settlement.py:111`), so a
   live replan would net against nothing and spend the day twice. Live replan is
   refused and its preconditions are written down as a follow-up.
3. **No migrations.** Dropped `SkipReason.REPLANNED` (the stopped version is
   already `HALTED`/`OPERATOR`; supersession lives in v2's snapshot and the
   audit event) and the `Tick` uniqueness change (`TaskRun` is unique per
   `(trading_date, task, stream, tick_no)`, so two versions can never run the
   same round -- kept as a regression test instead).
4. **Phase 0 ranks from the book, not by rewriting `Holding`.** `Holding` stays
   the broker's view (the divergence check needs two records --
   `portfolio/models.py:79-83`); `gather` reads a new `engine/book.py`.
5. **Three safety fixes pulled forward into Phase 0**, because the races exist
   today with plain Stop today: ladder/fallback re-read the latest version
   inside the lock; `placement.cancel` locks and re-reads the order row;
   `market_fallback` catches `Busy`.
6. **The "frozen opening" is a budget, not a ledger input.** Settlement keeps
   computing the opening itself; a first-draft claim that plan and ledger
   "cannot disagree" was wrong and is removed.
7. **Settlement goes day-scoped in Phase 1**, ahead of replan, and is a no-op
   for days with one buying version.

### New considerations discovered
- **Paper days already rank against an empty book** (P0 below). This affects
  tomorrow's first v3 sheet regardless of this feature.
- The order lock is a file under `/tmp` (`orders/idempotency.py:39`); systemd's
  `PrivateTmp=` in Phase 4 would give each unit its own copy and the lock would
  silently exclude nothing. Follow-up, not in scope.
- `web/deck.py:521-523` shows any sheet with a `skip_reason` as a skipped day;
  `stop_day` already writes `OPERATOR` onto `HALTED` sheets that bought.
- `web/phone.py:373` counts charges as `f.charges` while settlement uses
  `fill.charges or estimated.total` (`engine/settlement.py:84-88`); the phone's
  "unspent" can disagree with the ledger today.

## Overview

Once any of a day's daily buy has filled, the tradesheet is frozen
(`engine/lock.py` `why_locked`). That rule stops a real double buy (commit
`b6737f9`), but leaves no way to act on a decision made mid-session. On
24 Sep 2026 the model portfolio was replaced (v2 → v3) after 32 of the day's
orders had filled; the rest of the day's ₹5.2 lakh could not be re-pointed at
v3.

This plan adds one explicit operator action, **Replan the rest**, available on
a day that has bought and been stopped: a new version of today's sheet that
treats everything already bought as held, spends only the cash not yet spent,
and ranks against the current model portfolio. It needs authorising like any
version, and the day still settles into **one** ledger row counting every
version's fills.

## Problem statement

### P0 -- paper days rank against an empty book (bug)

`plan_from_broker` runs `holdings_sync.sync(broker.holdings())` before every
plan (`engine/planning.py:83-87`). There is no paper broker -- `get_broker()`
always returns `KiteBroker` (`broker/__init__.py:31-33`) -- so in paper mode the
sync writes the **real** account's holdings into `Holding` and zeroes every
name the real account lacks (`portfolio/holdings.py:69-83`). Ranking reads only
`Holding` (`engine/tradesheet.py:92-94`).

Observed 24 Sep: `Holding` shows GOLDBEES 2 and zero elsewhere; the lots hold
GOLDBEES 150, ITC 49, HDFCBANK 14, ... Every paper sheet ranks as if the
portfolio were empty, which PRD M19c exists to prevent. The lots are a reliable
paper book: they are created per fill in both modes (`orders/booking.py:67`)
and sells consume them since `2c8e123`.

### P1 -- a day that has bought cannot be re-pointed

1. Ranking holdings exclude today's fills.
2. A new version's cash would be the whole day's spendable.
3. Placement counts per version (`orders/placement.py:363-396`), so a new
   version starts at committed = 0 -- the original double buy.
4. Settlement reads only the latest version's fills
   (`engine/settlement.py:58-59,102,121-122`), so an earlier version's spend
   drops out of the ledger row.
5. `ladder_tick` / `market_fallback` load the latest version before taking the
   order lock and act on that stale object (`ladder_tick.py:46,76`,
   `market_fallback.py:49`) -- already a race with Stop today.

## Decisions

| # | Decision | Why |
|---|----------|-----|
| R1 | Replan is a **separate operation** (view + `replan_tradesheet` command). Generate/Regenerate keep refusing after a fill; `why_locked` is unchanged. | `ops/local/run_day.sh:88-92` re-runs `generate_tradesheet` and relies on the refusal; M8a's lumpsum rule runs through `why_locked` (`engine/ledger.py:353-367`). |
| R2 | "The rest" = **remaining cash, re-ranked against the current model portfolio**. Finishing the original plan's shares stays with line overrides (M15a). | What the 24 Sep case needed; the alternative exists. |
| R3 | **Stop today first.** Replan requires the latest version to be `HALTED`, or to have no orders at all (an unauthorised replanned version), and **no non-terminal daily-buy order on the day** (`live_orders_on(day)` empty). Replan makes no cancel calls. *(Changed by review: first draft did its own stop.)* | Removes the cancel-vs-fill races from replan; Stop today already cancels and records an override (`orders/overrides.py:335-379`). |
| R4 | **Paper only.** Live replan is refused with a message naming the follow-up. *(Changed by review.)* | Live `Fill` rows don't exist until settlement; Kite's same-day sell semantics (`used_quantity`) are unverified. |
| R5 | Holdings for **every** plan come from `engine/book.py` `book_for_ranking(day)`: paper = `lots.held_quantity` per name (includes today's fills); live = `Holding` as synced. `gather` calls it; `Holding` is not rewritten in paper mode. *(Changed by review.)* | One holdings source for the daily plan and the replan, so they cannot drift; keeps `Holding` as the broker's record. |
| R6 | Budget = `opening_for(day).spendable` read at replan time **minus** the day's daily-buy spend and charges across all versions (`settlement.day_spend`, same charge rule as settlement: `fill.charges or estimated.total`). The opening is recorded in the snapshot as an audit value only; `settle_day` keeps computing its own opening, and any difference (a mid-day contract-note adjustment) flows through closing carry and the D7 overhang. *(Changed by review.)* | Settlement already recomputes the opening (`engine/ledger.py:232`) and applies pending adjustments (`:259-262`); freezing it for the ledger would lose money. |
| R7 | Allowed only: today, market in session, **before `market_fallback_time − 2 min`** (re-checked under the lock), not paused, not settled, something bought today (else "use Regenerate"), and the new version buys something (else refused -- the day stays stopped). | A v2 with quantity 0 would be marked SKIPPED by `_never_authorized` on a day that bought; the close's first round must not lose the lock to a replan. |
| R8 | Reserve: copy the superseded version's `reserve_symbol`/`reserve_days`/`reserve_engaged`; the amount held back is the superseded version's held-back amount, capped at the remaining budget -- not recomputed. `reserve.announce` does not fire on a replan. | Recomputing on the smaller budget would count the lead name unaffordable only because the morning spent the cash; `held_back` capped at spendable could otherwise swallow all of it (`engine/reserve.py:128`). |
| R9 | Replan runs under `order_lock()` (non-blocking; `Busy` → "a round is running -- try again"). It makes no broker cancel calls, so it holds the lock for milliseconds; the quote read happens **before** the lock. | Serialises with ladder, fallback and settle (`orders/idempotency.py:62-85`) without a retry loop. |
| R10 | Settlement is **per day** for the daily-buy stream: `fills_for` filters `order__trading_date=day` instead of `order__tradesheet=latest`; paper holdings refresh for every name bought today; the latest version's state settles on its own quantity; earlier versions stay `HALTED`; the note names each version. | One ledger row per day, every rupee traced to a day and an order. A no-op for days with one buying version. |
| R11 | A replanned version excludes names with **any sell order today** (open or filled, any stream). | Buying back a name sold the same day nets off under CNC; a resting sell can still fill after v2 is ranked. |
| R12 | Overrides refuse a version that is not the day's latest (`require_latest`). Authorise already refuses one and locks the row (`engine/authorization.py:40-57`) -- no change there. | A stale tab must not act on a superseded version. |
| R13 | No replan on the phone; phone Today shows day totals and "v(n) needs authorising". | Five screens by design. |

`_is_being_bought_today` (`rebalance/batches.py:180-190`) keeps counting a
HALTED version's lines: a name the morning planned stays unsellable today.
Unchanged, deliberately.

## Technical approach

### Phase 0 -- safety fixes (ship alone, before 09:45 on 25 Sep if possible)

**0a. Rank against the book.**
- `engine/book.py` (new): `book_for_ranking(trading_date: date) -> dict[str, int]`.
  Paper: `lots.held_quantity(symbol)` for every name in the model portfolio or
  with an open lot. Live: `Holding` quantities (as today).
- `engine/tradesheet.py` `gather` (`:92-94`): holdings from
  `book_for_ranking`; keep an optional `holdings=` override for tests.
- Mode from `config.is_live` (`configuration/models.py:191`).
- `plan_from_broker` still calls `broker.holdings()` and `holdings_sync.sync`
  in both modes -- the BrokerCall and the broker's view are still recorded.
- Check the opening import is complete (`require_complete`) before ranking a
  paper day from lots; otherwise lots understate the book.
- `settlement._apply_paper_holdings` is left as is (known debt: in paper mode it
  already makes `Holding` a copy of lots at settle; tracked separately).

**0b. Ladder and fallback act on the version that is latest *now*.**
- `ladder_tick.py` and `market_fallback.py`: move the latest-version fetch
  inside `idempotency.once`; `run_round` / `fallback.run` receive the re-read
  sheet. Refuse only a superseded version; keep the fallback's
  `_never_authorized` and `_close_without_buying` paths (`orders/fallback.py:203-208`).
- `market_fallback.py:71-79`: catch `Busy` as `ladder_tick.py:91` does.

**0c. A cancel never overwrites a fill.**
- `orders/placement.py:338-360` `cancel`: in its own `atomic()`, re-read the
  order with `select_for_update`; if it became terminal, return it unchanged.
  Same pattern in `reconcile._reconcile_one`.

Tests: `src/engine/tests/test_planning.py` (paper plan ranks against lots; a
broker `holdings()` without those names does not change the ranking; live
unchanged), `src/orders/tests/test_control.py` (stale sheet: load v1, stop the
day, run the command path -- no order placed), `src/orders/tests/test_halt.py`
(a paper fill committed during a cancel leaves the order COMPLETE).

### Phase 1 -- settle the day, not the version (ships alone; no behaviour change)

- `engine/settlement.py`:
  - `fills_for(trading_date, *, stream)` → `Fill.objects.filter(order__trading_date=..., order__stream=...)`.
  - `day_spend(trading_date, *, stream) -> DaySpend` (frozen dataclass:
    `opening`, `spent`, `charges`, `fills: dict[str, int]`, property
    `remaining`); `spend_and_charges` delegates to it. Named `day_spend`, in
    settlement, because `engine.ledger.money()` already exists.
  - `_apply_paper_holdings` over every name with a daily-buy fill today.
  - note: `"v1 halted, 118 filled; v2 42 of 42 filled"` when versions > 1.
- `orders/placement.py` `_orders`: accept `tradesheet=` **or** `trading_date=`,
  building the filter from whichever is given; existing callers unchanged.
- `web/phone.py:373`: read `day_spend` instead of summing `f.charges`.

Tests: `src/engine/tests/test_settlement.py` -- a day whose v1 filled and v2
filled settles into **one** `CashLedger` row whose `cash_spent` and `charges`
equal the sum over every daily-buy fill of the day; money conservation holds;
`reconcile()` totals zero; a second settle changes nothing.

### Phase 2 -- the replan operation (paper only)

`engine/tradesheet.py`: split `_generate`. Gates (pause `:165`, closed day
`:169`, `require_unlocked` `:163`) stay in `_generate`; lines `:194-249` move to

```python
def _plan(
    *,
    inputs: Inputs,
    spendable_cash: Decimal,
    reserve_state: reserve.ReserveState | None,
    held_back: Decimal | None,
    generated_by: str,
    has_session: bool,
    replan: dict | None = None,
) -> TradeSheet:
    """Rank, select and write one version. `reserve_state=None` advances from
    the previous trading day; a replan passes the superseded sheet's state."""
```

`Inputs` (`tradesheet.py:62-72`) gains `excluded: frozenset[str] = frozenset()`,
passed to `rank(being_sold=being_sold | inputs.excluded)`.

`engine/replan.py` (new):

```python
class ReplanRefused(Exception):
    """A replan the day cannot take. Not a GenerationRefused: callers tell them apart."""


def replan(sheet: TradeSheet, *, actor: str, reason: str, broker: Broker) -> TradeSheet:
    """Plan what is left of a stopped day against the current model (R2-R11)."""
    token = bind_trading_day(sheet.trading_date)
    try:
        if refusal := why_not_replannable(sheet):              # R3, R4, R7
            raise ReplanRefused(refusal)
        quotes = quote_reader.fetch(sorted(current_portfolio().weights()), broker=broker)
        with order_lock():                                     # R9; Busy propagates
            with transaction.atomic():
                current = (
                    TradeSheet.objects.select_for_update().get(pk=sheet.pk)
                )
                if refusal := why_not_replannable(current):    # re-check under lock
                    raise ReplanRefused(refusal)
                spend = day_spend(current.trading_date, stream=DAILY_BUY_STREAM)
                new = _plan(
                    inputs=gather(
                        trading_date=current.trading_date,
                        quotes=quotes,
                        excluded=sold_today(current.trading_date),   # R11
                    ),
                    spendable_cash=spend.remaining,                  # R6
                    reserve_state=reserve_of(current),               # R8
                    held_back=min(held_back_of(current), spend.remaining),
                    generated_by=actor,
                    has_session=True,
                    replan={
                        "supersedes_version": current.version,
                        "reason": reason,
                        "opening": str(spend.opening),
                        "spent_before": str(spend.spent),
                        "charges_before": str(spend.charges),
                        "fills_before": spend.fills,
                    },
                )
                if new.quantity == 0:
                    raise ReplanRefused("Nothing is left to buy; the day stays stopped.")
                record("tradesheet.replanned", actor=actor, entity=new, before=..., after=...)
                return new                                     # unauthorised (M15f)
    finally:
        reset_trace_id(token)
```

- `why_not_replannable(sheet) -> str | None` in `engine/replan.py`, mirroring
  `why_locked` / `authorization.why_not`: today; in session; before the
  fallback cutoff; not paused; no `CashLedger` row; paper mode; `sheet` is the
  day's latest (`engine/lock.py:76` `latest_sheet`); latest is `HALTED` or has
  no orders; `live_orders_on(day)` empty; something bought today.
- Reuse `orders.idempotency.Busy` (`idempotency.py:54`) -- no `ReplanBusy`.
- The replan takes the **sheet the operator was looking at**, as `stop_day`
  does, so a stale tab is refused ("A later version exists").
- `engine/views.py` + `engine/urls.py`: `POST <date>/v<n>/replan/`, reason
  required, non-blank, ≤ 255 chars. `regeneration()` context (`views.py:208-240`)
  offers **Replan the rest** on a stopped day that has bought, with a note
  between the placement close and the fallback: "buys at market in the close".
- `templates/engine/_regenerate.html`: the replan form. Run `npm run build`.
- `engine/management/commands/replan_tradesheet.py`: `--reason` (required),
  `--actor`; `Busy` and `ReplanRefused` exit non-zero.
- `orders/overrides.py`: `require_latest(tradesheet)` on every lever (R12).

Tests (`src/engine/tests/test_replan.py`, new; stub broker as in
`src/orders/tests/test_halt.py:69-78`; helpers as in `test_settlement.py:50-99`):
- refusals: not today; market closed; after the cutoff; paused; settled; live
  mode; nothing bought (points to Regenerate); nothing left to buy; latest still
  RUNNING; an open order on the day; a superseded sheet; lock busy.
- the replanned version ranks against lots including today's fills, spends
  `remaining`, uses the current model, excludes names with a sell order today,
  copies the reserve, starts unauthorised, and records the snapshot block.
- replanning twice (v3) nets v1 and v2 together.
- every row written carries `ts-2026-09-24`, including through the view.
- end-to-end in `src/engine/tests/test_full_day.py`: v1 fills 3 of 5 A → stop
  → replan → authorise → ladder rounds and fallback to the end → the day's A
  fills never exceed what the combined plans wanted, gross spend ≤ day
  spendable + D7 overhang, one ledger row.
- regression: after a replan, v2's ladder and fallback rounds each create their
  own `Tick` rows and the fallback's `_record_tick` (`orders/fallback.py:543`)
  does not raise.
- through `call_command`: the command exits non-zero on `Busy`.

### Phase 3 -- views

- `web/trading.py:54-72` `buy_sheet`: the latest version, not the sheet that
  owns the first order.
- Today (web and phone) show the day's totals from `day_spend` and the
  day-scoped placement queries; per-version detail on the tradesheet page.
  `engine/day.py` shapes them for templates; it does not compute them.
- `web/deck.py:521-523`: a skipped day is `state == SKIPPED` on the latest
  version, not a non-blank `skip_reason`.
- `web/trading.py:755` lists every tick of the day: group by version.

### Phase 4 -- documents

- PRD: amend M8a (a replan never picks up money credited after the day's first
  order), M15h and M17d (replan after a stop), add the replan requirement
  beside M15a; D13 (`docs/plans/2026-09-22-feat-daily-equity-accumulation-system-plan.md:331-358`)
  gains the replan exception and its conditions.
- `CLAUDE.md` vocabulary: **replan**. `OPERATIONS.md`: Stop today, Regenerate
  and Replan the rest -- when each applies.

## Alternatives considered

- **Relax `why_locked` and let Regenerate net the fills.** Rejected: changes
  `run_day.sh` and M8a silently; an accidental re-run becomes a replan.
- **Replan cancels orders itself** (first draft). Rejected on review: its cancel
  loop re-created every race Stop today already handles, and could overwrite a
  Stop today pressed mid-replan.
- **Copy lots into `Holding` in paper mode** (first-draft Phase 0). Rejected on
  review: the broker divergence check (M42) would compare lots with lots.
- **A separate top-up stream.** Rejected: a second daily-buy-like stream
  complicates the single deposit/spend row and the reserve counter.
- **Make `Tick` unique per version.** Not needed: rounds are claimed per day by
  `TaskRun`. Kept as a regression test.

## System-wide impact

- **Interaction graph.** Stop today → cancels, override, `HALTED`. Replan →
  quote read → `order_lock` → lock the sheet row → `day_spend` → `gather`
  (book) → `_plan` → `_write` (version under `select_for_update`,
  `tradesheet.py:350-357`) → audit `tradesheet.replanned`. Next ladder round →
  re-reads latest (v2) inside the lock → refuses until authorised → after
  authorise, `schedule_window` starts at `authorized_at` (a short window if
  late -- intended).
- **Error propagation.** Quote failure → nothing changed. `Busy` → try again.
  `ReplanRefused` → message, nothing changed. Any error inside the atomic block
  rolls back v2.
- **State lifecycle.** Replan writes one version and one audit row in one
  transaction; it changes no order and no earlier version.
- **API surface parity.** Web Today and tradesheet detail, the command; phone
  reads only.
- **Performance/records.** `performance` and `records` already read per day
  (`performance/returns.py:75`, `ledgers.py:55,186-188`,
  `orders/reconcile.py:88,257`).

## Acceptance criteria

- [x] Phase 0: paper plans rank against lots; `Holding` is untouched by ranking; a stale ladder or fallback never places for a superseded or stopped version; a cancel never overwrites a fill; `market_fallback` handles `Busy`.
- [x] Phase 1: a multi-version day settles into one ledger row equal to the sum of every version's fills and charges; single-version days settle exactly as before (existing settlement tests pass unchanged).
- [x] Phase 2: every refusal in R3/R4/R7 has a test; the replanned version meets R5/R6/R8/R11; the end-to-end test shows no double buy.
- [x] Phase 3: Today (web, phone) shows day totals; a replanned day never displays as skipped.
- [x] Regenerate still refuses after a fill (`src/orders/tests/test_control.py:233-244`, `src/engine/tests/test_planning.py:132` unchanged).
- [x] `manage.py makemigrations --check` reports no changes.
- [x] `npm run build` run after template class changes; `app.css` committed (the build produced no change).
- [x] PRD, D13, `CLAUDE.md` and `OPERATIONS.md` updated.

## Implementation notes (2026-09-24)

Where the build departed from, or added to, the plan above:

- **Stop today takes the order lock** (`order_lock(wait_seconds=15)`), and
  reads the sheet again under it. Not in the plan: re-reading the sheet in the
  ladder closes the replan race, but a round that had already re-read a running
  sheet could still place after a stop that did not wait for it.
- **A stopped version may be SKIPPED as well as HALTED.** Stop today on a
  version with no fills of its own skips it; replan accepts either.
- **`replan` reads the sheet from the database first.** The end-to-end test
  found it judging the caller's stale copy.
- **Today's cash line and the phone's Stop screen** share one figure:
  `settlement.planned_budget(sheet)` (the running version's budget plus what
  earlier versions spent, as recorded when it was replanned) less
  `day_spend` (every version's fills, charges as settlement counts them).
- **Not changed, deliberately:** `web.views._what_each_day_did` keeps reading
  one version (its name and count must come from the same version);
  `reconcile._reconcile_one` is left for the live-replan follow-up; the trading
  board's tick list is not yet grouped by version.
- **Gap review (same day) fixed:** a day that has bought is never SKIPPED --
  skip, Stop today and the fallback's never-authorized path ask whether the
  *day* bought (`placement.day_has_bought`), not the version; a resting
  rebalance sell no longer blocks a replan (only resting daily-buy orders do);
  a sell cancelled or rejected without a fill no longer keeps its name out;
  Today's cash line counts estimated charges as settlement does; `buy_sheet`
  shows a replanned version before its first order; the paper book is one
  query and honours superseded lots; the replan form reads the placement
  close from configuration.
- **Gap review, deliberately not done:** `require_complete` before ranking a
  paper day from lots -- the paper book is paper fills, and refusing to plan on
  an install without an opening import would stop every paper day; and the
  `_plan` split -- `_write_replan` repeats `_generate`'s rank/select/write
  sequence, a follow-up refactor rather than a change to the daily planner on
  a trading day.
- **Audit of per-version filters** (`tradesheet=` in `src/orders`,
  `src/engine`, `src/rebalance`): every remaining one is per-version accounting
  -- the ladder's and the close's own orders and ticks, committed and filled
  quantities, the detail page, rejection counting. The close's cancel sweeps are
  per version, which is safe because a replan requires nothing resting on an
  earlier one.

## Risks

- **A per-version query missed in review.** Before Phase 2, audit every
  `tradesheet=` filter in `src/orders` and `src/engine`
  (`rg "tradesheet=" src/orders src/engine`) and classify each as per-version
  (ladder's own accounting) or per-day (money, display, settlement).
- **Estimated charges** make the replan budget approximate; D7 covers an
  overhang.
- **Paper-only** means the feature cannot be used on the first live days.

## Follow-ups (not in this plan)

- **Live replan.** Preconditions: `reconcile.from_broker` + `record_fills` for
  the day inside the lock before netting; cancels confirmed from the broker's
  order book (CANCELLED/COMPLETE, not CANCEL PENDING); live holdings =
  `quantity + t1_quantity − used_quantity` from a sync taken at replan time,
  plus today's buy fills after that sync (today's CNC buys sit in positions
  until T+1 -- [Kite portfolio docs](https://kite.trade/docs/connect/v3/portfolio/),
  [Zerodha support](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-holdings/articles/what-does-t1-above-the-shares-i-ve-bought-mean)).
  Verify same-day sell effects on `used_quantity` against
  `/portfolio/positions` on the first live sell.
- **Order lock under systemd.** Replace the `/tmp` flock with a Postgres
  session advisory lock (`pg_try_advisory_lock`, one fixed key), or move it to a
  shared `/run/steadystack/` directory, before Phase 4 units use `PrivateTmp=`.
- **`_apply_paper_holdings`** makes `Holding` a copy of lots in paper mode;
  decide whether paper keeps a broker view at all.

## Operational note for 25 Sep 2026

Until Phase 0a lands, tomorrow's first v3 sheet ranks against a near-empty
`Holding` and will spread the ~₹4.7 lakh carry across all 20 names by weight
instead of filling v3's new names first. Ship Phase 0a before 09:45, or hold
tomorrow's plan until it lands.

## Sources

- Research this session: code-path map (planning, settlement, ladder,
  fallback, locking, views); git history of the lock (`b6737f9`, `c1b119d`,
  `d0fb663`, `b035936`, `2c8e123`); flow analysis; seven plan reviews.
- `engine/lock.py:28,57-59,76`, `engine/settlement.py:58-122,165,189-222`,
  `engine/ledger.py:178-199,232,259-262,353-367`,
  `engine/tradesheet.py:62-72,75-108,127,141-151,154-257,350-357`,
  `engine/planning.py:52-100`, `engine/authorization.py:26-80`,
  `engine/reserve.py:128`, `orders/placement.py:338-396`,
  `orders/ladder.py:170,216-217,352-366`,
  `orders/fallback.py:119,136-161,203-231,543`,
  `orders/idempotency.py:39,54,62-85`, `orders/models.py:193,288`,
  `orders/paperfill.py:108-115`, `orders/reconcile.py:196-279`,
  `portfolio/holdings.py:60-101`, `portfolio/models.py:79-83`,
  `rebalance/batches.py:180-190,419-434`, `web/deck.py:521-523`,
  `web/phone.py:373`, `web/trading.py:54-72,755`, `ops/local/run_day.sh:88-92`.
- `docs/plans/2026-09-23-design-gaps.md:28`, `docs/plans/2026-09-23-review-gaps.md`.
- Kite Connect: https://kite.trade/docs/connect/v3/portfolio/ ;
  https://kite.trade/forum/discussion/15315 ; https://kite.trade/forum/discussion/2053
- Django: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update ;
  Postgres advisory locks: https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS
