"""Filling paper orders against the live feed.

This is what M19c means in practice, and it is the reason there is no
simulator anywhere in this system. An open paper order is evaluated against
every tick that arrives on the real websocket: a paper buy fills when the
traded price reaches its limit or better, and a paper sell mirrors it.

What it writes is a `Fill` row like any other, in the same transaction that
creates the lot. So carry, FIFO consumption, the ledger, the day's committed
and unfilled totals and the state machine all run exactly as they will on the
first live day. The market does the simulating; nothing here invents a price.

The one thing it cannot model is queue position. A real limit order at the
touch may not fill even though the price traded there, because other orders
were ahead of it. Filling on price alone is therefore optimistic, and
deliberately so: it exercises the fill path, which is what it is for. It is
not a backtest and no conclusion about achievable execution should be drawn
from it.
"""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from orders.models import Fill, Order, OrderMode, OrderSide, OrderType
from orders.placement import apply_event
from orders.state import OrderEvent

logger = logging.getLogger("steadystack.orders")

#: Paper trade ids are prefixed so no query, report or reconciliation can
#: mistake one for a broker's.
PAPER_TRADE_PREFIX = "paper-"


@dataclass(frozen=True)
class PaperFill:
    order: Order
    fill: Fill
    lot_created: bool


def new_paper_trade_id() -> str:
    return f"{PAPER_TRADE_PREFIX}{uuid.uuid4().hex[:16]}"


def would_fill(order: Order, traded_price: Decimal) -> bool:
    """Whether this tick would have filled the order.

    A market order fills at whatever traded; a limit order fills only at its
    price or better. "Better" is lower for a buy and higher for a sell.
    """
    if order.is_terminal or order.unfilled_quantity <= 0:
        return False

    if order.order_type == OrderType.MARKET:
        return traded_price > 0

    if order.limit_price is None:
        return False

    if order.side == OrderSide.BUY:
        return Decimal(traded_price) <= order.limit_price
    return Decimal(traded_price) >= order.limit_price


def fill_price_for(order: Order, traded_price: Decimal) -> Decimal:
    """What the order would have paid.

    A limit order never pays worse than its limit, so a buy fills at the
    traded price when that is lower and at its limit otherwise. A market order
    takes the traded price, which is why the fallback can overspend and why
    the engine's carry may go negative until the next allocation repays it (D7).
    """
    traded_price = Decimal(traded_price)

    if order.order_type == OrderType.MARKET or order.limit_price is None:
        return traded_price

    if order.side == OrderSide.BUY:
        return min(traded_price, order.limit_price)
    return max(traded_price, order.limit_price)


@transaction.atomic
def apply_tick(
    *,
    instrument_id: str,
    traded_price: Decimal,
    at=None,
) -> list[PaperFill]:
    """Fill whatever this tick would have filled.

    Locked for update, because the websocket thread and a scheduled task can
    both be looking at the same orders, and two fills of one order would buy
    the shares twice.
    """
    at = at or timezone.now()
    filled: list[PaperFill] = []

    candidates = (
        Order.objects.select_for_update()
        .filter(
            instrument_id=instrument_id,
            mode=OrderMode.PAPER,
        )
        .exclude(state__in=["COMPLETE", "CANCELLED", "REJECTED"])
    )

    for order in candidates:
        if not would_fill(order, traded_price):
            continue
        filled.append(_fill(order, price=fill_price_for(order, traded_price), at=at))

    return filled


def _fill(order: Order, *, price: Decimal, at) -> PaperFill:
    """Write the fill, move the order, and book it against the lots."""
    from orders import booking

    quantity = order.unfilled_quantity

    fill = Fill.objects.create(
        order=order,
        kite_trade_id=new_paper_trade_id(),
        quantity=quantity,
        price=price,
        executed_at=at,
        trace_id=order.trace_id or current_trace_id(),
    )

    order.filled_quantity += quantity
    order.save(update_fields=["filled_quantity", "updated_at"])

    apply_event(order, OrderEvent.FILLED, detail="paper fill against the live feed")

    booked = booking.book(fill)

    record(
        "fill.recorded",
        entity=fill,
        after={
            "tag": order.tag,
            "mode": order.mode,
            "quantity": quantity,
            "price": str(price),
            "instrument": order.instrument_id,
        },
    )
    logger.info(
        "paper order filled against the live feed",
        extra=fields(
            tag=order.tag,
            instrument=order.instrument_id,
            quantity=quantity,
            price=str(price),
        ),
    )
    return PaperFill(order=order, fill=fill, lot_created=booked.lot is not None)


def open_paper_orders(instrument_id: str | None = None):
    """Orders the tick feed should be watched for."""
    orders = Order.objects.filter(mode=OrderMode.PAPER).exclude(
        state__in=["COMPLETE", "CANCELLED", "REJECTED"]
    )
    if instrument_id:
        orders = orders.filter(instrument_id=instrument_id)
    return orders


def instruments_to_watch() -> list[int]:
    """The instrument tokens the ticker should subscribe to.

    Only what has an open paper order: subscribing to the whole universe would
    work and would also mean every tick of every name walking a query that can
    only ever match a handful of rows.
    """
    return list(
        open_paper_orders().values_list("instrument__instrument_token", flat=True).distinct()
    )
