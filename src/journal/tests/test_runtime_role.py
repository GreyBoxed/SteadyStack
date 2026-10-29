"""The runtime role's privileges, in the suite rather than only on the box (M14).

The suite connects as the schema owner, and locally the owner is a superuser:
an UPDATE of an append-only row, or a row lock taken on one, passes every test
and fails on the box, where the app runs as `steadystack_app` with UPDATE revoked.
The `runtime_role` fixture (src/conftest.py) gives a test body the box's
privileges; these tests run the paths that matter under it, and a static
guard keeps row locks off the append-only tables altogether.
"""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import DatabaseError, connection, transaction

from config.runtime_role import update_revoked_tables

SRC = Path(__file__).resolve().parents[2]


def _append_only_models() -> dict[str, str]:
    """Class name to table, for every model whose table refuses UPDATE."""
    from django.apps import apps

    revoked = update_revoked_tables()
    return {
        model.__name__: model._meta.db_table
        for model in apps.get_models()
        if model._meta.db_table in revoked
    }


# -- the fixture itself -------------------------------------------------------


@pytest.mark.django_db
def test_the_runtime_role_is_refused_an_update_of_the_journal(runtime_role):
    from journal import cash

    entry = cash.contribute(Decimal("100"), on=date(2026, 9, 21), actor="operator")

    # Refused for want of the privilege, before the append-only trigger runs.
    with pytest.raises(DatabaseError, match="permission denied"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE journal_journalentry SET description = 'x' WHERE id = %s", [entry.pk]
            )


@pytest.mark.django_db
def test_the_runtime_role_cannot_lock_an_append_only_row(runtime_role):
    """Why the guard below exists: Postgres wants UPDATE for FOR UPDATE."""
    from journal import cash
    from journal.models import JournalEntry

    entry = cash.contribute(Decimal("100"), on=date(2026, 9, 21), actor="operator")

    with pytest.raises(DatabaseError, match="permission denied"), transaction.atomic():
        list(JournalEntry.objects.select_for_update().filter(pk=entry.pk))


@pytest.mark.django_db
def test_the_owner_the_suite_runs_as_can_lock_it():
    """The contrast that makes the fixture worth having."""
    from journal import cash
    from journal.models import JournalEntry

    entry = cash.contribute(Decimal("100"), on=date(2026, 9, 21), actor="operator")

    assert list(JournalEntry.objects.select_for_update().filter(pk=entry.pk)) == [entry]


def test_every_journal_table_is_among_the_revoked():
    """The fixture reads the REVOKEs from the migrations; a regex that found
    none would make every runtime-role test pass for nothing."""
    tables = update_revoked_tables()

    assert {
        "journal_journalentry",
        "journal_journalline",
        "journal_periodclose",
        "journal_statementupload",
        "journal_balancecheck",
        "journal_settlementexplanation",
        "engine_cashledger",
        "gains_realisedgain",
        "audit_auditevent",
        "configuration_configurationversion",
    } <= tables


# -- a live day, as the box runs it ------------------------------------------


class FakeBroker:
    def __init__(self, opening):
        self.opening = opening

    def margins(self):
        return {"equity": {"available": {"opening_balance": float(self.opening)}}}


