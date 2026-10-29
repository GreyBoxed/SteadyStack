"""The engine's cash, read from the journal.

Carry is no longer a column somebody writes: it is the balance of the engine's
accounts (spec §4), and every rupee in it arrived by an allocation and left by
a fill. Three rules follow:

  * **the day's budget is an allocation**, posted when the day is planned
    (`journal.cash.allocate_daily`). Asking what a day is worth writes
    nothing, so a screen can ask before the day is planned;
  * **carry may go negative** when a fill costs more than the day was sized
    at. What the engine may spend is floored at zero (D7) and the next
    allocation repays the rest -- there is no overhang column to carry it;
  * **sale proceeds are an allocation too.** A settled rebalance batch's net
    proceeds reach the engine as a `reinvest` allocation (D-S1); the row
    shows them in their own column, apart from the other extra allocations;
  * **the daily row is a summary.** `settle_day` reads what the engine
    account did since the previous row and writes it down once. Each row
    balances on its own and opens where the previous one closed.

One quantisation point for display, `money()`, to two decimal places. The
journal itself keeps six, because Zerodha's statement does.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from django.db import transaction
from django.db.models import Q, Sum

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from engine.models import CashLedger
from journal.accounts import ENGINE_ACCOUNTS, ENGINE_CASH, INVESTMENTS, SETTLE_ENGINE
from journal.balances import engine_balance
from journal.models import Component, EntryKind, JournalLine
from journal.posting import lock_book, q6
from trading_calendar.sessions import is_trading_day

logger = logging.getLogger("steadystack.engine")

PAISE = Decimal("0.01")
ZERO = Decimal("0")


def money(value: Decimal) -> Decimal:
    """The single place a rupee amount is rounded for display."""
    return Decimal(value).quantize(PAISE, rounding=ROUND_HALF_UP)


class LedgerError(Exception):
    """A ledger operation that would break one of the rules."""


@dataclass(frozen=True)
class DayOpening:
    """What a trading day has to spend, and where it came from."""

    trading_date: date
    carry: Decimal
    daily_allocation: Decimal

    @property
    def spendable(self) -> Decimal:
        """`max(0, ...)` is D7's floor: an overspend is repaid, never spent twice."""
        return max(ZERO, money(self.carry + self.daily_allocation))

    @property
    def shortfall(self) -> Decimal:
        raw = money(self.carry + self.daily_allocation)
        return -raw if raw < ZERO else ZERO


def engine_book(book: str | None = None) -> str:
    from configuration.store import current

    return book or current().order_mode


def opening_for(day: date, *, book: str | None = None) -> DayOpening:
    """What *day* has to spend (M14). Pure: it writes nothing.

    Before the day is planned its allocation is the configured amount it will
    receive; once planned, it is what was actually allocated. Sale proceeds
    allocated on *day* itself are left out until the next day. A paused day
    receives nothing -- accruing while paused would bring a month away back as
    one large purchase at whatever the market was doing that morning.
    """
    from configuration.pause import is_paused
    from configuration.store import current
    from journal.balances import balance
    from journal.cash import REINVEST, daily_allocation_for

    book = engine_book(book)
    posted = daily_allocation_for(day, book=book)
    # A rebalance batch closed today -- cancelled, stopped or at the close --
    # gives its proceeds to the engine dated today. The row shows them, but
    # they are the next day's money (D-S1): not today's plan, replan or cash
    # line (I-1).
    proceeds = balance(ENGINE_CASH, book=book, on=day, source_type=REINVEST)
    carry = engine_balance(book, through=day) - posted - proceeds

    if posted:
        daily = posted
    elif not is_trading_day(day) or is_paused(on=day):
        daily = ZERO
    else:
        daily = q6(current().daily_amount)

    return DayOpening(trading_date=day, carry=q6(carry), daily_allocation=q6(daily))


def spendable_on(day: date, *, book: str | None = None) -> Decimal:
    return opening_for(day, book=book).spendable


def closing_carry_before(day: date, *, book: str | None = None) -> Decimal:
    """The engine's balance at the end of the previous calendar day."""
    return money(engine_balance(engine_book(book), through=day - timedelta(days=1)))


def row_for(day: date, *, book: str | None = None) -> CashLedger | None:
    return CashLedger.objects.filter(book=engine_book(book), trading_date=day).first()


