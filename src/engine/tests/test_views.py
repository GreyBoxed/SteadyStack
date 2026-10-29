"""The tradesheet screens.

A tradesheet answers "why that one", so the page shows the whole ranking --
the row above the chosen one is the answer.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from engine.selection import Quote
from engine.tradesheet import generate
from model_portfolio.uploads import store
from portfolio.instruments import refresh

FAKE_PASSWORD = "fake-password-for-tests"
TUESDAY = date(2026, 9, 22)


def master(symbol, token):
    return {
        "tradingsymbol": symbol,
        "instrument_token": token,
        "name": symbol,
        "exchange": "NSE",
        "instrument_type": "EQ",
        "segment": "NSE",
        "tick_size": 0.05,
        "lot_size": 1,
    }


@pytest.fixture
def operator(client, db):
    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.login(username="operator", password=FAKE_PASSWORD)
    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()
    return user


@pytest.fixture
def sheet(operator):
    refresh([master("AAA", 1), master("BBB", 2)])
    store("symbol,target_pct\nAAA,40\nBBB,60\n", uploaded_by="operator")
    return generate(
        trading_date=TUESDAY,
        quotes={
            "AAA": Quote("AAA", Decimal("100"), Decimal("100")),
            "BBB": Quote("BBB", Decimal("100"), Decimal("100")),
        },
    )


@pytest.mark.django_db
def test_the_list_needs_a_verified_session(client):
    assert client.get(reverse("engine:index")).status_code == 302


@pytest.mark.django_db
def test_the_list_renders_when_empty(client, operator):
    response = client.get(reverse("engine:index"))
    assert response.status_code == 200
    assert b"No tradesheet" in response.content


@pytest.mark.django_db
def test_the_list_shows_a_generated_day(client, sheet):
    response = client.get(reverse("engine:index"))
    assert b"2026-09-22" in response.content


@pytest.mark.django_db
def test_the_detail_page_shows_the_whole_ranking(client, sheet):
    response = client.get(reverse("engine:detail", args=["2026-09-22", 1]))

    assert response.status_code == 200
    assert b"AAA" in response.content
    assert b"BBB" in response.content


@pytest.mark.django_db
def test_the_detail_page_names_what_is_bought(client, sheet):
    response = client.get(reverse("engine:detail", args=["2026-09-22", 1]))
    assert b"Buy" in response.content


@pytest.mark.django_db
def test_the_detail_page_counts_the_days_buys_not_a_sale_on_its_sheet(client, sheet):
    """A rebalance batch's sells hang off the day's sheet; they are not what
    the day bought, and must not swell its filled, gross or average."""
    from django.utils import timezone

    from orders.models import Fill, Order, OrderSide, OrderType

    for stream, side, quantity, price in (
        ("daily_buy", OrderSide.BUY, 2, "100"),
        ("rebalance-abc12345", OrderSide.SELL, 3, "3000"),
    ):
        order = Order.objects.create(
            tag=f"tx-{stream[:8]}-{side}",
            tradesheet=sheet,
            instrument_id="AAA",
            trading_date=TUESDAY,
            stream=stream,
            tick_no=1,
            side=side,
            mode="paper",
            order_type=OrderType.LIMIT,
            quantity=quantity,
            filled_quantity=quantity,
            limit_price=Decimal(price),
            state="COMPLETE",
        )
        Fill.objects.create(
            order=order,
            kite_trade_id=f"paper-{order.tag}",
            quantity=quantity,
            price=Decimal(price),
            executed_at=timezone.now(),
        )

    context = client.get(reverse("engine:detail", args=["2026-09-22", 1])).context

    assert context["filled"] == 2
    assert context["gross"] == Decimal("200")


@pytest.mark.django_db
def test_an_unknown_version_is_a_404(client, sheet):
    assert client.get(reverse("engine:detail", args=["2026-09-22", 99])).status_code == 404
