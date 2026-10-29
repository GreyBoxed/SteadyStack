"""Device selection.

Preselecting the operator's only authenticator is a deliberate narrowing of
django-otp's behaviour, so the boundary is tested from both sides: one device
is chosen for the operator, more than one is not chosen for them, and a device
belonging to somebody else is never chosen at all.
"""

import time

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

FAKE_PASSWORD = "fake-password-for-tests"


def make_user(username: str):
    return get_user_model().objects.create_user(username=username, password=FAKE_PASSWORD)


def token_for(device: TOTPDevice) -> str:
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = time.time()
    return f"{totp.token():0{device.digits}d}"


def post_login(client, username="operator", otp_token="", otp_device=""):
    return client.post(
        reverse("accounts:login"),
        {
            "username": username,
            "password": FAKE_PASSWORD,
            "otp_token": otp_token,
            "otp_device": otp_device,
        },
    )


@pytest.mark.django_db
def test_one_device_is_preselected(client):
    user = make_user("operator")
    device = TOTPDevice.objects.create(user=user, name="phone", confirmed=True)

    response = post_login(client, otp_token=token_for(device))

    assert response.status_code == 302


@pytest.mark.django_db
def test_two_devices_require_an_explicit_choice(client):
    """With a second device present, a blind match would throttle the device
    the operator did not use."""
    user = make_user("operator")
    phone = TOTPDevice.objects.create(user=user, name="phone", confirmed=True)
    TOTPDevice.objects.create(user=user, name="backup", confirmed=True)

    response = post_login(client, otp_token=token_for(phone))

    assert response.status_code == 200
    assert client.session.get("_auth_user_id") is None


@pytest.mark.django_db
def test_naming_the_device_works_when_there_are_two(client):
    user = make_user("operator")
    phone = TOTPDevice.objects.create(user=user, name="phone", confirmed=True)
    TOTPDevice.objects.create(user=user, name="backup", confirmed=True)

    response = post_login(client, otp_token=token_for(phone), otp_device=phone.persistent_id)

    assert response.status_code == 302


@pytest.mark.django_db
def test_an_unconfirmed_device_does_not_count_as_the_one_device(client):
    """An enrolment that was never completed must not become a second factor."""
    user = make_user("operator")
    device = TOTPDevice.objects.create(user=user, name="half-enrolled", confirmed=False)

    response = post_login(client, otp_token=token_for(device))

    assert response.status_code == 200
    assert client.session.get("_auth_user_id") is None


@pytest.mark.django_db
def test_another_users_device_is_never_chosen(client):
    """Naming somebody else's device must not authenticate against it."""
    operator = make_user("operator")
    TOTPDevice.objects.create(user=operator, name="phone", confirmed=True)

    stranger = make_user("stranger")
    stranger_device = TOTPDevice.objects.create(user=stranger, name="phone", confirmed=True)

    response = post_login(
        client,
        otp_token=token_for(stranger_device),
        otp_device=stranger_device.persistent_id,
    )

    assert response.status_code == 200
    assert client.session.get("_auth_user_id") is None
