"""The opening import, and the refusal that depends on it.

Two properties. A lot's basis cannot be rewritten once it exists -- the
database refuses, not the application. And until the import is complete,
anything that needs a whole lot history refuses to run, because FIFO against
half a set gives an answer that looks fine and is wrong.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.db import DatabaseError

from audit.models import AuditEvent
from portfolio.instruments import refresh
from portfolio.models import BasisState, Lot, OpeningImport
from portfolio.opening import (
    ImportRefused,
    OpeningImportIncomplete,
    is_complete,
    mark_complete,
    parse,
    require_complete,
    run,
)

CLEAN = (
    "symbol,quantity,acquired_on,cost_per_unit\n"
    "INFY,10,2024-03-15,1450.50\n"
    "INFY,5,2025-01-20,1600.00\n"
    "TCS,3,2023-11-02,3400.75\n"
)


def master(symbol, token):
    return {
        "tradingsymbol": symbol,
        "instrument_token": token,
        "name": symbol,
        "exchange": "NSE",
        "instrument_type": "EQ",
        "segment": "NSE",
        "tick_size": 0.05,
        "lot_size": 1,
    }


@pytest.fixture
def universe(db):
    refresh([master("INFY", 408065), master("TCS", 2953217)])


# -- parsing ---------------------------------------------------------------


def test_a_clean_file_parses():
    assert parse(CLEAN).ok is True


def test_two_purchases_of_one_name_stay_two_lots():
    """Collapsing them would change which shares a future sale consumes."""
    result = parse(CLEAN)
    assert len([lot for lot in result.lots if lot.symbol == "INFY"]) == 2


def test_the_acquisition_date_is_read_as_a_date():
    assert parse(CLEAN).lots[0].acquired_on == date(2024, 3, 15)


def test_alternative_header_names_are_understood():
    assert parse("ticker,qty,date,rate\nINFY,1,2024-03-15,1450\n").ok is True


def test_a_missing_column_is_named():
    result = parse("symbol,quantity\nINFY,10\n")
    assert "acquired_on" in result.errors[0]


def test_a_malformed_date_is_refused():
    result = parse("symbol,quantity,acquired_on,cost_per_unit\nINFY,10,15-03-2024,1450\n")
    assert any("YYYY-MM-DD" in error for error in result.errors)


def test_a_future_acquisition_is_refused():
    result = parse("symbol,quantity,acquired_on,cost_per_unit\nINFY,10,2099-01-01,1450\n")
    assert any("in the future" in error for error in result.errors)


def test_a_zero_quantity_is_refused():
    result = parse("symbol,quantity,acquired_on,cost_per_unit\nINFY,0,2024-03-15,1450\n")
    assert any("quantity of 0" in error for error in result.errors)


def test_a_fractional_quantity_is_refused():
    """Shares are whole. A fraction means the file is about something else."""
    result = parse("symbol,quantity,acquired_on,cost_per_unit\nINFY,1.5,2024-03-15,1450\n")
    assert any("whole number" in error for error in result.errors)


def test_a_zero_cost_is_refused():
    result = parse("symbol,quantity,acquired_on,cost_per_unit\nINFY,10,2024-03-15,0\n")
    assert any("cost of 0" in error for error in result.errors)


def test_an_empty_file_is_refused():
    assert parse("").ok is False


# -- importing -------------------------------------------------------------


@pytest.mark.django_db
def test_an_import_creates_one_lot_per_purchase(universe):
    imported = run(CLEAN, imported_by="operator", source="contract notes")

    assert imported.lot_count == 3
    assert Lot.objects.count() == 3


@pytest.mark.django_db
def test_imported_lots_carry_their_true_acquisition_dates(universe):
    run(CLEAN, imported_by="operator", source="contract notes")

    dates = sorted(Lot.objects.values_list("acquired_on", flat=True))
    assert dates == [date(2023, 11, 2), date(2024, 3, 15), date(2025, 1, 20)]


@pytest.mark.django_db
def test_an_imported_lot_starts_fully_unconsumed(universe):
    run(CLEAN, imported_by="operator", source="contract notes")

    lot = Lot.objects.get(instrument_id="TCS")
    assert lot.remaining_quantity == lot.quantity == 3


@pytest.mark.django_db
def test_an_imported_lot_is_marked_as_imported(universe):
    """Not estimated: it is what the operator actually paid."""
    run(CLEAN, imported_by="operator", source="contract notes")
    assert Lot.objects.first().basis_state == BasisState.IMPORTED


@pytest.mark.django_db
def test_an_import_is_audited_with_its_source(universe):
    run(CLEAN, imported_by="operator", source="Zerodha console export")

    event = AuditEvent.objects.get(action="opening_import.completed")
    assert event.after["source"] == "Zerodha console export"
    assert event.after["lots"] == 3


@pytest.mark.django_db
def test_a_refused_import_writes_nothing(universe):
    with pytest.raises(ImportRefused):
        run(
            "symbol,quantity,acquired_on,cost_per_unit\nNOSUCHCO,1,2024-03-15,10\n",
            imported_by="operator",
            source="a spreadsheet",
        )

    assert Lot.objects.count() == 0
    assert OpeningImport.objects.count() == 0


@pytest.mark.django_db
def test_importing_twice_is_refused(universe):
    """The second import would double the portfolio's history."""
    run(CLEAN, imported_by="operator", source="contract notes")

    with pytest.raises(ImportRefused, match="already been imported"):
        run(CLEAN, imported_by="operator", source="contract notes again")

    assert Lot.objects.count() == 3


