"""Replacing the authenticator.

`enrol_totp` refuses to do this on a web page, reasoning that a page which can
add an authenticator turns one factor into two. These are the guards that
answer that: a session alone cannot start one, and nothing is revoked until
the replacement has proved itself.
"""

import time

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import recovery, totp_reset

pytestmark = pytest.mark.django_db

FAKE_PASSWORD = "fake-password-for-tests"


def code_for(device: TOTPDevice) -> str:
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = time.time()
    return str(totp.token()).zfill(device.digits)


@pytest.fixture
def operator(client, db):
    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.login(username="operator", password=FAKE_PASSWORD)
    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()
    return user


def _device(user) -> TOTPDevice:
    return TOTPDevice.objects.get(user=user, name="default", confirmed=True)


# -- starting it ---------------------------------------------------------


def test_a_session_alone_cannot_start_a_reset(client, operator):
    """The objection enrol_totp raises, answered."""
    response = client.post(reverse("accounts:reset_totp"), {"step": "begin"}, follow=True)

    assert b"did not verify" in response.content
    assert TOTPDevice.objects.filter(user=operator, confirmed=False).count() == 0


def test_a_current_authenticator_code_starts_one(client, operator):
    response = client.post(
        reverse("accounts:reset_totp"),
        {"step": "begin", "otp_token": code_for(_device(operator))},
        follow=True,
    )

    assert b"STEP 2 OF 2" in response.content
    assert TOTPDevice.objects.filter(user=operator, confirmed=False).count() == 1


def test_a_recovery_code_starts_one_too(client, operator):
    """Losing the phone is the case recovery codes exist for."""
    codes = recovery.issue(operator, actor="operator")

    response = client.post(
        reverse("accounts:reset_totp"),
        {"step": "begin", "otp_token": codes[0]},
        follow=True,
    )

    assert b"STEP 2 OF 2" in response.content


def test_a_refused_start_is_audited(client, operator):
    from audit.models import AuditEvent

    client.post(reverse("accounts:reset_totp"), {"step": "begin", "otp_token": "000000"})

    assert AuditEvent.objects.filter(action="totp.reset_refused").exists()


# -- finishing it --------------------------------------------------------


def _begin(client, operator):
    client.post(
        reverse("accounts:reset_totp"),
        {"step": "begin", "otp_token": code_for(_device(operator))},
    )
    return TOTPDevice.objects.get(user=operator, confirmed=False)


def test_the_old_device_still_works_until_the_new_one_proves_itself(client, operator):
    """A reset abandoned halfway must lock nobody out."""
    original = _device(operator)
    _begin(client, operator)

    assert TOTPDevice.objects.filter(pk=original.pk, confirmed=True).exists()


def test_a_wrong_code_does_not_revoke_anything(client, operator):
    original = _device(operator)
    _begin(client, operator)

    response = client.post(
        reverse("accounts:reset_totp"),
        {"step": "confirm", "confirm_token": "000000"},
        follow=True,
    )

    assert b"did not match the new authenticator" in response.content
    assert TOTPDevice.objects.filter(pk=original.pk, confirmed=True).exists()


def test_confirming_replaces_the_old_device(client, operator):
    original = _device(operator)
    pending = _begin(client, operator)

    client.post(
        reverse("accounts:reset_totp"),
        {"step": "confirm", "confirm_token": code_for(pending)},
        follow=True,
    )

    assert not TOTPDevice.objects.filter(pk=original.pk).exists()
    pending.refresh_from_db()
    assert pending.confirmed is True
    assert pending.name == "default"


def test_confirming_reissues_the_recovery_codes(client, operator):
    """An authenticator replaced under suspicion makes the codes issued beside
    it suspect too."""
    old_codes = recovery.issue(operator, actor="operator")
    pending = _begin(client, operator)

    response = client.post(
        reverse("accounts:reset_totp"),
        {"step": "confirm", "confirm_token": code_for(pending)},
        follow=True,
    )

    assert b"SHOWN ONCE" in response.content
    from django_otp.plugins.otp_static.models import StaticDevice

    device = StaticDevice.objects.get(user=operator, name=recovery.DEVICE_NAME)
    assert device.verify_token(old_codes[0]) is False


def test_cancelling_leaves_everything_alone(client, operator):
    original = _device(operator)
    _begin(client, operator)

    response = client.post(reverse("accounts:reset_totp"), {"step": "cancel"}, follow=True)

    assert b"unchanged" in response.content
    assert TOTPDevice.objects.filter(pk=original.pk, confirmed=True).exists()
    assert TOTPDevice.objects.filter(user=operator, confirmed=False).count() == 0


def test_one_operator_cannot_reset_another(client, operator):
    """The pending device is looked up against the signed-in user, not just
    by the id somebody put in their session."""
    other = get_user_model().objects.create_user(username="other", password=FAKE_PASSWORD)
    theirs = TOTPDevice.objects.create(user=other, name="pending", confirmed=False)

    session = client.session
    session[totp_reset.SESSION_KEY] = str(theirs.pk)
    session.save()

    client.post(
        reverse("accounts:reset_totp"),
        {"step": "confirm", "confirm_token": code_for(theirs)},
        follow=True,
    )

    theirs.refresh_from_db()
    assert theirs.confirmed is False


def test_the_operator_stays_signed_in_and_sees_the_new_codes(client, operator):
    """The session was verified against the device just revoked. Without
    re-verifying it against the replacement, the operator is bounced to login
    before ever seeing the codes they were handed."""
    pending = _begin(client, operator)

    response = client.post(
        reverse("accounts:reset_totp"),
        {"step": "confirm", "confirm_token": code_for(pending)},
        follow=True,
    )

    assert response.status_code == 200
    assert b"SHOWN ONCE" in response.content
    assert b"Sign in" not in response.content


def test_a_refused_confirmation_is_still_recorded(client, operator):
    """A refusal recorded inside the transaction the refusal aborts is a
    refusal nobody can see afterwards."""
    from audit.models import AuditEvent

    _begin(client, operator)
    client.post(reverse("accounts:reset_totp"), {"step": "confirm", "confirm_token": "000000"})

    assert AuditEvent.objects.filter(action="totp.reset_refused").exists()
