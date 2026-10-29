"""The day page: a tradesheet read back from what it stored.

The decision column and the day's navigation are read from rows, never
recomputed, so what these tests pin is that the reading is faithful.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db

DAY = date(2026, 9, 18)


def a_sheet(day=DAY, *, version=1, instrument=None, quantity=0, spendable="3000", snapshot=None):
    from engine.models import TradeSheet, TradeSheetState

    return TradeSheet.objects.create(
        trading_date=day,
        version=version,
        trace_id=f"ts-{day:%Y-%m-%d}",
        state=TradeSheetState.FILLED if quantity else TradeSheetState.SKIPPED,
        instrument=instrument,
        quantity=quantity,
        spendable_cash=Decimal(spendable),
        ranking_snapshot=snapshot or {},
    )


def ranked(*rows):
    return {
        "ranked": [
            {"rank": i, "symbol": sym, "gap": gap, "ask": ask, "target_pct": "10"}
            for i, (sym, gap, ask) in enumerate(rows, start=1)
        ]
    }


@pytest.fixture
def infy(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=1, name="Infosys")


def test_the_walk_is_read_back_from_the_stored_ranking(infy):
    from engine.day import ranking_rows

    sheet = a_sheet(
        instrument=infy,
        quantity=1,
        spendable="2000",
        snapshot={
            **ranked(("TCS", "-3.0", "3500"), ("INFY", "-2.0", "1700"), ("ITC", "-1.0", "450")),
            "passed_over": {"unaffordable": ["TCS"], "capped": []},
        },
    )

    rows = {row["symbol"]: row for row in ranking_rows(sheet)}

    assert rows["TCS"]["decision"] == "passed · cannot afford one"
    assert rows["TCS"]["max_qty"] == 0
    assert rows["INFY"]["decision"] == "buy 1"
    assert rows["INFY"]["chosen"] is True
    assert rows["ITC"]["decision"] == "not reached"
    assert rows["ITC"]["max_qty"] == 4
    assert rows["TCS"]["gap_pct"] == 50.0


def test_a_capped_name_is_named_as_capped(infy):
    from engine.day import ranking_rows

    sheet = a_sheet(
        instrument=infy,
        quantity=1,
        snapshot={
            **ranked(("ITC", "-3.0", "450"), ("INFY", "-2.0", "1700")),
            "passed_over": {"unaffordable": [], "capped": ["ITC"]},
        },
    )

    assert ranking_rows(sheet)[0]["decision"] == "passed · would overshoot target"


def test_an_older_sheet_without_the_walk_still_says_what_it_can(infy):
    """Sheets written before the walk was stored infer only the one thing the
    stored prices prove: that a share was out of reach."""
    from engine.day import ranking_rows

    sheet = a_sheet(
        instrument=infy,
        quantity=1,
        spendable="2000",
        snapshot=ranked(("TCS", "-3.0", "3500"), ("INFY", "-2.0", "1700")),
    )

    assert ranking_rows(sheet)[0]["decision"] == "passed · cannot afford one"


def test_neighbours_skip_to_the_days_that_have_sheets():
    from engine.day import neighbours

    a_sheet(date(2026, 9, 16))
    a_sheet(DAY)
    a_sheet(date(2026, 9, 21))
    a_sheet(date(2026, 9, 21), version=2)

    around = neighbours(DAY)

    assert around["previous"].trading_date == date(2026, 9, 16)
    assert around["next"].trading_date == date(2026, 9, 21)
    assert around["next"].version == 2
    assert around["on_record"] == 3


def test_charges_are_taken_apart_by_component(infy):
    from django.utils import timezone

    from engine.day import charge_lines
    from orders.models import Fill, Order, OrderMode, OrderType

    sheet = a_sheet(instrument=infy, quantity=1)
    order = Order.objects.create(
        tag="t0",
        tradesheet=sheet,
        instrument=infy,
        trading_date=DAY,
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=Decimal("10000"),
    )
    Fill.objects.create(
        order=order,
        kite_trade_id="p-1",
        quantity=1,
        price=Decimal("10000"),
        executed_at=timezone.now(),
    )

    lines = {line.label: line.amount for line in charge_lines(order.fills.select_related("order"))}

    assert lines["STT"] == Decimal("10.00")
    assert lines["stamp duty"] == Decimal("1.50")
    assert "DP charge" not in lines  # a buy pays none


def test_a_lumpsum_that_arrived_after_the_sheet_is_shown_apart():
    from engine.day import lumpsums
    from journal.cash import record_lumpsum

    record_lumpsum(Decimal("100"), received_on=DAY, actor="op", reference="before")
    a_sheet(DAY)
    record_lumpsum(Decimal("50000"), received_on=DAY, actor="op", reference="after")

    found = lumpsums(DAY)

    assert [c["amount"] for c in found["in_sheet"]] == [Decimal("100")]
    assert [c["amount"] for c in found["after"]] == [Decimal("50000")]


def test_a_saturday_lumpsum_is_mondays_money():
    from engine.day import lumpsums
    from journal.cash import record_lumpsum

    monday = date(2026, 9, 21)
    record_lumpsum(Decimal("5000"), received_on=date(2026, 9, 19), actor="op", reference="sat")
    record_lumpsum(Decimal("70"), received_on=DAY, actor="op", reference="friday's")
    a_sheet(monday)

    found = lumpsums(monday)

    assert [(c["amount"], c["label"]) for c in found["in_sheet"]] == [(Decimal("5000"), "lumpsum")]
    assert found["after"] == []


def test_reinvested_proceeds_are_labelled_as_what_they_are():
    from engine.day import lumpsums
    from journal.cash import REINVEST, allocate

    allocate(
        Decimal("800"),
        book="paper",
        on=DAY,
        actor="system",
        description="reinvested proceeds of rebalance sell 1",
        source_type=REINVEST,
    )

    found = lumpsums(DAY)

    assert [c["label"] for c in found["after"]] == ["reinvested proceeds"]


def test_the_page_shows_navigation_decisions_and_before_after(signed_in, infy):
    from audit.context import reset_trace_id, set_trace_id
    from audit.events import record

    a_sheet(date(2026, 9, 17))
    sheet = a_sheet(
        instrument=infy,
        quantity=1,
        snapshot=ranked(("INFY", "-2.0", "1700"), ("ITC", "-1.0", "450")),
    )
    token = set_trace_id(sheet.trace_id)
    try:
        record("tradesheet.overridden", actor="op", entity=sheet, before={"q": 2}, after={"q": 1})
    finally:
        reset_trace_id(token)

    body = signed_in.get(reverse("engine:detail", args=[DAY, 1])).content

    assert b"Previous trading day" in body
    assert b"buy 1" in body
    assert b"not reached" in body
    assert b"q=2" in body and b"q=1" in body


def test_a_row_without_a_stored_ask_claims_nothing_about_affordability():
    from engine.day import ranking_rows

    sheet = a_sheet(snapshot={"ranked": [{"tradingsymbol": "ITC", "target_pct": "9"}]})

    row = ranking_rows(sheet)[0]

    assert row["symbol"] == "ITC"
    assert row["max_qty"] is None
    assert row["decision"] == "—"
