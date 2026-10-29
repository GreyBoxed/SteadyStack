"""The daily Kite login: one click out, one redirect back.

The callback is the single path through the OTP gate (see
`accounts.middleware`). It has to be, because completing the handshake
requires a session that the handshake is what establishes -- and a test in
that module asserts the exemption is still there.

Being outside the gate, it is treated as hostile input. A request token is
worthless without the API secret, which never leaves the server -- but it is
not worthless to its owner: anybody can sign in to Zerodha as themselves and
hand the operator the callback link. So two things must hold before a token is
exchanged and stored: the callback carries a state this system issued when the
operator started the login (single use, ten minutes), and the account Kite
signed in is the one this system trades (`session.establish`). What the
endpoint will not do is say anything useful to somebody who arrives with a
guess.
"""

import logging

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET, require_POST

from audit.events import record
from audit.logging import fields
from broker import get_broker, session
from broker.exceptions import BrokerError

logger = logging.getLogger("steadystack.broker")


@require_POST
def start_login(request: HttpRequest) -> HttpResponse:
    """Send the operator to Kite. A POST, so no link can trigger it.

    Behind the OTP gate, so only a verified operator can mint the state the
    callback will insist on.
    """
    state = session.mint_login_state(started_by=request.user.get_username())
    return redirect(session.login_url(get_broker(), state=state))


@require_GET
def callback(request: HttpRequest) -> HttpResponse:
    """Where Kite returns the request token (M5).

    Reachable without an OTP-verified session, so it says as little as
    possible and stores nothing it was not asked for.
    """
    request_token = request.GET.get("request_token", "")
    status = request.GET.get("status", "")

    if status and status != "success":
        record(
            "kite_session.login_failed",
            entity_type="broker.KiteSession",
            after={"status": status},
        )
        messages.error(request, "Kite refused the login. Try again.")
        return redirect("web:home")

    if not request_token:
        # Somebody arrived at this URL by hand, or the redirect was malformed.
        logger.warning("kite callback without a request token")
        messages.error(request, "That login did not complete. Start it again.")
        return redirect("web:home")

    if not session.consume_login_state(request.GET.get("state", "")):
        # Not a login this system started, or one already used or too old.
        # The token is not exchanged: exchanging it is what would store it.
        record(
            "kite_session.login_refused",
            entity_type="broker.KiteSession",
            after={"reason": "no valid state"},
        )
        logger.warning("kite callback without a valid state")
        messages.error(request, "That login was not started here, or has expired. Start it again.")
        return redirect("web:home")

    actor = request.user.get_username() if request.user.is_authenticated else "operator"
    try:
        established = session.establish(request_token, broker=get_broker(), established_by=actor)
    except session.WrongAccount as exc:
        record(
            "kite_session.login_refused",
            entity_type="broker.KiteSession",
            after={"reason": str(exc)},
        )
        logger.warning("kite login for the wrong account", extra=fields(error=str(exc)))
        messages.error(request, str(exc))
        return redirect("web:home")
    except BrokerError as exc:
        # The message is the broker's and may name the failure; the token it
        # was carrying is never part of it.
        logger.warning("kite session could not be established", extra=fields(error=str(exc)))
        messages.error(request, "Kite would not complete the login. Try again.")
        return redirect("web:home")

    messages.success(
        request,
        f"Signed in to Kite as {established.kite_user_id}. "
        f"The session lasts until {established.expires_at:%H:%M on %d %b}.",
    )
    _plan_the_waiting_day(request, actor=actor)
    return redirect("web:home")


def _plan_the_waiting_day(request: HttpRequest, *, actor: str) -> None:
    """Plan today if the login is what it was waiting for (M7).

    The 09:45 plan reads holdings first, so without a session it writes no
    sheet at all. A trading day, in session, with no sheet yet is exactly that
    day, and signing in is the moment it can be planned. The plan still needs
    authorizing before anything is placed (M15f).

    Nothing here may undo the login: the session is stored and committed, and
    a plan that fails is the operator's to retry with Generate.
    """
    try:
        from audit.context import today_ist
        from engine import planning
        from engine.models import TradeSheet
        from trading_calendar import sessions

        today = today_ist()
        if not sessions.is_trading_day(today) or planning.market_closed_reason(today):
            return
        if TradeSheet.objects.filter(trading_date=today).exists():
            return
        sheet = planning.plan_from_broker(trading_date=today, actor=actor)
    except Exception as exc:  # the login stands whatever planning does
        logger.warning("late login could not plan the day", extra=fields(error=str(exc)))
        messages.warning(request, f"Signed in, but today could not be planned: {exc}")
        return

    messages.info(
        request,
        f"Today's tradesheet is v{sheet.version}: {sheet.names_label or 'nothing to buy'}. "
        "Authorize it to start buying.",
    )
