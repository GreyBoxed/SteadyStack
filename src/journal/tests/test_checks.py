from datetime import date
from decimal import Decimal

import pytest

from journal import cash
from journal.checks import check_balance, funding_warnings, investments_disagree
from journal.models import BalanceCheck


def _opened(on=date(2026, 9, 1)):
    """The live book opened with nothing in it, so the live checks run."""
    from journal.external import open_books

    open_books(cash=Decimal("0"), on=on, actor="o")


class FakeBroker:
    def __init__(self, opening):
        self.opening = opening

    def margins(self):
        return {"equity": {"available": {"opening_balance": float(self.opening)}}}


@pytest.mark.django_db
def test_a_balance_that_agrees_is_recorded_quietly(caplog):
    _opened()
    cash.contribute(Decimal("500"), on=date(2026, 9, 21), actor="o")
    row = check_balance(broker=FakeBroker(Decimal("500")), on=date(2026, 9, 22))
    assert row.difference == 0
    assert "disagrees" not in caplog.text


@pytest.mark.django_db
def test_a_balance_that_disagrees_beyond_a_rupee_alerts(caplog):
    _opened()
    cash.contribute(Decimal("500"), on=date(2026, 9, 21), actor="o")
    row = check_balance(broker=FakeBroker(Decimal("411.50")), on=date(2026, 9, 22))
    assert row.difference == Decimal("-88.50")
    assert "zerodha balance disagrees" in caplog.text


@pytest.mark.django_db
def test_a_morning_is_checked_once():
    _opened()
    check_balance(broker=FakeBroker(Decimal("0")), on=date(2026, 9, 22))
    check_balance(broker=FakeBroker(Decimal("5")), on=date(2026, 9, 22))
    assert BalanceCheck.objects.count() == 1


@pytest.mark.django_db
def test_unfunded_allocations_and_carry_owed_are_warned_about():
    _opened()
    cash.allocate_daily(date(2026, 9, 22), book="live")
    assert any("unallocated cash is overdrawn by ₹1000.00" in w for w in funding_warnings())

    cash.contribute(Decimal("3000"), on=date(2026, 9, 21), actor="o")
    cash.withdraw(Decimal("3000"), on=date(2026, 9, 23), actor="s", reason="sweep", sweep=True)
    assert any("quarterly settlement" in w for w in funding_warnings())


@pytest.mark.django_db
def test_the_balance_is_not_checked_before_the_live_book_opens(caplog):
    """I5: before go-live the live book holds only part of Zerodha's cash."""
    cash.contribute(Decimal("500"), on=date(2026, 9, 21), actor="o")
    assert check_balance(broker=FakeBroker(Decimal("9000")), on=date(2026, 9, 22)) is None
    assert BalanceCheck.objects.count() == 0
    assert "disagrees" not in caplog.text


@pytest.mark.django_db
def test_no_balance_check_on_or_before_the_opening_date(caplog):
    """I3: an opening dated the check's own morning (or later) is excluded
    from yesterday's close, so the check would compare Zerodha's whole balance
    with a fragment of it."""
    from journal.external import open_books

    open_books(cash=Decimal("20000"), on=date(2026, 9, 22), actor="o")

    for on in (date(2026, 9, 21), date(2026, 9, 22)):
        assert check_balance(broker=FakeBroker(Decimal("20000")), on=on) is None
    assert BalanceCheck.objects.count() == 0
    assert "disagrees" not in caplog.text


@pytest.mark.django_db
def test_opening_on_the_last_day_before_go_live_agrees_on_the_first_morning(caplog):
    """--on is the last trading day before go-live and --cash that day's
    closing balance, which is go-live morning's opening balance."""
    from journal.external import open_books

    open_books(cash=Decimal("20000"), on=date(2026, 9, 21), actor="o")

    row = check_balance(broker=FakeBroker(Decimal("20000")), on=date(2026, 9, 22))

    assert row.difference == 0
    assert "disagrees" not in caplog.text


@pytest.mark.django_db
def test_no_funding_warning_before_the_live_book_opens():
    cash.account_charge(
        Decimal("88.5"), on=date(2026, 9, 21), component="amc", actor="o", description="AMC"
    )
    cash.contribute(Decimal("3000"), on=date(2026, 9, 21), actor="o")
    cash.withdraw(Decimal("3000"), on=date(2026, 9, 23), actor="s", reason="sweep", sweep=True)
    assert funding_warnings() == []


@pytest.mark.django_db
def test_the_command_says_the_live_book_is_not_open(monkeypatch):
    from io import StringIO

    from django.core.management import call_command

    from broker import session

    class Valid:
        valid = True
        reason = ""

    monkeypatch.setattr(session, "status", lambda: Valid())
    monkeypatch.setattr(session, "attach", lambda broker: None)
    monkeypatch.setattr(
        "journal.management.commands.check_balance.get_broker", lambda: FakeBroker(Decimal("1"))
    )
    out = StringIO()
    call_command("check_balance", stdout=out)
    assert "live book not opened" in out.getvalue()


@pytest.mark.django_db
def test_investments_agree_with_the_lots_after_a_booked_fill(paper_buy_fill):
    from orders.booking import book

    book(paper_buy_fill(quantity=1, price="100"))
    assert investments_disagree(book="paper") == {}


@pytest.mark.django_db
def test_a_kite_failure_reading_the_balance_logs_an_error(monkeypatch, caplog):
    """M-4: a scheduled check that cannot run must alarm (task_failed reads
    ERROR lines from steadystack.*), not only tell stdout."""
    import logging

    from django.core.management import call_command

    from broker import session
    from broker.exceptions import TransientBrokerError

    _opened()

    class Valid:
        valid = True
        reason = ""

    class Down:
        def margins(self):
            raise TransientBrokerError("Kite did not answer")

    monkeypatch.setattr(session, "status", lambda: Valid())
    monkeypatch.setattr(session, "attach", lambda broker: None)
    monkeypatch.setattr("journal.management.commands.check_balance.get_broker", lambda: Down())

    with caplog.at_level(logging.ERROR, logger="steadystack.journal"):
        call_command("check_balance")

    [line] = [
        r for r in caplog.records if r.name == "steadystack.journal" and r.levelno >= logging.ERROR
    ]
    assert line.getMessage() == "could not read zerodha's balance to check it"
