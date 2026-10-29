"""The settings screen.

It writes through `store.update` and nowhere else, so everything the store
refuses -- the live gate above all -- is refused here for the same reason and
with the same words.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def no_outbound_lookup(monkeypatch):
    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: None)


def test_configuration_is_not_reachable_anonymously(client):
    response = client.get(reverse("configuration:index"))

    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]


def test_it_shows_the_defaults_before_anything_is_configured(signed_in):
    response = signed_in.get(reverse("configuration:index"))

    assert response.status_code == 200
    assert b"running on defaults" in response.content


def test_saving_a_change_appends_a_version(signed_in, no_outbound_lookup):
    from configuration.store import current

    response = signed_in.post(
        reverse("configuration:index"),
        _form_data(daily_amount="2600.00", reason="raising the daily amount", otp_token=_code()),
        follow=True,
    )

    assert response.status_code == 200
    assert current().daily_amount == Decimal("2600.00")
    assert current().version == 1


def test_the_reason_is_required(signed_in, no_outbound_lookup):
    from configuration.models import ConfigurationVersion

    signed_in.post(reverse("configuration:index"), _form_data(daily_amount="2600.00", reason=""))

    assert ConfigurationVersion.objects.count() == 0


def test_saving_nothing_changed_writes_no_version(signed_in, no_outbound_lookup):
    """The store appends per call, so a save that changed nothing would bury
    the one that mattered under a version that meant nothing."""
    from configuration.models import ConfigurationVersion

    signed_in.post(reverse("configuration:index"), _form_data(reason="no change at all"))

    assert ConfigurationVersion.objects.count() == 0


def test_going_live_without_a_registered_address_is_refused(signed_in, no_outbound_lookup):
    """M19b, at the one gate that matters. The screen must not be a way round
    what the store refuses."""
    from configuration.store import current

    response = signed_in.post(
        reverse("configuration:index"),
        _form_data(order_mode="live", reason="go live"),
        follow=True,
    )

    assert current().order_mode == "paper"
    assert b"refused" in response.content.lower() or b"static" in response.content.lower()


def test_the_history_shows_what_changed_from_what(signed_in, no_outbound_lookup):
    from configuration.store import update

    update(changed_by="operator", reason="first move", daily_amount=Decimal("1500"))
    update(changed_by="operator", reason="second move", daily_amount=Decimal("2600"))

    body = signed_in.get(reverse("configuration:index")).content

    assert b"second move" in body
    assert b"daily amount" in body


# -- the changes that need the authenticator -------------------------------


def _code() -> str:
    """A current code from the signed-in operator's authenticator."""
    import time

    from django_otp.oath import TOTP
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device = TOTPDevice.objects.get(user__username="operator")
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = time.time()
    return f"{totp.token():0{device.digits}d}"


def test_raising_the_daily_amount_without_a_code_is_refused(signed_in, no_outbound_lookup):
    """A session that has been taken over must not be able to spend more."""
    from configuration.models import ConfigurationVersion

    response = signed_in.post(
        reverse("configuration:index"), _form_data(daily_amount="9000.00", reason="more")
    )

    assert ConfigurationVersion.objects.count() == 0
    assert b"authenticator" in response.content


def test_raising_the_daily_amount_with_a_wrong_code_is_refused(signed_in, no_outbound_lookup):
    from configuration.models import ConfigurationVersion

    signed_in.post(
        reverse("configuration:index"),
        _form_data(daily_amount="9000.00", reason="more", otp_token="000000"),
    )

    assert ConfigurationVersion.objects.count() == 0


def test_raising_the_daily_amount_with_a_current_code_is_saved(signed_in, no_outbound_lookup):
    from configuration.store import current

    signed_in.post(
        reverse("configuration:index"),
        _form_data(daily_amount="9000.00", reason="more", otp_token=_code()),
    )

    assert current().daily_amount == Decimal("9000.00")


def test_lowering_the_daily_amount_needs_no_code(signed_in, no_outbound_lookup):
    """Spending less is the safe direction; asking for a code there only
    teaches the operator to type it without reading why."""
    from configuration.store import current, update

    update(changed_by="operator", reason="start", daily_amount=Decimal("3000"))

    signed_in.post(
        reverse("configuration:index"), _form_data(daily_amount="1000.00", reason="less")
    )

    assert current().daily_amount == Decimal("1000.00")


