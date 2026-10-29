"""Authorizing a day to trade (M15f).

Planning is automatic: the 09:45 timer, the Generate button and Regenerate all
write a tradesheet without asking. Placing one is not. The ladder and the
15:00 fallback place nothing for the daily buy until a person has authorized
that version of the day, so the first order of any day follows a decision
somebody made while looking at the plan.
"""

import logging

from django.db import transaction
from django.utils import timezone

from audit.events import record
from audit.logging import fields
from engine.models import TradeSheet, TradeSheetState

logger = logging.getLogger("steadystack.engine")


class AuthorizationRefused(Exception):
    """This sheet cannot be authorized, and the message says why."""


def why_not(sheet: TradeSheet) -> str | None:
    """Why this sheet cannot be authorized now, or None if it can."""
    from audit.context import today_ist
    from configuration.pause import active_pause

    paused = active_pause()
    if paused is not None:
        # A sheet planned before the stop is still sitting there, and the
        # ladder would refuse it anyway -- but a button that looks live and
        # does nothing is worse than one that says why.
        return f"Trading is stopped ({paused.reason}). Resume before authorizing a day."
    if sheet.is_authorized:
        return f"v{sheet.version} was authorized by {sheet.authorized_by}."
    if sheet.trading_date != today_ist():
        return "Only today's tradesheet can be authorized."
    latest = (
        TradeSheet.objects.filter(trading_date=sheet.trading_date)
        .order_by("-version")
        .values_list("pk", flat=True)
        .first()
    )
    if latest != sheet.pk:
        return "A later version exists; authorize that one."
    if sheet.state == TradeSheetState.SKIPPED or not sheet.quantity:
        return "This version buys nothing, so there is nothing to authorize."
    return None


@transaction.atomic
def authorize(sheet: TradeSheet, *, actor: str) -> TradeSheet:
    """Let this version of the day place orders."""
    locked = TradeSheet.objects.select_for_update().get(pk=sheet.pk)
    reason = why_not(locked)
    if reason:
        raise AuthorizationRefused(reason)

    locked.authorized_at = timezone.now()
    locked.authorized_by = actor
    locked.save(update_fields=["authorized_at", "authorized_by"])

    record(
        "tradesheet.authorized",
        actor=actor,
        entity=locked,
        after={
            "version": locked.version,
            "names": locked.bought_quantities(),
            "planned_cost": str(locked.planned_cost),
        },
    )
    logger.info(
        "tradesheet authorized",
        extra=fields(trading_date=str(locked.trading_date), version=locked.version, actor=actor),
    )
    return locked
