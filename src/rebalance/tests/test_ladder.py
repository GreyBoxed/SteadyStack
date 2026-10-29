"""The sell ladder on the round model, and the close (M33).

A batch's names each run to an even schedule from the moment it was executed
to the placement close, one resting order at a time, the price starting at
the ask and giving ground toward the bid as a name falls behind -- the buy
ladder's rules with the direction reversed. From the fallback time whatever
is unsold goes out at market. Nothing is ever sold that the day is buying.
"""

from datetime import datetime, time
from decimal import Decimal

import pytest
import time_machine
from django.utils import timezone

from engine.models import TradeSheet, TradeSheetLine, TradeSheetState
from orders.models import NoPlaceReason, Order, OrderSide, OrderType
from orders.state import OrderState
from rebalance import batches
from rebalance import ladder as sell_ladder
from rebalance.models import BatchState
from rebalance.tests.test_batches import TUE, StubBroker, universe  # noqa: F401

pytestmark = pytest.mark.django_db


def at(hour, minute, second=0):
    return timezone.make_aware(
        datetime.combine(TUE, time(hour, minute, second)), timezone.get_current_timezone()
    )


def quote(bid="1650.00", ask="1650.50", last="1650.25"):
    return {
        "depth": {
            "buy": [{"price": float(bid), "quantity": 100}],
            "sell": [{"price": float(ask), "quantity": 100}],
        },
        "last_price": float(last),
    }


QUOTES = {"INFY": quote(), "TCS": quote("3400.00", "3401.00", "3400.50")}


def the_days_sheet(**lines):
    """The day's buy plan: the lines it buys, by symbol and quantity."""
    sheet = TradeSheet.objects.create(
        trading_date=TUE,
        version=(TradeSheet.objects.filter(trading_date=TUE).count() + 1),
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("1000"),
    )
    for rank, (symbol, quantity) in enumerate(lines.items(), start=1):
        TradeSheetLine.objects.create(
            tradesheet=sheet,
            instrument_id=symbol,
            rank=rank,
            quantity=quantity,
            reference_price=Decimal("100"),
        )
    return sheet


def a_running_batch(content=b"symbol,quantity\nINFY,4\nTCS,4\n", *, started=None):
    if not TradeSheet.objects.filter(trading_date=TUE).exists():
        the_days_sheet()
    with time_machine.travel(started or at(9, 45), tick=False):
        batch = batches.upload(content, uploaded_by="operator", trading_date=TUE)
        return batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)


def a_round(batch, moment, *, quotes=QUOTES, room=None, broker=None):
    with time_machine.travel(moment, tick=False):
        return sell_ladder.run_tick(
            batch=batch, quotes=quotes, broker=broker or StubBroker(), moment=moment, room=room
        )


def sells(batch, **filters):
    return Order.objects.filter(stream=batch.stream, side=OrderSide.SELL, **filters)


# -- pacing ----------------------------------------------------------------


def test_the_first_round_places_one_slice_of_each_name_at_the_ask(universe):  # noqa: F811
    batch = a_running_batch()

    outcome = a_round(batch, at(9, 45))

    assert sorted(outcome.placed) == ["INFY", "TCS"]
    infy = sells(batch).get(instrument_id="INFY")
    assert (infy.quantity, infy.limit_price, infy.order_type) == (1, Decimal("1650.50"), "LIMIT")
    assert infy.stream == batch.stream


def test_nothing_is_placed_for_a_name_while_its_sell_rests(universe):  # noqa: F811
    batch = a_running_batch()
    a_round(batch, at(9, 45))

    outcome = a_round(batch, at(9, 45, 30))

    assert outcome.skipped == {"INFY": NoPlaceReason.RESTING, "TCS": NoPlaceReason.RESTING}
    assert sells(batch).count() == 2


