"""Trace and request identity, carried out of band.

Every log line, audit event and broker call belonging to a trading day carries
the same trace ID, `ts-YYYY-MM-DD` (M45). Reconstructing a day years later
means grepping one string rather than joining on timestamps and hoping.

Work that is not a trading session -- a lumpsum credited on a Sunday, a
configuration change, an external trade entered a week late -- carries the
trace ID of its own calendar date in the same form. "Every event carries one"
then holds without pretending the event belongs to a session that never ran.

Identity travels in contextvars rather than as an argument threaded through
every call, because the alternative is a `trace_id` parameter on functions
that have no other reason to know about tracing, and one forgotten hand-off
produces an untraceable row.
"""

import contextvars
import uuid
from datetime import date

from django.utils import timezone

TRACE_ID_PREFIX = "ts-"

#: Empty rather than None: these land in CharFields that are `blank=True`, and
#: a missing ID should read as absent, not as the string "None".
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("steadystack_trace_id", default="")
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "steadystack_request_id", default=""
)


def trace_id_for(day: date) -> str:
    """The trace ID for a calendar date. The only place the format is built."""
    return f"{TRACE_ID_PREFIX}{day:%Y-%m-%d}"


def today_ist() -> date:
    """Today in IST.

    TIME_ZONE is Asia/Kolkata, so `localdate` is the IST calendar date even on
    a UTC host. A trading day is a date, never an instant.
    """
    return timezone.localdate()


def current_trace_id() -> str:
    """The trace ID in force, falling back to today's.

    The fallback exists so that code reached outside a request and outside a
    scheduled task -- a shell, a data migration, a test -- still produces
    traceable rows instead of blank ones.
    """
    return _trace_id.get() or trace_id_for(today_ist())


def set_trace_id(value: str) -> contextvars.Token:
    return _trace_id.set(value)


def bind_trading_day(day: date) -> contextvars.Token:
    """Bind the trace ID for *day*. Returned token restores the previous one."""
    return _trace_id.set(trace_id_for(day))


def reset_trace_id(token: contextvars.Token) -> None:
    _trace_id.reset(token)


def current_request_id() -> str:
    return _request_id.get()


def new_request_id() -> str:
    """A short ID, unique enough to correlate the lines of one request."""
    return uuid.uuid4().hex[:12]


def set_request_id(value: str) -> contextvars.Token:
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)
