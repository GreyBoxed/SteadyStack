"""The Today page's additions: where the ladder is, the cash line under what
is running, and which notices are warnings rather than news."""

from datetime import date, datetime, time
from decimal import Decimal

import pytest
import time_machine
from django.urls import reverse
from django.utils import timezone

pytestmark = pytest.mark.django_db

DAY = date(2026, 9, 18)


def at(hour, minute):
    return timezone.make_aware(datetime.combine(DAY, time(hour, minute)))


def test_the_day_says_which_round_the_clock_is_on():
    from configuration.store import current
    from web.deck import _ladder_position

    with time_machine.travel(at(10, 7), tick=False):
        note = _ladder_position(DAY, current())

    assert note.startswith(" · round 44 of 509")
    assert note.endswith("next 10:07:30")


def test_before_the_first_round_it_says_when_that_is():
    from configuration.store import current
    from web.deck import _ladder_position

    with time_machine.travel(at(9, 0), tick=False):
        assert _ladder_position(DAY, current()) == " · first round 09:45"


def test_the_cash_line_counts_fills_and_resting_orders(instrument):
    from engine.models import TradeSheet, TradeSheetState
    from orders.models import Fill, Order, OrderMode, OrderState, OrderType
    from web.deck import running_totals

    sheet = TradeSheet.objects.create(
        trading_date=DAY,
        version=1,
        trace_id="ts-2026-09-18",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=2,
        spendable_cash=Decimal("5000"),
    )
    filled, _resting = (
        Order.objects.create(
            tag=f"t{n}",
            tradesheet=sheet,
            instrument=instrument,
            trading_date=DAY,
            tick_no=n,
            mode=OrderMode.PAPER,
            order_type=OrderType.LIMIT,
            quantity=1,
            limit_price=Decimal("2000"),
            state=state,
        )
        for n, state in ((0, OrderState.COMPLETE), (1, OrderState.OPEN))
    )
    Fill.objects.create(
        order=filled,
        kite_trade_id="p1",
        quantity=1,
        price=Decimal("1990"),
        charges=Decimal("2"),
        executed_at=at(10, 0),
    )

    totals = running_totals(DAY)

    assert totals["deployed"] == Decimal("1992")
    assert totals["committed"] == Decimal("2000")
    assert totals["projected_carry"] == Decimal("1008")


def test_a_rejected_order_is_a_warning_and_a_lumpsum_is_not(signed_in, no_broker):
    from audit.events import record

    record("order.rejected", actor="system", after={"reason": "insufficient funds"})
    record("ledger.lumpsum_credited", actor="op", after={"amount": "100"})

    response = signed_in.get(reverse("web:home"))
    levels = {n["action"]: n["level"] for n in response.context["notices"]}

    assert levels == {"order.rejected": "warning", "ledger.lumpsum_credited": "notice"}
    assert response.context["attention"] == 1
    assert b"1 needs attention" in response.content
