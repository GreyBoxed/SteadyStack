"""The live regions on Today and the trading board: refreshed only while the
market is open, only from what Postgres holds, and only when switched on."""

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


@pytest.fixture
def broker_forbidden(monkeypatch):
    """A refresh that asks Kite for a quote fails the test outright."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a live fragment called the broker")

    monkeypatch.setattr("engine.quotes.fetch", refuse)


@pytest.fixture
def sheet(instrument):
    from engine.models import TradeSheet, TradeSheetState

    return TradeSheet.objects.create(
        trading_date=DAY,
        version=1,
        trace_id="ts-2026-09-18",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=2,
        spendable_cash=Decimal("5000"),
    )


def tick(sheet, instrument, tick_no, bid, ask, book=None):
    from orders.models import Tick

    return Tick.objects.create(
        tradesheet=sheet,
        trading_date=DAY,
        tick_no=tick_no,
        instrument=instrument,
        best_bid=bid,
        best_ask=ask,
        depth_snapshot=book or {},
    )


def book(bid, ask):
    return {
        "buy": [{"price": float(bid), "quantity": 400, "orders": 7}],
        "sell": [{"price": float(ask), "quantity": 250, "orders": 4}],
    }


def depth_page(client, **params):
    return client.get(reverse("web:instrument_day", args=["2026-09-18", "INFY"]), params)


@pytest.mark.parametrize("page", ["web:home", "web:orders"])
def test_in_session_the_page_carries_a_live_region_and_its_switch(signed_in, no_broker, page):
    with time_machine.travel(at(11, 0), tick=False):
        html = signed_in.get(reverse(page)).content.decode()

    assert "data-live=" in html
    assert 'data-live-every="30"' in html
    assert "data-live-switch" in html


@pytest.mark.parametrize("page", ["web:home", "web:orders"])
def test_out_of_session_there_is_nothing_to_refresh_or_switch(signed_in, no_broker, page):
    with time_machine.travel(at(18, 0), tick=False):
        html = signed_in.get(reverse(page)).content.decode()

    assert "data-live=" not in html
    assert "data-live-switch" not in html


def test_a_past_days_board_is_never_live(signed_in, no_broker):
    with time_machine.travel(at(11, 0), tick=False):
        html = signed_in.get(reverse("web:orders"), {"date": "2026-09-17"}).content.decode()

    assert "data-live=" not in html


def test_the_boards_region_asks_for_the_same_filter_it_shows(signed_in, no_broker):
    with time_machine.travel(at(11, 0), tick=False):
        html = signed_in.get(reverse("web:orders"), {"show": "open"}).content.decode()

    assert f'data-live="{reverse("web:orders_live")}?show=open"' in html


@pytest.mark.parametrize("fragment", ["web:home_live", "web:orders_live"])
def test_a_fragment_is_marked_and_never_calls_the_broker(signed_in, broker_forbidden, fragment):
    with time_machine.travel(at(11, 0), tick=False):
        response = signed_in.get(reverse(fragment))

    assert response.status_code == 200
    assert response["X-Live-Fragment"] == "1"
    assert response["X-Live-Open"] == "1"
    assert response["Cache-Control"] == "no-store"
    # The region's contents only, never a second page inside the first.
    assert b"<html" not in response.content


@pytest.mark.parametrize("fragment", ["web:home_live", "web:orders_live"])
def test_after_the_close_a_fragment_tells_the_page_to_stop(signed_in, broker_forbidden, fragment):
    with time_machine.travel(at(15, 45), tick=False):
        response = signed_in.get(reverse(fragment))

    assert response["X-Live-Open"] == "0"


def test_the_refreshed_board_prices_from_the_latest_round(
    signed_in, broker_forbidden, sheet, instrument
):
    with time_machine.travel(at(10, 0), tick=False):
        tick(sheet, instrument, 30, Decimal("1600.00"), Decimal("1600.20"))
    with time_machine.travel(at(10, 30), tick=False):
        tick(sheet, instrument, 90, Decimal("1650.50"), Decimal("1650.70"))

    with time_machine.travel(at(11, 0), tick=False):
        response = signed_in.get(reverse("web:orders_live"))

    [row] = response.context["instruments"]
    assert row["last_price"] == Decimal("1650.60")
    assert b"not a quote" in response.content


def test_a_round_that_saw_one_side_of_the_book_has_no_mid(sheet, instrument):
    from web.trading import stored_mids

    with time_machine.travel(at(10, 0), tick=False):
        tick(sheet, instrument, 30, Decimal("1600.00"), Decimal("1600.20"))
    with time_machine.travel(at(10, 30), tick=False):
        tick(sheet, instrument, 90, None, Decimal("1650.70"))

    assert stored_mids(DAY) == {"INFY": Decimal("1600.10")}


def test_the_depth_panel_follows_the_rounds_while_unpinned(signed_in, no_broker, sheet, instrument):
    with time_machine.travel(at(11, 0), tick=False):
        html = depth_page(signed_in).content.decode()

    live = reverse("web:instrument_depth_live", args=["2026-09-18", "INFY"])
    assert f'data-live="{live}?tab=orders"' in html
    assert "data-live-switch" in html


def test_a_pinned_round_is_history_and_does_not_move(signed_in, no_broker, sheet, instrument):
    with time_machine.travel(at(10, 0), tick=False):
        tick(sheet, instrument, 30, Decimal("1600.00"), Decimal("1600.20"), book(1600, 1600.2))

    with time_machine.travel(at(11, 0), tick=False):
        html = depth_page(signed_in, tick=30, tab="ticks").content.decode()

    assert "data-live=" not in html
    # The tabs keep the round the reader chose.
    assert "?tab=fills&tick=30" in html


def test_unpinned_the_tabs_do_not_pin_a_round(signed_in, no_broker, sheet, instrument):
    with time_machine.travel(at(10, 0), tick=False):
        tick(sheet, instrument, 30, Decimal("1600.00"), Decimal("1600.20"), book(1600, 1600.2))

    with time_machine.travel(at(11, 0), tick=False):
        html = depth_page(signed_in).content.decode()

    assert '?tab=fills"' in html
    assert "?tab=fills&tick=" not in html


def test_the_depth_fragment_is_the_latest_rounds_book(
    signed_in, broker_forbidden, sheet, instrument
):
    with time_machine.travel(at(10, 0), tick=False):
        tick(sheet, instrument, 30, Decimal("1600.00"), Decimal("1600.20"), book(1600, 1600.2))
    with time_machine.travel(at(10, 30), tick=False):
        tick(sheet, instrument, 90, Decimal("1650.50"), Decimal("1650.60"), book(1650.5, 1650.6))

    with time_machine.travel(at(11, 0), tick=False):
        response = signed_in.get(reverse("web:instrument_depth_live", args=["2026-09-18", "INFY"]))

    assert response["X-Live-Fragment"] == "1"
    assert response["X-Live-Open"] == "1"
    assert response.context["ladder"]["tick"].tick_no == 90
    assert b"ROUND 90" in response.content
    assert b"<html" not in response.content
