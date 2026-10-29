"""TOTP enrolment from the console.

Enrolment is a console act because a web page that can add an authenticator
turns one factor into two. These tests hold that shape: a device is created
and confirmed, the act is audited, and an existing device is never silently
replaced -- losing an authenticator without noticing is how an operator gets
locked out of their own portfolio.
"""

import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django_otp.plugins.otp_totp.models import TOTPDevice

from audit.models import AuditEvent

FAKE_PASSWORD = "fake-password-for-tests"


@pytest.fixture
def operator(db):
    return get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)


def enrol(*args) -> str:
    out = io.StringIO()
    call_command("enrol_totp", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_a_device_is_created_and_confirmed(operator):
    enrol("operator")

    device = TOTPDevice.objects.get(user=operator)
    assert device.confirmed is True
    assert device.name == "default"


@pytest.mark.django_db
def test_a_qr_code_is_printed(operator):
    output = enrol("operator")
    # print_ascii draws with block characters; their presence is the QR.
    assert "█" in output or "▀" in output


@pytest.mark.django_db
def test_the_secret_is_never_printed(operator):
    """The QR carries it. Text on a terminal ends up in scrollback and logs."""
    output = enrol("operator")
    device = TOTPDevice.objects.get(user=operator)
    assert device.key not in output


@pytest.mark.django_db
def test_enrolment_is_audited(operator):
    enrol("operator")
    event = AuditEvent.objects.get(action="totp.device_enrolled")
    assert event.actor == "operator"


@pytest.mark.django_db
def test_an_unknown_user_is_refused(db):
    with pytest.raises(CommandError, match="No user named"):
        enrol("ghost")


@pytest.mark.django_db
def test_a_second_enrolment_is_refused_without_replace(operator):
    enrol("operator")
    with pytest.raises(CommandError, match="--replace"):
        enrol("operator")


@pytest.mark.django_db
def test_replace_revokes_the_previous_device(operator):
    enrol("operator")
    first = TOTPDevice.objects.get(user=operator).key

    enrol("operator", "--replace")

    devices = TOTPDevice.objects.filter(user=operator)
    assert devices.count() == 1
    assert devices.first().key != first


@pytest.mark.django_db
def test_a_revocation_is_audited(operator):
    enrol("operator")
    enrol("operator", "--replace")
    assert AuditEvent.objects.filter(action="totp.device_revoked").count() == 1


@pytest.mark.django_db
def test_a_second_named_device_does_not_collide(operator):
    """A replacement phone enrolled alongside the old one, deliberately."""
    enrol("operator")
    enrol("operator", "--name", "backup")

    assert TOTPDevice.objects.filter(user=operator).count() == 2


@pytest.mark.django_db
def test_nothing_is_written_when_the_user_does_not_exist(db):
    with pytest.raises(CommandError):
        enrol("ghost")

    assert TOTPDevice.objects.count() == 0
    assert AuditEvent.objects.count() == 0