def is_settled(day: date, *, book: str | None = None) -> bool:
    return row_for(day, book=book) is not None


def book_of_day(day: date) -> str:
    """The book *day* was planned in: its latest tradesheet's, stored when it
    was planned; the mode in force for a day not yet planned (M1)."""
    from engine.models import TradeSheet

    latest = TradeSheet.objects.filter(trading_date=day).order_by("-version").first()
    return latest.book if latest is not None else engine_book()


def day_settled(day: date) -> bool:
    """Whether *day* has its own book's ledger row (M1).

    A row in the other book says nothing about this day: around a switch of
    mode, the two books' rows for one date are two different days' worth.
    """
    return is_settled(day, book=book_of_day(day))


def _unsummarised(book: str, day: date, previous: CashLedger | None):
    """Engine-account lines no earlier row counted, dated on or before *day*.

    An entry dated into a day that already has its row -- a statement line
    confirmed a week late -- is counted by the next row rather than lost.
    """
    lines = JournalLine.objects.filter(
        entry__book=book, account_id__in=ENGINE_ACCOUNTS, entry__occurred_on__lte=day
    )
    if previous is not None:
        lines = lines.exclude(
            Q(entry__occurred_on__lte=previous.trading_date)
            & Q(entry__created_at__lte=previous.created_at)
        )
    return lines


def _total(lines) -> Decimal:
    return q6(lines.aggregate(total=Sum("amount"))["total"] or ZERO)


@dataclass(frozen=True)
class DaySummary:
    """What a day's ledger row says, before anything is written."""

    trading_date: date
    opening_carry: Decimal
    daily_allocation: Decimal
    extra_allocation: Decimal
    proceeds: Decimal
    cash_spent: Decimal
    charges: Decimal
    other: Decimal
    closing_carry: Decimal


def summarise_day(day: date, *, book: str | None = None) -> DaySummary:
    """The row *day* would settle as, read from the journal. Writes nothing.

    `settle_day` writes exactly this; a screen may ask it of a day that has
    not settled yet, and gets what the row would say if it settled now.
    """
    from journal.cash import DAILY, REINVEST

    book = engine_book(book)
    previous = (
        CashLedger.objects.filter(book=book, trading_date__lt=day).order_by("-trading_date").first()
    )
    window = _unsummarised(book, day, previous)
    allocations = window.filter(entry__kind=EntryKind.ALLOCATION, account_id=ENGINE_CASH)
    daily = _total(allocations.filter(entry__source_type=DAILY))
    # A rebalance batch's net sale money, given back to the engine (D-S1).
    # Neither a contribution nor the budget, so the row keeps it apart.
    proceeds = _total(allocations.filter(entry__source_type=REINVEST))
    extra = _total(allocations.exclude(entry__source_type__in=(DAILY, REINVEST)))

    fill_entries = window.filter(entry__kind=EntryKind.BUY_FILL, account_id=SETTLE_ENGINE).values(
        "entry_id"
    )
    fill_costs = JournalLine.objects.filter(entry_id__in=fill_entries, account_id=INVESTMENTS)
    spent = _total(fill_costs.filter(component=Component.GROSS))
    charges = _total(fill_costs.exclude(component=Component.GROSS)) - _total(
        window.filter(entry__kind=EntryKind.CHARGE_CORRECTION)
    )

    opening = previous.closing_carry if previous else ZERO
    closing = q6(opening + _total(window))
    other = q6(closing - (opening + daily + extra + proceeds - spent - charges))
    return DaySummary(
        trading_date=day,
        opening_carry=opening,
        daily_allocation=daily,
        extra_allocation=extra,
        proceeds=proceeds,
        cash_spent=spent,
        charges=charges,
        other=other,
        closing_carry=closing,
    )


