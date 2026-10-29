"""Pausing and resuming, with a record of both.

The alternative -- not logging in -- looks like stopping and is not. The day
parks, the carry accrues, and the whole accumulated pile deploys the moment
somebody logs in, which turns a month of caution into one large purchase at
whatever the market happens to be doing that morning.

So a pause is explicit, audited, and suspends carry accrual: a paused day is
not a day the portfolio was owed money for. It can carry a resume date, for
the case it is usually wanted in -- away for three weeks, back on the 14th.
"""

import logging
from datetime import date

from django.db import transaction
from django.utils import timezone

from audit.context import current_trace_id, today_ist
from audit.events import record
from audit.logging import fields
from configuration.models import SystemPause

logger = logging.getLogger("steadystack.configuration")


class PauseError(Exception):
    """A pause or resume that does not make sense."""


def active_pause(*, on: date | None = None) -> SystemPause | None:
    """The pause in force, if any.

    A pause with a resume date in the past is over whether or not anybody
    came back to end it -- that is what the date is for.
    """
    on = on or today_ist()
    current = SystemPause.objects.filter(resumed_at__isnull=True).order_by("-paused_at").first()

    if current is None:
        return None
    if current.resume_on is not None and current.resume_on <= on:
        return None
    return current


def is_paused(*, on: date | None = None) -> bool:
    return active_pause(on=on) is not None


def require_running(what: str, *, on: date | None = None) -> None:
    pause = active_pause(on=on)
    if pause is None:
        return

    until = f" until {pause.resume_on}" if pause.resume_on else ""
    raise PauseError(f"{what} is refused: the system is paused{until} ({pause.reason}).")


@transaction.atomic
def pause(*, actor: str, reason: str, resume_on: date | None = None) -> SystemPause:
    if is_paused():
        raise PauseError("The system is already paused.")
    if resume_on is not None and resume_on <= today_ist():
        raise PauseError("A resume date in the past would end the pause before it began.")

    entry = SystemPause.objects.create(
        paused_by=actor,
        reason=reason[:255],
        resume_on=resume_on,
        trace_id=current_trace_id(),
    )

    record(
        "system.paused",
        actor=actor,
        entity=entry,
        after={"reason": reason, "resume_on": str(resume_on) if resume_on else None},
    )
    logger.warning(
        "system paused",
        extra=fields(actor=actor, reason=reason, resume_on=str(resume_on) if resume_on else None),
    )
    return entry


@transaction.atomic
def resume(*, actor: str, note: str = "") -> SystemPause:
    entry = SystemPause.objects.filter(resumed_at__isnull=True).order_by("-paused_at").first()
    if entry is None:
        raise PauseError("The system is not paused.")

    entry.resumed_at = timezone.now()
    entry.resumed_by = actor
    entry.save(update_fields=["resumed_at", "resumed_by"])

    record(
        "system.resumed",
        actor=actor,
        entity=entry,
        after={"note": note, "paused_for_days": (timezone.now() - entry.paused_at).days},
    )
    logger.warning("system resumed", extra=fields(actor=actor, note=note))
    return entry