def test_changing_the_registered_address_needs_a_code(signed_in, no_outbound_lookup):
    from configuration.store import current

    signed_in.post(
        reverse("configuration:index"),
        _form_data(registered_static_ip="203.0.113.7", reason="new box"),
    )
    assert not current().registered_static_ip

    signed_in.post(
        reverse("configuration:index"),
        _form_data(registered_static_ip="203.0.113.7", reason="new box", otp_token=_code()),
    )
    assert current().registered_static_ip == "203.0.113.7"


def test_going_live_needs_a_code_before_anything_else_is_asked(signed_in, monkeypatch):
    """The code is asked for first: the static-IP gate behind it is the
    store's, and a stolen session must not even learn what it says."""
    from audit.models import AuditEvent
    from configuration.store import current

    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: "203.0.113.7")

    response = signed_in.post(
        reverse("configuration:index"), _form_data(order_mode="live", reason="go live")
    )

    assert current().order_mode == "paper"
    assert b"authenticator" in response.content
    assert AuditEvent.objects.filter(action="configuration.code_refused").count() == 1


def test_other_changes_need_no_code(signed_in, no_outbound_lookup):
    from configuration.store import current

    signed_in.post(
        reverse("configuration:index"), _form_data(slice_quantity="3", reason="bigger slices")
    )

    assert current().slice_quantity == 3


def test_the_form_asks_for_the_code_and_says_when(signed_in):
    body = signed_in.get(reverse("configuration:index")).content

    assert b'name="otp_token"' in body
    assert b"going live" in body


def _form_data(**overrides) -> dict:
    """A full form post, built from the store's own field list.

    Enumerated rather than hard-coded on purpose: a field added to the model
    and forgotten here would make every save in these tests fail for a reason
    that has nothing to do with what they are testing.
    """
    from datetime import time

    from configuration.store import MUTABLE_FIELDS, current

    config = current()
    data = {"reason": "because"}
    for name in MUTABLE_FIELDS:
        value = getattr(config, name)
        if isinstance(value, time):
            value = value.strftime("%H:%M")
        data[name] = "" if value is None else str(value)

    data.update(overrides)
    return data


@pytest.mark.django_db
def test_the_broker_settings_are_shown_masked_and_never_the_secret(signed_in, settings):
    from django.urls import reverse

    settings.KITE_API_KEY = "abcd1234wxyz"

    body = signed_in.get(reverse("configuration:index")).content

    assert "abcd…yz".encode() in body
    assert b"abcd1234wxyz" not in body
    assert b"/kite/postback/" in body


# -- the emergency stop ----------------------------------------------------


@pytest.mark.django_db
def test_the_page_offers_to_stop_trading(signed_in):
    from django.urls import reverse

    body = signed_in.get(reverse("configuration:index")).content

    assert b"Stop trading and cancel all orders" in body


@pytest.mark.django_db
def test_stopping_pauses_and_says_so(signed_in):
    from django.urls import reverse

    from configuration.pause import is_paused

    response = signed_in.post(
        reverse("configuration:stop"), {"reason": "a circuit move"}, follow=True
    )

    assert is_paused() is True
    assert b"Trading is stopped" in response.content


@pytest.mark.django_db
def test_stopping_without_a_reason_is_refused(signed_in):
    """Every other irreversible action here needs one, and in six months the
    question will be why."""
    from django.urls import reverse

    from configuration.pause import is_paused

    signed_in.post(reverse("configuration:stop"), {"reason": "  "}, follow=True)

    assert is_paused() is False


@pytest.mark.django_db
def test_a_stopped_system_shows_why_and_offers_to_resume(signed_in):
    from django.urls import reverse

    signed_in.post(reverse("configuration:stop"), {"reason": "a circuit move"})
    body = signed_in.get(reverse("configuration:index")).content

    assert b"TRADING IS STOPPED" in body
    assert b"a circuit move" in body
    assert b"Resume trading" in body


@pytest.mark.django_db
def test_resuming_starts_it_again(signed_in):
    from django.urls import reverse

    from configuration.pause import is_paused

    signed_in.post(reverse("configuration:stop"), {"reason": "a circuit move"})
    signed_in.post(reverse("configuration:resume"), {"note": "back"}, follow=True)

    assert is_paused() is False


@pytest.mark.django_db
def test_stopping_is_a_post_only_action(signed_in):
    """A GET that stops trading is a link a crawler can press."""
    from django.urls import reverse

    assert signed_in.get(reverse("configuration:stop")).status_code == 405
    assert signed_in.get(reverse("configuration:resume")).status_code == 405