def test_a_name_that_has_filled_places_the_next_slice_when_the_schedule_owes_it(universe):  # noqa: F811
    batch = a_running_batch(b"symbol,quantity\nINFY,4\n")
    a_round(batch, at(9, 45))
    sells(batch).update(state=OrderState.COMPLETE, filled_quantity=1)

    early = a_round(batch, at(9, 50))
    later = a_round(batch, at(11, 0))

    assert early.skipped == {"INFY": NoPlaceReason.ON_SCHEDULE}
    assert later.placed == ["INFY"]


def test_a_name_behind_its_schedule_gives_ground_toward_the_bid(universe):  # noqa: F811
    batch = a_running_batch(b"symbol,quantity\nINFY,4\n")

    a_round(batch, at(12, 0))  # executed at 09:45, nothing sold: well behind

    price = sells(batch).get().limit_price
    assert Decimal("1650.00") <= price < Decimal("1650.50")


def test_a_sell_resting_past_its_time_is_cancelled_and_replaced_next_round(universe):  # noqa: F811
    batch = a_running_batch(b"symbol,quantity\nINFY,4\n")
    a_round(batch, at(9, 45))

    stale = a_round(batch, at(9, 51))
    fresh = a_round(batch, at(9, 51, 30))

    assert stale.skipped == {"INFY": NoPlaceReason.RESTING}
    assert sells(batch, state=OrderState.CANCELLED).count() == 1
    assert fresh.placed == ["INFY"]


def test_a_round_places_no_more_than_the_room_it_is_given(universe):  # noqa: F811
    batch = a_running_batch()

    outcome = a_round(batch, at(9, 45), room=1)

    assert len(outcome.placed) == 1
    assert list(outcome.skipped.values()) == [NoPlaceReason.WAITING_TURN]


def test_nothing_is_placed_outside_the_placement_window(universe):  # noqa: F811
    batch = a_running_batch()

    assert a_round(batch, at(14, 30)).placed == []
    assert not sells(batch).exists()


# -- never selling what the day is buying (D-S2) ---------------------------


def test_a_name_on_the_days_buy_sheet_is_never_sold(universe):  # noqa: F811
    batch = a_running_batch()
    the_days_sheet(INFY=3)  # a later version decides to buy INFY

    outcome = a_round(batch, at(9, 45))

    assert outcome.placed == ["TCS"]
    assert "INFY" in outcome.skipped
    assert not sells(batch, instrument_id="INFY").exists()


def test_a_name_an_earlier_version_bought_is_not_sold(universe):  # noqa: F811
    """v1 bought INFY this morning and was stopped; v2 plans other names.
    INFY is still bought today, and CNC would net a sale against it."""
    batch = a_running_batch()
    morning = the_days_sheet(INFY=3)
    TradeSheet.objects.filter(pk=morning.pk).update(state=TradeSheetState.HALTED)
    the_days_sheet(TCS=0)

    outcome = a_round(batch, at(9, 45))

    assert "INFY" in outcome.skipped


def test_a_skipped_plan_is_not_one_that_buys(universe):  # noqa: F811
    """A skipped version bought nothing, whatever its lines say -- the same
    rule executing a batch checks, so a batch accepted can also sell."""
    batch = a_running_batch()
    skipped = the_days_sheet(INFY=3)
    TradeSheet.objects.filter(pk=skipped.pk).update(state=TradeSheetState.SKIPPED)

    outcome = a_round(batch, at(9, 45))

    assert "INFY" in outcome.placed


def test_a_sell_resting_on_an_earlier_version_is_still_seen(universe):  # noqa: F811
    """After a replan the day's latest sheet is v2; the sale's order still
    hangs off v1 and is found by its stream."""
    batch = a_running_batch(b"symbol,quantity\nINFY,4\n")
    a_round(batch, at(9, 45))
    the_days_sheet()

    assert a_round(batch, at(9, 45, 30)).skipped == {"INFY": NoPlaceReason.RESTING}


# -- what was sold, and closing a batch ------------------------------------


