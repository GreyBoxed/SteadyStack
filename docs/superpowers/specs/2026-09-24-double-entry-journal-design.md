# steadystack — a double-entry journal for the money

**Date:** 2026-09-24
**Status:** Implemented (see docs/plans/2026-09-24-feat-double-entry-journal-plan.md)
**Relates to:** `docs/superpowers/specs/2026-09-21-steadystack-prd.md` (M8a, M35b,
M40a, M41, D7), `docs/superpowers/specs/2026-09-23-long-hold-bucket-design.md`
(§8, the dividend gap)

---

## 1. Purpose

The books are going to be read by a chartered accountant who reconciles them
against two statements: Zerodha's funds ledger (Console → Funds → Statement)
and the owner's bank statement. Today's cash ledger cannot support that. It is
a daily summary of what the engine was *allowed* to spend, not a record of
what money *did*:

- `CashLedger` holds one row per trading day. A lumpsum arriving on a Sunday
  has no row, so it waits in `LumpsumCredit`; a contract-note difference waits
  in `LedgerAdjustment`. Both tables are mutated when a day settles
  (`applied`, `applied_to`) and neither is protected by the append-only
  trigger — the record of money arriving is the one that can be edited.
- `deposit` is the configured daily amount, a budget. No real transfer into
  Zerodha is recorded anywhere.
- Spend and charges are one number per day. The charge components are
  computed by `portfolio/charges.py` and then thrown away; `Fill.charges`
  keeps the total.
