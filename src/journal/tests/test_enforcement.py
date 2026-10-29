"""Each invariant is a property of the database, not of this code (spec §9)."""

from datetime import date
from decimal import Decimal

import pytest
from django.db import DatabaseError, connection, transaction

from journal import accounts
from journal.models import Book, EntryKind, JournalEntry, JournalLine, PeriodClose


def _entry(number=1, occurred_on=date(2026, 9, 18), book=Book.LIVE):
    return JournalEntry.objects.create(
        book=book,
        number=number,
        kind=EntryKind.CONTRIBUTION,
        occurred_on=occurred_on,
        description="test",
        actor="test",
    )


def _lines(entry, amount=Decimal("100")):
    JournalLine.objects.create(entry=entry, account_id=accounts.UNALLOCATED, amount=amount)
    JournalLine.objects.create(entry=entry, account_id=accounts.CONTRIBUTIONS, amount=-amount)


def _check_now():
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.django_db
def test_a_balanced_entry_commits():
    _lines(_entry())
    _check_now()


@pytest.mark.django_db
def test_an_unbalanced_entry_is_refused_at_commit():
    entry = _entry()
    JournalLine.objects.create(entry=entry, account_id=accounts.UNALLOCATED, amount=Decimal("100"))
    JournalLine.objects.create(
        entry=entry, account_id=accounts.CONTRIBUTIONS, amount=Decimal("-99")
    )
    with pytest.raises(DatabaseError, match="does not balance"):
        _check_now()


@pytest.mark.django_db
def test_an_entry_with_one_line_is_refused():
    entry = _entry()
    JournalLine.objects.create(entry=entry, account_id=accounts.UNALLOCATED, amount=Decimal("0"))
    with pytest.raises(DatabaseError, match="does not balance"):
        _check_now()


@pytest.mark.django_db
def test_an_entry_with_no_lines_is_refused():
    _entry()
    with pytest.raises(DatabaseError, match="has no lines"):
        _check_now()


@pytest.mark.django_db
def test_an_entry_cannot_be_edited():
    entry = _entry()
    _lines(entry)
    with pytest.raises(DatabaseError), transaction.atomic():
        JournalEntry.objects.filter(pk=entry.pk).update(description="changed")


@pytest.mark.django_db
def test_a_line_cannot_be_edited_or_deleted():
    entry = _entry()
    _lines(entry)
    line = entry.lines.first()
    with pytest.raises(DatabaseError), transaction.atomic():
        JournalLine.objects.filter(pk=line.pk).update(amount=Decimal("1"))
    with pytest.raises(DatabaseError), transaction.atomic():
        JournalLine.objects.filter(pk=line.pk).delete()


@pytest.mark.django_db
def test_a_closed_year_accepts_no_entry_dated_inside_it():
    PeriodClose.objects.create(book=Book.LIVE, fy_end=date(2026, 3, 31), closed_by="ca")
    with pytest.raises(DatabaseError, match="closed"), transaction.atomic():
        _entry(occurred_on=date(2026, 3, 31))


@pytest.mark.django_db
def test_a_closed_year_in_one_book_does_not_close_the_other():
    PeriodClose.objects.create(book=Book.LIVE, fy_end=date(2026, 3, 31), closed_by="ca")
    _lines(_entry(occurred_on=date(2026, 3, 31), book=Book.PAPER))
    _check_now()


@pytest.mark.django_db
def test_the_day_after_a_closed_year_is_open():
    PeriodClose.objects.create(book=Book.LIVE, fy_end=date(2026, 3, 31), closed_by="ca")
    _lines(_entry(occurred_on=date(2026, 4, 1)))
    _check_now()
