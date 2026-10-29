"""Site-wide OTP enforcement.

django-otp ships OTPMiddleware, which populates request.user.is_verified() and
enforces nothing, and an otp_required decorator, which enforces per view. This
project needs the inverse default: everything is protected unless it appears in
the allowlist below, so a view added next year is safe because nobody exempted
it rather than because its author remembered a decorator.
"""

from collections.abc import Callable

from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse

#: Paths reachable without an OTP-verified session. Matched exactly, never by
#: prefix -- opening /health/ must not also open /healthy-secrets/.
EXEMPT_PATHS = frozenset(
    {
        "/accounts/login/",
        "/accounts/logout/",
        # The phone's sign-in, and the switch between phone and desk pages,
        # which sets a cookie and nothing else.
        "/m/login/",
        "/m/switch/",
        # The daily Kite token handshake returns here. Behind the OTP gate it
        # would deadlock: completing the handshake needs a session that the
        # handshake is what establishes.
        "/broker/kite/callback/",
        # Kite's servers post here, not the operator's browser: there is no
        # session to verify, and the checksum is what authenticates it.
        "/orders/kite/postback/",
        "/health/",
    }
)


class OTPEnforcementMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path in EXEMPT_PATHS:
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.is_verified():
            return self.get_response(request)

        return redirect_to_login(request.get_full_path())
