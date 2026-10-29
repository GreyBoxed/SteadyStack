from datetime import date
from decimal import Decimal

import pytest

from journal import accounts, cash
from journal.balances import general_ledger, trial_balance
from journal.models import Book

MON, TUE = date(2026, 9, 21), date(2026, 9, 22)


@pytest.mark.django_db
def test_the_trial_balance_balances():
    cash.contribute(Decimal("5000"), on=MON, actor="o")
    cash.allocate_daily(MON, book=Book.LIVE)
    cash.account_charge(Decimal("88.5"), on=TUE, component="amc", actor="o", description="AMC")

    rows = trial_balance(book=Book.LIVE, through=TUE)
    assert sum(r.debit for r in rows) == sum(r.credit for r in rows)
    by = {r.code: r for r in rows}
    assert by[accounts.CONTRIBUTIONS].credit == Decimal("5000")
    assert by[accounts.ENGINE_CASH].debit == Decimal("1000")
    assert by[accounts.ACCOUNT_CHARGES].debit == Decimal("88.5")


@pytest.mark.django_db
def test_the_general_ledger_runs_from_an_opening_balance():
    cash.contribute(Decimal("5000"), on=MON, actor="o")
    cash.contribute(Decimal("1000"), on=TUE, actor="o")

    opening, rows = general_ledger(accounts.UNALLOCATED, book=Book.LIVE, start=TUE, end=TUE)
    assert opening == Decimal("5000")
    assert [r.running for r in rows] == [Decimal("6000")]


# --- Go-live round 1, wave B: a financial year's trial balance (M11) ---


def _dividends_in_two_years(infy):
    from journal.external import record_dividend

    record_dividend("INFY", Decimal("40"), paid_on=date(2026, 3, 10), actor="o")
    record_dividend("INFY", Decimal("60"), paid_on=date(2026, 9, 10), actor="o")


@pytest.mark.django_db
def test_fy_trial_balance_shows_only_that_years_income(infy):
    from journal.balances import BROUGHT_FORWARD

    _dividends_in_two_years(infy)

    rows = {
        row.code: row
        for row in trial_balance(book=Book.LIVE, start=date(2026, 4, 1), through=date(2026, 9, 22))
    }

    assert rows[accounts.DIVIDENDS].credit == Decimal("60")
    assert rows[BROUGHT_FORWARD].credit == Decimal("40")
    assert rows[accounts.WITHDRAWALS].debit == Decimal("100")  # equity is as at
    assert sum(r.debit for r in rows.values()) == sum(r.credit for r in rows.values())


@pytest.mark.django_db
def test_the_books_page_offers_a_financial_year(signed_in, infy):
    from django.urls import reverse

    _dividends_in_two_years(infy)

    earlier = signed_in.get(reverse("journal:index"), {"fy": "2026-03-31"})
    assert earlier.context["through"] == date(2026, 3, 31)
    assert earlier.context["start"] == date(2025, 4, 1)
    assert b"FY 2025" in earlier.content
    by_code = {row.code: row for row in earlier.context["rows"]}
    assert by_code[accounts.DIVIDENDS].credit == Decimal("40")

    later = signed_in.get(reverse("journal:index"), {"to": "2026-09-22"})
    by_code = {row.code: row for row in later.context["rows"]}
    assert later.context["start"] == date(2026, 4, 1)
    assert by_code[accounts.DIVIDENDS].credit == Decimal("60")
