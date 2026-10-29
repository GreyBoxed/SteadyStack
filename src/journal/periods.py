"""Financial-year closes (spec J9). April to March, one close per year per book."""

from datetime import date

from audit.context import current_trace_id, today_ist
from audit.events import record
from journal.models import Book, PeriodClose


class JournalPeriodError(Exception):
    pass


def fy_end_for(day: date) -> date:
    return date(day.year if day.month <= 3 else day.year + 1, 3, 31)


def is_closed(book: str, day: date) -> bool:
    return PeriodClose.objects.filter(book=book, fy_end__gte=day).exists()


def _refuse_unless_reconciled(fy_end: date) -> None:
    """The live book closes only once Zerodha's statement ties out through *fy_end* (I8).

    A close is permanent, and a line dated inside a closed year can then only
    post as a prior-period item: so the year's statement must be held, every
    line of it explained, and every settlement of it agreed or corrected.
    """
    from django.db.models import Max

    from journal.models import StatementLine
    from journal.statement.reconcile import disagreeing_settlements, queue

    last = StatementLine.objects.aggregate(last=Max("posting_date"))["last"]
    if last is None or last < fy_end:
        held = f"the last line held is dated {last}" if last else "none is held"
        raise JournalPeriodError(
            f"Upload Zerodha's statement through {fy_end} first ({held}). If the "
            "year's last line falls before 31 March, upload the next month's too."
        )
    waiting = queue().filter(posting_date__lte=fy_end).count()
    if waiting:
        raise JournalPeriodError(
            f"{waiting} statement line{'s' if waiting != 1 else ''} dated on or before "
            f"{fy_end} {'are' if waiting != 1 else 'is'} still waiting for confirmation."
        )
    disagreeing = [row for row in disagreeing_settlements() if row["line"].posting_date <= fy_end]
    if disagreeing:
        raise JournalPeriodError(
            f"{len(disagreeing)} settlement{'s' if len(disagreeing) != 1 else ''} dated on or "
            f"before {fy_end} still disagree with ours: correct or explain them first."
        )


def close_year(*, book: str, fy_end: date, actor: str) -> PeriodClose:
    if (fy_end.month, fy_end.day) != (3, 31):
        raise JournalPeriodError("A financial year ends on 31 March.")
    if fy_end >= today_ist():
        raise JournalPeriodError("That year is not over yet.")
    if book == Book.LIVE and not is_closed(book, fy_end):
        # The paper book has no statement to reconcile against.
        _refuse_unless_reconciled(fy_end)

    close, created = PeriodClose.objects.get_or_create(
        book=book, fy_end=fy_end, defaults={"closed_by": actor, "trace_id": current_trace_id()}
    )
    if created:
        record(
            "journal.year_closed",
            actor=actor,
            entity=close,
            after={"book": book, "fy_end": str(fy_end)},
        )
    return close
