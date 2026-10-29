"""Checks that need no person: Zerodha's balance, and whether the engine is funded.

A difference is recorded and alarmed, never balanced away (spec §7). The
tolerance is a rupee because the rate card and Zerodha round a day's charges
differently until the statement's settlement line corrects them.

**The margins field.** Kite Connect's documented shape for `margins()` nests
`available.opening_balance` under `equity`, described as the funds ledger
balance the day started with -- what a Console statement calls its opening
balance. As at 2026-09-24, `audit_brokercall` in the main development
database held no `user.margins` call to verify that against a real response
(1,100 rows over seven endpoints: `market.quote` 1,007, `order.list` 56,
`portfolio.holdings` 11, `market.instruments` 10, `user.profile` 8,
`session.generate` 5, `order.trades` 3 -- no margins read has ever been
made). `opening_balance` is kept on the documented shape, but
is **unverified** against a live response; confirm it against a real
`BrokerCall` row and a Console statement's morning balance the first time
this runs against Kite, and switch to `cash` or `live_balance` if either
turns out to match the statement better.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from journal import cash
from journal.accounts import INVESTMENTS, UNALLOCATED
from journal.balances import balance, zerodha_cash
from journal.models import BalanceCheck, Book, JournalLine
from journal.posting import q6

logger = logging.getLogger("steadystack.journal")

TOLERANCE = Decimal("1.00")
ZERO = Decimal("0")


def zerodha_opening_balance(margins: dict) -> Decimal:
    return q6(Decimal(str(margins["equity"]["available"]["opening_balance"])))


def check_balance(*, broker, on: date) -> BalanceCheck | None:
    """Record Zerodha's opening balance against ours; None before go-live.

    Until `open_books` has run, the live book holds only the real events
    recorded so far -- a part of Zerodha's cash, not all of it -- and a check
    would alarm every morning over a difference nobody can act on.

    Nor on or before the opening's own date (I3). The check compares with the
    books at yesterday's close, which leaves out an opening dated *on*: the
    whole of Zerodha's balance against a fragment of it. `open_books --on` is
    the last trading day before go-live, so go-live morning is checked.
    """
    from journal.external import standing_opening

    opening = standing_opening()
    if opening is None or opening.occurred_on >= on:
        return None
    existing = BalanceCheck.objects.filter(checked_on=on).first()
    if existing is not None:
        return existing

    theirs = zerodha_opening_balance(broker.margins())
    ours = zerodha_cash(Book.LIVE, through=on - timedelta(days=1))
    row = BalanceCheck.objects.create(
        checked_on=on,
        ours=ours,
        theirs=theirs,
        difference=q6(theirs - ours),
        trace_id=current_trace_id(),
    )
    record("journal.balance_checked", entity=row, after={"ours": str(ours), "theirs": str(theirs)})
    if abs(row.difference) > TOLERANCE:
        # The line an alarm is written against; see OPERATIONS.md.
        logger.error(
            "zerodha balance disagrees with the books",
            extra=fields(
                checked_on=str(on),
                ours=str(ours),
                theirs=str(theirs),
                difference=str(row.difference),
            ),
        )
    return row


def funding_warnings() -> list[str]:
    """Nothing before go-live: until then the live book's cash is partial.

    Unallocated can go negative from an allocation, a direct buy or an
    account charge, so the warning says what it is rather than guess a cause.
    """
    from journal.external import live_book_opened

    if not live_book_opened():
        return []
    warnings = []
    unallocated = balance(UNALLOCATED, book=Book.LIVE)
    if unallocated < ZERO:
        warnings.append(
            f"Zerodha's unallocated cash is overdrawn by ₹{-unallocated:.2f}: transfer that "
            "much to Zerodha."
        )
    owed = cash.carry_owed()
    if owed > ZERO:
        warnings.append(
            f"The quarterly settlement or the Kite Connect fee took ₹{owed:.2f} of the "
            "engine's carry out of Zerodha. Transfer it back before the next session."
        )
    return warnings


def investments_disagree(*, book: str) -> dict[str, tuple[Decimal, Decimal]]:
    from journal.external import live_open_lots, paper_open_lots

    lots = paper_open_lots() if book == Book.PAPER else live_open_lots()

    at_cost: dict[str, Decimal] = {}
    shares: dict[str, int] = {}
    for lot in lots:
        at_cost[lot.instrument_id] = (
            at_cost.get(lot.instrument_id, ZERO) + lot.cost_per_unit * lot.remaining_quantity
        )
        shares[lot.instrument_id] = shares.get(lot.instrument_id, 0) + lot.remaining_quantity

    journalled = set(
        JournalLine.objects.filter(entry__book=book, account_id=INVESTMENTS).values_list(
            "instrument_id", flat=True
        )
    )
    out = {}
    for symbol in (set(at_cost) | journalled) - {None}:
        journal = balance(INVESTMENTS, book=book, instrument_id=symbol)
        lots_cost = q6(at_cost.get(symbol, ZERO))
        # A lot's cost per unit is stored to four decimals, so the lots can
        # sit a hundredth of a paisa per share away from what was paid.
        tolerance = Decimal("0.01") + Decimal("0.0001") * shares.get(symbol, 0)
        if abs(journal - lots_cost) > tolerance:
            out[symbol] = (journal, lots_cost)
    return out
