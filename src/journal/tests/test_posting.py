from datetime import date
from decimal import Decimal

import pytest
from django.db import connection

from audit.models import AuditEvent
from journal import accounts
from journal.models import Book, EntryKind, JournalEntry
from journal.posting import JournalError, Line, post, reverse

D = date(2026, 9, 18)


def _contribution(amount="1000", **kwargs):
    return post(
        book=kwargs.pop("book", Book.LIVE),
        kind=EntryKind.CONTRIBUTION,
        occurred_on=kwargs.pop("occurred_on", D),
        description="funds added",
        lines=[
            Line(accounts.UNALLOCATED, Decimal(amount)),
            Line(accounts.CONTRIBUTIONS, -Decimal(amount)),
        ],
        **kwargs,
    )


@pytest.mark.django_db
def test_post_writes_the_entry_its_lines_and_an_audit_event():
    entry = _contribution()
    assert entry.number == 1
    assert entry.lines.count() == 2
    assert entry.trace_id
    assert AuditEvent.objects.filter(action="journal.posted", entity_id=str(entry.id)).exists()


@pytest.mark.django_db
def test_numbers_are_gapless_per_book():
    assert _contribution().number == 1
    assert _contribution().number == 2
    assert _contribution(book=Book.PAPER, occurred_on=D).number == 1


@pytest.mark.django_db
def test_an_unbalanced_entry_is_refused_before_the_database_sees_it():
    with pytest.raises(JournalError, match="balance"):
        post(
            book=Book.LIVE,
            kind=EntryKind.CONTRIBUTION,
            occurred_on=D,
            description="x",
            lines=[
                Line(accounts.UNALLOCATED, Decimal("1")),
                Line(accounts.CONTRIBUTIONS, Decimal("-2")),
            ],
        )
    assert JournalEntry.objects.count() == 0


@pytest.mark.django_db
def test_zero_lines_are_dropped_and_an_entry_left_with_fewer_than_two_is_refused():
    with pytest.raises(JournalError, match="two lines"):
        post(
            book=Book.LIVE,
            kind=EntryKind.CONTRIBUTION,
            occurred_on=D,
            description="x",
            lines=[
                Line(accounts.UNALLOCATED, Decimal("0")),
                Line(accounts.CONTRIBUTIONS, Decimal("0")),
            ],
        )


@pytest.mark.django_db
def test_paper_funding_cannot_appear_in_the_live_book():
    with pytest.raises(JournalError, match="paper"):
        post(
            book=Book.LIVE,
            kind=EntryKind.ALLOCATION,
            occurred_on=D,
            description="x",
            lines=[
                Line(accounts.ENGINE_CASH, Decimal("1")),
                Line(accounts.PAPER_FUNDING, Decimal("-1")),
            ],
        )


@pytest.mark.django_db
def test_an_idempotency_key_returns_the_first_entry():
    first = _contribution(idempotency_key="k1")
    second = _contribution(idempotency_key="k1")
    assert first.pk == second.pk
    assert JournalEntry.objects.count() == 1


@pytest.mark.django_db
def test_amounts_are_kept_to_six_decimals():
    entry = _contribution("248.221084")
    assert sorted(line.amount for line in entry.lines.all())[1] == Decimal("248.221084")


@pytest.mark.django_db
def test_a_reversal_mirrors_the_entry_and_points_at_it():
    entry = _contribution("500")
    undone = reverse(entry, reason="recorded twice", actor="operator")
    assert undone.kind == EntryKind.REVERSAL
    assert undone.reverses_id == entry.id
    assert sorted(line.amount for line in undone.lines.all()) == [Decimal("-500"), Decimal("500")]
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.django_db
def test_an_entry_is_reversed_at_most_once_and_a_reversal_never():
    entry = _contribution("500")
    undone = reverse(entry, reason="once", actor="operator")
    with pytest.raises(JournalError, match="already reversed"):
        reverse(entry, reason="twice", actor="operator")
    with pytest.raises(JournalError, match="reversal"):
        reverse(undone, reason="undo the undo", actor="operator")


@pytest.mark.django_db
def test_posting_takes_no_row_lock_on_an_append_only_table():
    """The runtime role cannot lock rows it may not update (see configuration/store.py)."""
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        _contribution()
    assert not [q for q in queries if "for update" in q["sql"].lower()]


@pytest.mark.django_db
def test_the_idempotency_key_is_looked_up_under_the_books_lock():
    """The lock must be taken before the key is checked.

    Otherwise two concurrent posts with the same key both pass the lookup,
    both try to insert, and the loser hits a raw IntegrityError instead of
    returning the winner's entry (see posting.py's docstring on `post`).
    """
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        _contribution(idempotency_key="lock-order")

    sqls = [q["sql"] for q in queries]
    lock_index = next(i for i, sql in enumerate(sqls) if "pg_advisory_xact_lock" in sql)
    key_lookup_index = next(i for i, sql in enumerate(sqls) if "idempotency_key" in sql)
    assert lock_index < key_lookup_index


@pytest.mark.django_db
def test_a_long_source_id_is_truncated_rather_than_refused():
    """M1: a lumpsum or dividend reference is longer than the column."""
    entry = _contribution(source_id="r" * 200)
    entry.refresh_from_db()
    assert entry.source_id == "r" * 64


@pytest.mark.django_db
def test_an_entry_dated_into_a_closed_year_is_refused_in_python():
    """M2: a JournalError the views catch, not a raw database error."""
    from journal.models import PeriodClose

    PeriodClose.objects.create(book=Book.LIVE, fy_end=date(2026, 3, 31), closed_by="ca")
    with pytest.raises(JournalError, match="closed"):
        _contribution(occurred_on=date(2026, 3, 31))
    assert _contribution(occurred_on=date(2026, 4, 1)).number == 1
