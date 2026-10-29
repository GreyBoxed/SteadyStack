"""Fixtures shared by the page tests.

The OTP middleware denies by default, so there is exactly one way to produce a
usable session and it lives here. A second way invented in a test file is a
test that passes for the wrong reason.
"""

import pytest
from django.contrib.auth import get_user_model
from django_otp.plugins.otp_totp.models import TOTPDevice

FAKE_PASSWORD = "fake-password-for-tests"


@pytest.fixture
def signed_in(client, db):
    """A client whose session is authenticated *and* OTP-verified."""
    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.login(username="operator", password=FAKE_PASSWORD)

    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()

    return client


_BOUGHT = iter(range(1, 10_000))


def paper_lot(instrument, *, acquired_on, quantity, cost, **fields):
    """A lot bought by a paper fill.

    The tests run in paper mode, and a paper sale consumes only paper lots
    (I6), so the lots a rebalance sells from are paper ones here.
    """
    from datetime import date as date_type
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from engine.models import TradeSheet
    from orders.models import (
        DAILY_BUY_STREAM,
        Fill,
        Order,
        OrderMode,
        OrderSide,
        OrderState,
        OrderType,
    )
    from portfolio.models import Lot

    day = date_type.fromisoformat(acquired_on) if isinstance(acquired_on, str) else acquired_on
    sheet, _ = TradeSheet.objects.get_or_create(trading_date=day, version=1)
    n = next(_BOUGHT)
    order = Order.objects.create(
        tag=f"bought-{n}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=day,
        stream=DAILY_BUY_STREAM,
        tick_no=n,
        side=OrderSide.BUY,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        filled_quantity=quantity,
        limit_price=cost,
        state=OrderState.COMPLETE,
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-bought-{n}",
        quantity=quantity,
        price=cost,
        executed_at=datetime(day.year, day.month, day.day, 10, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    return Lot.objects.create(
        instrument=instrument,
        acquired_on=day,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=cost,
        created_from_fill=fill,
        **fields,
    )


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


@pytest.fixture
def no_broker(monkeypatch):
    """No page may need the network to render.

    Every dashboard reads live prices, and every dashboard has to stay
    readable when the feed is down -- which is exactly when somebody is
    looking at it.
    """
    from broker.exceptions import BrokerError

    def refuse(*_args, **_kwargs):
        raise BrokerError("no session in a test")

    monkeypatch.setattr("engine.quotes.fetch", refuse)
