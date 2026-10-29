"""Recovery codes.

The way back in when the authenticator is gone, and the one screen whose
secret can never be shown again.
"""

import pytest
from django.contrib.auth import get_user_model

from accounts import recovery

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator(db):
    return get_user_model().objects.create_user(username="operator", password="fake-pass")


def test_ten_codes_are_issued(operator):
    codes = recovery.issue(operator, actor="operator")

    assert len(codes) == recovery.HOW_MANY
    assert len(set(codes)) == recovery.HOW_MANY


def test_they_are_readable_only_once(operator):
    """Stored by django-otp's static device, which nobody can read back."""
    codes = recovery.issue(operator, actor="operator")

    assert recovery.remaining(operator) == recovery.HOW_MANY
    # Nothing in the API hands them back.
    assert not hasattr(recovery, "read")
    assert codes  # the caller's copy is the only copy


def test_issuing_again_replaces_rather_than_adds(operator):
    """Two live sets is one more than anybody can keep track of."""
    first = recovery.issue(operator, actor="operator")
    second = recovery.issue(operator, actor="operator")

    assert recovery.remaining(operator) == recovery.HOW_MANY
    assert set(first).isdisjoint(second)


def test_a_used_code_is_gone(operator):
    """Each replaces one TOTP code and then dies."""
    from django_otp.plugins.otp_static.models import StaticDevice

    codes = recovery.issue(operator, actor="operator")
    device = StaticDevice.objects.get(user=operator, name=recovery.DEVICE_NAME)

    assert device.verify_token(codes[0]) is True
    assert recovery.remaining(operator) == recovery.HOW_MANY - 1
    assert device.verify_token(codes[0]) is False


def test_codes_avoid_characters_that_are_read_back_wrongly(operator):
    """These get written on paper and typed back by hand."""
    for code in recovery.issue(operator, actor="operator"):
        assert not set(code) & set("aeiou01lIO")


def test_issuing_is_audited(operator):
    from audit.models import AuditEvent

    recovery.issue(operator, actor="operator")

    assert AuditEvent.objects.filter(action="totp.recovery_issued").exists()


# -- the security page ---------------------------------------------------


@pytest.fixture
def signed_in(client, db):
    from django_otp.plugins.otp_totp.models import TOTPDevice

    user = get_user_model().objects.create_user(username="operator2", password="fake-pass")
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.login(username="operator2", password="fake-pass")
    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()
    return client


def test_the_page_says_how_many_are_left_without_showing_them(signed_in):
    from django.urls import reverse

    user = get_user_model().objects.get(username="operator2")
    codes = recovery.issue(user, actor="operator2")

    body = signed_in.get(reverse("accounts:security")).content

    assert b"RECOVERY CODES LEFT" in body
    for code in codes:
        assert code.encode() not in body


def _current_code(user) -> str:
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device = TOTPDevice.objects.get(user=user, confirmed=True)
    return device.bin_key and _totp_now(device)


def _totp_now(device) -> str:
    from django_otp.oath import TOTP

    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = __import__("time").time()
    return str(totp.token()).zfill(device.digits)


def test_a_live_session_alone_cannot_rotate_the_codes(signed_in):
    """A session that has been taken over could otherwise kill the operator's
    codes and keep ten of its own, which outlives the session it came from."""
    from django.urls import reverse

    user = get_user_model().objects.get(username="operator2")
    first = recovery.issue(user, actor="operator2")

    response = signed_in.post(reverse("accounts:security"), follow=True)

    assert b"did not verify" in response.content
    assert b"SHOWN ONCE" not in response.content

    from django_otp.plugins.otp_static.models import StaticDevice

    device = StaticDevice.objects.get(user=user, name=recovery.DEVICE_NAME)
    assert device.verify_token(first[0]) is True  # the real codes still work


def test_a_refused_rotation_is_audited(signed_in):
    from django.urls import reverse

    from audit.models import AuditEvent

    signed_in.post(reverse("accounts:security"), {"otp_token": "000000"}, follow=True)

    assert AuditEvent.objects.filter(action="totp.recovery_refused").exists()


def test_a_current_authenticator_code_rotates_them(signed_in):
    from django.urls import reverse

    user = get_user_model().objects.get(username="operator2")
    first = recovery.issue(user, actor="operator2")

    response = signed_in.post(
        reverse("accounts:security"), {"otp_token": _current_code(user)}, follow=True
    )

    assert b"SHOWN ONCE" in response.content
    assert recovery.remaining(user) == recovery.HOW_MANY
    # The download is built from what is on screen, not fetched again.
    assert b'download="steadystack-recovery-codes.txt"' in response.content

    from django_otp.plugins.otp_static.models import StaticDevice

    device = StaticDevice.objects.get(user=user, name=recovery.DEVICE_NAME)
    assert device.verify_token(first[0]) is False  # the previous set is dead


def test_a_recovery_code_cannot_mint_more_recovery_codes(signed_in):
    """Rotating the fallback requires the primary factor: one stolen code must
    not mint ten more."""
    from django.urls import reverse

    user = get_user_model().objects.get(username="operator2")
    codes = recovery.issue(user, actor="operator2")

    response = signed_in.post(reverse("accounts:security"), {"otp_token": codes[0]}, follow=True)

    assert b"did not verify" in response.content
