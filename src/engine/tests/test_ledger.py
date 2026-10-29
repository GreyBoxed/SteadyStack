"""The engine's cash, now read from the journal.

The property test at the bottom is the point of this file: money is conserved
over any run of days, however spending, lumpsums and overspends interleave.
"""

from datetime import date
from decimal import Decimal
from itertools import pairwise

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from configuration.store import update as configure
from engine.ledger import (
    LedgerError,
    closing_carry_before,
    opening_for,
    reconcile,
    settle_day,
    spendable_on,
)
from engine.models import CashLedger
from journal import accounts, cash
from journal.models import Book, Component, EntryKind
from journal.posting import Line, post

MON, TUE, WED, THU, FRI, SAT = (date(2026, 9, d) for d in (21, 22, 23, 24, 25, 26))
NEXT_MON = date(2026, 9, 28)

pytestmark = pytest.mark.django_db


def spend(day, gross, charges="0"):
    """A buy fill the engine paid for, as the journal records one."""
    gross, charges = Decimal(gross), Decimal(charges)
    lines = [Line(accounts.INVESTMENTS, gross, component=Component.GROSS)]
    if charges:
        lines.append(Line(accounts.INVESTMENTS, charges, component=Component.STT))
    lines.append(Line(accounts.SETTLE_ENGINE, -(gross + charges)))
    post(book=Book.PAPER, kind=EntryKind.BUY_FILL, occurred_on=day, description="buy", lines=lines)


def run_day(day, gross="0", charges="0"):
    cash.allocate_daily(day, book=Book.PAPER)
    if Decimal(gross):
        spend(day, gross, charges)
    return settle_day(day, book=Book.PAPER)


# -- opening a day ---------------------------------------------------------


def test_the_first_day_opens_with_the_daily_amount_alone():
    opening = opening_for(MON, book=Book.PAPER)
    assert opening.carry == Decimal("0")
    assert opening.daily_allocation == Decimal("1000.00")
    assert opening.spendable == Decimal("1000.00")


def test_opening_a_day_writes_nothing():
    opening_for(MON, book=Book.PAPER)
    from journal.models import JournalEntry

    assert JournalEntry.objects.count() == 0


def test_the_configured_amount_is_what_is_allocated():
    configure(changed_by="operator", reason="t", daily_amount=Decimal("2500"))
    assert opening_for(MON, book=Book.PAPER).daily_allocation == Decimal("2500.00")


def test_an_allocated_day_reports_the_allocation_not_the_configuration():
    cash.allocate_daily(MON, book=Book.PAPER)
    configure(changed_by="operator", reason="t", daily_amount=Decimal("2500"))
    assert opening_for(MON, book=Book.PAPER).daily_allocation == Decimal("1000")


def test_what_a_day_did_not_spend_carries_into_the_next():
    run_day(MON, gross="400")
    assert opening_for(TUE, book=Book.PAPER).carry == Decimal("600")
    assert spendable_on(TUE, book=Book.PAPER) == Decimal("1600.00")
    assert closing_carry_before(TUE, book=Book.PAPER) == Decimal("600.00")


def test_a_weekend_carries_to_monday():
    run_day(FRI, gross="250")
    assert opening_for(NEXT_MON, book=Book.PAPER).carry == Decimal("750")


def test_an_overspend_leaves_negative_carry_that_the_next_allocation_repays():
    run_day(MON, gross="1050")
    tuesday = opening_for(TUE, book=Book.PAPER)
    assert tuesday.carry == Decimal("-50")
    assert tuesday.spendable == Decimal("950.00")


def test_an_overspend_logs_the_line_the_alarm_matches(caplog):
    """I-4: the `overspend` alarm in infra/terraform/observability.tf is a
    metric filter on `$.message = "*spent more than it was sized against*"`.
    Reworded, the line would stop the alarm firing with no test failing."""
    import fnmatch
    import logging
    import re
    from pathlib import Path

    infra = Path(__file__).resolve().parents[3] / "infra"
    pattern = re.search(
        r'overspend = \{\s*pattern\s*= "\{ \$\.message = \\"([^"\\]+)\\" \}"',
        (infra / "terraform" / "observability.tf").read_text(),
    ).group(1)

    with caplog.at_level(logging.WARNING, logger="steadystack.engine"):
        run_day(MON, gross="1050")

    [line] = [
        r for r in caplog.records if r.name == "steadystack.engine" and r.levelno >= logging.WARNING
    ]
    assert fnmatch.fnmatchcase(line.getMessage(), pattern)


def test_an_overspend_larger_than_a_day_floors_at_zero_and_is_still_owed():
    run_day(MON, gross="2500")
    tuesday = opening_for(TUE, book=Book.PAPER)
    assert tuesday.spendable == Decimal("0")
    assert tuesday.shortfall == Decimal("500.00")


