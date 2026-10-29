"""The phone: the device switch, and the five screens it leads to.

Every decision here is the engine's -- these assert the phone reaches the same
rules the desk does, lands back on the phone afterwards, and cannot get round
one of them.
"""

from datetime import date, datetime, time
from decimal import Decimal

import pytest
import time_machine
from django.urls import reverse
from django.utils import timezone

pytestmark = pytest.mark.django_db

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
IPAD = "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Safari"
MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Version/18.0 Safari"

TUESDAY = date(2026, 9, 22)


def at(hour, minute):
    return timezone.make_aware(datetime.combine(TUESDAY, time(hour, minute)))


@pytest.fixture
def morning():
    with time_machine.travel(at(10, 30), tick=False):
        yield


@pytest.fixture
def sheet(db):
    """Today's plan: two names, planned and not yet authorised."""
    from engine.models import TradeSheet, TradeSheetLine, TradeSheetState
    from portfolio.models import Instrument

    for token, symbol in enumerate(("INFY", "ITC"), start=1):
        Instrument.objects.create(tradingsymbol=symbol, instrument_token=token, name=symbol)
    sheet = TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument_id="INFY",
        quantity=5,
        spendable_cash=Decimal("6000"),
        ranking_snapshot={
            "ranked": [
                {"rank": 1, "symbol": "INFY", "gap": "-2.14", "ask": "1650.50"},
                {"rank": 2, "symbol": "ITC", "gap": "-1.86", "ask": "450.00"},
                {"rank": 3, "symbol": "TCS", "gap": "-1.42", "ask": "4100.00"},
            ]
        },
    )
    TradeSheetLine.objects.create(
        tradesheet=sheet,
        instrument_id="INFY",
        rank=1,
        quantity=2,
        reference_price=Decimal("1650.50"),
    )
    TradeSheetLine.objects.create(
        tradesheet=sheet, instrument_id="ITC", rank=2, quantity=3, reference_price=Decimal("450.00")
    )
    return sheet


@pytest.fixture
def phone(signed_in):
    """A signed-in client that is a phone, as the screens below expect."""
    signed_in.defaults["HTTP_USER_AGENT"] = IPHONE
    return signed_in


# -- the device switch -------------------------------------------------------


def test_a_phone_asking_for_today_is_sent_to_the_phone(client):
    response = client.get("/", HTTP_USER_AGENT=IPHONE)

    assert response.status_code == 302
    assert response["Location"] == "/m/"


def test_a_desktop_asking_for_the_phone_is_sent_to_the_desk(client):
    response = client.get("/m/kite/", HTTP_USER_AGENT=MAC)

    assert response["Location"] == "/broker/session/"


def test_a_tablet_gets_the_desk(client):
    assert client.get("/m/", HTTP_USER_AGENT=IPAD)["Location"] == "/"


def test_a_signed_out_phone_ends_at_the_phones_sign_in(client):
    """/ -> /m/ -> the sign-in the OTP gate asks for -> the phone's sign-in,
    still carrying where to go afterwards."""
    response = client.get("/", HTTP_USER_AGENT=IPHONE, follow=True)

    path, _status = response.redirect_chain[-1]
    assert path == "/m/login/?next=%2Fm%2F"
    assert b"Verify and sign in" in response.content


def test_only_pages_with_a_phone_counterpart_are_swapped(signed_in):
    """A phone following a link into the records gets the records."""
    response = signed_in.get(reverse("web:cash_ledger"), HTTP_USER_AGENT=IPHONE)

    assert response.status_code == 200


def test_a_post_is_never_bounced(signed_in, sheet, morning):
    response = signed_in.post(
        reverse("engine:authorize", args=[TUESDAY, 1]), HTTP_USER_AGENT=MAC, data={"next": "/m/"}
    )

    assert response["Location"] == "/m/"


def test_choosing_the_desk_on_a_phone_is_remembered(signed_in):
    response = signed_in.get("/m/switch/?to=desktop", HTTP_USER_AGENT=IPHONE)
    assert response.cookies["site"].value == "desktop"

    assert signed_in.get("/", HTTP_USER_AGENT=IPHONE).status_code == 200


def test_choosing_the_phone_on_a_desk_is_remembered(signed_in, no_broker):
    signed_in.get("/m/switch/?to=phone", HTTP_USER_AGENT=MAC)

    assert signed_in.get("/", HTTP_USER_AGENT=MAC)["Location"] == "/m/"


def test_the_switch_ignores_anything_else(client):
    assert "site" not in client.get("/m/switch/?to=elsewhere").cookies


# -- Today -------------------------------------------------------------------


def test_today_without_a_session_asks_for_kite(phone, morning):
    body = phone.get(reverse("phone:today")).content.decode()

    assert "NO SESSION" in body
    assert "Connect to Kite" in body


def test_today_waiting_to_be_authorised_says_so(phone, sheet, morning):
    body = phone.get(reverse("phone:today")).content.decode()

    assert "Waiting to be authorised" in body
    assert "PROPOSED" in body
    assert "Review and authorise" in body
    assert "Stop trading today" in body


