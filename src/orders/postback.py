"""Kite's order postbacks.

The handler is a **trigger, not an applier** (D8). A postback says "something
changed about this order"; it does not say what the order is now. So the
handler verifies the checksum, takes a row lock, and re-reads the
authoritative state from the broker, reconciling to absolutes rather than
applying a delta.

That is not caution for its own sake. Postbacks arrive twice, arrive out of
order, and arrive before the placement response that created the local row has
been written. A handler that applied what it was given would double-count a
duplicate and go backwards on a reordered pair; one that re-reads cannot.

An unmatched postback is queued rather than dropped, because the common reason
for one is that the placement response has not landed yet -- the order exists
at the exchange and not yet here. Dropping it would lose the only notice this
system gets that something happened.

A fill halts nothing. It reduces that order's unfilled quantity and with it
the day's committed and unfilled totals (M27), so the next tick sizes its
slice against the true remainder. With several orders resting at once the
totals that matter are the day's, never one order's.
"""

import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from audit.events import record
from audit.logging import fields

logger = logging.getLogger("steadystack.orders")


class InvalidPostback(Exception):
    """A postback that did not prove it came from Kite."""


@dataclass(frozen=True)
class PostbackOutcome:
    matched: bool
    order_tag: str = ""
    queued: bool = False
    detail: str = ""


def expected_checksum(*, order_id: str, order_timestamp: str, api_secret: str) -> str:
    """Kite's postback checksum: sha256(order_id + order_timestamp + api_secret)."""
    payload = f"{order_id}{order_timestamp}{api_secret}".encode()
    return hashlib.sha256(payload).hexdigest()


def verify(payload: dict, *, api_secret: str | None = None) -> None:
    """Refuse anything that cannot prove it came from Kite.

    The endpoint is public -- postbacks are inbound, so no IP restriction
    applies to them -- which makes this the only thing standing between the
    order book and whatever else finds the URL.
    """
    secret = api_secret if api_secret is not None else settings.KITE_API_SECRET

    order_id = str(payload.get("order_id") or "")
    order_timestamp = str(payload.get("order_timestamp") or "")
    checksum = str(payload.get("checksum") or "")

    if not (order_id and order_timestamp and checksum):
        raise InvalidPostback("The postback is missing the fields its checksum is built from.")

    if not secret:
        raise InvalidPostback("No API secret is configured, so no postback can be verified.")

    expected = expected_checksum(
        order_id=order_id, order_timestamp=order_timestamp, api_secret=secret
    )

    # Constant-time: the checksum is the only authentication this endpoint has.
    if not _matches(expected, checksum):
        raise InvalidPostback("The postback checksum does not match.")


def _matches(expected: str, given: str) -> bool:
    return hmac.compare_digest(expected, given)


@transaction.atomic
def handle(payload: dict, *, broker, api_secret: str | None = None) -> PostbackOutcome:
    """Verify, then go and read what is actually true.

    The payload's own `status` and `filled_quantity` are deliberately not
    used: they are the trigger, not the answer.
    """
    from orders.models import Order
    from orders.reconcile import from_broker

    verify(payload, api_secret=api_secret)

    tag = str(payload.get("tag") or "")
    order_id = str(payload.get("order_id") or "")

    order = _find(Order, tag=tag, order_id=order_id)

    if order is None:
        queued = _queue(payload)
        logger.warning(
            "postback for an order this system does not have yet",
            extra=fields(order_id=order_id, tag=tag),
        )
        return PostbackOutcome(matched=False, order_tag=tag, queued=queued, detail="queued")

    # Lock the row, then ask the broker what is true. The postback told us to
    # look; it did not tell us what we would find.
    Order.objects.select_for_update().filter(pk=order.pk).exists()
    from_broker(trading_date=order.trading_date, broker=broker, stream=order.stream)

    record(
        "postback.handled",
        entity=order,
        after={"order_id": order_id, "tag": tag, "status_hint": payload.get("status")},
    )
    return PostbackOutcome(matched=True, order_tag=order.tag)


def _find(order_model, *, tag: str, order_id: str):
    if tag:
        found = order_model.objects.filter(tag=tag).first()
        if found is not None:
            return found
    if order_id:
        return order_model.objects.filter(kite_order_id=order_id).first()
    return None


def _queue(payload: dict) -> bool:
    """Keep an unmatched postback for the reconciliation pass to pick up."""
    from orders.models import QueuedPostback

    order_id = str(payload.get("order_id") or "")
    QueuedPostback.objects.get_or_create(
        order_id=order_id,
        received_at_key=str(payload.get("order_timestamp") or uuid.uuid4().hex),
        defaults={"payload": payload},
    )
    return True


def drain_queue(*, broker) -> int:
    """Try the queued postbacks again.

    Run after placement responses have landed: the usual reason a postback was
    unmatched is that it overtook the response that created the local row.
    """
    from orders.models import Order, QueuedPostback

    resolved = 0
    for queued in QueuedPostback.objects.filter(resolved_at__isnull=True):
        payload = queued.payload
        order = _find(
            Order,
            tag=str(payload.get("tag") or ""),
            order_id=str(payload.get("order_id") or ""),
        )
        if order is None:
            continue

        from orders.reconcile import from_broker

        from_broker(trading_date=order.trading_date, broker=broker, stream=order.stream)
        queued.resolved_at = timezone.now()
        queued.save(update_fields=["resolved_at"])
        resolved += 1

    if resolved:
        logger.info("queued postbacks resolved", extra=fields(resolved=resolved))

    return resolved


def unresolved_count() -> int:
    from orders.models import QueuedPostback

    return QueuedPostback.objects.filter(resolved_at__isnull=True).count()


def stale_unresolved(older_than_minutes: int = 30) -> models.QuerySet:
    """Postbacks that never found an order.

    One that is still unmatched half an hour later is not a race any more --
    it is an order this system does not know about, which is exactly the
    alarm worth raising.
    """
    from orders.models import QueuedPostback

    cutoff = timezone.now() - timedelta(minutes=older_than_minutes)
    return QueuedPostback.objects.filter(resolved_at__isnull=True, received_at__lt=cutoff)
