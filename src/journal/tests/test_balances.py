from datetime import date
from decimal import Decimal

import pytest

from journal import accounts
from journal.balances import balance, engine_balance, zerodha_cash
from journal.models import Book, EntryKind
from journal.posting import Line, post

MON, TUE = date(2026, 9, 21), date(2026, 9, 22)


def _post(on, lines, book=Book.LIVE, kind=EntryKind.ALLOCATION):
    return post(book=book, kind=kind, occurred_on=on, description="t", lines=lines)


@pytest.mark.django_db
def test_balance_sums_signed_lines_through_a_date():
    _post(
        MON,
        [Line(accounts.ENGINE_CASH, Decimal("1000")), Line(accounts.UNALLOCATED, Decimal("-1000"))],
    )
    _post(
        TUE,
        [Line(accounts.ENGINE_CASH, Decimal("500")), Line(accounts.UNALLOCATED, Decimal("-500"))],
    )

    assert balance(accounts.ENGINE_CASH, book=Book.LIVE) == Decimal("1500")
    assert balance(accounts.ENGINE_CASH, book=Book.LIVE, through=MON) == Decimal("1000")
    assert balance(accounts.ENGINE_CASH, book=Book.LIVE, on=TUE) == Decimal("500")
    assert balance(accounts.ENGINE_CASH, book=Book.PAPER) == Decimal("0")


@pytest.mark.django_db
def test_engine_balance_nets_unsettled_engine_buys():
    _post(
        MON,
        [Line(accounts.ENGINE_CASH, Decimal("1000")), Line(accounts.UNALLOCATED, Decimal("-1000"))],
    )
    _post(
        MON,
        [Line(accounts.INVESTMENTS, Decimal("400")), Line(accounts.SETTLE_ENGINE, Decimal("-400"))],
        kind=EntryKind.BUY_FILL,
    )
    assert engine_balance(Book.LIVE) == Decimal("600")
    assert zerodha_cash(Book.LIVE) == Decimal("-400")
