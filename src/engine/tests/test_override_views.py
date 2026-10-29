"""Overrides from a browser.

The first screen that changes what the system trades rather than what it
records. Every rule is the engine's; these assert the screen cannot get round
one of them.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


@pytest.fixture
def sheet(instrument):
    from engine.models import TradeSheet, TradeSheetState

    return TradeSheet.objects.create(
        trading_date="2026-09-22",
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=5,
        spendable_cash=Decimal("10000"),
    )


def _url(sheet):
    return reverse("engine:override", args=[sheet.trading_date, sheet.version])


def test_overriding_is_not_reachable_anonymously(client, sheet):
    assert client.post(_url(sheet), {"action": "skip", "reason": "x"}).status_code == 302


def test_the_panel_says_what_each_override_does(signed_in, sheet):
    body = signed_in.get(reverse("engine:detail", args=[sheet.trading_date, sheet.version])).content

    assert b"Operator override" in body
    assert b"a skip is refused once" in body


def test_a_reduction_changes_the_day(signed_in, sheet):
    signed_in.post(
        _url(sheet), {"action": "reduce", "quantity": 2, "reason": "too much"}, follow=True
    )

    sheet.refresh_from_db()
    assert sheet.quantity == 2


def test_skipping_a_day_with_a_fill_is_refused(signed_in, sheet, instrument):
    """There is no way to unwind a fill, so a day with fills is partial rather
    than skipped -- calling it skipped would be a lie about what happened."""
    from django.utils import timezone

    from engine.models import TradeSheetState
    from orders.models import Fill, Order, OrderMode, OrderState, OrderType

    order = Order.objects.create(
        tag="tx-1",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=1,
        filled_quantity=1,
        limit_price=Decimal("1700"),
        state=OrderState.COMPLETE,
    )
    Fill.objects.create(
        order=order,
        kite_trade_id="t-1",
        quantity=1,
        price=Decimal("1700"),
        executed_at=timezone.now(),
    )

    signed_in.post(_url(sheet), {"action": "skip", "reason": "changed my mind"}, follow=True)

    sheet.refresh_from_db()
    assert sheet.state != TradeSheetState.SKIPPED


def test_excluding_a_tick_leaves_the_quantity_alone(signed_in, sheet):
    from orders import overrides

    signed_in.post(
        _url(sheet), {"action": "exclude", "tick_no": 3, "reason": "wide spread"}, follow=True
    )

    sheet.refresh_from_db()
    assert sheet.quantity == 5
    assert 3 in overrides.excluded_ticks(sheet)


def test_an_unknown_action_does_nothing(signed_in, sheet):
    response = signed_in.post(_url(sheet), {"action": "sell_everything"}, follow=True)

    sheet.refresh_from_db()
    assert sheet.quantity == 5
    assert b"not something an override can do" in response.content


def test_an_override_without_a_reason_is_refused(signed_in, sheet):
    signed_in.post(_url(sheet), {"action": "reduce", "quantity": 1}, follow=True)

    sheet.refresh_from_db()
    assert sheet.quantity == 5


# -- the day, reconstructed from stored data alone -----------------------


def test_the_day_shows_the_ledger_row_it_wrote(signed_in, sheet):
    from engine.models import CashLedger

    CashLedger.objects.create(
        book="paper",
        trading_date=sheet.trading_date,
        daily_allocation=Decimal("2600"),
        cash_spent=Decimal("1700"),
        charges=Decimal("2.04"),
        closing_carry=Decimal("897.96"),
    )

    body = signed_in.get(reverse("engine:detail", args=[sheet.trading_date, sheet.version])).content

    assert b"ledger row this day wrote" in body
    assert b"897.96" in body
    assert b"never updated" in body


def test_the_day_shows_everything_under_its_trace_id(signed_in, sheet):
    """One trace id on every line the day wrote is what makes a past day
    reconstructable years later."""
    from audit.context import reset_trace_id, set_trace_id
    from audit.events import record

    token = set_trace_id(sheet.trace_id)
    try:
        record("tradesheet.generated", actor="system", entity=sheet)
    finally:
        reset_trace_id(token)

    body = signed_in.get(reverse("engine:detail", args=[sheet.trading_date, sheet.version])).content

    assert b"tradesheet.generated" in body
    assert sheet.trace_id.encode() in body


def test_the_day_totals_what_it_cost(signed_in, sheet, instrument):
    from django.utils import timezone

    from orders.models import Fill, Order, OrderMode, OrderState, OrderType

    order = Order.objects.create(
        tag="tx-1",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=1,
        filled_quantity=1,
        limit_price=Decimal("1700"),
        state=OrderState.COMPLETE,
    )
    Fill.objects.create(
        order=order,
        kite_trade_id="t-1",
        quantity=1,
        price=Decimal("1700"),
        charges=Decimal("2.04"),
        executed_at=timezone.now(),
    )

    body = signed_in.get(reverse("engine:detail", args=[sheet.trading_date, sheet.version])).content

    assert b"1700.00 gross" in body
    assert b"1702.04" in body


def test_a_past_day_renders_without_the_broker(signed_in, sheet):
    from audit.models import BrokerCall

    before = BrokerCall.objects.count()

    signed_in.get(reverse("engine:detail", args=[sheet.trading_date, sheet.version]))

    assert BrokerCall.objects.count() == before
