"""Login: one step, audited, and throttled from the audit trail itself.

The interesting cases are not "a good password works". They are: a locked-out
subject is not punished further for waiting, an unknown username is throttled
on what was typed, and every outcome leaves a row somebody can read later.
"""

import time

import pytest
import time_machine
from django.contrib.auth import get_user_model
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import throttle
from audit.models import AuditEvent

FAKE_PASSWORD = "fake-password-for-tests"

#: The address every request below comes from unless it says otherwise.
HERE = "127.0.0.1"


@pytest.fixture
def operator(db):
    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    return user


def token_for(user) -> str:
    """A currently-valid code for the user's device, generated the same way an
    authenticator app would."""
    device = TOTPDevice.objects.get(user=user)
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = time.time()
    return f"{totp.token():0{device.digits}d}"


def post_login(client, username="operator", fake_password=FAKE_PASSWORD, otp_token="", ip=HERE):
    return client.post(
        reverse("accounts:login"),
        {"username": username, "password": fake_password, "otp_token": otp_token},
        REMOTE_ADDR=ip,
    )


def fail_n_times(client, n: int, username: str = "operator", ip: str = HERE) -> None:
    for _ in range(n):
        post_login(client, username=username, fake_password="wrong", ip=ip)


@pytest.mark.django_db
def test_a_correct_password_and_code_signs_in(client, operator):
    response = post_login(client, otp_token=token_for(operator))
    assert response.status_code == 302
    assert client.session.get("_auth_user_id") == str(operator.pk)


@pytest.mark.django_db
def test_the_session_records_the_verified_device(client, operator):
    """Without this the operator is logged in but never OTP-verified, and the
    enforcement middleware would bounce them off every page."""
    post_login(client, otp_token=token_for(operator))
    assert client.session.get("otp_device_id")


@pytest.mark.django_db
def test_a_correct_password_without_a_code_is_refused(client, operator):
    response = post_login(client, otp_token="")
    assert response.status_code == 200
    assert client.session.get("_auth_user_id") is None


@pytest.mark.django_db
def test_a_wrong_code_is_refused(client, operator):
    response = post_login(client, otp_token="000000")
    assert response.status_code == 200
    assert client.session.get("_auth_user_id") is None


@pytest.mark.django_db
def test_a_wrong_password_is_refused(client, operator):
    response = post_login(client, fake_password="wrong", otp_token=token_for(operator))
    assert response.status_code == 200
    assert client.session.get("_auth_user_id") is None


@pytest.mark.django_db
def test_a_successful_login_is_audited(client, operator):
    post_login(client, otp_token=token_for(operator))
    assert AuditEvent.objects.filter(action=throttle.ACTION_SUCCEEDED).count() == 1


@pytest.mark.django_db
def test_a_failed_login_is_audited(client, operator):
    post_login(client, fake_password="wrong")
    event = AuditEvent.objects.get(action=throttle.ACTION_FAILED)
    assert event.entity_id == "operator"


@pytest.mark.django_db
def test_attempts_below_the_threshold_are_allowed(client, operator):
    fail_n_times(client, throttle.MAX_FAILURES - 1)
    response = post_login(client, otp_token=token_for(operator))
    assert response.status_code == 302


@pytest.mark.django_db
def test_too_many_failures_lock_the_subject_out(client, operator):
    fail_n_times(client, throttle.MAX_FAILURES)

    response = post_login(client, otp_token=token_for(operator))

    assert response.status_code == 200
    assert client.session.get("_auth_user_id") is None
    assert AuditEvent.objects.filter(action=throttle.ACTION_THROTTLED).count() == 1


@pytest.mark.django_db
def test_a_throttled_attempt_is_not_also_counted_as_a_failure(client, operator):
    """Otherwise trying again extends the lockout, and it never ends."""
    fail_n_times(client, throttle.MAX_FAILURES)
    before = AuditEvent.objects.filter(action=throttle.ACTION_FAILED).count()

    post_login(client, otp_token=token_for(operator))
    post_login(client, otp_token=token_for(operator))

    assert AuditEvent.objects.filter(action=throttle.ACTION_FAILED).count() == before