def a_fill(order, quantity):
    from orders.models import Fill

    Order.objects.filter(pk=order.pk).update(
        filled_quantity=quantity,
        state=OrderState.COMPLETE if quantity >= order.quantity else OrderState.PARTIAL,
    )
    return Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-{order.tag}",
        quantity=quantity,
        price=order.limit_price or Decimal("1650"),
        charges=Decimal("1.00"),
        executed_at=timezone.now(),
    )


def test_sold_by_row_counts_the_fills(universe):  # noqa: F811
    batch = a_running_batch()
    a_round(batch, at(9, 45))
    a_fill(sells(batch).get(instrument_id="INFY"), 1)

    assert sell_ladder.sold_by_row(batch) == {"INFY": 1, "TCS": 0}


@pytest.mark.parametrize(
    ("sold", "state"),
    [
        ({"INFY": 4, "TCS": 4}, BatchState.COMPLETE),
        ({"INFY": 1}, BatchState.PARTIAL),
        ({}, BatchState.CANCELLED),
    ],
)
def test_closing_a_batch_takes_its_state_from_what_filled(universe, sold, state):  # noqa: F811
    batch = a_running_batch()
    for tick, (symbol, quantity) in enumerate(sold.items()):
        order = Order.objects.create(
            tag=f"tx-sold-{symbol}",
            tradesheet=TradeSheet.objects.get(trading_date=TUE),
            instrument_id=symbol,
            trading_date=TUE,
            stream=batch.stream,
            tick_no=tick,
            side=OrderSide.SELL,
            mode="paper",
            order_type=OrderType.LIMIT,
            quantity=quantity,
            limit_price=Decimal("1650"),
            state=OrderState.OPEN,
        )
        a_fill(order, quantity)

    closed = sell_ladder.close_batch(batch, reason="day ended")

    assert closed.state == state
    assert closed.finished_at is not None
    rows = {row.instrument_id: row.sold_quantity for row in closed.rows.all()}
    assert rows == {"INFY": sold.get("INFY", 0), "TCS": sold.get("TCS", 0)}


# -- the close -------------------------------------------------------------


def a_close(batch, moment, *, room=None):
    with time_machine.travel(moment, tick=False):
        return sell_ladder.close_round(
            batch=batch, quotes=QUOTES, broker=StubBroker(), moment=moment, room=room
        )


def test_the_close_cancels_a_resting_sell_and_sells_the_rest_at_market(universe):  # noqa: F811
    batch = a_running_batch(b"symbol,quantity\nINFY,4\n")
    a_round(batch, at(9, 45))
    a_fill(sells(batch).get(), 1)
    a_round(batch, at(11, 0))  # a second slice, resting

    outcome = a_close(batch, at(15, 0))

    assert outcome.placed == ["INFY"]
    market = sells(batch, order_type=OrderType.MARKET).get()
    assert (market.quantity, market.is_fallback) == (3, True)
    assert (
        not sells(batch, order_type=OrderType.LIMIT)
        .exclude(state__in=[OrderState.COMPLETE, OrderState.CANCELLED])
        .exists()
    )


def test_the_close_sells_each_remainder_once(universe):  # noqa: F811
    batch = a_running_batch(b"symbol,quantity\nINFY,4\n")
    a_close(batch, at(15, 0))

    again = a_close(batch, at(15, 0, 30))

    assert again.placed == []
    assert sells(batch, order_type=OrderType.MARKET).count() == 1


def test_the_close_never_sells_more_than_the_row(universe):  # noqa: F811
    batch = a_running_batch(b"symbol,quantity\nINFY,4\n")
    a_round(batch, at(9, 45))
    a_fill(sells(batch).get(), 1)

    a_close(batch, at(15, 0))

    assert (
        sum(sells(batch).exclude(state=OrderState.CANCELLED).values_list("quantity", flat=True))
        == 4
    )


def test_the_close_respects_the_room_it_is_given(universe):  # noqa: F811
    batch = a_running_batch()

    outcome = a_close(batch, at(15, 0), room=1)

    assert len(outcome.placed) == 1
    assert list(outcome.skipped.values()) == [NoPlaceReason.WAITING_TURN]
