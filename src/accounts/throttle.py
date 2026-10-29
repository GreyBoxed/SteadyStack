"""Login throttling, counted from the audit trail.

Failed logins are audited anyway -- they are exactly the kind of event this
system exists to keep a record of -- so the throttle counts those rows rather
than introducing a cache or a table of its own. Three consequences, all of
them wanted:

  * the lockout survives a restart, where a local-memory cache would not;
  * the evidence for a lockout is the same append-only trail everything else
    is explained from;
  * there is one fewer moving part to provision on the box in Phase 4.

The cost is a COUNT or two per login attempt, which at one operator is nothing.

Failures are counted per account *and* address, with a looser ceiling per
address across every account. Counting per account alone let anybody who
knew the username lock the operator out from anywhere, for the price of five
wrong guesses; counting per address alone would let one address spray every
username it could think of. The pair closes both.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.http import HttpRequest
from django.utils import timezone

from audit.events import record
from audit.models import AuditEvent

#: Generous enough to survive a mistyped token, tight enough that an attacker
#: with the password gets very few guesses at a six-digit code.
MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)

#: Failures from one address, whatever usernames it tried. Looser than the
#: per-account limit so a mistyped username or two never locks the operator's
#: own address, tight enough that a spray across names is stopped early.
MAX_FAILURES_PER_ADDRESS = 20

ACTION_FAILED = "login.failed"
ACTION_SUCCEEDED = "login.succeeded"
ACTION_THROTTLED = "login.throttled"

#: An unknown username is still a subject -- an attacker guessing usernames
#: must be throttled on what they typed, not excused because it matched nobody.
UNKNOWN_SUBJECT = "(unknown)"


@dataclass(frozen=True)
class Throttle:
    """How many attempts remain for a subject, and when the window clears."""

    failures: int
    locked: bool
    retry_after: timedelta | None


def client_ip(request: HttpRequest) -> str:
    """The caller's address, as nginx saw it.

    nginx is the only thing that can reach gunicorn, and it sets X-Real-IP to
    the address of the connection it accepted. X-Forwarded-For is only
    consulted without it, and then only its *last* entry: the client writes
    whatever it likes at the start of that header and the proxy appends the
    end, so the first entry is the one an attacker chooses. The address keys
    the lockout, so it must be one the caller cannot pick.
    """
    real = request.META.get("HTTP_X_REAL_IP", "").strip()
    if real:
        return real
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.META.get("REMOTE_ADDR", "")


def subject_of(username: str) -> str:
    return (username or "").strip().lower() or UNKNOWN_SUBJECT


def _failures_since(since, **match):
    return AuditEvent.objects.filter(
        action=ACTION_FAILED,
        entity_type="accounts.Login",
        occurred_at__gte=since,
        **match,
    ).order_by("-occurred_at")


def _judge(failures, limit: int, now) -> Throttle:
    count = failures.count()
    if count < limit:
        return Throttle(failures=count, locked=False, retry_after=None)

    # The window clears relative to the oldest failure still counted, so a
    # locked-out subject is not punished indefinitely by one early attempt.
    oldest_counted = failures[limit - 1]
    retry_after = (oldest_counted.occurred_at + WINDOW) - now
    return Throttle(failures=count, locked=True, retry_after=max(retry_after, timedelta(0)))


def status(username: str, ip: str) -> Throttle:
    """Whether *username* may attempt a login from *ip* right now.

    The failures reported are the account's from this address, which is what
    "attempts left" counts down; the address's own ceiling only ever adds a
    lock, and the longer wait of the two is the one given.
    """
    now = timezone.now()
    since = now - WINDOW
    account = _judge(
        _failures_since(since, entity_id=subject_of(username), after__ip=ip), MAX_FAILURES, now
    )
    address = _judge(_failures_since(since, after__ip=ip), MAX_FAILURES_PER_ADDRESS, now)
    if not address.locked:
        return account
    waits = [t.retry_after for t in (account, address) if t.retry_after is not None]
    return Throttle(failures=account.failures, locked=True, retry_after=max(waits))


def note_failure(username: str, request: HttpRequest) -> None:
    record(
        ACTION_FAILED,
        actor=subject_of(username),
        entity_type="accounts.Login",
        entity_id=subject_of(username),
        after={"ip": client_ip(request)},
    )


def note_success(username: str, request: HttpRequest) -> None:
    record(
        ACTION_SUCCEEDED,
        actor=subject_of(username),
        entity_type="accounts.Login",
        entity_id=subject_of(username),
        after={"ip": client_ip(request)},
    )


def note_throttled(username: str, request: HttpRequest, throttle: Throttle) -> None:
    record(
        ACTION_THROTTLED,
        actor=subject_of(username),
        entity_type="accounts.Login",
        entity_id=subject_of(username),
        after={
            "ip": client_ip(request),
            "failures": throttle.failures,
            "retry_after_seconds": int((throttle.retry_after or timedelta(0)).total_seconds()),
        },
    )
