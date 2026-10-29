"""Money arriving, leaving, and being given to the engine (spec §3, §6, §7.4).

Two ideas carry this module:

  * **an allocation is the budget.** The daily amount, a lumpsum and
    reinvested sale proceeds all move money from *unallocated* to *engine*;
    carry is simply what the engine account holds;
  * **a sweep leaves carry owed.** SEBI's quarterly settlement empties the
    account, engine carry included. What it took from the engine is owed
    back, and the next contribution repays it before anything else.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from audit.events import SYSTEM_ACTOR
from journal.accounts import (
    ACCOUNT_CHARGES,
    CONTRIBUTIONS,
    ENGINE_CASH,
    PAPER_FUNDING,
    UNALLOCATED,
    WITHDRAWALS,
)
from journal.balances import _lines, balance
from journal.models import Book, EntryKind, JournalEntry
from journal.posting import Line, lock_book, post, q6

ZERO = Decimal("0")

DAILY = "daily"
SWEEP = "quarterly_settlement"
RESTORE = "carry_restore"
#: The owner's own cost (J7), paid out of the account like a withdrawal.
OWNER_EXPENSE = "owner_expense"
LUMPSUM = "lumpsum"
REINVEST = "reinvest"


class CashError(Exception):
    pass


@dataclass(frozen=True)
class Lumpsum:
    contribution: JournalEntry
    restored: Decimal
    allocation: JournalEntry | None


def _positive(amount) -> Decimal:
    amount = q6(amount)
    if amount <= ZERO:
        raise CashError("An amount must be positive.")
    return amount


def allocate(
    amount,
    *,
    book: str,
    on: date,
    actor: str,
    description: str,
    source_type: str,
    source_id: str = "",
    idempotency_key: str | None = None,
    source_account: str | None = None,
) -> JournalEntry:
    amount = _positive(amount)
    source = source_account or (PAPER_FUNDING if book == Book.PAPER else UNALLOCATED)
    return post(
        book=book,
        kind=EntryKind.ALLOCATION,
        occurred_on=on,
        description=description,
        lines=[Line(ENGINE_CASH, amount), Line(source, -amount)],
        actor=actor,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=idempotency_key,
    )


def allocate_daily(
    day: date, *, book: str | None = None, actor: str = SYSTEM_ACTOR
) -> JournalEntry | None:
    """The day's budget, once per day and book. Nothing on a closed or paused day."""
    from configuration import pause
    from configuration.store import current
    from trading_calendar.sessions import is_trading_day

    config = current()
    book = book or config.order_mode
    if not is_trading_day(day) or pause.is_paused(on=day) or config.daily_amount <= ZERO:
        return None
    return allocate(
        config.daily_amount,
        book=book,
        on=day,
        actor=actor,
        description=f"daily amount for {day}",
        source_type=DAILY,
        source_id=day.isoformat(),
        idempotency_key=f"{DAILY}:{book}:{day.isoformat()}",
    )


def daily_allocation_for(day: date, *, book: str) -> Decimal:
    return balance(ENGINE_CASH, book=book, on=day, source_type=DAILY)


def _unreversed(source_type: str, through: date | None) -> Decimal:
    """Engine cash moved by entries of *source_type* that stand unreversed.

    A reversal carries `source_type="journal_entry"`, so a plain source-type
    sum would still count a sweep (or a restore) that was taken back.

    Only entries dated after the standing opening count: the opening moved
    whatever the engine held before go-live into unallocated, so a sweep
    before it owes nothing and a restore before it repaid nothing.
    """
    from journal.external import standing_opening

    lines = _lines(ENGINE_CASH, book=Book.LIVE, through=through, source_type=source_type)
    opening = standing_opening()
    if opening is not None:
        lines = lines.filter(entry__occurred_on__gt=opening.occurred_on)
    total = lines.filter(entry__reversed_by__isnull=True).aggregate(total=Sum("amount"))["total"]
    return q6(total or ZERO)


def carry_owed(*, through: date | None = None) -> Decimal:
    """Engine carry the sweeps took and contributions have not yet restored.

    The owner's own cost -- the Kite Connect fee -- counts too: it is paid
    from unallocated first, and daily money is allocated at once, so it
    usually comes out of engine carry, and nothing else would give it back.

    With *through*, as it stood at the end of that day: a restore repays only
    a sweep dated on or before it, whatever order the two were recorded in.
    """
    swept = -(_unreversed(SWEEP, through) + _unreversed(OWNER_EXPENSE, through))
    restored = _unreversed(RESTORE, through)
    return max(ZERO, q6(swept - restored))


