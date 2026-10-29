"""The daily login, and knowing whether there is one.

The operator's only recurring act. Everything here exists so that the answer
to "can the system trade this morning?" is a fact on a screen rather than
something discovered at 09:45 when the day fails to buy.

On the expiry: a Kite access token is assumed to last until early the next
morning, which is what the 09:00 check, the park and the resume all take for
granted. The assumption is written down in one place -- `EXPIRY_TIME` -- and
is confirmed against the real broker in Phase 4 rather than being scattered
through the code as a guess in several forms.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.db import transaction
from django.utils import timezone

from audit.events import record
from audit.logging import fields
from broker.base import Broker
from broker.exceptions import BrokerError
from broker.models import KiteLoginState, KiteSession

logger = logging.getLogger("steadystack.broker")

#: Assumed daily expiry, in IST. Confirmed empirically in Phase 4; until then
#: an early hour is the safe assumption, because treating a dead token as live
#: costs a trading day and treating a live one as dead costs a login.
EXPIRY_TIME = time(6, 0)

#: How long a started login may take to come back. Zerodha's page, a password
#: and a TOTP fit in a minute; ten allows for fetching the phone.
LOGIN_STATE_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class SessionStatus:
    """What the home page shows, and what the 09:00 check asserts."""

    session: KiteSession | None
    valid: bool
    reason: str = ""

    @property
    def expires_at(self):
        return self.session.expires_at if self.session else None

    @property
    def kite_user_id(self) -> str:
        return self.session.kite_user_id if self.session else ""

    def __bool__(self) -> bool:
        return self.valid


def next_expiry(now: datetime | None = None) -> datetime:
    """When a token established now would stop working.

    The next occurrence of the expiry hour. A login at 09:30 lasts until
    tomorrow morning; a login at 05:00 lasts an hour, which is correct and is
    exactly why the status shows the time rather than a yes or no.
    """
    now = now or timezone.localtime()
    today = timezone.make_aware(
        datetime.combine(timezone.localdate(now), EXPIRY_TIME),
        timezone.get_current_timezone(),
    )
    return today if today > now else today + timedelta(days=1)


def current() -> KiteSession | None:
    return KiteSession.objects.filter(revoked_at__isnull=True).order_by("-created_at").first()


def status(*, api_key: str | None = None) -> SessionStatus:
    """Whether the system can talk to Kite right now (M4).

    The API key is checked as well as the expiry. A token belongs to the app
    that issued it, so changing the key -- a new app, a rotated secret --
    leaves every existing token worthless while its stored expiry still says
    tomorrow morning. Reporting that as a valid session sends the day out to
    discover it at 09:45.
    """
    from django.conf import settings

    session = current()
    expected_key = api_key if api_key is not None else settings.KITE_API_KEY

    if session is None:
        return SessionStatus(None, False, "no session has been established")
    if session.expires_at <= timezone.now():
        return SessionStatus(session, False, "the session has expired")
    if session.api_key and expected_key and session.api_key != expected_key:
        return SessionStatus(
            session,
            False,
            "the session was issued for a different API key, so its token is dead",
        )

    return SessionStatus(session, True)


def login_url(broker: Broker, *, state: str = "") -> str:
    """Where the operator goes to start the daily handshake (M5).

    `state` rides in Kite's `redirect_params`: an URL-encoded query string
    Kite appends, decoded, to the callback it redirects to.
    """
    url = broker.login_url()
    if state:
        url += "&" + urlencode({"redirect_params": urlencode({"state": state})})
    return url


def _digest(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def mint_login_state(*, started_by: str) -> str:
    """A fresh state for one login, good for `LOGIN_STATE_TTL` and one use."""
    state = secrets.token_urlsafe(32)
    KiteLoginState.objects.create(
        digest=_digest(state),
        expires_at=timezone.now() + LOGIN_STATE_TTL,
        started_by=started_by,
    )
    return state


def consume_login_state(state: str) -> bool:
    """Whether *state* was issued here, is unexpired and unused -- and use it.

    One conditional UPDATE, so two callbacks racing with the same state cannot
    both see it unused.
    """
    if not state:
        return False
    now = timezone.now()
    return bool(
        KiteLoginState.objects.filter(
            digest=_digest(state), used_at__isnull=True, expires_at__gt=now
        ).update(used_at=now)
    )


class WrongAccount(BrokerError):
    """Kite signed in an account other than the one this system trades."""


def _check_account(user_id: str) -> None:
    """Refuse a session for any account but `settings.KITE_USER_ID`.

    A request token says which Zerodha account signed in, not which one was
    meant to. Without this, whoever completes a login decides where the next
    order goes.
    """
    from django.conf import settings

    expected = (getattr(settings, "KITE_USER_ID", "") or "").strip().upper()
    if not expected:
        logger.warning(
            "KITE_USER_ID is not set, so the session is stored for whichever account signed in",
            extra=fields(kite_user_id=user_id),
        )
        return
    if user_id.strip().upper() != expected:
        raise WrongAccount(
            f"Kite signed in {user_id or 'an unnamed account'}, not the account this "
            f"system trades ({expected}). The session was not stored.",
            call="session.generate",
        )


@transaction.atomic
def establish(
    request_token: str, *, broker: Broker, established_by: str = "operator"
) -> KiteSession:
    """Exchange a request token for an access token and store it.

    The token is never logged, never rendered and never returned from here --
    `broker/redaction.py` covers the call that carries it, and this function
    hands back the row rather than the secret.
    """
    payload = broker.create_session(request_token)

    access_token = payload.get("access_token")
    if not access_token:
        raise BrokerError(
            "Kite returned a session without an access token.", call="session.generate"
        )

    # Before anything is revoked: a refused login leaves the live one alone.
    _check_account(payload.get("user_id", "") or "")

    # Previous sessions are revoked rather than deleted: which token was live
    # when an order was placed is part of the day's explanation.
    KiteSession.objects.filter(revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoked_reason="replaced by a new login"
    )

    session = KiteSession(
        expires_at=next_expiry(),
        kite_user_id=payload.get("user_id", "") or "",
        user_name=payload.get("user_name", "") or "",
        api_key=getattr(broker, "api_key", "") or "",
        established_by=established_by,
    )
    session.set_access_token(access_token)
    session.save()

    record(
        "kite_session.established",
        actor=established_by,
        entity=session,
        after={
            "kite_user_id": session.kite_user_id,
            "expires_at": session.expires_at.isoformat(),
        },
    )
    logger.info(
        "kite session established",
        extra=fields(kite_user_id=session.kite_user_id, expires_at=session.expires_at.isoformat()),
    )
    return session


def revoke(*, actor: str, reason: str) -> int:
    """End the live session. Used when Kite says a token is dead (D8)."""
    revoked = KiteSession.objects.filter(revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoked_reason=reason[:255]
    )
    if revoked:
        record(
            "kite_session.revoked",
            actor=actor,
            entity_type="broker.KiteSession",
            after={"reason": reason, "sessions": revoked},
        )
        logger.warning("kite session revoked", extra=fields(reason=reason))
    return revoked


def attach(broker: Broker) -> bool:
    """Give *broker* the stored token, if there is a live one.

    Returns whether it had one. Callers that need a session check the return
    rather than discovering the problem in a broker exception.
    """
    state = status()
    if not state.valid:
        return False

    broker.set_access_token(state.session.access_token)
    return True
