"""The cash flows XIRR is computed over, read from the journal (M41).

Money deployed into shares -- by the engine, directly, or as an IPO paid from
the owner's bank -- is an outflow; proceeds and dividends come back; account
charges are a cost of holding. Contributions and withdrawals of *cash* are
not flows of the portfolio: money sitting in Zerodha has not been put to work
(and the owner's own costs, J7, never touch it).
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from journal.accounts import (
    ACCOUNT_CHARGES,
    CONTRIBUTIONS,
    DIVIDENDS,
    INVESTMENTS,
    OPENING,
    PAPER_FUNDING,
    REALISED,
    SETTLE_ENGINE,
    SETTLE_OTHER,
    WITHDRAWALS,
)
from journal.balances import balance
from journal.models import Book, EntryKind, JournalLine
from journal.posting import q6

ZERO = Decimal("0")

#: Kinds whose Investments debit is money leaving for shares.
ACQUISITION_KINDS = (
    EntryKind.BUY_FILL,
    EntryKind.EXTERNAL_ACQUISITION,
    EntryKind.OPENING_BALANCE,
)
#: Kinds whose settlement-account debit is the net proceeds coming back.
DISPOSAL_KINDS = (EntryKind.SELL_FILL, EntryKind.EXTERNAL_DISPOSAL)


def book_for_performance() -> str:
    from configuration.store import current

    return current().order_mode


def flows(*, book: str, as_of: date) -> list[tuple[date, Decimal]]:
    """Every rupee that left for shares, every rupee that came back (spec §8).

    Outflows negative, inflows positive, one row per day that moved money.
    """
    by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    lines = JournalLine.objects.filter(
        entry__book=book, entry__occurred_on__lte=as_of
    ).select_related("entry", "entry__reverses", "lot")

    for line in lines:
        kind, account, day = line.entry.kind, line.account_id, line.entry.occurred_on
        if kind == EntryKind.REVERSAL and line.entry.reverses is not None:
            # A reversal's lines mirror the original's, so reading them as the
            # original's kind takes the flow back out on the day it was undone.
            kind = line.entry.reverses.kind
        if kind == EntryKind.OPENING_BALANCE and line.lot is not None:
            # Shares held before go-live left for the portfolio when they were
            # bought, not when the books opened: dated at go-live, years of
            # gain would read as return earned since (I10). A reversal carries
            # the lot too, so it takes the flow back out on the same date.
            day = line.lot.acquired_on
        if account == INVESTMENTS and kind in ACQUISITION_KINDS:
            # Net every 1300 line on the entry, not only the positive ones: a
            # charge estimate that overshot the contract note posts a
            # negative OTHER line, and that money was never spent.
            by_day[day] -= line.amount
        elif account in (SETTLE_ENGINE, SETTLE_OTHER) and kind in DISPOSAL_KINDS:
            by_day[day] += line.amount
        elif account == WITHDRAWALS and kind == EntryKind.EXTERNAL_DISPOSAL:
            by_day[day] += line.amount
        elif account == DIVIDENDS:
            by_day[day] -= line.amount  # a credit, so negative: an inflow
        elif account == ACCOUNT_CHARGES:
            by_day[day] -= line.amount
        elif kind == EntryKind.CHARGE_CORRECTION and account in (INVESTMENTS, REALISED):
            by_day[day] -= line.amount

    return [(day, q6(amount)) for day, amount in sorted(by_day.items()) if q6(amount) != ZERO]


def contributed(*, book: str, as_of: date) -> Decimal:
    """Money put to work, spec §8: the paper book's 3900, or in the live book
    3100 less 3200 plus 3300 -- the opening balance is the owner's capital
    too, in cash and in shares held at go-live (I10)."""
    if book == Book.PAPER:
        return -balance(PAPER_FUNDING, book=book, through=as_of)
    return -balance([CONTRIBUTIONS, WITHDRAWALS, OPENING], book=book, through=as_of)
