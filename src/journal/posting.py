"""The only way an entry is written.

`post()` checks in Python what the database will check again at commit --
balance, at least two lines, paper-only accounts -- so a mistake fails at the
call that made it rather than at a commit three frames away. The database
check stays, because code is not the only thing that can write a row.

Numbers are gapless per book, allocated under an advisory lock. Not
`SELECT ... FOR UPDATE`: the runtime role has UPDATE revoked on this table,
and Postgres refuses a row lock without it.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import connection, transaction
from django.db.models import Max

from audit.context import current_trace_id, today_ist
from audit.events import SYSTEM_ACTOR, record
from audit.logging import fields
from journal.accounts import PAPER_ONLY
from journal.models import Book, EntryKind, JournalEntry, JournalLine

logger = logging.getLogger("steadystack.journal")

SIX = Decimal("0.000001")
ZERO = Decimal("0")

#: One advisory-lock key per book, so the books never wait on each other.
_NUMBER_LOCKS = {Book.LIVE: 7_310_442_901_115_001, Book.PAPER: 7_310_442_901_115_002}


def lock_book(book: str) -> None:
    """Serialise with every post to *book* until this transaction ends.

    `post()` takes it to issue gapless numbers. A reader that must not miss an
    entry being written -- the daily summary -- takes it too: an entry's
    `created_at` is stamped under the lock but becomes visible only when its
    transaction commits, and waiting here is what makes it visible.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_NUMBER_LOCKS[book]])


class JournalError(Exception):
    """An entry that would break the journal's rules."""


@dataclass(frozen=True)
class Line:
    account: str
    amount: Decimal
    instrument_id: str | None = None
    component: str = ""
    lot_id: object = None


def q6(value) -> Decimal:
    return Decimal(value).quantize(SIX, rounding=ROUND_HALF_UP)


def _checked(book: str, lines: Iterable[Line], *, keep_zero: bool = False) -> list[Line]:
    kept = [
        Line(line.account, q6(line.amount), line.instrument_id, line.component, line.lot_id)
        for line in lines
        if keep_zero or q6(line.amount) != ZERO
    ]
    if len(kept) < 2:
        raise JournalError("An entry needs at least two lines with an amount.")
    total = sum((line.amount for line in kept), ZERO)
    if total != ZERO:
        raise JournalError(f"The lines do not balance: they sum to {total}.")
    if book == Book.LIVE and any(line.account in PAPER_ONLY for line in kept):
        raise JournalError("Paper funding is a paper-book account.")
    return kept


def _refuse_inside_the_opening(
    book: str, kind: str, occurred_on: date, reverses: JournalEntry | None
) -> None:
    """A real event dated on or before the standing opening is already in it (NI2).

    `open_books` nets everything the live book holds through its date into
    Zerodha's cash as the statement shows it, so an event dated inside that
    would be counted twice. The opening itself and its reversal are the
    exceptions; the paper book, and a live book not yet opened, are not bound.
    """
    if book != Book.LIVE or kind == EntryKind.OPENING_BALANCE:
        return
    if reverses is not None and reverses.kind == EntryKind.OPENING_BALANCE:
        return
    from journal.external import standing_opening

    opening = standing_opening()
    if opening is not None and occurred_on <= opening.occurred_on:
        raise JournalError(
            f"{occurred_on} is on or before the opening balance of {opening.occurred_on}; "
            "the live books begin there."
        )


@transaction.atomic
def post(
    *,
    book: str,
    kind: str,
    occurred_on: date,
    lines: list[Line],
    description: str,
    actor: str = SYSTEM_ACTOR,
    source_type: str = "",
    source_id: str = "",
    idempotency_key: str | None = None,
    reverses: JournalEntry | None = None,
    keep_zero: bool = False,
) -> JournalEntry:
    """Append one balanced entry. Idempotent on `idempotency_key`.

    Lines of zero are dropped, unless *keep_zero*: the one entry that must
    exist even when it moves nothing is the live book's opening, which marks
    the book as open (`journal.external.live_book_opened`).

    The key is looked up *after* the per-book advisory lock is taken, not
    before: two concurrent posts with the same key would otherwise both pass
    the lookup and race to insert, and the loser would hit a raw
    `IntegrityError` on the unique constraint instead of returning the
    winner's entry. A key never spans books, so both callers contend for the
    same book's lock and the race closes there. Shape validation may still
    run before the lock -- it does not touch the row a concurrent caller
    could be racing to write.
    """
    kept = _checked(book, lines, keep_zero=keep_zero)

    lock_book(book)

    if idempotency_key:
        existing = JournalEntry.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing

    # The database refuses this too (0003), but as a raw error no view expects.
    # After the key lookup, so a replay of an entry since closed still returns.
    from journal.periods import is_closed

    if is_closed(book, occurred_on):
        raise JournalError(
            f"{occurred_on} falls in a financial year of the {book} book that is closed."
        )
    _refuse_inside_the_opening(book, kind, occurred_on, reverses)

    last = JournalEntry.objects.filter(book=book).aggregate(n=Max("number"))["n"] or 0

    entry = JournalEntry.objects.create(
        book=book,
        number=last + 1,
        kind=kind,
        occurred_on=occurred_on,
        description=description[:255],
        source_type=source_type,
        # A reference typed into a form can be longer than the column; the
        # reference still reads in full in the entry's description.
        source_id=str(source_id)[:64],
        idempotency_key=idempotency_key,
        reverses=reverses,
        actor=actor,
        trace_id=current_trace_id(),
    )
    JournalLine.objects.bulk_create(
        [
            JournalLine(
                entry=entry,
                account_id=line.account,
                amount=line.amount,
                instrument_id=line.instrument_id,
                component=line.component,
                lot_id=line.lot_id,
            )
            for line in kept
        ]
    )

    record(
        "journal.posted",
        actor=actor,
        entity=entry,
        after={
            "book": book,
            "number": entry.number,
            "kind": kind,
            "occurred_on": str(occurred_on),
            "lines": [[line.account, str(line.amount), line.instrument_id or ""] for line in kept],
        },
    )
    logger.info(
        "journal entry posted",
        extra=fields(book=book, number=entry.number, kind=kind, occurred_on=str(occurred_on)),
    )
    return entry


def reverse(
    entry: JournalEntry, *, reason: str, actor: str, on: date | None = None
) -> JournalEntry:
    """Undo an entry by appending its mirror (spec §6, `reversal`).

    Dated *on*, or today. An opening is the exception: its reversal defaults
    to the opening's own date, unless that year is closed, so no trial
    balance between the two dates shows the opening twice.
    """
    if entry.kind == EntryKind.REVERSAL:
        raise JournalError("A reversal is not reversed; post the entry again instead.")
    if JournalEntry.objects.filter(reverses=entry).exists():
        raise JournalError("That entry is already reversed.")
    if on is None and entry.kind == EntryKind.OPENING_BALANCE:
        from journal.periods import is_closed

        if not is_closed(entry.book, entry.occurred_on):
            on = entry.occurred_on

    return post(
        book=entry.book,
        kind=EntryKind.REVERSAL,
        occurred_on=on or today_ist(),
        description=f"reverses #{entry.number}: {reason}",
        lines=[
            Line(line.account_id, -line.amount, line.instrument_id, line.component, line.lot_id)
            for line in entry.lines.all()
        ],
        actor=actor,
        source_type="journal_entry",
        source_id=str(entry.id),
        idempotency_key=f"reversal:{entry.id}",
        reverses=entry,
        keep_zero=entry.kind == EntryKind.OPENING_BALANCE,
    )