@pytest.mark.django_db
def test_an_import_can_arrive_in_several_files(universe):
    first = run(
        "symbol,quantity,acquired_on,cost_per_unit\nINFY,10,2024-03-15,1450\n",
        imported_by="operator",
        source="part one",
        complete=False,
    )

    assert is_complete() is False

    mark_complete(first, actor="operator")
    assert is_complete() is True


# -- the refusal -----------------------------------------------------------


@pytest.mark.django_db
def test_gains_are_refused_before_the_import(universe):
    with pytest.raises(OpeningImportIncomplete, match="realised gains"):
        require_complete("Computing realised gains")


@pytest.mark.django_db
def test_a_rebalance_sell_is_refused_before_the_import(universe):
    with pytest.raises(OpeningImportIncomplete):
        require_complete("Executing a rebalance sell")


@pytest.mark.django_db
def test_an_open_import_does_not_satisfy_the_requirement(universe):
    """Half a lot set is exactly the dangerous case."""
    run(CLEAN, imported_by="operator", source="part one", complete=False)

    with pytest.raises(OpeningImportIncomplete):
        require_complete("Computing realised gains")


@pytest.mark.django_db
def test_the_refusal_lifts_once_the_import_is_complete(universe):
    run(CLEAN, imported_by="operator", source="contract notes")
    require_complete("Computing realised gains")  # does not raise


# -- immutability ----------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_lots_cost_cannot_be_rewritten(universe):
    run(CLEAN, imported_by="operator", source="contract notes")
    lot = Lot.objects.first()

    with pytest.raises(DatabaseError, match="immutable basis"):
        Lot.objects.filter(pk=lot.pk).update(cost_per_unit=Decimal("1"))


@pytest.mark.django_db(transaction=True)
def test_a_lots_acquisition_date_cannot_be_rewritten(universe):
    run(CLEAN, imported_by="operator", source="contract notes")
    lot = Lot.objects.first()

    with pytest.raises(DatabaseError, match="immutable basis"):
        Lot.objects.filter(pk=lot.pk).update(acquired_on=date(2020, 1, 1))


@pytest.mark.django_db(transaction=True)
def test_a_lots_quantity_cannot_be_rewritten(universe):
    run(CLEAN, imported_by="operator", source="contract notes")
    lot = Lot.objects.first()

    with pytest.raises(DatabaseError, match="immutable basis"):
        Lot.objects.filter(pk=lot.pk).update(quantity=999)


@pytest.mark.django_db(transaction=True)
def test_a_lot_cannot_be_deleted(universe):
    """A lot that was wrong is superseded, never removed."""
    run(CLEAN, imported_by="operator", source="contract notes")
    lot = Lot.objects.first()

    with pytest.raises(DatabaseError, match="cannot be deleted"):
        Lot.objects.filter(pk=lot.pk).delete()


