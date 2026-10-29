"""Journal migrations are reversible, and none weakens the append-only rules."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


def _function_source() -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT prosrc FROM pg_proc WHERE proname = 'journal_statementline_match_only'"
        )
        return cursor.fetchone()[0]


@pytest.mark.django_db(transaction=True)
def test_0008_reverses_to_the_match_once_trigger_and_back():
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    assert "reverses_id" in _function_source()
    try:
        executor.migrate([("journal", "0007_balancecheck_append_only")])
        assert "reverses_id" not in _function_source()
        assert "may only be matched, once" in _function_source()
    finally:
        MigrationExecutor(connection).migrate(leaves)
    assert "reverses_id" in _function_source()


def _columns() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'journal_journalline'"
        )
        return {row[0] for row in cursor.fetchall()}


@pytest.mark.django_db(transaction=True)
def test_0009_adds_the_lot_column_reversibly_and_lines_stay_append_only():
    from datetime import date
    from decimal import Decimal

    from django.db import DatabaseError, transaction

    from journal.external import open_books
    from journal.models import JournalLine
    from portfolio.models import BasisState, Instrument, Lot, OpeningImport

    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    assert "lot_id" in _columns()
    try:
        executor.migrate([("journal", "0008_statementline_rematch_after_reversal")])
        assert "lot_id" not in _columns()
    finally:
        MigrationExecutor(connection).migrate(leaves)
    assert "lot_id" in _columns()

    instrument = Instrument.objects.create(
        tradingsymbol="INFY", instrument_token=408065, name="Infosys"
    )
    lot = Lot.objects.create(
        instrument=instrument,
        acquired_on=date(2024, 1, 1),
        quantity=1,
        remaining_quantity=1,
        cost_per_unit=Decimal("100"),
        basis_state=BasisState.IMPORTED,
        opening_import=OpeningImport.objects.create(imported_by="o", source="statement"),
    )
    open_books(cash=Decimal("0"), on=date(2026, 9, 1), actor="o")
    line = JournalLine.objects.get(lot=lot)
    with pytest.raises(DatabaseError), transaction.atomic():
        JournalLine.objects.filter(pk=line.pk).update(lot=None)
    with pytest.raises(DatabaseError), transaction.atomic():
        JournalLine.objects.filter(lot__isnull=True).update(lot=lot)


def _tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


@pytest.mark.django_db(transaction=True)
def test_0010_adds_the_explanation_table_reversibly_and_append_only():
    """m-4: the table and its trigger land and leave together."""
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    assert "journal_settlementexplanation" in _tables()
    try:
        executor.migrate([("journal", "0009_journalline_lot")])
        assert "journal_settlementexplanation" not in _tables()
    finally:
        MigrationExecutor(connection).migrate(leaves)
    assert "journal_settlementexplanation" in _tables()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_trigger WHERE tgname = 'journal_settlementexplanation_append_only'"
        )
        assert cursor.fetchone() is not None