- Sell proceeds never reach the ledger (`engine/settlement.py` counts only a
  sell's charges).
- AMC, DP charges, DDPI, delayed-payment interest and the Kite Connect fee
  appear on Zerodha's statement and nowhere in this system.
- Direct buys and IPO allotments enter the portfolio value through
  `ExternalTransaction` but never as money spent, so XIRR is overstated.

This document replaces the cash ledger's role as the source of truth with a
double-entry journal. The engine's daily budget, carry and the M40a screen
survive, rebuilt on top of it.

## 2. Decisions

| # | Decision |
|---|---|
| J1 | A double-entry journal is the single source of truth for money. Every movement is one balanced, append-only entry. |
| J2 | The books cover the Zerodha account only. That account is the portfolio's alone. The linked bank account is the owner's and is not kept; money crossing the boundary is a contribution or a withdrawal. |
| J3 | Zerodha cash is split into *unallocated* and *engine*. The daily amount, a lumpsum and reinvested sell proceeds are all **allocations** from one to the other. Carry is the *engine* balance. |
| J4 | Charges stay part of the shares' cost (D6), and each component is its own posting line. |
| J5 | Rate-card charges post at fill. The statement's net-settlement line is the check; a difference posts a correction entry and finalises the lots. The contract-note PDF is optional. |
| J6 | Dividends land in the owner's bank. Each is recorded as the portfolio's income and, in the same entry, a withdrawal to the owner. |
| J7 | The Kite Connect subscription is the owner's cost of automation, recorded as a withdrawal. AMC, DDPI and delayed-payment interest are the portfolio's account charges. |
| J8 | Amounts are stored to six decimal places, because Zerodha's statement is (`248.221084`). Screens show paise. |
| J9 | Periods close by financial year (April–March). A closed year accepts no entry dated inside it. |
| J10 | Existing paper history is not converted. The live book opens with an opening-balance entry on go-live day. |

## 3. Books

Every entry belongs to one **book**: `live` or `paper`.

- Real-world events always post to `live`, whatever the order mode:
  contributions, withdrawals, account charges, dividends, direct buys, IPOs
  and imported statement lines. The money is real even while the engine is
  paper-trading.
- The engine's own entries — allocations, fills, settlements, charge
  corrections for its orders — post to the book matching `ORDER_MODE`.
- In the paper book, allocations are credited from **Paper funding**, an
  account that exists only there. A real lumpsum recorded during paper mode
  therefore sits in live *unallocated*, where it actually is, while the paper
  engine receives its budget.

Nothing the CA is shown reads the paper book.

A day's book is fixed when it is planned: the tradesheet stores it, the day's
allocation posts to it, and the day settles into that book's `CashLedger` row
(`engine.ledger.day_settled`) even if the mode changes before 15:45. Lots
belong to a book too (their origin fill's mode; anything without a fill is
live). In live mode every lot reader sees the live lots only; in paper mode
the screens see every lot, and the paper engine ranks and holds against the
paper lots alone (`portfolio.lots.reader_book`). `DailyValuation` is kept
per book and read in the performance book.

## 4. Chart of accounts

Fixed, created by a data migration. Accounts are rows, not an enum, so a
report can join to them; they are never deleted.

| Code | Type | Account | Notes |
|---|---|---|---|
| 1100 | Asset | Zerodha cash: unallocated | |
| 1110 | Asset | Zerodha cash: engine | balance is carry |
| 1200 | Asset | Broker settlement: engine | engine trades awaiting settlement |
| 1210 | Asset | Broker settlement: other | direct trades and sells awaiting settlement |
| 1300 | Asset | Investments | sub-ledgered by instrument on the posting line |
| 3100 | Equity | Contributions | |
| 3200 | Equity | Withdrawals | |
| 3300 | Equity | Opening balance | |
| 3900 | Equity | Paper funding | paper book only |
| 4100 | Income | Realised gains | a loss is a debit |
| 4200 | Income | Dividends | |
| 5100 | Expense | Account charges | component on the line: AMC, DP, DDPI, interest, other |

Zerodha cash as the CA sees it is 1100 + 1110. What the engine may spend is
`max(0, 1110 + 1200)`: engine cash less what its own unsettled buys owe (D7's
floor). The engine balance may go negative when a fill costs more than it was
sized at; the next allocation repays it, and there is no `overhang` column.

## 5. Model

```
JournalEntry     id, book, number (gapless per book), kind, occurred_on,
                 description, source_type, source_id, reverses (unique, nullable),
                 actor, trace_id, created_at
JournalLine      id, entry, account, amount (signed, 20,6 — debit positive),
                 instrument (nullable), component (blank | gross | stt |
                 stamp_duty | exchange_txn | sebi | gst | brokerage | dp |
                 amc | ddpi | interest | other), lot (nullable; set only on
                 an opening entry's per-lot Investments lines, §8 Performance)
PeriodClose      fy_end (unique per book), book, closed_by, closed_at
StatementUpload  id, file (stored), sha256, period_from, period_to,
                 opening_balance, closing_balance, uploaded_by, uploaded_at
StatementLine    id, upload, fingerprint (unique), posting_date, particulars,
                 voucher_type, debit, credit, net_balance, matched_entry (nullable)
```

`number` is the book's highest number plus one, read under a per-book
advisory lock (`pg_advisory_xact_lock`) held to commit, so voucher numbers
have no gaps; a Postgres sequence would leave gaps on rollback, and an
auditor asks about gaps. Not a counter row locked for update: the runtime
role has UPDATE revoked on the journal, and Postgres refuses a row lock
without it.

`StatementLine.matched_entry` is the one field set after insert: a line
arrives unmatched and is matched once. A trigger permits exactly that update
(null → value), and one more -- replacing the entry, only once the entry it
was matched to has been reversed, so a line confirmed as the wrong kind can
be confirmed again -- and nothing else.

## 6. Entry kinds

Every entry balances to zero. `Dr` is positive, `Cr` negative.

| Kind | Lines | Source |
|---|---|---|
| `contribution` | Dr 1100 / Cr 3100 | statement line, or a lumpsum recorded in the app |
| `withdrawal` | Dr 3200 / Cr 1100, then Cr 1110 for what 1100 cannot cover | statement line (payout, quarterly settlement, Kite Connect fee) |
| `allocation` | Dr 1110 / Cr 1100 (paper: Cr 3900) | daily amount, lumpsum, reinvested proceeds |
| `buy_fill` | Dr 1300 gross + one Dr 1300 per charge component / Cr 1200 or 1210 | `Fill` |
| `sell_fill` | Dr 1210 net proceeds, Dr 4100 per charge component, Cr 1300 FIFO cost, Cr 4100 gross gain (gross − cost; negative for a loss) | `Fill` + `RealisedGain` |
| `settlement` | Dr 1200/1210 / Cr 1110/1100 (and the reverse for net proceeds) | trade date's net obligation |
| `charge_correction` | Dr/Cr 1300 (the held share) and/or Dr/Cr 4100 (the already-sold share) per instrument / Cr/Dr 1110 or 1100 | one per fill or lot: a settlement difference (7.3) or a contract note |
| `account_charge` | Dr 5100 (component) / Cr 1100 | statement line |
| `dividend` | Dr 3200 / Cr 4200 | recorded in the app |
| `external_acquisition` | Dr 1300 / Cr 1210 (paid from Zerodha) or Cr 3100 (IPO paid from the bank) | `ExternalTransaction` |
| `external_disposal` | Dr 1210 net proceeds, Dr 4100 per charge component, Cr 1300 FIFO cost, Cr 4100 gross gain (a sale); or Dr 3200 / Cr 1300 at cost, no gain (a transfer out) | `ExternalTransaction` |
| `reversal` | the exact mirror of `reverses` | operator, with a reason |
| `opening_balance` | Dr/Cr 1100; Cr/Dr 1110 and 1200 for what the engine held before go-live, moved into 1100 (the engine opens at nought); Dr 1300 per lot at the cost of what it still holds, the line carrying the lot / Cr 3300 | go-live |

Rules that apply to every kind:

- A lumpsum recorded in the app posts a `contribution` and an `allocation` in
  one transaction. When the statement is imported, the UPI line matches the
  contribution by reference number and amount; nothing is posted twice.
- The daily `allocation` is posted when the day is first planned, unique on
  `(book, occurred_on, kind='allocation', source_type='daily')`. A paused day
  and a non-trading day get none. As M8a says today, an allocation is spendable only by
  a tradesheet planned after it.
- Reinvested sell proceeds are allocated when their `settlement` posts — the
  money is then actually in Zerodha cash — if the batch's `reinvest` flag is
  set.
- A charge correction or any other late fact never touches an earlier entry.
  It is a new entry dated when it is known.
- An external sale or a transfer out posts `external_disposal`: shaped like
  `sell_fill` for a sale, or a plain disposal at cost with no gain for a
  transfer out, with `source_type = external_transaction`.
- Corporate actions without cash (bonus, split) remain lot operations and post
  nothing.
- Only a `contribution`, `withdrawal`, `account_charge`, `dividend`,
  `opening_balance` or `charge_correction` is reversed from the books. The
  engine's entries have lots or a cash-ledger row resting on them. An
  opening's reversal is dated on the opening's own date unless that year is
  closed.

## 7. Reconciliation

Three checks, each producing findings and never an automatic balancing entry.

**7.1 Daily balance.** Each trading morning, shortly after the session check,
Kite's `margins()` response gives the account's opening balance for the day.
It is compared with 1100 + 1110 + 1200 + 1210 in the live book as it stood at
the previous close. A difference beyond a rupee is recorded as a finding and
alerts, like a holdings divergence (M31a). The field read
(`equity.available.opening_balance`) is the documented shape, but unverified:
no `margins` call has yet been made against the real API, so it must be
confirmed against a live response the first live morning.

**7.2 Statement import.** The Console funds-ledger CSV
(`particulars, posting_date, cost_center, voucher_type, debit, credit,
net_balance`, with `Opening Balance` and `Closing Balance` rows) is uploaded
from the Records screen, capped at 1 MB -- a Console statement is a few dozen
lines of text, and a file that much larger is not one.

- The upload's opening balance must equal the previous upload's closing
  balance, or it is refused.
- Each row's fingerprint is its date, particulars, debit and credit plus its
  position among identical rows. Overlapping uploads insert nothing twice.
- Rows are classified by `voucher_type` and `particulars`:

  | Statement | Proposed kind |
  |---|---|
  | `Bank Receipts` / "Funds added using UPI … reference N" | `contribution` (match by reference) |
  | `Bank Payments` / "Funds auto-settled to the primary bank account with reference number N" | `withdrawal` (quarterly settlement, §7.4) |
  | "Payout done for quarterly settlement" | `withdrawal` (quarterly settlement, §7.4) |
  | `Bank Payments`, any other text (an owner-requested payout) | `withdrawal`, confirmed by hand |
  | `Book Voucher` / "Net settlement for Equity … settlement number N" | match to `settlement` |
  | `Journal Entry` / "AMC for Demat Account…" | `account_charge` (amc) |
  | `Journal Entry` / "Delayed payment charges…" | `account_charge` (interest) |
  | `Journal Entry` / "Charges for enabling DDPI" | `account_charge` (ddpi) |
  | `Journal Entry` / "Kite Connect API Charges…" | `withdrawal` (J7) |
  | anything else | queue |

- A row that matches an existing entry is linked. A row with a confident
  proposal is shown for one-click confirmation; nothing posts without a
  person confirming it. Unmatched rows wait in a queue with the kind chosen by
  hand.
- A settlement row matches the engine's `settlement` whose trade date is the
  row's posting date, falling back to the previous trading day -- matched on
  the date alone, whatever the two amounts say, because a disagreement there
  is the charges check (7.3), not a reason to leave the line unexplained.
  Evidence so far says the posting date is the trade date: settlement 2026153
  is dated 2026-08-14, and 14 August is the 153rd session of 2026 if the
  Sunday Budget session of 1 February counts.
- A row dated on or before the opening entry's date is inside the opening's
  cash: auto-matching links it to the opening (many rows may point at one
  entry) and confirming it is refused. `open_books` links rows already
  uploaded. The statement page warns when the chain's running balance at
  the opening date differs from the live book's Zerodha cash then.
- A queued row may be matched by hand to an existing entry: of the row's
  proposed kind (any of contribution, withdrawal, account charge for an
  unrecognised row), standing, unclaimed, within seven days either side, and
  moving Zerodha's cash by exactly the row's amount. It posts nothing and is
  audited (`journal.statement_matched`).
- A row dated inside a closed financial year is confirmed on the day it is
  confirmed, described "relates to <date>, after the year close" -- a late
  fact, dated when known.

**7.3 Charges.** Charges are a function of the trade amount, so the rate card
is the calculation and Kite Connect offers nothing per order to compare it
with. What can differ is the broker's rounding, which is done on the day's
aggregate rather than per fill. That difference appears only on the statement's
net-settlement line, so the settlement match in 7.2 is the charges check: a
difference is split across that trade date's fills in proportion to turnover
and posted as one `charge_correction` per fill, never one entry with a line
per instrument -- a buy fill's share finalises its lot at final basis through
contract-note finalisation, the same mechanism a contract note itself uses,
superseding the lot; a sell fill's share adjusts its realised gain directly.
The contract-note PDF is optional, for sampling. A correction is refused
beyond what rounding can explain -- ₹5, or 0.1% of the date's turnover when
that is more -- since a larger difference is a missing fill, a sale or a
wrong date, and booking it as charges would bury it in lot cost.

**7.4 Quarterly settlement.** SEBI's running-account settlement makes
Zerodha return a client's unused funds to the primary bank on the first Friday
of every quarter (monthly after 30 days' inactivity, which a daily buyer never
reaches), keeping back only open obligations. The balance -- the engine's
carry included -- leaves the account four times a year.

- It is a `withdrawal`, taken from *unallocated* first and *engine* for the
  rest.
- The part taken from *engine* is recorded on the entry as **carry owed**.
  The owner's own cost (the Kite Connect fee, J7) counts the same way when it
  is paid out of engine carry. Only sweeps and fees dated after the opening
  entry count: the opening moved the engine's earlier balance to unallocated. The
  next contribution posts an `allocation` restoring it before anything else is
  allocated, so the engine's budget survives the sweep while the books show
  what happened.
- Until that contribution arrives, the engine spends only what the account
  holds, and Today warns that the account needs funding before the next
  session. A live plan is capped at Kite's `margins` `live_balance` (a field
  still to be verified against a real response), falling back to the books'
  own Zerodha cash, `max(0, 1100 + 1110 + 1200 + 1210)`, when it cannot be
  read; the cap and its source are kept on the tradesheet.