@transaction.atomic
def contribute(
    amount,
    *,
    on: date,
    actor: str,
    reference: str = "",
    source_type: str = "contribution",
    source_id: str = "",
    idempotency_key: str | None = None,
    description: str | None = None,
    allocate_on: date | None = None,
) -> JournalEntry:
    """Money in, to unallocated; then any carry owed restored to the engine.

    The restore is dated *allocate_on* when given -- a lumpsum's allocation
    day (`allocated_on`), so carry restored on a day that has already bought
    is the next day's money like the rest of the lumpsum (M4) -- and on the
    contribution's own date otherwise.
    """
    amount = _positive(amount)
    # Held to the end of the transaction, so the carry owed read below cannot
    # change under a sweep being confirmed at the same moment.
    lock_book(Book.LIVE)
    entry = post(
        book=Book.LIVE,
        kind=EntryKind.CONTRIBUTION,
        occurred_on=on,
        description=description or f"contribution {reference}".strip(),
        lines=[Line(UNALLOCATED, amount), Line(CONTRIBUTIONS, -amount)],
        actor=actor,
        source_type=source_type,
        source_id=source_id or reference,
        idempotency_key=idempotency_key,
    )
    take = min(carry_owed(through=on), amount)
    if take > ZERO:
        allocate(
            take,
            book=Book.LIVE,
            on=allocate_on or on,
            actor=actor,
            description="carry restored after the quarterly settlement",
            source_type=RESTORE,
            source_id=str(entry.id),
            idempotency_key=f"{RESTORE}:{entry.id}",
        )
    return entry


def restored_by(contribution: JournalEntry) -> Decimal:
    """How much of a contribution went to repaying swept carry."""
    entry = JournalEntry.objects.filter(idempotency_key=f"{RESTORE}:{contribution.id}").first()
    if entry is None:
        return ZERO
    return q6(sum((line.amount for line in entry.lines.filter(account_id=ENGINE_CASH)), ZERO))


def allocated_on(received_on: date) -> date:
    """The day a lumpsum received on *received_on* is allocated to (M8a).

    That day, while it can still be replanned; the next trading day once it
    has placed its buy (`engine.lock.why_locked`), so a replan of a decided
    day (M15i) spends only what it already had.
    """
    from engine.lock import why_locked
    from trading_calendar.sessions import next_trading_day

    return next_trading_day(received_on) if why_locked(received_on) else received_on


@transaction.atomic
def record_lumpsum(
    amount,
    *,
    received_on: date,
    actor: str,
    reference: str = "",
    idempotency_key: str | None = None,
) -> Lumpsum:
    """Money in, and all of it -- less any carry it restored -- budget (M8a).

    The contribution is real money and always live. The allocation goes to the
    engine's own book, so a lumpsum during paper trading funds the paper
    engine while the rupees sit in live *unallocated*, where they really are.
    Paper trading does not draw down what a live carry restore already took,
    since that restore happened in the live book regardless: the paper engine
    still gets the lumpsum in full.

    The allocation, and any carry the lumpsum restores, is dated by
    `allocated_on`; the contribution keeps its real date -- the money did
    arrive then.
    """
    from configuration.store import current

    amount = _positive(amount)
    book = current().order_mode
    on = allocated_on(received_on)
    contribution = contribute(
        amount,
        on=received_on,
        actor=actor,
        reference=reference,
        source_type=LUMPSUM,
        source_id=reference,
        idempotency_key=idempotency_key,
        allocate_on=on,
    )
    restored = restored_by(contribution)
    rest = q6(amount - restored) if book == Book.LIVE else amount
    allocation = None
    if rest > ZERO:
        allocation = allocate(
            rest,
            book=book,
            on=on,
            actor=actor,
            description=f"lumpsum {reference}".strip(),
            source_type=LUMPSUM,
            source_id=str(contribution.id),
            idempotency_key=f"{LUMPSUM}:{contribution.id}",
        )
    return Lumpsum(contribution=contribution, restored=restored, allocation=allocation)


@transaction.atomic
def withdraw(
    amount,
    *,
    on: date,
    actor: str,
    reason: str,
    sweep: bool = False,
    source_type: str = "withdrawal",
    source_id: str = "",
    idempotency_key: str | None = None,
) -> JournalEntry:
    """Money back to the owner. Unallocated first, then engine carry (spec §6).

    Only a sweep (`source_type=SWEEP`) and the owner's own cost
    (`OWNER_EXPENSE`) count toward `carry_owed`: an ordinary withdrawal may
    still spill into engine cash when unallocated cannot cover it, but the
    owner took that on purpose, so nothing is owed back for it.
    """
    amount = _positive(amount)
    if sweep:
        source_type = SWEEP
    # Split on what unallocated held through the withdrawal's own date, under
    # the book lock: a line dated later, or one being posted right now, must
    # not change how an earlier movement divided.
    lock_book(Book.LIVE)
    held = balance(UNALLOCATED, book=Book.LIVE, through=on)
    from_unallocated = min(amount, max(ZERO, held))
    lines = [
        Line(WITHDRAWALS, amount),
        Line(UNALLOCATED, -from_unallocated),
        Line(ENGINE_CASH, -(amount - from_unallocated)),
    ]
    return post(
        book=Book.LIVE,
        kind=EntryKind.WITHDRAWAL,
        occurred_on=on,
        description=reason,
        lines=lines,
        actor=actor,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=idempotency_key,
    )


def account_charge(
    amount,
    *,
    on: date,
    component: str,
    actor: str,
    description: str,
    source_id: str = "",
    idempotency_key: str | None = None,
) -> JournalEntry:
    amount = _positive(amount)
    return post(
        book=Book.LIVE,
        kind=EntryKind.ACCOUNT_CHARGE,
        occurred_on=on,
        description=description,
        lines=[Line(ACCOUNT_CHARGES, amount, component=component), Line(UNALLOCATED, -amount)],
        actor=actor,
        source_type="account_charge",
        source_id=source_id,
        idempotency_key=idempotency_key,
    )
