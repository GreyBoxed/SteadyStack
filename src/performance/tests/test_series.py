"""The value series and the subscription's cost to the return.

The chart is only worth drawing if its lines mean what the legend says: value
as written that evening, invested as XIRR counts it, and the subscription
added on top in whole billed months.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

START = date(2026, 1, 5)


def a_day(day, *, spent="1000", deposit="1000", lumpsum="0"):
    from engine.models import CashLedger

    return CashLedger.objects.create(
        book="paper",
        trading_date=day,
        daily_allocation=Decimal(deposit),
        extra_allocation=Decimal(lumpsum),
        cash_spent=Decimal(spent),
        charges=Decimal("0"),
        closing_carry=Decimal(deposit) + Decimal(lumpsum) - Decimal(spent),
    )


def an_evening(day, *, value, invested):
    from performance.models import DailyValuation

    return DailyValuation.objects.create(
        trading_date=day, portfolio_value=Decimal(value), invested_to_date=Decimal(invested)
    )


def a_subscription(monthly="500"):
    from configuration.store import update

    update(changed_by="test", reason="feed", market_data_monthly_cost=Decimal(monthly))


def test_months_paid_counts_the_first_day_as_a_month():
    from performance.series import months_paid

    assert months_paid(START, START) == 1
    assert months_paid(START, START + timedelta(days=29)) == 1
    # Billed on day 0, then again once each month has elapsed.
    assert months_paid(START, START + timedelta(days=31)) == 2
    assert months_paid(START, START + timedelta(days=45)) == 2
    assert months_paid(START, START + timedelta(days=61)) == 3
    assert months_paid(START, START - timedelta(days=1)) == 0


def test_the_history_carries_the_subscription_on_top_of_what_was_invested():
    from performance import series

    a_subscription()
    a_day(START)
    an_evening(START, value="1010", invested="1000")
    an_evening(START + timedelta(days=62), value="2100", invested="2000")

    points = series.value_history()

    assert [p.value for p in points] == [Decimal("1010.00"), Decimal("2100.00")]
    assert points[0].with_subscription == Decimal("1500.00")
    assert points[1].with_subscription == Decimal("3500.00")  # three months billed by day 62


def test_the_history_can_start_later():
    from performance import series

    an_evening(START, value="1", invested="1")
    an_evening(START + timedelta(days=10), value="2", invested="2")

    points = series.value_history(start=START + timedelta(days=1))

    assert [p.on for p in points] == [START + timedelta(days=10)]


def test_without_a_subscription_the_net_rate_says_so():
    from performance import series
    from performance.xirr import Flow

    a_subscription("0")
    a_day(START)
    flows = [Flow(on=START, amount=Decimal("-1000")), Flow(on=date(2026, 9, 1), amount=1100)]

    rate, why = series.xirr_net_of_subscription(flows, as_of=date(2026, 9, 1))

    assert rate is None
    assert "No subscription" in why


def test_the_subscription_lowers_the_rate():
    from performance import series
    from performance.xirr import Flow, solve

    a_subscription("10")
    a_day(START)
    as_of = date(2026, 9, 1)
    flows = [Flow(on=START, amount=Decimal("-1000")), Flow(on=as_of, amount=Decimal("1100"))]

    net, _ = series.xirr_net_of_subscription(flows, as_of=as_of)

    assert net is not None
    assert net < solve(flows)
    assert len(series.subscription_flows(as_of)) == series.months_paid(START, as_of)


def test_totals_count_contributions_and_the_days_that_bought():
    from journal.cash import allocate
    from performance import series

    a_subscription("100")
    a_day(START, spent="900", deposit="1000", lumpsum="500")
    a_day(START + timedelta(days=1), spent="0", deposit="1000")
    # The days_run/days_on_record figures still come from CashLedger above;
    # `contributed` now comes from the journal's paper-funding balance.
    allocate(
        Decimal("2500"),
        book="paper",
        on=START,
        actor="t",
        description="test funding",
        source_type="daily",
    )

    totals = series.totals(gain=Decimal("250"), as_of=START + timedelta(days=1))

    assert totals.contributed == Decimal("2500.00")
    assert totals.days_run == 1
    assert totals.days_on_record == 2
    assert totals.subscription == Decimal("100.00")
    assert totals.gain_after_subscription == Decimal("150.00")


def test_drift_is_read_from_each_evenings_holdings():
    from performance import series
    from performance.models import DailyValuation

    DailyValuation.objects.create(
        trading_date=START,
        portfolio_value=Decimal("100"),
        holdings_snapshot={
            "AAA": {"quantity": 3, "price": "25"},
            "BBB": {"quantity": 1, "price": "25"},
        },
    )

    points = series.drift_history({"AAA": Decimal("50"), "BBB": Decimal("50")})

    # 75% and 25% against 50/50 is 25 + 25 away.
    assert points == [{"date": START, "drift": Decimal("50.00")}]