def test_a_lumpsum_on_a_saturday_is_monday_money():
    cash.record_lumpsum(Decimal("50000"), received_on=SAT, actor="operator")
    assert spendable_on(NEXT_MON, book=Book.PAPER) == Decimal("51000.00")


def test_a_lumpsum_after_the_day_planned_is_seen_by_a_regeneration():
    cash.allocate_daily(MON, book=Book.PAPER)
    before = spendable_on(MON, book=Book.PAPER)
    cash.record_lumpsum(Decimal("5000"), received_on=MON, actor="operator")
    assert spendable_on(MON, book=Book.PAPER) == before + Decimal("5000")


# -- the daily summary -----------------------------------------------------


def test_settling_writes_a_row_that_balances_on_its_own():
    row = run_day(MON, gross="400", charges="2.50")
    assert row.opening_carry == 0
    assert row.daily_allocation == Decimal("1000")
    assert row.cash_spent == Decimal("400")
    assert row.charges == Decimal("2.50")
    assert row.closing_carry == Decimal("597.50")
    assert row.reconciles


def test_settling_is_idempotent():
    first = run_day(MON, gross="400")
    second = settle_day(MON, book=Book.PAPER)
    assert first.pk == second.pk
    assert CashLedger.objects.count() == 1


def test_no_row_is_due_on_a_weekend():
    with pytest.raises(LedgerError):
        settle_day(SAT, book=Book.PAPER)


def test_the_row_is_append_only():
    from django.db import DatabaseError, transaction

    row = run_day(MON)
    with pytest.raises(DatabaseError), transaction.atomic():
        CashLedger.objects.filter(pk=row.pk).update(note="changed")


def test_a_lumpsum_shows_as_an_extra_allocation():
    cash.record_lumpsum(Decimal("5000"), received_on=MON, actor="operator")
    row = run_day(MON, gross="1000")
    assert row.extra_allocation == Decimal("5000")
    assert row.closing_carry == Decimal("5000")


def test_an_entry_dated_into_a_settled_day_lands_in_the_next_row():
    run_day(MON)
    cash.allocate(
        Decimal("300"), book=Book.PAPER, on=MON, actor="t", description="late", source_type="t"
    )
    tuesday = run_day(TUE)
    assert tuesday.extra_allocation == Decimal("300")
    assert tuesday.closing_carry == Decimal("2300")


def test_an_allocation_for_a_later_day_is_not_counted_early():
    cash.allocate_daily(TUE, book=Book.PAPER)
    monday = run_day(MON)
    assert monday.closing_carry == Decimal("1000")
    tuesday = run_day(TUE)
    assert tuesday.daily_allocation == Decimal("1000")


def test_the_books_do_not_mix():
    run_day(MON, gross="400")
    assert opening_for(TUE, book=Book.LIVE).carry == 0


# -- sale proceeds (D-S1) --------------------------------------------------


def reinvest(day, amount):
    """A settled rebalance batch's net proceeds, given to the engine."""
    return cash.allocate(
        Decimal(amount),
        book=Book.PAPER,
        on=day,
        actor="system",
        description="reinvested proceeds",
        source_type=cash.REINVEST,
        source_account=accounts.UNALLOCATED,
    )


def test_a_days_sale_proceeds_join_its_closing_carry():
    cash.allocate_daily(MON, book=Book.PAPER)
    spend(MON, "900", "1")
    reinvest(MON, "5000")
    row = settle_day(MON, book=Book.PAPER)

    assert row.proceeds == Decimal("5000")
    assert row.extra_allocation == Decimal("0")
    assert row.closing_carry == Decimal("5099")  # 1000 + 5000 - 900 - 1
    assert row.reconciles


def test_proceeds_are_the_next_days_money():
    cash.allocate_daily(MON, book=Book.PAPER)
    spend(MON, "1000")
    reinvest(MON, "5000")
    settle_day(MON, book=Book.PAPER)

    assert opening_for(TUE, book=Book.PAPER).spendable == Decimal("6000.00")


def test_proceeds_are_not_what_the_day_had_before_it_spent():
    """`available` is the morning's money; a sale arrives during the day."""
    cash.allocate_daily(MON, book=Book.PAPER)
    reinvest(MON, "5000")
    row = settle_day(MON, book=Book.PAPER)

    assert row.available == Decimal("1000")


def test_proceeds_of_a_batch_cancelled_mid_day_are_not_spendable_that_day():
    """I-1 / M6: a batch closed mid-day -- a cancel or a stop -- gives its
    proceeds to the engine dated today. The row shows them; a Regenerate, a
    replan or the cash line of the same day must not spend them (D-S1)."""
    from engine.settlement import day_spend

    cash.allocate_daily(MON, book=Book.PAPER)
    spend(MON, "400")
    reinvest(MON, "5000")

    opening = opening_for(MON, book=Book.PAPER)
    assert opening.spendable == Decimal("600.00")
    assert day_spend(MON).remaining == Decimal("600.00")

    row = settle_day(MON, book=Book.PAPER)
    assert row.proceeds == Decimal("5000")
    assert opening_for(TUE, book=Book.PAPER).spendable == Decimal("6600.00")


