"""Send a phone to the phone screens, and everything else to the desk.

The phone surface is five screens, not a copy of the site, so only the pages
that have a phone counterpart are swapped: Today, sign-in and the Kite
session. Everything else -- ledgers, holdings, a past day -- opens as it is on
any device, because a phone that follows a link into the records should get
the records rather than be bounced to Today.

The guess is the user agent's "Mobi" token, which every phone browser sends
and tablets do not: a tablet has the room for the desk pages. A person can
overrule the guess either way, and the choice is kept in a cookie
(`SITE_COOKIE`) until they change it back.
"""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect

SITE_COOKIE = "site"
PHONE, DESKTOP = "phone", "desktop"

#: Desk page -> its phone counterpart. Exact paths, never prefixes.
TO_PHONE = {
    "/": "/m/",
    "/accounts/login/": "/m/login/",
    "/broker/session/": "/m/kite/",
}
#: Phone page -> where the same question is answered at a desk.
TO_DESKTOP = {
    "/m/": "/",
    "/m/login/": "/accounts/login/",
    "/m/trade/": "/",
    "/m/stop/": "/",
    "/m/kite/": "/broker/session/",
}


def is_phone(request: HttpRequest) -> bool:
    return "Mobi" in request.headers.get("User-Agent", "")


def wants(request: HttpRequest) -> str:
    """Which surface this request should see: the cookie, else the device."""
    chosen = request.COOKIES.get(SITE_COOKIE)
    if chosen in (PHONE, DESKTOP):
        return chosen
    return PHONE if is_phone(request) else DESKTOP


class DeviceRedirectMiddleware:
    """Redirect a GET to the surface the device should see.

    Only GET and HEAD: a form posted from one surface is answered where it was
    posted, never bounced mid-decision.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method in ("GET", "HEAD"):
            table = TO_PHONE if wants(request) == PHONE else TO_DESKTOP
            target = table.get(request.path)
            if target is not None:
                query = request.META.get("QUERY_STRING", "")
                return HttpResponseRedirect(f"{target}?{query}" if query else target)
        return self.get_response(request)
