"""OTP enforcement.

django-otp's OTPMiddleware populates request.user.is_verified() and enforces
nothing. Enforcement is ours, and it denies by default: a view added later is
protected because it was not named, not because its author remembered.
"""

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from accounts.middleware import OTPEnforcementMiddleware


class FakeUser:
    def __init__(self, *, authenticated: bool, verified: bool) -> None:
        self.is_authenticated = authenticated
        self._verified = verified

    def is_verified(self) -> bool:
        return self._verified


def build(path: str, user: FakeUser):
    request = RequestFactory().get(path)
    request.user = user
    return request


def run(request):
    reached = {"view": False}

    def get_response(_req):
        reached["view"] = True
        return HttpResponse("ok")

    response = OTPEnforcementMiddleware(get_response)(request)
    return response, reached["view"]


ANONYMOUS = FakeUser(authenticated=False, verified=False)
UNVERIFIED = FakeUser(authenticated=True, verified=False)
VERIFIED = FakeUser(authenticated=True, verified=True)


def test_verified_user_reaches_a_protected_view():
    _response, reached = run(build("/portfolio/", VERIFIED))
    assert reached is True


def test_unverified_user_is_redirected_away_from_a_protected_view():
    response, reached = run(build("/portfolio/", UNVERIFIED))
    assert reached is False
    assert response.status_code == 302


def test_anonymous_user_is_redirected_away_from_a_protected_view():
    response, reached = run(build("/portfolio/", ANONYMOUS))
    assert reached is False
    assert response.status_code == 302


def test_unknown_paths_are_protected_by_default():
    """A view added later must be protected because nobody exempted it."""
    response, reached = run(build("/some/route/invented/tomorrow/", UNVERIFIED))
    assert reached is False
    assert response.status_code == 302


def test_login_page_is_reachable_anonymously():
    """Otherwise the redirect target redirects, forever."""
    _response, reached = run(build("/accounts/login/", ANONYMOUS))
    assert reached is True


@pytest.mark.parametrize("user", [ANONYMOUS, UNVERIFIED, VERIFIED])
def test_kite_oauth_redirect_is_always_reachable(user):
    """The daily Kite token handshake returns to this endpoint.

    If it sits behind the OTP gate, the handshake deadlocks: completing it
    requires a session the handshake itself is needed to establish. This test
    is the reason the allowlist exists.
    """
    _response, reached = run(build("/broker/kite/callback/", user))
    assert reached is True


def test_healthcheck_is_reachable_anonymously():
    _response, reached = run(build("/health/", ANONYMOUS))
    assert reached is True


def test_allowlist_does_not_match_on_prefix():
    """/health/ being open must not open /healthy-secrets/."""
    response, reached = run(build("/healthy-secrets/", ANONYMOUS))
    assert reached is False
    assert response.status_code == 302
