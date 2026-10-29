from datetime import date

import pytest

from audit.models import AuditEvent
from journal.models import Book, PeriodClose
from journal.periods import JournalPeriodError, close_year, fy_end_for, is_closed
from journal.statement.store import store_upload
from journal.tests.conftest import csv_of


def _statement_past(day="2026-04-02", opening="0"):
    """A statement running past 31 March 2026, its only line after it."""
    raw = csv_of(opening, [("Charges for enabling DDPI", day, "118", "0")])
    return store_upload(raw, filename=f"s-{day}.csv", actor="o")


def test_the_financial_year_ends_on_31_march():
    assert fy_end_for(date(2026, 3, 31)) == date(2026, 3, 31)
    assert fy_end_for(date(2026, 4, 1)) == date(2027, 3, 31)
    assert fy_end_for(date(2026, 12, 31)) == date(2027, 3, 31)


@pytest.mark.django_db
def test_closing_a_year_records_who_and_closes_every_day_in_it():
    _statement_past()
    close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")
    assert is_closed(Book.LIVE, date(2025, 4, 1))
    assert not is_closed(Book.LIVE, date(2026, 4, 1))


@pytest.mark.django_db
def test_only_a_31_march_can_be_closed():
    with pytest.raises(JournalPeriodError, match="31 March"):
        close_year(book=Book.LIVE, fy_end=date(2026, 6, 30), actor="ca")


@pytest.mark.django_db
def test_a_year_cannot_be_closed_while_it_is_still_running(time_machine):
    time_machine.move_to("2026-03-15")
    with pytest.raises(JournalPeriodError, match="not over"):
        close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")


@pytest.mark.django_db
def test_closing_the_same_year_twice_is_idempotent():
    _statement_past()
    close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")
    close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")

    assert PeriodClose.objects.filter(book=Book.LIVE, fy_end=date(2026, 3, 31)).count() == 1
    assert AuditEvent.objects.filter(action="journal.year_closed").count() == 1


# --- Go-live round 1, wave B: preconditions (I8) -----------------------


@pytest.mark.django_db
def test_close_year_refuses_until_a_statement_runs_through_the_year_end():
    with pytest.raises(JournalPeriodError, match="statement"):
        close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")
    _statement_past(day="2026-03-28")
    with pytest.raises(JournalPeriodError, match="statement"):
        close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")
    _statement_past(day="2026-04-02", opening="-118")
    from journal.models import StatementLine
    from journal.statement.reconcile import confirm

    march = StatementLine.objects.get(posting_date=date(2026, 3, 28))
    confirm(march, kind="account_charge", actor="operator")
    close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")


@pytest.mark.django_db
def test_close_year_refuses_while_lines_of_that_year_are_queued():
    from journal.statement.reconcile import confirm

    raw = csv_of(
        "0",
        [
            ("AMC for Demat Account", "2026-03-20", "88.5", "0"),
            ("Charges for enabling DDPI", "2026-04-02", "118", "0"),
        ],
    )
    store_upload(raw, filename="s.csv", actor="o")

    with pytest.raises(JournalPeriodError, match="waiting"):
        close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")

    from journal.models import StatementLine

    amc = StatementLine.objects.get(particulars__startswith="AMC")
    confirm(amc, kind="account_charge", actor="operator")
    # The April line still waits, but it belongs to the next year.
    close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")


@pytest.mark.django_db
def test_close_year_refuses_while_a_settlement_of_that_year_disagrees(time_machine):
    from datetime import datetime
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from journal import accounts
    from journal.posting import Line, post
    from journal.statement.reconcile import auto_match

    time_machine.move_to(datetime(2026, 9, 25, 12, tzinfo=ZoneInfo("Asia/Kolkata")))
    post(
        book=Book.LIVE,
        kind="settlement",
        occurred_on=date(2026, 3, 20),
        description="t",
        lines=[Line(accounts.SETTLE_OTHER, Decimal("100")), Line(accounts.UNALLOCATED, -100)],
    )
    raw = csv_of(
        "0",
        [
            ("Net settlement for Equity with settlement number: 2026050", "2026-03-20", "99", "0"),
            ("Charges for enabling DDPI", "2026-04-02", "118", "0"),
        ],
        voucher_type="Book Voucher",
    )
    auto_match(store_upload(raw, filename="s.csv", actor="o").upload)

    with pytest.raises(JournalPeriodError, match="disagree"):
        close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")


@pytest.mark.django_db
def test_the_paper_book_closes_without_a_statement():
    close_year(book=Book.PAPER, fy_end=date(2026, 3, 31), actor="ca")
    assert is_closed(Book.PAPER, date(2026, 3, 31))


@pytest.mark.django_db
def test_a_line_in_a_closed_year_posts_today_as_a_prior_period_item(time_machine):
    """A line reopened after the close (its entry reversed) is still confirmable."""
    from datetime import datetime
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from journal import accounts
    from journal.balances import balance
    from journal.models import StatementLine
    from journal.posting import reverse
    from journal.statement.reconcile import confirm, queue

    time_machine.move_to(datetime(2026, 9, 25, 12, tzinfo=ZoneInfo("Asia/Kolkata")))
    raw = csv_of(
        "0",
        [
            ("AMC for Demat Account", "2026-03-20", "88.5", "0"),
            ("Charges for enabling DDPI", "2026-04-02", "118", "0"),
        ],
    )
    store_upload(raw, filename="s.csv", actor="o")
    amc = StatementLine.objects.get(particulars__startswith="AMC")
    wrong = confirm(amc, kind="withdrawal", actor="operator")
    close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")

    reverse(wrong, reason="it was the AMC", actor="operator")
    assert amc in list(queue())
    entry = confirm(amc, kind="account_charge", component="amc", actor="operator")

    assert entry.occurred_on == date(2026, 9, 25)
    assert "relates to 2026-03-20" in entry.description
    assert "after the year close" in entry.description
    assert balance(accounts.ACCOUNT_CHARGES, book=Book.LIVE) == Decimal("88.5")
    assert amc not in list(queue())


@pytest.mark.django_db
def test_an_explained_settlement_lets_the_year_close(time_machine):
    """m-4: an uncorrectable difference, explained by a person, no longer blocks."""
    from datetime import datetime
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from journal import accounts
    from journal.models import StatementLine
    from journal.posting import Line, post
    from journal.statement.reconcile import auto_match, explain_settlement

    time_machine.move_to(datetime(2026, 9, 25, 12, tzinfo=ZoneInfo("Asia/Kolkata")))
    post(
        book=Book.LIVE,
        kind="settlement",
        occurred_on=date(2026, 3, 20),
        description="t",
        lines=[Line(accounts.SETTLE_OTHER, Decimal("100")), Line(accounts.UNALLOCATED, -100)],
    )
    raw = csv_of(
        "0",
        [
            ("Net settlement for Equity with settlement number: 2026050", "2026-03-20", "99", "0"),
            ("Charges for enabling DDPI", "2026-04-02", "118", "0"),
        ],
        voucher_type="Book Voucher",
    )
    auto_match(store_upload(raw, filename="s.csv", actor="o").upload)
    line = StatementLine.objects.get(particulars__startswith="Net settlement")

    explain_settlement(line, reason="reconciled by hand against the contract note", actor="ca")

    close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")
    assert is_closed(Book.LIVE, date(2026, 3, 31))