def test_today_once_authorised_is_running(phone, sheet, morning):
    sheet.authorized_at, sheet.authorized_by = timezone.now(), "operator"
    sheet.save()

    body = phone.get(reverse("phone:today")).content.decode()

    assert "TODAY IS RUNNING" in body
    assert "0 / 2" in body


def test_today_live_is_a_fragment(phone, sheet, morning):
    response = phone.get(reverse("phone:today_live"))

    assert response["X-Live-Fragment"] == "1"
    assert b"Waiting to be authorised" in response.content


# -- authorising -------------------------------------------------------------


def _decide(client, **data):
    return client.post(reverse("phone:trade"), {"version": 1, **data})


def test_the_tradesheet_shows_every_name_and_why(phone, sheet, morning):
    body = phone.get(reverse("phone:trade")).content.decode()

    assert 'name="qty_INFY"' in body
    assert 'name="qty_ITC"' in body
    assert "TCS" in body  # the row below the picks is the answer to "why these"
    assert "₹1349.00" in body  # 6000 to spend, 2 x 1650.50 + 3 x 450 planned
    assert "Authorise" in body


def test_authorising_unchanged_authorises_that_version(phone, sheet, morning):
    response = _decide(phone, action="authorize", qty_INFY=2, qty_ITC=3)

    sheet.refresh_from_db()
    assert sheet.is_authorized
    assert response["Location"] == reverse("phone:today")


def test_a_changed_quantity_is_an_override_taken_with_the_authorisation(phone, sheet, morning):
    from engine.models import OverrideKind

    _decide(phone, action="authorize", qty_INFY=2, qty_ITC=1, reason="less ITC today")

    sheet.refresh_from_db()
    assert sheet.is_authorized
    assert {line.instrument_id: line.quantity for line in sheet.buy_lines()} == {
        "INFY": 2,
        "ITC": 1,
    }
    override = sheet.overrides.get()
    assert override.kind == OverrideKind.ADJUST_LINE
    assert override.reason == "less ITC today"


def test_a_change_without_a_reason_is_refused_and_nothing_is_authorised(phone, sheet, morning):
    _decide(phone, action="authorize", qty_INFY=2, qty_ITC=1)

    sheet.refresh_from_db()
    assert not sheet.is_authorized
    assert not sheet.overrides.exists()


def test_more_than_the_day_has_is_refused_and_rolls_back_every_change(phone, sheet, morning):
    """ITC down is fine on its own; INFY up is not. Neither happens, and the
    day is not authorised: what is authorised is what the screen showed."""
    _decide(phone, action="authorize", qty_INFY=9, qty_ITC=1, reason="more INFY")

    sheet.refresh_from_db()
    assert not sheet.is_authorized
    assert {line.instrument_id: line.quantity for line in sheet.buy_lines()} == {
        "INFY": 2,
        "ITC": 3,
    }


def test_a_stale_version_is_not_decided_on(phone, sheet, morning):
    phone.post(reverse("phone:trade"), {"version": 7, "action": "authorize"})

    sheet.refresh_from_db()
    assert not sheet.is_authorized


# -- stopping ----------------------------------------------------------------


def test_the_stop_screen_says_what_it_will_and_will_not_do(phone, sheet, morning):
    body = phone.get(reverse("phone:stop")).content.decode()

    assert "End the day as skipped" in body
    assert "It does not sell anything" in body
    assert "Hold to stop today" in body


def test_stopping_from_the_phone_lands_back_on_the_phone(phone, sheet, morning):
    from engine.models import TradeSheetState

    response = phone.post(
        reverse("engine:override", args=[TUESDAY, 1]),
        {"action": "stop", "reason": "on a train", "next": reverse("phone:stop")},
    )

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.SKIPPED
    assert response["Location"] == reverse("phone:stop")
    assert "Nothing more will be placed today" in phone.get(reverse("phone:stop")).content.decode()


def test_an_offsite_next_is_ignored(phone, sheet, morning):
    response = phone.post(
        reverse("engine:override", args=[TUESDAY, 1]),
        {"action": "stop", "reason": "x", "next": "https://example.com/"},
    )

    assert response["Location"] == reverse("engine:detail", args=[TUESDAY, 1])


# -- Kite and sign-in --------------------------------------------------------


def test_without_a_session_the_kite_screen_shows_what_is_blocked(phone):
    body = phone.get(reverse("phone:kite")).content.decode()

    assert "BLOCKED" in body
    assert "UNAFFECTED" in body
    assert reverse("broker:start_login") in body


def test_the_phone_sign_in_is_the_same_door(client):
    """Same form, same throttle: a wrong password counts as a failure there
    exactly as it does on the desk's page."""
    response = client.post(
        reverse("phone:login"),
        {"username": "nobody", "password": "wrong", "otp_token": "123456"},
    )

    assert response.status_code == 200
    assert b"attempt" in response.content
    assert b'autocomplete="one-time-code"' in response.content
