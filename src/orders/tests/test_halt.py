"""Stopping everything, now.

The emergency action. Pausing on its own stops the system *deciding* to buy
and leaves whatever it already decided resting at the broker, where DAY
validity keeps it live until the close -- so the thing that spends money
carries on for hours after the operator has said stop. This is the one action
that means both.

What is tested here is mostly what happens when it goes wrong: a broker that
refuses one cancellation must not leave the other orders unexamined, and the
operator must be told exactly which orders are still live rather than being
told it worked.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from audit.models import AuditEvent
from broker.exceptions import TransientBrokerError
from configuration import pause as pause_module
from engine.models import TradeSheet, TradeSheetState
from orders import halt
from orders.models import Order, OrderSide, OrderType
from orders.state import OrderState
from portfolio.instruments import refresh
from portfolio.models import Instrument

pytestmark = pytest.mark.django_db

TUESDAY = date(2026, 9, 22)


@pytest.fixture
def instrument(db):
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
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=6,
        spendable_cash=Decimal("12000"),
        reference_price=Decimal("1650.50"),
    )


class StubBroker:
    """A broker that can be told to refuse particular cancellations."""

    def __init__(self, *, refuses=()):
        self.refuses = set(refuses)
        self.cancelled = []

    def cancel_order(self, order_id):
        if order_id in self.refuses:
            raise TransientBrokerError(f"{order_id} could not be cancelled")
        self.cancelled.append(order_id)
        return order_id


def an_order(sheet, instrument, *, tick_no, state=OrderState.OPEN, stream="daily_buy", side=None):
    return Order.objects.create(
        tag=f"tx-halt-{stream}-{tick_no}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        stream=stream,
        tick_no=tick_no,
        side=side or OrderSide.BUY,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=Decimal("1650.00"),
        state=state,
        kite_order_id=f"kite-{stream}-{tick_no}",
    )


# -- a cancel never overwrites a fill --------------------------------------


def test_a_fill_that_lands_after_the_order_was_read_is_not_overwritten_by_the_cancel(
    sheet, instrument
):
    """The paper filler runs in the tick-feed process and commits COMPLETE on
    its own. A cancel holding a copy read a moment earlier must not write
    CANCELLED over it: the shares were bought."""
    from orders import placement

    stale = an_order(sheet, instrument, tick_no=1)
    Order.objects.filter(pk=stale.pk).update(state=OrderState.COMPLETE, filled_quantity=1)

    placement.cancel(stale, broker=StubBroker())

    assert Order.objects.get(pk=stale.pk).state == OrderState.COMPLETE
    assert not AuditEvent.objects.filter(action="order.cancelled").exists()


# -- it stops both things --------------------------------------------------


def test_stopping_cancels_every_open_order(sheet, instrument):
    first = an_order(sheet, instrument, tick_no=0)
    second = an_order(sheet, instrument, tick_no=1)
    broker = StubBroker()

    result = halt.stop_trading(actor="operator", reason="stepping away", broker=broker)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.state == OrderState.CANCELLED
    assert second.state == OrderState.CANCELLED
    assert sorted(result.cancelled) == sorted([first.tag, second.tag])


def test_stopping_pauses_the_system(sheet, instrument):
    """Cancelling without pausing would have the next tick place again five
    minutes later."""
    an_order(sheet, instrument, tick_no=0)

    halt.stop_trading(actor="operator", reason="stepping away", broker=StubBroker())

    assert pause_module.is_paused() is True


def test_stopping_reaches_every_stream(sheet, instrument):
    """A rebalance batch places into its own stream, and a stop that only
    swept the daily buy would leave a sale running."""
    buy = an_order(sheet, instrument, tick_no=0)
    sell = an_order(sheet, instrument, tick_no=0, stream="rebalance-1", side=OrderSide.SELL)
    broker = StubBroker()

    result = halt.stop_trading(actor="operator", reason="stop", broker=broker)

    buy.refresh_from_db()
    sell.refresh_from_db()
    assert buy.state == OrderState.CANCELLED
    assert sell.state == OrderState.CANCELLED
    assert len(result.cancelled) == 2


def test_an_order_that_already_finished_is_left_alone(sheet, instrument):
    """A fill is a fact. Stopping never unwinds one."""
    done = an_order(sheet, instrument, tick_no=0, state=OrderState.COMPLETE)
    done.filled_quantity = 1
    done.save(update_fields=["filled_quantity"])

    result = halt.stop_trading(actor="operator", reason="stop", broker=StubBroker())

    done.refresh_from_db()
    assert done.state == OrderState.COMPLETE
    assert done.filled_quantity == 1
    assert result.cancelled == []


# -- when the broker will not co-operate -----------------------------------


def test_one_refused_cancellation_does_not_abandon_the_rest(sheet, instrument):
    """The whole point of an emergency stop: it tries every order.

    Stopping at the first failure would leave orders resting that nobody has
    even looked at, which is the opposite of what was asked for.
    """
    stubborn = an_order(sheet, instrument, tick_no=0)
    other = an_order(sheet, instrument, tick_no=1)
    broker = StubBroker(refuses=[stubborn.kite_order_id])

    result = halt.stop_trading(actor="operator", reason="stop", broker=broker)

    other.refresh_from_db()
    stubborn.refresh_from_db()
    assert other.state == OrderState.CANCELLED
    assert stubborn.state == OrderState.OPEN
    assert result.unconfirmed == [stubborn.tag]


def test_a_refused_cancellation_makes_the_result_not_clean(sheet, instrument):
    """The operator has to be told an order is still live, not told it worked."""
    stubborn = an_order(sheet, instrument, tick_no=0)
    broker = StubBroker(refuses=[stubborn.kite_order_id])

    result = halt.stop_trading(actor="operator", reason="stop", broker=broker)

    assert result.clean is False
    assert "still" in result.summary


def test_the_system_is_paused_even_when_a_cancellation_fails(sheet, instrument):
    """Pausing is the half that always works, and it is what stops the next
    tick placing more."""
    stubborn = an_order(sheet, instrument, tick_no=0)
    broker = StubBroker(refuses=[stubborn.kite_order_id])

    halt.stop_trading(actor="operator", reason="stop", broker=broker)

    assert pause_module.is_paused() is True


# -- the record ------------------------------------------------------------


def test_stopping_is_audited(sheet, instrument):
    an_order(sheet, instrument, tick_no=0)

    halt.stop_trading(actor="operator", reason="a circuit move", broker=StubBroker())

    event = AuditEvent.objects.filter(action="system.trading_stopped").first()
    assert event is not None
    assert event.actor == "operator"
    assert event.after["reason"] == "a circuit move"


def test_stopping_an_already_paused_system_still_cancels(sheet, instrument):
    """The two halves are independent: somebody who paused yesterday and finds
    an order resting today still needs it cancelled."""
    pause_module.pause(actor="operator", reason="away")
    order = an_order(sheet, instrument, tick_no=0)

    result = halt.stop_trading(actor="operator", reason="and cancel that too", broker=StubBroker())

    order.refresh_from_db()
    assert order.state == OrderState.CANCELLED
    assert result.cancelled == [order.tag]


def test_a_running_batch_is_closed_rather_than_left_running(sheet, instrument):
    from rebalance.models import BatchState, RebalanceSell

    batch = RebalanceSell.objects.create(
        uploaded_by="operator",
        state=BatchState.RUNNING,
        trading_date=TUESDAY,
        stream="rebalance-1",
    )
    an_order(sheet, instrument, tick_no=0, stream="rebalance-1", side=OrderSide.SELL)

    halt.stop_trading(actor="operator", reason="stop", broker=StubBroker())

    batch.refresh_from_db()
    assert batch.state != BatchState.RUNNING
    assert "stop" in batch.cancelled_reason


def test_stopping_with_nothing_running_is_not_an_error():
    """The operator pressing it twice, or pressing it on a quiet Sunday."""
    result = halt.stop_trading(actor="operator", reason="just in case", broker=StubBroker())

    assert result.clean is True
    assert result.cancelled == []
    assert pause_module.is_paused() is True


def test_the_summary_says_what_happened(sheet, instrument):
    an_order(sheet, instrument, tick_no=0)
    an_order(sheet, instrument, tick_no=1)

    result = halt.stop_trading(actor="operator", reason="stop", broker=StubBroker())

    assert "2" in result.summary
    assert result.clean is True


def test_resuming_is_a_separate_deliberate_act(sheet, instrument):
    """Nothing here un-stops. Coming back is its own audited decision, which
    is the whole reason stopping is safe to press."""
    halt.stop_trading(actor="operator", reason="stop", broker=StubBroker())
    assert pause_module.is_paused() is True

    pause_module.resume(actor="operator", note="back")

    assert pause_module.is_paused() is False


def test_the_trade_date_is_not_required_to_have_a_sheet(instrument):
    """A stop must work on a day the engine never planned -- which is exactly
    the day somebody is most likely to press it."""
    result = halt.stop_trading(actor="operator", reason="stop", broker=StubBroker())

    assert result.clean is True


def test_a_paper_order_is_cancelled_too(sheet, instrument):
    """Paper runs the same state machine, and a paper order left open would
    keep filling against the live feed."""
    order = an_order(sheet, instrument, tick_no=0)
    assert order.is_paper

    halt.stop_trading(actor="operator", reason="stop", broker=StubBroker())

    order.refresh_from_db()
    assert order.state == OrderState.CANCELLED


def test_the_timestamp_of_the_stop_is_recorded(sheet, instrument):
    an_order(sheet, instrument, tick_no=0)
    before = timezone.now()

    result = halt.stop_trading(actor="operator", reason="stop", broker=StubBroker())

    assert result.stopped_at >= before
