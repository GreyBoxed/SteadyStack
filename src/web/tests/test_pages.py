"""The two pages that exist in Phase 1, and the gate in front of one of them.

The health endpoint is the only view deliberately open to the world, so what
it does *not* say matters as much as what it does.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

FAKE_PASSWORD = "fake-password-for-tests"


@pytest.fixture
def sunday():
    """A Sunday: every check that depends on the trading day stands down, so
    the endpoint's shape and openness are tested apart from the day's state.
    The checks themselves are in test_health."""
    import time_machine

    with time_machine.travel("2026-09-20 11:00:00 +05:30", tick=False):
        yield


@pytest.mark.django_db
def test_health_is_reachable_without_signing_in(client, sunday):
    response = client.get("/health/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_health_reports_ok(client, sunday):
    assert json.loads(client.get("/health/").content)["status"] == "ok"


@pytest.mark.django_db
def test_health_says_nothing_a_stranger_could_use(client, sunday):
    """Booleans only. No balances, no holdings, no symbols, no expiry -- a
    health endpoint that leaks has to be authenticated, and then nothing can
    reach it."""
    body = json.loads(client.get("/health/").content)

    assert set(body) == {
        "status",
        "checks",
        "session",
        "tasks_ran_today",
        "last_task_failed",
        "divergences_open",
    }
    assert set(body["checks"]) == {
        "session",
        "tradesheet",
        "authorized",
        "rounds",
        "close",
        "feed",
        "settled",
    }
    assert all(isinstance(value, bool) for value in body["checks"].values())
    assert all(
        isinstance(value, bool) for key, value in body.items() if key not in {"status", "checks"}
    )


@pytest.mark.django_db
def test_health_reports_whether_the_day_can_trade(client, sunday):
    """The one failure that costs a trading day, on the endpoint a monitor
    watches (M51)."""
    body = json.loads(client.get("/health/").content)
    assert body["session"] is False


@pytest.mark.django_db
def test_the_home_page_is_not_reachable_anonymously(client):
    response = client.get("/")
    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]


@pytest.mark.django_db
def test_the_home_page_is_not_reachable_with_a_password_alone(client):
    """Authenticated but not OTP-verified is not enough: the middleware denies
    by default and only a verified session passes."""
    get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    client.login(username="operator", password=FAKE_PASSWORD)

    assert client.get("/").status_code == 302


@pytest.mark.django_db
def test_the_home_page_is_reachable_once_verified(client):
    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.login(username="operator", password=FAKE_PASSWORD)

    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()

    assert client.get("/").status_code == 200


@pytest.mark.django_db
def test_every_response_carries_a_request_id(client):
    """So a report of "it failed just now" can be matched to a log line."""
    assert client.get("/health/")["X-Request-ID"]


@pytest.mark.django_db
def test_the_stylesheet_url_changes_with_each_build(client):
    """The CSS keeps one filename, so without a stamp a browser keeps the old
    styles after a rebuild."""
    import re

    body = client.get(reverse("accounts:login")).content.decode()

    assert re.search(r"css/app\.css\?v=\d+", body)