@pytest.mark.django_db
def test_the_lockout_message_says_how_long_to_wait(client, operator):
    fail_n_times(client, throttle.MAX_FAILURES)
    response = post_login(client, otp_token=token_for(operator))
    assert b"Too many failed attempts" in response.content


@pytest.mark.django_db
def test_failures_outside_the_window_do_not_count(client, operator):
    """The rows are append-only, so the failures are made old by being written
    in the past rather than by being rewritten afterwards."""
    with time_machine.travel("2026-09-22 09:00:00+05:30"):
        fail_n_times(client, throttle.MAX_FAILURES)
        assert throttle.status("operator", HERE).locked is True

    with time_machine.travel("2026-09-22 09:20:00+05:30"):
        assert throttle.status("operator", HERE).locked is False


@pytest.mark.django_db
def test_an_unknown_username_is_throttled_on_what_was_typed(client):
    """An attacker guessing usernames must not get unlimited attempts because
    none of them matched an account."""
    fail_n_times(client, throttle.MAX_FAILURES, username="ghost")
    assert throttle.status("ghost", HERE).locked is True


@pytest.mark.django_db
def test_throttling_is_per_subject(client, operator):
    fail_n_times(client, throttle.MAX_FAILURES, username="ghost")
    assert throttle.status("operator", HERE).locked is False


@pytest.mark.django_db
def test_the_subject_is_matched_case_insensitively(client, operator):
    fail_n_times(client, throttle.MAX_FAILURES, username="OPERATOR")
    assert throttle.status("operator", HERE).locked is True


@pytest.mark.django_db
def test_an_empty_username_still_has_a_subject(client):
    """A blank submission must not write a row with no subject at all."""
    fail_n_times(client, 1, username="")
    assert AuditEvent.objects.get(action=throttle.ACTION_FAILED).entity_id == (
        throttle.UNKNOWN_SUBJECT
    )


@pytest.mark.django_db
def test_the_retry_window_clears_from_the_oldest_counted_failure(client, operator):
    fail_n_times(client, throttle.MAX_FAILURES)
    state = throttle.status("operator", HERE)
    assert state.locked is True
    assert state.retry_after is not None
    assert state.retry_after <= throttle.WINDOW


@pytest.mark.django_db
def test_a_recovery_code_signs_in_when_the_authenticator_is_gone(client):
    """The way back in that does not need the phone."""
    from accounts import recovery

    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    codes = recovery.issue(user, actor="operator")

    response = client.post(
        reverse("accounts:login"),
        {"username": "operator", "password": FAKE_PASSWORD, "otp_token": codes[0]},
    )

    assert response.status_code == 302
    assert recovery.remaining(user) == recovery.HOW_MANY - 1


@pytest.mark.django_db
def test_the_same_recovery_code_does_not_work_twice(client):
    from accounts import recovery

    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    codes = recovery.issue(user, actor="operator")

    client.post(
        reverse("accounts:login"),
        {"username": "operator", "password": FAKE_PASSWORD, "otp_token": codes[0]},
    )
    client.logout()
    again = client.post(
        reverse("accounts:login"),
        {"username": "operator", "password": FAKE_PASSWORD, "otp_token": codes[0]},
    )

    assert again.status_code == 200  # refused, form redisplayed


@pytest.mark.django_db
def test_a_failure_says_how_many_attempts_are_left(client, operator):
    response = post_login(client, fake_password="wrong")

    assert response.context["attempts_left"] == throttle.MAX_FAILURES - 1
    assert b"attempts left before" in response.content


@pytest.mark.django_db
def test_the_sign_in_page_explains_the_door_and_nothing_behind_it(client):
    from django.urls import reverse

    body = client.get(reverse("accounts:login")).content

    assert b"HOW THIS DOOR IS BUILT" in body


@pytest.mark.django_db
def test_signing_out_lands_on_the_sign_in_page_and_ends_the_session(client, operator):
    from django.urls import reverse

    post_login(client, otp_token=token_for(operator))
    assert client.session.get("_auth_user_id")

    response = client.post(reverse("accounts:logout"))

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:login")
    assert client.session.get("_auth_user_id") is None
    assert client.get(reverse("web:home")).status_code == 302


@pytest.mark.django_db
def test_every_signed_in_page_offers_sign_out(client, operator):
    from django.urls import reverse

    post_login(client, otp_token=token_for(operator))
    body = client.get(reverse("web:home")).content

    assert reverse("accounts:logout").encode() in body
    assert b"Sign out" in body


