"""The pages, on a day that was stopped and replanned.

Every figure that describes the *day* -- what it bought, what it spent, what
is left, whether it was skipped -- must count every version that bought, not
just the latest one. A page reading the latest version alone would show the
morning's buying as never having happened, and one subtracting the morning
from a replanned version's budget would count it twice.
"""

from datetime import date
from decimal import Decimal

import pytest
import time_machine
from django.utils import timezone

from engine.models import CashLedger, SkipReason, TradeSheet, TradeSheetState
from journal.trades import post_fill
from orders.models import Fill, Order, OrderType
from orders.state import OrderState
from portfolio import lots
from portfolio.instruments import refresh

TUESDAY = date(2026, 9, 22)
WEDNESDAY = date(2026, 9, 23)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def half_past_eleven():
    with time_machine.travel("2026-09-23 11:30:00 +05:30", tick=False):
        yield


def sheet(version, *, state, spendable):
    return TradeSheet.objects.create(
        trading_date=WEDNESDAY,
        version=version,
        trace_id="ts-2026-09-23",
        state=state,
        skip_reason=SkipReason.OPERATOR if state == TradeSheetState.HALTED else "",
        instrument_id="INFY",
        quantity=4,
        spendable_cash=Decimal(spendable),
        reference_price=Decimal("1000.00"),
        authorized_at=timezone.now(),
        authorized_by="operator",
    )


def bought(on, quantity, price, *, tick_no, charges="3.00"):
    order = Order.objects.create(
        tag=f"tx-day-{on.version}-{tick_no}",
        tradesheet=on,
        instrument_id="INFY",
        trading_date=WEDNESDAY,
        tick_no=tick_no,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=quantity,
        filled_quantity=quantity,
        limit_price=Decimal(price),
        state=OrderState.COMPLETE,
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-{order.tag}",
        quantity=quantity,
        price=Decimal(price),
        charges=Decimal(charges),
        executed_at=timezone.now(),
    )
    lots.from_fill(fill)
    post_fill(fill)


@pytest.fixture
def replanned_day(db):
    """10,000 to spend. v1 bought 2 at 1,500 and was stopped; v2 was planned
    from the 6,997 left and has bought 2 at 1,000."""
    return a_replanned_day()


@pytest.fixture
def replanned_day_without_charges(db):
    """The same day, its paper fills carrying no charges of their own."""
    return a_replanned_day(charges="0")


def a_replanned_day(charges="3.00"):
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
    # Tuesday left 9,000 in the engine account; Wednesday's 1,000 makes 10,000.
    from engine.ledger import settle_day
    from journal.cash import allocate

    allocate(
        Decimal("9000"),
        book="paper",
        on=TUESDAY,
        actor="test",
        description="what Tuesday left",
        source_type="test",
    )
    settle_day(TUESDAY, book="paper")
    v1 = sheet(1, state=TradeSheetState.HALTED, spendable="10000.00")
    bought(v1, 2, "1500.00", tick_no=0, charges=charges)
    v2 = sheet(2, state=TradeSheetState.RUNNING, spendable="6997.00")
    # What engine.replan records on the version it writes.
    v2.ranking_snapshot = {
        "replan": {"supersedes_version": 1, "spent_before": "3000.00", "charges_before": "3.00"}
    }
    v2.save(update_fields=["ranking_snapshot"])
    bought(v2, 2, "1000.00", tick_no=40, charges=charges)
    return v1, v2


def test_the_sheet_that_decides_the_days_buy_is_the_latest_that_bought(replanned_day):
    from web.trading import buy_sheet

    assert buy_sheet(WEDNESDAY) == replanned_day[1]


def test_todays_cash_line_counts_the_morning_once(replanned_day):
    from web.deck import running_totals

    totals = running_totals(WEDNESDAY)

    assert totals["available"] == Decimal("10000.00")
    assert totals["deployed"] == Decimal("5006.00")
    assert totals["projected_carry"] == Decimal("4994.00")


def test_todays_cash_line_counts_charges_as_settlement_will(replanned_day_without_charges):
    """A paper fill carries no charges of its own; the journal posts them from
    the schedule, and Today must say what the ledger will."""
    from engine.settlement import day_spend
    from web.deck import running_totals

    spend = day_spend(WEDNESDAY)

    totals = running_totals(WEDNESDAY)

    assert spend.charges > 0
    assert totals["deployed"] == spend.spent + spend.charges
    assert totals["projected_carry"] == spend.remaining


def test_a_replanned_version_is_the_days_buy_before_it_has_placed(replanned_day):
    """Authorised at 13:00, nothing placed yet: Today shows the plan that is
    about to run, not the stopped one."""
    from web.trading import buy_sheet

    v3 = sheet(3, state=TradeSheetState.RUNNING, spendable="4994.00")
    v3.ranking_snapshot = {"replan": {"supersedes_version": 2}}
    v3.save(update_fields=["ranking_snapshot"])

    assert buy_sheet(WEDNESDAY) == v3


def test_the_phones_today_counts_every_version(replanned_day):
    from web.phone import _today_state

    state = _today_state(WEDNESDAY)

    assert state["filled"] == 4
    assert state["spent"] == Decimal("5000.00")


def test_the_phones_stop_screen_says_what_the_day_has_left(replanned_day, signed_in):
    from django.urls import reverse

    from web.tests.test_phone import IPHONE

    response = signed_in.get(reverse("phone:stop"), HTTP_USER_AGENT=IPHONE)

    assert response.status_code == 200, response.get("Location")
    assert response.context["filled"] == 4
    assert response.context["unspent"] == Decimal("4994.00")


def test_a_replanned_day_is_not_listed_as_skipped(replanned_day):
    from web.deck import cash_exceptions

    CashLedger.objects.create(
        book="paper",
        trading_date=WEDNESDAY,
        daily_allocation=Decimal("1000"),
        cash_spent=Decimal("5000"),
    )

    assert WEDNESDAY not in {row["date"] for row in cash_exceptions()}


def test_a_day_that_was_skipped_is_still_listed(db):
    from web.deck import cash_exceptions

    TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.SKIPPED,
        skip_reason=SkipReason.NO_AFFORDABLE_INSTRUMENT,
    )
    CashLedger.objects.create(
        book="paper",
        trading_date=TUESDAY,
        daily_allocation=Decimal("1000"),
        closing_carry=Decimal("1000"),
    )

    assert TUESDAY in {row["date"] for row in cash_exceptions()}
