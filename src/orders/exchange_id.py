"""Recording a live order's exchange number by hand (Minor 3, wave H).

`Order.exchange_order_id` normally arrives from the order book, a postback's
read of it, or the trade book. The one case that leaves it unset: Console's
tradebook import finds two or more of a live day's orders open, of the same
symbol and side, with no exchange number and nothing else to tell them apart
(`tradebook._unmatched`) -- and the only thing left that can say which is
which is an operator reading Kite's order history or the contract note.

`Order` is not append-only, so the write itself is an ordinary UPDATE and
violates no invariant. What this module buys over a shell UPDATE is the
audit event the shell leaves no trace of.
"""

from django.db import transaction

from audit.events import record
from orders.models import Order, OrderMode


class RecordRefused(Exception):
    """This exchange number cannot be recorded, and the message says why."""


@transaction.atomic
def record_exchange_order_id(tag: str, exchange_id: str, *, actor: str) -> Order:
    """Set *tag*'s exchange number, audited, or refuse and say why.

    Refused for a blank number, a paper order (nothing at the exchange to
    number), one that already carries a number (never overwritten -- reverse
    the mistake by hand and ask again), and a number already recorded on
    another order (the two cannot both be it).
    """
    exchange_id = exchange_id.strip()
    if not exchange_id:
        raise RecordRefused("An exchange number is required.")

    try:
        order = Order.objects.select_for_update().get(tag=tag)
    except Order.DoesNotExist as exc:
        raise RecordRefused(f"No order is tagged {tag}.") from exc

    if order.mode == OrderMode.PAPER:
        raise RecordRefused(f"{tag} is a paper order; there is no exchange number to record.")

    if order.exchange_order_id:
        raise RecordRefused(
            f"{tag} already carries exchange number {order.exchange_order_id}; it is never "
            "overwritten."
        )

    clash = (
        Order.objects.exclude(pk=order.pk)
        .filter(exchange_order_id=exchange_id)
        .values_list("tag", flat=True)
        .first()
    )
    if clash:
        raise RecordRefused(f"Exchange number {exchange_id} is already recorded on {clash}.")

    before = {"exchange_order_id": order.exchange_order_id}
    order.exchange_order_id = exchange_id
    order.save(update_fields=["exchange_order_id"])

    record(
        "order.exchange_order_id_recorded",
        actor=actor,
        entity=order,
        before=before,
        after={"exchange_order_id": exchange_id},
    )
    return order