# -- a refusal says nothing about which factor failed ----------------------


def _refusal(response) -> list[str]:
    return list(response.context["form"].non_field_errors())


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["accounts:login", "phone:login"])
def test_every_refusal_reads_the_same_whichever_factor_failed(client, operator, url):
    """Otherwise the password can be guessed without the code: a refusal that
    asks for the code, or calls it wrong, has just said the password was right."""
    attempts = {
        "wrong password, good code": ("wrong", token_for(operator), "operator"),
        "good password, no code": (FAKE_PASSWORD, "", "operator"),
        "good password, wrong code": (FAKE_PASSWORD, "000000", "operator"),
        "nobody by that name": (FAKE_PASSWORD, "000000", "ghost"),
    }
    refusals = {}
    for label, (password, code, username) in attempts.items():
        response = client.post(
            reverse(url), {"username": username, "password": password, "otp_token": code}
        )
        refusals[label] = _refusal(response)

    assert len({tuple(r) for r in refusals.values()}) == 1, refusals
    assert refusals["good password, no code"] == [
        response.context["form"].error_messages["invalid_login"]
        % {"username": response.context["form"].username_field.verbose_name}
    ]


@pytest.mark.django_db
def test_a_right_password_is_not_echoed_back_into_the_form(client, operator):
    """django-otp re-renders the password once it has verified -- which, on
    its own, says that it verified."""
    response = post_login(client, otp_token="000000")

    assert FAKE_PASSWORD.encode() not in response.content


@pytest.mark.django_db
def test_two_authenticators_refuse_in_the_same_words(client, operator):
    """Asking which device is only ever said to somebody whose password was right."""
    code = token_for(operator)
    TOTPDevice.objects.create(user=operator, name="backup", confirmed=True)

    named = _refusal(post_login(client, otp_token=code))
    wrong = _refusal(post_login(client, fake_password="wrong", otp_token="000000"))

    assert named == wrong


# -- the lockout is per account *and* address -----------------------------


@pytest.mark.django_db
def test_a_stranger_cannot_lock_the_operator_out_from_elsewhere(client, operator):
    """Five wrong guesses from one address used to close the door for everyone,
    the operator included: a denial of service for the price of a username."""
    fail_n_times(client, throttle.MAX_FAILURES, ip="198.51.100.9")

    assert throttle.status("operator", "198.51.100.9").locked is True
    assert throttle.status("operator", HERE).locked is False
    assert post_login(client, otp_token=token_for(operator)).status_code == 302


@pytest.mark.django_db
def test_one_address_trying_many_usernames_is_locked_out_too(client):
    """Per-account counting alone lets one address spray every name it can
    think of. A looser ceiling across all of them stops that."""
    for n in range(throttle.MAX_FAILURES_PER_ADDRESS):
        fail_n_times(client, 1, username=f"guess{n}", ip="198.51.100.9")

    assert throttle.status("somebody-new", "198.51.100.9").locked is True
    assert throttle.status("somebody-new", HERE).locked is False


@pytest.mark.django_db
def test_the_address_ceiling_is_looser_than_the_account_one():
    assert throttle.MAX_FAILURES_PER_ADDRESS > throttle.MAX_FAILURES


# -- which address ----------------------------------------------------------


def _request(**meta):
    from django.test import RequestFactory

    return RequestFactory().get("/", **meta)


def test_the_address_nginx_saw_is_preferred():
    request = _request(
        HTTP_X_REAL_IP="203.0.113.5", HTTP_X_FORWARDED_FOR="10.0.0.1", REMOTE_ADDR="127.0.0.1"
    )
    assert throttle.client_ip(request) == "203.0.113.5"


def test_a_forged_forwarded_for_does_not_choose_the_address():
    """The client writes the start of X-Forwarded-For; nginx appends the end.
    Only the entry nginx wrote is evidence."""
    request = _request(HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.5", REMOTE_ADDR="127.0.0.1")
    assert throttle.client_ip(request) == "203.0.113.5"


def test_without_a_proxy_the_socket_address_is_used():
    assert throttle.client_ip(_request(REMOTE_ADDR="192.0.2.8")) == "192.0.2.8"
