"""Placement: the one place that decides whether an order is sent.

A paper order is not a discarded order. It is written with the same price, the
same quantity, the same tick, the same depth snapshot, into the same tables,
through the same state machine, raising the same audit events -- marked paper,
and simply never handed to Kite (M19a). It then fills against the live tick
feed like any other order, because the market data is real in every
environment (M19c).

That is why there is no simulator here and no fake broker anywhere: the only
difference between a paper day and a live day is one call that is not made.
Everything either side of it is the code that will run with real money.

The caller owns the rows. This module owns the decision and the identity the
rows are keyed by.
"""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from audit.logging import fields
from broker import mode
from broker.base import Broker
from configuration.models import OrderMode

logger = logging.getLogger("steadystack.broker")

#: Paper identifiers are prefixed so that no query, log line or report can
#: mistake one for a broker order id -- including a report written years from
#: now by somebody who has never heard of paper mode.
PAPER_ID_PREFIX = "paper-"


@dataclass(frozen=True)
class PlacementRequest:
    tradingsymbol: str
    exchange: str
    transaction_type: str
    quantity: int
    order_type: str
    price: Decimal | None = None
    tag: str = ""
    market_protection: Decimal | None = None


@dataclass(frozen=True)
class Placement:
    """What came back, and whether anyone outside this process knows about it."""

    order_id: str
    mode: str
    sent: bool

    @property
    def is_paper(self) -> bool:
        return self.mode == OrderMode.PAPER


def new_paper_order_id() -> str:
    return f"{PAPER_ID_PREFIX}{uuid.uuid4().hex[:16]}"


def is_paper_order_id(order_id: str) -> bool:
    return str(order_id).startswith(PAPER_ID_PREFIX)


def place(request: PlacementRequest, *, broker: Broker) -> Placement:
    """Send the order, or record it as paper.

    `mode.should_send_to_broker` raises rather than returning False when the
    mode is live but the conditions for live do not hold. The distinction
    matters: an operator who believes they are live must never be quietly
    downgraded to paper, because the portfolio would stop growing and every
    screen would still look right.
    """
    if not mode.should_send_to_broker():
        placement = Placement(order_id=new_paper_order_id(), mode=OrderMode.PAPER, sent=False)
        logger.info(
            "paper order recorded",
            extra=fields(
                order_id=placement.order_id,
                tradingsymbol=request.tradingsymbol,
                quantity=request.quantity,
                price=str(request.price) if request.price is not None else None,
                order_type=request.order_type,
            ),
        )
        return placement

    order_id = broker.place_order(
        tradingsymbol=request.tradingsymbol,
        exchange=request.exchange,
        transaction_type=request.transaction_type,
        quantity=request.quantity,
        order_type=request.order_type,
        price=request.price,
        tag=request.tag,
        market_protection=request.market_protection,
    )
    logger.info(
        "live order placed",
        extra=fields(
            order_id=order_id,
            tradingsymbol=request.tradingsymbol,
            quantity=request.quantity,
        ),
    )
    return Placement(order_id=order_id, mode=OrderMode.LIVE, sent=True)


def cancel(order_id: str, *, broker: Broker) -> str:
    """Cancel an order, or acknowledge the cancellation of a paper one.

    A paper order has no broker-side existence to cancel, but its row moves
    through the same state machine, so the caller gets the same answer either
    way and needs no branch of its own.
    """
    if is_paper_order_id(order_id):
        logger.info("paper order cancelled", extra=fields(order_id=order_id))
        return order_id

    return broker.cancel_order(order_id)