@transaction.atomic
def settle_day(day: date, *, book: str | None = None, note: str = "") -> CashLedger:
    """Write the day's summary row, once. A second call returns the first."""
    if not is_trading_day(day):
        raise LedgerError(f"{day} is not a trading day; no ledger row is due.")

    book = engine_book(book)
    # Wait out any post to this book still in flight. Its lines were stamped
    # before this row will be, so without the lock a slow commit would fall
    # between this row's window and the next row's, and be counted by neither.
    lock_book(book)

    existing = row_for(day, book=book)
    if existing is not None:
        return existing

    later = CashLedger.objects.filter(book=book, trading_date__gt=day).order_by("trading_date")
    if later.exists():
        # A later row has already counted every line dated on or before its
        # day, this day's included; summarising this day now would count them
        # twice.
        raise LedgerError(
            f"{later.first().trading_date} is already summarised; {day} cannot be settled after it."
        )

    summary = summarise_day(day, book=book)
    opening, daily = summary.opening_carry, summary.daily_allocation
    extra, proceeds = summary.extra_allocation, summary.proceeds
    spent, charges = summary.cash_spent, summary.charges
    other, closing = summary.other, summary.closing_carry

    row = CashLedger.objects.create(
        book=book,
        trading_date=day,
        trace_id=current_trace_id(),
        opening_carry=opening,
        daily_allocation=daily,
        extra_allocation=extra,
        proceeds=proceeds,
        cash_spent=spent,
        charges=charges,
        other=other,
        closing_carry=closing,
        note=note[:255],
    )

    record(
        "ledger.day_settled",
        entity=row,
        after={
            "book": book,
            "trading_date": str(day),
            "opening_carry": str(opening),
            "daily_allocation": str(daily),
            "extra_allocation": str(extra),
            "proceeds": str(proceeds),
            "cash_spent": str(spent),
            "charges": str(charges),
            "other": str(other),
            "closing_carry": str(closing),
        },
    )
    if closing < ZERO:
        # Worth a warning every time: a fill went through above the price the
        # day was sized against, and the next allocation pays for it. The
        # `overspend` alarm matches this wording (infra/terraform/observability.tf).
        logger.warning(
            "the day spent more than it was sized against",
            extra=fields(trading_date=str(day), book=book, closing_carry=str(closing)),
        )
    else:
        logger.info(
            "ledger day settled",
            extra=fields(trading_date=str(day), book=book, closing_carry=str(closing)),
        )
    return row


def reconcile(
    start: date | None = None, end: date | None = None, *, book: str | None = None
) -> dict:
    """Every row balances, and each opens where the previous one closed."""
    book = engine_book(book)
    rows = CashLedger.objects.filter(book=book).order_by("trading_date")
    if start:
        rows = rows.filter(trading_date__gte=start)
    if end:
        rows = rows.filter(trading_date__lte=end)
    rows = list(rows)

    breaks = [
        {
            "date": str(current.trading_date),
            "problem": "opening carry does not match the previous closing",
            "expected": str(previous.closing_carry),
            "found": str(current.opening_carry),
        }
        for previous, current in pairwise(rows)
        if current.opening_carry != previous.closing_carry
    ]
    unbalanced = [str(row.trading_date) for row in rows if not row.reconciles]
    journal_breaks = _disagreements_with_journal(book, rows)
    return {
        "rows": len(rows),
        "unbalanced": unbalanced,
        "chain_breaks": breaks,
        "journal_breaks": journal_breaks,
        "ok": not unbalanced and not breaks and not journal_breaks,
        "net_in": str(money(sum((r.daily_allocation + r.extra_allocation for r in rows), ZERO))),
        "net_out": str(money(sum((r.cash_spent + r.charges for r in rows), ZERO))),
        # Money back from rebalance sales: neither a contribution nor a spend.
        "proceeds": str(money(sum((r.proceeds for r in rows), ZERO))),
    }


def _disagreements_with_journal(book: str, rows: list[CashLedger]) -> list[dict]:
    """Rows whose closing carry is not what the journal held when they were written.

    A row counts every engine-account line dated on or before its day that
    existed when it was written, so its closing carry must equal exactly that
    balance (spec section 8). A line posted later, however it is dated,
    belongs to a later row and is not part of this one.
    """
    found = []
    for row in rows:
        held = _total(
            JournalLine.objects.filter(
                entry__book=book,
                account_id__in=ENGINE_ACCOUNTS,
                entry__occurred_on__lte=row.trading_date,
                entry__created_at__lte=row.created_at,
            )
        )
        if held != q6(row.closing_carry):
            found.append(
                {
                    "date": str(row.trading_date),
                    "problem": "closing carry does not agree with the journal",
                    "expected": str(held),
                    "found": str(row.closing_carry),
                }
            )
    return found
