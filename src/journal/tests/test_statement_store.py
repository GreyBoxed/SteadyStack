from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext

from journal import accounts
from journal.models import Book, EntryKind, StatementLine
from journal.posting import Line, post
from journal.statement.parse import StatementError
from journal.statement.store import link, store_upload
from journal.tests.conftest import csv_of

FIXTURES = Path(__file__).parent / "fixtures"
AUG = (FIXTURES / "statement_aug.csv").read_bytes()
SEP = (FIXTURES / "statement_sep.csv").read_bytes()


def _entry(number):
    """A minimal balanced live entry, distinguishable by its number."""
    return post(
        book=Book.LIVE,
        kind=EntryKind.CONTRIBUTION,
        occurred_on=date(2026, 8, 5),
        lines=[
            Line(accounts.UNALLOCATED, Decimal("1")),
            Line(accounts.CONTRIBUTIONS, Decimal("-1")),
        ],
        description=f"test entry {number}",
        actor="operator",
        idempotency_key=f"test-entry-{number}",
    )


@pytest.mark.django_db
def test_an_upload_stores_every_line():
    stored = store_upload(AUG, filename="aug.csv", actor="operator")
    assert stored.added == 4
    assert StatementLine.objects.count() == 4


@pytest.mark.django_db
def test_an_overlapping_upload_adds_only_new_lines():
    store_upload(AUG, filename="aug.csv", actor="operator")
    stored = store_upload(SEP, filename="sep.csv", actor="operator")
    assert stored.already_held == 1
    assert stored.added == 4
    assert StatementLine.objects.count() == 8


@pytest.mark.django_db
def test_the_same_file_twice_is_refused():
    store_upload(AUG, filename="aug.csv", actor="operator")
    with pytest.raises(StatementError, match="already"):
        store_upload(AUG, filename="aug-again.csv", actor="operator")
    assert StatementLine.objects.count() == 4


@pytest.mark.django_db
def test_a_file_that_starts_where_the_last_ended_is_accepted():
    store_upload(csv_of("0", [("AMC", "2026-08-01", "10", "0")]), filename="a.csv", actor="o")
    stored = store_upload(
        csv_of("-10", [("AMC", "2026-09-01", "10", "0")]), filename="b.csv", actor="o"
    )
    assert stored.added == 1


@pytest.mark.django_db
def test_a_broken_balance_chain_is_refused():
    store_upload(csv_of("0", [("AMC", "2026-08-01", "10", "0")]), filename="a.csv", actor="o")
    with pytest.raises(StatementError, match="continue"):
        store_upload(csv_of("500", [("AMC", "2026-10-01", "10", "0")]), filename="c.csv", actor="o")


@pytest.mark.django_db
def test_a_statement_line_cannot_be_edited_except_to_match_it():
    store_upload(AUG, filename="aug.csv", actor="operator")
    line = StatementLine.objects.first()
    with pytest.raises(DatabaseError), transaction.atomic():
        StatementLine.objects.filter(pk=line.pk).update(particulars="changed")
    with pytest.raises(DatabaseError), transaction.atomic():
        StatementLine.objects.filter(pk=line.pk).delete()


@pytest.mark.django_db
def test_a_file_with_no_data_rows_is_refused():
    with pytest.raises(StatementError, match="no lines"):
        store_upload(csv_of("0", []), filename="empty.csv", actor="o")
    assert StatementLine.objects.count() == 0


@pytest.mark.django_db
def test_store_upload_takes_its_lock_before_checking_the_chain():
    with CaptureQueriesContext(connection) as queries:
        store_upload(AUG, filename="aug.csv", actor="operator")

    sqls = [q["sql"] for q in queries]
    lock_index = next(i for i, sql in enumerate(sqls) if "pg_advisory_xact_lock" in sql)
    last_held_index = next(
        i for i, sql in enumerate(sqls) if "ORDER BY" in sql.upper() and "DESC" in sql.upper()
    )
    assert lock_index < last_held_index


@pytest.mark.django_db
def test_linking_a_line_twice_to_the_same_entry_is_idempotent():
    store_upload(AUG, filename="aug.csv", actor="operator")
    line = StatementLine.objects.first()
    entry = _entry(1)

    link(line, entry)
    link(line, entry)

    assert line.matched_entry_id == entry.id
    assert StatementLine.objects.get(pk=line.pk).matched_entry_id == entry.id


@pytest.mark.django_db
def test_linking_a_line_already_matched_to_a_different_entry_is_refused():
    store_upload(AUG, filename="aug.csv", actor="operator")
    line = StatementLine.objects.first()
    first = _entry(1)
    second = _entry(2)

    link(line, first)
    with pytest.raises(StatementError, match=f"already matched to entry #{first.number}"):
        link(line, second)

    assert line.matched_entry_id == first.id
    assert StatementLine.objects.get(pk=line.pk).matched_entry_id == first.id


@pytest.mark.django_db
def test_the_database_refuses_rematching_a_line_whose_entry_stands():
    store_upload(AUG, filename="aug.csv", actor="operator")
    line = StatementLine.objects.first()
    first, second = _entry(1), _entry(2)
    StatementLine.objects.filter(pk=line.pk).update(matched_entry=first)
    with pytest.raises(DatabaseError), transaction.atomic():
        StatementLine.objects.filter(pk=line.pk).update(matched_entry=second)


@pytest.mark.django_db
def test_a_line_whose_entry_was_reversed_may_be_matched_again():
    """I4: the trigger allows a second transition only once the old entry is reversed."""
    from journal.posting import reverse

    store_upload(AUG, filename="aug.csv", actor="operator")
    line = StatementLine.objects.first()
    first, second = _entry(1), _entry(2)
    link(line, first)
    reverse(first, reason="wrong kind", actor="operator")

    link(line, second)

    assert StatementLine.objects.get(pk=line.pk).matched_entry_id == second.id
    # Back to nothing is still refused, as is touching any other column.
    with pytest.raises(DatabaseError), transaction.atomic():
        StatementLine.objects.filter(pk=line.pk).update(matched_entry=None)