- A restore made by a lumpsum is dated with the lumpsum's allocation: on a
  day that has already bought, the next trading day.

## 8. Changes to the existing system

**Engine.** `engine/ledger.py` is rewritten around the journal.
`opening_for(day)` returns the spendable engine balance in the mode's book,
less any `reinvest` allocation dated *day* itself -- sale proceeds are the
next day's money (D-S1), however early in the day their batch closed;
the settlement step posts `settlement` entries dated the trade date itself,
in both books, when that day's `settle_day` runs at end of day. A statement's
settlement line matches on that same posting date, falling back to the
previous trading day when the two do not coincide (§7.2).

**`CashLedger`** is kept as the append-only daily summary behind M40a, keyed
by `(book, trading_date)`. Its columns become `opening_carry`,
`daily_allocation`, `extra_allocation`, `proceeds` (the day's `reinvest`
allocations, kept apart from the other extra allocations so the row still
shows a rebalance sale's money as PRD M40a and D-S1 describe), `cash_spent`,
`charges`, `other` (a quarterly-settlement sweep, or carry restored after
one) and `closing_carry`; `adjustment`, `lumpsum` and `overhang` go. It is written from the journal when
the day settles, and `engine.ledger.reconcile()` asserts every row agrees
with the journal for that book. It is a view of the books, not the books.

**Removed:** `LumpsumCredit`, `LedgerAdjustment`, `post_adjustment`,
`credit_lumpsum` in their current form.

**`ExternalTransaction`** gains `paid_from` (`zerodha` | `bank`) and posts its
entry — a sale or a transfer out as `external_disposal`, a buy or a transfer
in as `external_acquisition`. **`RebalanceSell`** gains `reinvest` (default
on). **Contract-note finalisation** still supersedes the lot and writes a
`Correction`, and posts a `charge_correction` instead of a ledger adjustment;
the replacement lot carries the original's `external_transaction` link
forward, since both describe the same purchase.

**Records screen.** The lumpsum form (amount, date, UPI reference) stays on
the existing Cash ledger page. Books (`/books/`) gains: record a dividend (a
one-time submission token guards against a doubled POST), upload a statement,
the unmatched queue, reverse an entry, close a financial year.

**Performance.** XIRR flows come from the book matching the current order
mode -- paper while the engine trades paper, live once it trades live, never
a mix of the two. Investments debits are outflows (engine, direct and IPO
purchases alike), sell net proceeds and dividends are inflows, account
charges are outflows, terminal value last. An opening entry's Investments
line is dated by its lot's `acquired_on`, at the cost of what is still held:
shares sold before go-live are outside the portfolio and outside XIRR, and
years of gain before go-live are not read as return earned since. The live
benchmark comparison therefore reads **unavailable** ("No benchmark level
recorded for <date>") for as long as any opening lot is held: the benchmark
mirrors each flow at the index level of its date, and the daily value series
holds no level for a purchase made before go-live. Unavailable is chosen
over a comparison that would be wrong; benchmarking from the opening date
instead, with the opening lots at market value on that date, is a decision
not yet taken.
"Contributed" is 3100 less 3200 plus 3300 in the live book -- the opening
balance is the owner's capital too -- or 3900 in the paper book. This fixes the overstatement from
external purchases and closes the dividend gap in the long-hold design (§8
there).

**Reports** (screens and CSV, any date range): the journal, the general
ledger per account and the trial balance, for either book. The trial
balance shows income and expense for one financial year, earlier years'
brought forward on a single line. The reconciliation summary, per upload --
Zerodha's closing balance vs ours (1100 + 1110 + 1200 + 1210 through the
upload's last date); lines matched automatically, matched by hand,
confirmed, explained by the opening, queued -- is live-only: there is no
paper statement to reconcile a paper book against.

**Year close.** The live book closes a financial year only when a statement
is held through 31 March, no row dated in the year is queued, and no
settlement of the year still disagrees. The paper book has no statement,
and closes on the date alone.

A settlement difference that no correction can take -- larger than rounding
on the day's charges, on a date already settled, so neither **Correct
charges** nor `import_tradebook` accepts it -- is reconciled by hand and
then **explained**: a `SettlementExplanation` row naming the line, the
difference it saw, the reason, who and when. It is appended (never an edit
of the line), audited (`journal.settlement_explained`), posts nothing, and
holds only for the difference it names: if the line's difference moves, the
line disagrees again. An explained line no longer counts as disagreeing, on
the Statement page or for the year close.

## 9. Enforcement in the database

- `JournalEntry`, `JournalLine`, `PeriodClose`, `StatementUpload`,
  `BalanceCheck`, `SettlementExplanation` and `CashLedger` get the existing
  `steadystack_reject_mutation` trigger and the `REVOKE UPDATE, DELETE` from the
  runtime role.
- `journal.posting.post` refuses a live-book entry dated on or before the
  standing opening's date, other than the opening itself and its reversal:
  the opening's cash already holds everything dated inside it.
- `StatementLine` gets a trigger allowing only `matched_entry` null → value.
- A deferred constraint trigger on `JournalLine` rejects, at commit, any entry
  whose lines do not sum to zero or that has fewer than two lines.
- A trigger on `JournalEntry` rejects an `occurred_on` inside a closed
  financial year of that book.
- `reverses` is unique: an entry is reversed at most once, and a reversal
  cannot itself be reversed.

## 10. Testing

- Every entry kind: balances, lands in the right book, carries its trace id.
- The triggers: update, delete, unbalanced commit and closed-period insert
  are each refused at the database, run against Postgres.
- The sample statement's shape (synthetic rows, not the owner's): opening
  balance chain, every classification row in §7.2, a repeated upload, a UPI
  line matching an app-recorded lumpsum.
- Engine: spendable equals `max(0, 1110 + 1200)`; an overspend repays from the
  next allocation; paused and non-trading days allocate nothing; a lumpsum
  after a sheet is planned is not spent by it.
- `CashLedger` summary agrees with the journal over a seeded range.
- XIRR with an IPO acquisition and a dividend.

The charge-rate tests in `portfolio` are untouched.

## 11. Open items

1. **Owner-requested payout wording.** Zerodha documents the quarterly
   settlement texts but not the text of a withdrawal the owner requests. The
   classifier proposes nothing for an unrecognised `Bank Payments` row; it is
   classified by hand.
2. **Settlement posting date.** Evidence says trade date (§7.2); confirm
   against the Console tradebook for 13–14 August 2026. The match falls back a
   day either way.
3. **Budget session.** If NSE held a live session on Sunday 1 February 2026,
   `ops/calendar/nse-2026.csv` is missing it.

## 12. Documentation

- CLAUDE.md: "The cash ledger is append-only. One row per trading day" becomes
  "Every movement of money is a balanced, append-only journal entry; the daily
  cash ledger is a summary of the engine's book." The **carry** vocabulary
  entry becomes "the engine cash balance".
- PRD: M8a, M35b, M40a and the carry table in §6 are rewritten to match; a
  new M40c covers the statement import and the reports in §8.

## 13. Out of scope

Tax treatment of any kind; the owner's bank account beyond its crossings;
running costs outside the Zerodha account (EC2); year-end closing entries into
capital (a trial balance per financial year is what the CA takes); converting
paper history.