@pytest.mark.django_db(transaction=True)
def test_consumption_is_allowed(universe):
    """The one field that moves: a sell takes from the oldest lots first."""
    run(CLEAN, imported_by="operator", source="contract notes")
    lot = Lot.objects.get(instrument_id="TCS")

    Lot.objects.filter(pk=lot.pk).update(remaining_quantity=1)

    lot.refresh_from_db()
    assert lot.remaining_quantity == 1


@pytest.mark.django_db(transaction=True)
def test_a_lot_cannot_be_consumed_past_what_it_holds(universe):
    run(CLEAN, imported_by="operator", source="contract notes")
    lot = Lot.objects.get(instrument_id="TCS")

    with pytest.raises(DatabaseError):
        Lot.objects.filter(pk=lot.pk).update(remaining_quantity=99)


@pytest.mark.django_db(transaction=True)
def test_a_lot_can_be_superseded(universe):
    """The correction mechanism: a new row, with the old one left readable."""
    run(CLEAN, imported_by="operator", source="contract notes")
    original = Lot.objects.get(instrument_id="TCS")

    replacement = Lot.objects.create(
        instrument=original.instrument,
        acquired_on=original.acquired_on,
        quantity=original.quantity,
        remaining_quantity=original.remaining_quantity,
        cost_per_unit=Decimal("3500.00"),
        basis_state=BasisState.IMPORTED,
    )
    Lot.objects.filter(pk=original.pk).update(superseded_by=replacement)

    original.refresh_from_db()
    assert original.superseded_by_id == replacement.id
    assert original.is_open is False
    assert original.cost_per_unit == Decimal("3400.7500")


# -- an account that held nothing -------------------------------------------


@pytest.mark.django_db
def test_an_account_with_no_holdings_can_complete_the_import():
    """A header with no rows is refused, and rightly: it looks like a file
    that lost its rows. Holding nothing is said on purpose instead."""
    from portfolio.opening import record_empty

    opening = record_empty(imported_by="operator", source="holdings statement: none")

    assert opening.is_complete
    assert opening.lot_count == 0
    assert is_complete() is True
    assert Lot.objects.count() == 0
    assert AuditEvent.objects.filter(action="opening_import.completed").exists()
    require_complete("Computing realised gains")  # does not raise


@pytest.mark.django_db
def test_an_empty_import_is_refused_once_opening_lots_exist(universe):
    """An open import that already holds lots did not start from nothing."""
    from portfolio.opening import record_empty

    run(CLEAN, imported_by="operator", source="part one", complete=False)

    with pytest.raises(ImportRefused, match="lots"):
        record_empty(imported_by="operator", source="nothing")

    assert is_complete() is False


@pytest.mark.django_db
def test_an_empty_import_is_refused_after_a_complete_one(universe):
    from portfolio.opening import record_empty

    run(CLEAN, imported_by="operator", source="contract notes")

    with pytest.raises(ImportRefused, match="already been imported"):
        record_empty(imported_by="operator", source="nothing")

    assert OpeningImport.objects.count() == 1


@pytest.mark.django_db
def test_the_command_records_an_empty_import():
    from django.core.management import call_command

    call_command(
        "import_opening_positions", "--empty", "--actor", "operator", "--source", "statement"
    )

    [opening] = OpeningImport.objects.all()
    assert opening.is_complete
    assert opening.lot_count == 0


@pytest.mark.django_db
def test_the_command_will_not_take_a_file_and_empty_together(tmp_path):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    path = tmp_path / "holdings.csv"
    path.write_text(CLEAN)

    with pytest.raises(CommandError):
        call_command(
            "import_opening_positions",
            str(path),
            "--empty",
            "--actor",
            "operator",
            "--source",
            "statement",
        )

    assert OpeningImport.objects.count() == 0


@pytest.mark.django_db
def test_the_command_still_needs_a_file_without_empty():
    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("import_opening_positions", "--actor", "operator", "--source", "statement")
