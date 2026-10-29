"""Recording Order.exchange_order_id by hand (Minor 3, wave H).

`exchange_order_id` normally arrives from the order book, a postback or the
trade book. The one case it does not: Console's tradebook import finds two or
more of a live day's orders open with no exchange number and no way to tell
them apart, and the only thing left that can say which is which is an
operator reading Kite's order history or the contract note. `record_exchange_order_id`
is the audited way to write what they find, rather than a shell UPDATE that
leaves no trace.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from audit.models import AuditEvent
from engine.models import TradeSheet, TradeSheetState
from orders.exchange_id import RecordRefused, record_exchange_order_id
from orders.models import DAILY_BUY_STREAM, Order, OrderSide, OrderType
from orders.state import OrderState
from portfolio.instruments import refresh
from portfolio.models import Instrument

pytestmark = pytest.mark.django_db

TUE = date(2026, 9, 22)


@pytest.fixture
def instrument():
    refresh(
        [
            {
                "tradingsymbol": "INFY",
                "instrument_token": 408065,
                "name": "INFY",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "segment": "NSE",
                "tick_size": 0.05,
                "lot_size": 1,
            }
        ]
    )
    return Instrument.objects.get(pk="INFY")


@pytest.fixture
def sheet(instrument):
    return TradeSheet.objects.create(
        trading_date=TUE,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        book="live",
    )


def an_order(sheet, instrument, *, tag, mode="live", exchange_order_id="", tick_no=1):
    return Order.objects.create(
        tag=tag,
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        stream=DAILY_BUY_STREAM,
        tick_no=tick_no,
        side=OrderSide.BUY,
        mode=mode,
        order_type=OrderType.LIMIT,
        quantity=3,
        limit_price=Decimal("1650"),
        state=OrderState.OPEN,
        kite_order_id="250922000111" if mode == "live" else "",
        exchange_order_id=exchange_order_id,
    )


def test_records_the_number_on_a_live_order_that_has_none(sheet, instrument):
    order = an_order(sheet, instrument, tag="tx-live-1")

    result = record_exchange_order_id("tx-live-1", "1100000012345678", actor="operator")

    assert result.exchange_order_id == "1100000012345678"
    order.refresh_from_db()
    assert order.exchange_order_id == "1100000012345678"

    event = AuditEvent.objects.get(action="order.exchange_order_id_recorded")
    assert event.actor == "operator"
    assert event.entity_id == str(order.pk)
    assert event.before == {"exchange_order_id": ""}
    assert event.after == {"exchange_order_id": "1100000012345678"}


def test_refuses_an_unknown_tag():
    with pytest.raises(RecordRefused, match="tx-none"):
        record_exchange_order_id("tx-none", "1100000012345678", actor="operator")


def test_refuses_a_paper_order(sheet, instrument):
    an_order(sheet, instrument, tag="tx-paper-1", mode="paper")

    with pytest.raises(RecordRefused, match="paper"):
        record_exchange_order_id("tx-paper-1", "1100000012345678", actor="operator")


def test_refuses_when_one_is_already_set(sheet, instrument):
    an_order(sheet, instrument, tag="tx-live-1", exchange_order_id="1100000099999999")

    with pytest.raises(RecordRefused, match="1100000099999999"):
        record_exchange_order_id("tx-live-1", "1100000012345678", actor="operator")


def test_refuses_when_the_number_is_already_used_by_another_order(sheet, instrument):
    an_order(sheet, instrument, tag="tx-live-1", exchange_order_id="1100000012345678", tick_no=1)
    an_order(sheet, instrument, tag="tx-live-2", tick_no=2)

    with pytest.raises(RecordRefused, match="tx-live-1"):
        record_exchange_order_id("tx-live-2", "1100000012345678", actor="operator")


def test_refuses_a_blank_number(sheet, instrument):
    an_order(sheet, instrument, tag="tx-live-1")

    with pytest.raises(RecordRefused):
        record_exchange_order_id("tx-live-1", "  ", actor="operator")


def test_the_command_records_and_reports(sheet, instrument):
    an_order(sheet, instrument, tag="tx-live-1")

    call_command(
        "record_exchange_order_id",
        "--order",
        "tx-live-1",
        "--exchange-id",
        "1100000012345678",
        "--actor",
        "operator",
    )

    order = Order.objects.get(tag="tx-live-1")
    assert order.exchange_order_id == "1100000012345678"


def test_the_command_refuses_and_writes_nothing(sheet, instrument):
    an_order(sheet, instrument, tag="tx-paper-1", mode="paper")

    with pytest.raises(CommandError, match="paper"):
        call_command(
            "record_exchange_order_id",
            "--order",
            "tx-paper-1",
            "--exchange-id",
            "1100000012345678",
            "--actor",
            "operator",
        )

    assert not AuditEvent.objects.filter(action="order.exchange_order_id_recorded").exists()
