from datetime import date
from decimal import Decimal

import pytest

from journal import cash
from journal.accounts import INVESTMENTS, SETTLE_ENGINE
from journal.external import record_dividend
from journal.models import Book, Component, EntryKind
from journal.performance import contributed, flows
from journal.posting import Line, post
from records.external import record_external
from records.models import ExternalSide, PaidFrom

DAY = date(2026, 9, 22)


@pytest.mark.django_db
def test_an_ipo_is_money_invested(infy):
    record_external(
        instrument=infy,
        side=ExternalSide.BUY,
        quantity=10,
        occurred_on=DAY,
        actor="o",
        reason="IPO",
        price=Decimal("50"),
        paid_from=PaidFrom.BANK,
    )
    assert flows(book=Book.LIVE, as_of=DAY) == [(DAY, Decimal("-500"))]
    assert contributed(book=Book.LIVE, as_of=DAY) == Decimal("500")


@pytest.mark.django_db
def test_a_dividend_is_money_returned(infy):
    record_dividend("INFY", Decimal("42"), paid_on=DAY, actor="o")
    assert (DAY, Decimal("42")) in flows(book=Book.LIVE, as_of=DAY)


@pytest.mark.django_db
def test_the_kite_fee_is_not_a_cost_of_the_portfolio():
    cash.contribute(Decimal("1000"), on=DAY, actor="o")
    cash.withdraw(
        Decimal("500"), on=DAY, actor="o", reason="Kite Connect", source_type="owner_expense"
    )
    assert flows(book=Book.LIVE, as_of=DAY) == []
    assert contributed(book=Book.LIVE, as_of=DAY) == Decimal("500")


@pytest.mark.django_db
def test_amc_is_a_cost_of_the_portfolio():
    cash.account_charge(Decimal("88.5"), on=DAY, component="amc", actor="o", description="AMC")
    assert flows(book=Book.LIVE, as_of=DAY) == [(DAY, Decimal("-88.5"))]


@pytest.mark.django_db
def test_a_negative_other_line_on_a_buy_nets_the_outflow(infy):
    """The rate card's estimate can overshoot what the fill actually charged,
    which posts a negative OTHER line on 1300 (spec §7.3). Counting only the
    positive lines would overstate the outflow by that difference."""
    post(
        book=Book.LIVE,
        kind=EntryKind.BUY_FILL,
        occurred_on=DAY,
        description="buy with an over-estimated charge card",
        lines=[
            Line(INVESTMENTS, Decimal("1000"), "INFY", Component.GROSS),
            Line(INVESTMENTS, Decimal("-5"), "INFY", Component.OTHER),
            Line(SETTLE_ENGINE, Decimal("-995")),
        ],
        actor="o",
    )
    assert flows(book=Book.LIVE, as_of=DAY) == [(DAY, Decimal("-995"))]


@pytest.mark.django_db
def test_a_reversed_acquisition_is_no_longer_money_invested(infy):
    """M6: a reversal counts as the kind it reverses, so the two net out."""
    from journal.models import JournalEntry
    from journal.posting import reverse

    record_external(
        instrument=infy,
        side=ExternalSide.BUY,
        quantity=10,
        occurred_on=DAY,
        actor="o",
        reason="IPO",
        price=Decimal("50"),
        paid_from=PaidFrom.BANK,
    )
    entry = JournalEntry.objects.get(kind=EntryKind.EXTERNAL_ACQUISITION)
    reverse(entry, reason="recorded twice", actor="o", on=date(2026, 9, 23))

    assert flows(book=Book.LIVE, as_of=date(2026, 9, 23)) == [
        (DAY, Decimal("-500")),
        (date(2026, 9, 23), Decimal("500")),
    ]


# --- Go-live round 1, wave B: the opening in performance (I10) ---------


def _an_opening_lot(infy, *, quantity, cost, acquired_on, remaining=None):
    from portfolio.models import BasisState, Lot, OpeningImport

    opening = OpeningImport.objects.create(imported_by="operator", source="statement")
    return Lot.objects.create(
        instrument=infy,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity if remaining is None else remaining,
        cost_per_unit=Decimal(cost),
        basis_state=BasisState.IMPORTED,
        opening_import=opening,
    )


@pytest.mark.django_db
def test_opening_lots_flow_on_their_acquisition_dates(infy):
    from journal.external import open_books

    first = _an_opening_lot(infy, quantity=10, cost="100", acquired_on=date(2021, 5, 3))
    # Six of ten sold before go-live: those are outside the portfolio and XIRR.
    second = _an_opening_lot(
        infy, quantity=10, cost="50", acquired_on=date(2023, 1, 9), remaining=4
    )
    entry = open_books(cash=Decimal("700"), on=date(2026, 9, 1), actor="operator")

    by_lot = {line.lot_id: line.amount for line in entry.lines.filter(lot__isnull=False)}
    assert by_lot == {first.id: Decimal("1000"), second.id: Decimal("200")}

    assert flows(book=Book.LIVE, as_of=DAY) == [
        (date(2021, 5, 3), Decimal("-1000")),
        (date(2023, 1, 9), Decimal("-200")),
    ]


@pytest.mark.django_db
def test_a_reversed_opening_takes_its_lots_out_where_they_went_in(infy):
    from journal.external import open_books
    from journal.posting import reverse

    _an_opening_lot(infy, quantity=10, cost="100", acquired_on=date(2021, 5, 3))
    entry = open_books(cash=Decimal("0"), on=date(2026, 9, 1), actor="operator")
    reversal = reverse(entry, reason="wrong", actor="operator")

    assert {line.lot_id for line in reversal.lines.filter(account_id=INVESTMENTS)} == {
        line.lot_id for line in entry.lines.filter(account_id=INVESTMENTS)
    }
    assert flows(book=Book.LIVE, as_of=DAY) == []


@pytest.mark.django_db
def test_contributed_includes_the_opening_balance(infy):
    from journal.external import open_books

    _an_opening_lot(infy, quantity=10, cost="100", acquired_on=date(2021, 5, 3))
    open_books(cash=Decimal("500"), on=date(2026, 9, 1), actor="operator")
    cash.contribute(Decimal("2000"), on=date(2026, 9, 10), actor="o")

    assert contributed(book=Book.LIVE, as_of=DAY) == Decimal("3500")