def test_proceeds_are_not_contributions():
    cash.allocate_daily(MON, book=Book.PAPER)
    spend(MON, "1000")
    reinvest(MON, "5000")
    settle_day(MON, book=Book.PAPER)

    result = reconcile(book=Book.PAPER)

    assert result["net_in"] == "1000.00"
    assert result["proceeds"] == "5000.00"
    assert result["ok"] is True


# -- reconciliation --------------------------------------------------------


def test_reconcile_checks_every_row_and_the_chain():
    run_day(MON, gross="900")
    run_day(TUE, gross="1050")
    run_day(WED)
    run_day(THU, gross="1900", charges="3.40")
    result = reconcile(book=Book.PAPER)
    assert result["ok"]
    assert result["rows"] == 4
    assert result["journal_breaks"] == []


def test_a_row_that_disagrees_with_the_journal_is_reported():
    """Balanced on its own and chained to the day before, but not what the
    journal held: only the check against the journal can see it."""
    run_day(MON)
    CashLedger.objects.create(
        book=Book.PAPER,
        trading_date=TUE,
        opening_carry=Decimal("1000"),
        daily_allocation=Decimal("1000"),
        closing_carry=Decimal("2000"),
    )

    result = reconcile(book=Book.PAPER)

    assert result["unbalanced"] == []
    assert result["chain_breaks"] == []
    assert [b["date"] for b in result["journal_breaks"]] == [str(TUE)]
    assert result["journal_breaks"][0]["expected"] == "1000.000000"
    assert result["ok"] is False


def test_a_late_entry_does_not_break_the_row_it_missed():
    run_day(MON)
    cash.allocate(
        Decimal("300"), book=Book.PAPER, on=MON, actor="t", description="late", source_type="t"
    )
    run_day(TUE)

    assert reconcile(book=Book.PAPER)["journal_breaks"] == []


def test_a_day_cannot_be_settled_after_a_later_one():
    """Tuesday's row already counted every line dated Monday; a Monday row
    written now would count them again."""
    cash.allocate_daily(MON, book=Book.PAPER)
    run_day(TUE)

    with pytest.raises(LedgerError, match="already summarised"):
        settle_day(MON, book=Book.PAPER)
    assert CashLedger.objects.count() == 1


# -- money is conserved ----------------------------------------------------

DAYS = [MON, TUE, WED, THU, FRI, NEXT_MON]

day_plan = st.tuples(
    st.decimals(min_value=0, max_value=3000, places=2),
    st.decimals(min_value=0, max_value=20, places=2),
    st.decimals(min_value=0, max_value=20000, places=2),
)


@settings(
    max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
)
@given(plans=st.lists(day_plan, min_size=len(DAYS), max_size=len(DAYS)))
def test_money_is_conserved_over_any_run_of_days(plans):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SAVEPOINT prop")
    try:
        for day, (gross, charges, lumpsum) in zip(DAYS, plans, strict=True):
            if lumpsum:
                cash.record_lumpsum(lumpsum, received_on=day, actor="operator")
            run_day(day, gross=gross, charges=charges)

        rows = list(CashLedger.objects.filter(book=Book.PAPER).order_by("trading_date"))
        assert all(row.reconciles for row in rows)
        for previous, current in pairwise(rows):
            assert current.opening_carry == previous.closing_carry
        total_in = sum(
            (r.daily_allocation + r.extra_allocation + r.proceeds for r in rows), Decimal("0")
        )
        total_out = sum((r.cash_spent + r.charges for r in rows), Decimal("0"))
        assert total_in - total_out == rows[-1].closing_carry
        assert reconcile(book=Book.PAPER)["ok"]
    finally:
        with connection.cursor() as cursor:
            cursor.execute("ROLLBACK TO SAVEPOINT prop")


def test_day_settled_reads_the_days_own_book(db):
    """M1: whether a day has settled is its own book's row, not either book's."""
    from engine.ledger import book_of_day, day_settled
    from engine.models import CashLedger, TradeSheet

    day = date(2026, 9, 22)
    TradeSheet.objects.create(trading_date=day, version=1, book="paper")
    CashLedger.objects.create(book="live", trading_date=day)

    assert book_of_day(day) == "paper"
    assert not day_settled(day)

    CashLedger.objects.create(book="paper", trading_date=day)
    assert day_settled(day)


def test_a_day_with_no_sheet_is_the_mode_in_force(db, already_live):
    from engine.ledger import book_of_day, day_settled
    from engine.models import CashLedger

    day = date(2026, 9, 22)
    CashLedger.objects.create(book="paper", trading_date=day)
    already_live()

    assert book_of_day(day) == "live"
    assert not day_settled(day)
