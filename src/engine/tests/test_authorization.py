"""M15f: a plan is made automatically, placing it is not.

Nothing is placed for the daily buy -- no ladder slice, no 15:00 market
order -- until a person authorizes that version of the day.
"""

from datetime import date, datetime, time
from decimal import Decimal

import pytest
import time_machine
from django.urls import reverse
from django.utils import timezone

from engine.models import SkipReason, TradeSheet, TradeSheetLine, TradeSheetState
from orders import fallback, ladder
from orders.models import NoPlaceReason, Order
from orders.tests.test_fallback import StubBroker
from orders.tests.test_ladder import RecordingBroker, master, quote
from portfolio.instruments import refresh

pytestmark = pytest.mark.django_db

DAY = date(2026, 9, 23)


@pytest.fixture(autouse=True)
def morning():
    with time_machine.travel("2026-09-23 10:30:00 +05:30", tick=True):
        yield


@pytest.fixture
def sheet(db):
    refresh([master("INFY", 1)])
    sheet = TradeSheet.objects.create(
        trading_date=DAY,
        version=1,
        trace_id="ts-2026-09-23",
        state=TradeSheetState.DRAFT,
        instrument_id="INFY",
        quantity=2,
        spendable_cash=Decimal("5000"),
        reference_price=Decimal("1650.50"),
    )
    TradeSheetLine.objects.create(
        tradesheet=sheet,
        instrument_id="INFY",
        rank=1,
        quantity=2,
        reference_price=Decimal("1650.50"),
    )
    return sheet


def test_a_new_sheet_is_not_authorized(sheet):
    assert sheet.is_authorized is False


def test_the_ladder_places_nothing_until_authorized(sheet):
    moment = timezone.make_aware(datetime.combine(DAY, time(10, 30)))

    [outcome] = ladder.run_round(
        tradesheet=sheet,
        quotes={"INFY": quote()},
        broker=RecordingBroker(),
        moment=moment,
    )

    assert outcome.placed is False
    assert outcome.tick.no_place_reason == NoPlaceReason.NOT_AUTHORIZED
    assert Order.objects.count() == 0


def test_once_authorized_the_ladder_places(sheet):
    from engine.authorization import authorize

    authorize(sheet, actor="op")
    moment = timezone.make_aware(datetime.combine(DAY, time(10, 30)))

    [outcome] = ladder.run_round(
        tradesheet=TradeSheet.objects.get(pk=sheet.pk),
        quotes={"INFY": quote()},
        broker=RecordingBroker(),
        moment=moment,
    )

    assert outcome.placed is True


def test_an_unauthorized_day_buys_nothing_at_market_and_ends_skipped(sheet):
    outcome = fallback.run(
        tradesheet=sheet, broker=StubBroker(), last_prices={"INFY": Decimal("1651")}
    )

    sheet.refresh_from_db()
    assert outcome.placed is False
    assert Order.objects.count() == 0
    assert sheet.state == TradeSheetState.SKIPPED
    assert sheet.skip_reason == SkipReason.NOT_AUTHORIZED


def test_authorizing_is_audited_and_happens_once(sheet):
    from audit.models import AuditEvent
    from engine.authorization import AuthorizationRefused, authorize

    authorize(sheet, actor="op")

    event = AuditEvent.objects.get(action="tradesheet.authorized")
    assert event.actor == "op"
    assert event.after["names"] == {"INFY": 2}
    with pytest.raises(AuthorizationRefused, match="was authorized by op"):
        authorize(TradeSheet.objects.get(pk=sheet.pk), actor="op")


def test_an_older_version_cannot_be_authorized(sheet):
    from engine.authorization import AuthorizationRefused, authorize

    TradeSheet.objects.create(
        trading_date=DAY, version=2, trace_id="ts-2026-09-23", quantity=1, instrument_id="INFY"
    )

    with pytest.raises(AuthorizationRefused, match="later version"):
        authorize(sheet, actor="op")


def test_today_shows_the_authorize_button_and_it_works(signed_in, sheet):
    home = signed_in.get(reverse("web:home"))
    assert b"NOT AUTHORIZED" in home.content
    assert b"Authorize v1 to trade" in home.content

    signed_in.post(reverse("engine:authorize", args=[DAY.isoformat(), 1]))

    sheet.refresh_from_db()
    assert sheet.is_authorized
    assert sheet.authorized_by == "operator"
    assert b"AUTHORIZED TO TRADE" in signed_in.get(reverse("web:home")).content


def test_the_command_authorizes_todays_sheet(sheet):
    from django.core.management import call_command

    call_command("authorize_tradesheet", "--actor", "op")

    sheet.refresh_from_db()
    assert sheet.authorized_by == "op"


@pytest.mark.parametrize(
    "next_url", ["https://evil.example/", "//evil.example/", "javascript:alert(1)"]
)
def test_authorizing_never_redirects_off_the_site(signed_in, sheet, next_url):
    response = signed_in.post(
        reverse("engine:authorize", args=[DAY.isoformat(), 1]), {"next": next_url}
    )

    assert response["Location"] == reverse("web:home")


def test_authorizing_returns_to_the_page_it_came_from(signed_in, sheet):
    back = reverse("engine:detail", args=[DAY.isoformat(), 1])

    response = signed_in.post(
        reverse("engine:authorize", args=[DAY.isoformat(), 1]), {"next": back}
    )

    assert response["Location"] == back