@pytest.mark.django_db
def test_a_live_day_runs_under_the_runtime_role(runtime_role, live_mode, live_buy_fill):
    """The T2 probe, kept: every journal path of a live day -- the opening, a
    lumpsum, the daily allocation, a fill booked and settled, the ledger row,
    the reconcile, the balance check, a statement upload matched, confirmed,
    reversed and confirmed again, a settlement corrected, a basis finalised
    and a year closed -- with UPDATE revoked on every append-only table."""
    from engine.ledger import reconcile, settle_day
    from journal import cash
    from journal.checks import check_balance
    from journal.external import open_books
    from journal.models import Book
    from journal.periods import close_year
    from journal.posting import reverse
    from journal.statement.reconcile import (
        auto_match,
        confirm,
        correct_settlement,
        correction_bound,
        queue,
        settlement_difference,
    )
    from journal.statement.store import store_upload
    from journal.tests.conftest import LIVE_DAY, csv_of
    from portfolio.contract_note import finalise
    from portfolio.models import Lot

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone()[0] == "steadystack_app"

    open_books(cash=Decimal("0"), on=date(2026, 8, 1), actor="operator")
    cash.record_lumpsum(
        Decimal("5000"), received_on=date(2026, 8, 13), actor="operator", reference="999"
    )
    cash.allocate_daily(LIVE_DAY, book=Book.LIVE)
    live_buy_fill(quantity=2, price="240")
    row = settle_day(LIVE_DAY, book=Book.LIVE)
    assert row.cash_spent == Decimal("480")
    assert reconcile(book=Book.LIVE)["ok"]
    assert check_balance(broker=FakeBroker("4520"), on=date(2026, 8, 17)) is not None

    stored = store_upload(
        csv_of(
            "0",
            [
                ("Funds added using UPI with reference number 999", "2026-08-13", "0", "5000"),
                ("AMC for Demat Account", "2026-08-13", "88.5", "0"),
            ],
            voucher_type="Bank Receipts",
        ),
        filename="aug.csv",
        actor="operator",
    )
    auto_match(stored.upload)
    amc = next(line for line in queue() if line.particulars.startswith("AMC"))
    entry = confirm(amc, kind="account_charge", actor="operator")
    reverse(entry, reason="confirmed as the wrong thing", actor="operator")
    confirm(amc, kind="account_charge", actor="operator")

    # The settlement line: what the day's buy cost, off by paise.
    settlement = next(
        line
        for line in store_upload(
            csv_of(
                "4911.5",
                [
                    (
                        "Net settlement for Equity with settlement number: 2026153",
                        "2026-08-14",
                        "481.02",
                        "0",
                    )
                ],
                voucher_type="Book Voucher",
            ),
            filename="aug-settlement.csv",
            actor="operator",
        ).upload.lines.all()
    )
    auto_match(settlement.upload)
    settlement.refresh_from_db()
    assert settlement.matched_entry_id is not None
    assert abs(settlement_difference(settlement)) <= correction_bound(settlement)
    correct_settlement(settlement, actor="operator")

    lot = Lot.objects.filter(superseded_by__isnull=True, basis_state="estimated").first()
    if lot is not None:
        finalise(lot, actual_charges=Decimal("1"), actor="operator")

    close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="operator")


# -- the guard ------------------------------------------------------------------


def _row_locks() -> list[tuple[str, int, str | None]]:
    """(file, line, model) for every select_for_update in non-test code;
    model is None where the call is not `Model.objects.select_for_update()`."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        if {"tests", "migrations"} & set(relative.parts) or relative.name == "conftest.py":
            continue
        text = path.read_text()
        for match in re.finditer(r"select_for_update\(", text):
            line = text.count("\n", 0, match.start()) + 1
            lead = re.search(r"(\w+)\.objects\.\s*$", text[: match.start()])
            found.append((str(relative), line, lead.group(1) if lead else None))
    return found


def test_no_module_row_locks_an_append_only_model():
    """A row lock needs UPDATE, which the runtime role does not have on an
    append-only table: `select_for_update()` there passes every test run as
    the owner and fails on the box. Serialise with an advisory lock instead
    (`journal.posting.lock_book`, `configuration.store._append_version`)."""
    append_only = _append_only_models()
    locks = _row_locks()

    assert locks, "found no select_for_update at all; the scan is broken"
    unresolved = [f"{file}:{line}" for file, line, model in locks if model is None]
    offenders = [
        f"{file}:{line} locks {model} ({append_only[model]})"
        for file, line, model in locks
        if model in append_only
    ]
    assert not unresolved, (
        "write each row lock as Model.objects.select_for_update() so this guard can "
        f"read its model: {unresolved}"
    )
    assert not offenders, offenders


@pytest.mark.django_db
def test_a_settlement_is_explained_under_the_runtime_role(runtime_role, live_buy_fill):
    """m-4: an explanation is written, not edited, so the box's role can write it."""
    from journal.statement.reconcile import disagreeing_settlements, explain_settlement
    from journal.tests.test_statement_reconcile import _our_settlement, _settlement_line

    live_buy_fill(quantity=1, price="240")
    line = _settlement_line(_our_settlement() - Decimal("2400"))

    explanation = explain_settlement(line, reason="auction debit", actor="ca")

    assert disagreeing_settlements() == []
    with pytest.raises(DatabaseError, match="permission denied"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE journal_settlementexplanation SET reason = 'x' WHERE id = %s",
                [explanation.pk],
            )
