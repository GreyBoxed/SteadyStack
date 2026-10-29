"""Rebuilding the day from the broker, by tag.

Kite is authoritative for order state (D8). Whatever this system believes
after a restart, a timeout or a postback it never received, the broker's own
order book is what actually happened -- so recovery is a read of that book
matched against the tags this system generated before it placed anything.

Never a re-placement. An order whose placement call timed out may be resting
at the exchange, and the only safe way to find out is to look for its tag. A
system that places again to "make sure" buys twice on exactly the days the
network was worst.

This runs on boot, and again before the fallback, because both moments need
the same question answered: what does the exchange think we have out there?
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.events import record
from audit.logging import fields
from broker.base import Broker
from broker.exceptions import BrokerError
from orders import booking
from orders.models import DAILY_BUY_STREAM, Fill, Order, OrderMode
from orders.placement import TAG_PREFIX, UNCONFIRMED_GRACE
from orders.state import IllegalTransition, OrderEvent, OrderState, event_for_status

logger = logging.getLogger("steadystack.orders")


@dataclass
class ReconcileResult:
    matched: int = 0
    changed: int = 0
    unknown_to_broker: list[str] = field(default_factory=list)
    unrecognised_status: list[str] = field(default_factory=list)
    #: Orders carrying one of this system's tags that it has no row for. The
    #: worst thing the broker's book can say, so it is named separately.
    unknown_to_us: list[str] = field(default_factory=list)
    fills_recorded: int = 0

    @property
    def ok(self) -> bool:
        return not (self.unknown_to_broker or self.unrecognised_status or self.unknown_to_us)


def _aware(moment):
    """pykiteconnect returns the exchange's timestamps naive, in IST. Booking a
    sell reads the day off the fill, which a naive datetime cannot answer."""
    if moment is None or timezone.is_aware(moment):
        return moment
    return timezone.make_aware(moment, timezone.get_default_timezone())


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (ValueError, ArithmeticError):
        return Decimal("0")


@transaction.atomic
def from_broker(
    *,
    trading_date: date,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
) -> ReconcileResult:
    """Bring local orders level with the broker's order book.

    Every order the day placed, not just the one that looks suspicious: a
    system that reconciles selectively is one that trusts its own record,
    which is the thing being checked.

    It is checked in both directions. Walking only the local rows answers
    "did each of ours arrive?" and cannot answer "is there anything out there
    that is not ours?" -- and the second is the worse question to get wrong,
    because an order resting at the exchange that this system has no row for
    is money moving with nothing watching it.
    """
    result = ReconcileResult()

    try:
        book = broker.orders()
    except BrokerError as failure:
        logger.error("could not read the order book", extra=fields(error=str(failure)))
        raise

    by_tag = {row.get("tag"): row for row in book if row.get("tag")}
    local = list(Order.objects.select_for_update().filter(trading_date=trading_date, stream=stream))

    _report_orphans(by_tag, local=local, result=result)
    if not local:
        return result

    missing: list[str] = []
    for order in local:
        row = by_tag.get(order.tag)

        if row is None:
            if order.is_paper:
                # A paper order has no broker-side existence by design.
                continue
            if order.kite_order_id:
                # The broker gave it an id, so it arrived. Its absence from the
                # book is a disagreement to raise, never a fact to write down.
                missing.append(order.tag)
            elif order.state == OrderState.PLACED and (
                timezone.now() - order.placed_at >= UNCONFIRMED_GRACE
            ):
                # The placement was never accepted: the local row is all there
                # ever was, and nothing was bought. Only once the grace has
                # passed -- the row is committed before the call is made, and
                # a call still on the wire is an order about to exist.
                _apply(order, OrderEvent.REJECTED, detail="not present in the order book")
                result.changed += 1
            result.unknown_to_broker.append(order.tag)
            continue

        result.matched += 1
        changed = _reconcile_one(order, row, result)
        result.changed += int(changed)

    if missing:
        record(
            "orders.missing_from_book",
            entity_type="orders.Order",
            entity_id=str(trading_date),
            after={"tags": missing},
        )
        logger.error(
            "orders the broker acknowledged are missing from its order book",
            extra=fields(trading_date=str(trading_date), tags=missing),
        )

    record(
        "orders.reconciled",
        entity_type="orders.Order",
        entity_id=str(trading_date),
        after={
            "matched": result.matched,
            "changed": result.changed,
            "unknown_to_broker": result.unknown_to_broker,
            "unknown_to_us": result.unknown_to_us,
            "unrecognised_status": result.unrecognised_status,
        },
    )
    logger.info(
        "orders reconciled against the broker",
        extra=fields(
            trading_date=str(trading_date),
            matched=result.matched,
            changed=result.changed,
        ),
    )
    return result


def _report_orphans(by_tag: dict, *, local: list[Order], result: ReconcileResult) -> None:
    """Name any order at the broker wearing one of our tags that we have no
    row for.

    The tag is generated and written before the placement call precisely so
    this question can be asked. It goes unanswered only if the row never
    committed -- which `placement._reserve` now makes impossible -- or if a
    row was removed afterwards. Either way it is reported and never acted on:
    cancelling an order on the strength of a tag this system cannot explain
    is the same guess in the opposite direction.
    """
    ours = {order.tag for order in local}
    candidates = {tag for tag in by_tag if tag and tag.startswith(TAG_PREFIX) and tag not in ours}
    # Ours in another stream is still ours: the book is the whole account's,
    # and a rebalance batch's sells are not orphans of the daily buy.
    candidates -= set(Order.objects.filter(tag__in=candidates).values_list("tag", flat=True))
    orphans = sorted(candidates)
    if not orphans:
        return

    result.unknown_to_us.extend(orphans)
    record(
        "orders.unknown_to_us",
        entity_type="orders.Order",
        entity_id=",".join(orphans)[:64],
        after={"tags": orphans},
    )
    logger.error(
        "the broker holds orders tagged by this system that it has no record of",
        extra=fields(tags=orphans),
    )


def refresh_live_orders(
    *,
    trading_date: date,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
) -> ReconcileResult | None:
    """Read the broker's book at the top of a round, when the day has gone live.

    A paper order fills in this process and needs no reading. A live one
    changes at the exchange, and the postback that says so may never come:
    a round that did not look would call a filled order stale and try to
    cancel it, or size a name from a part fill it never saw. So every round
    of a day with a live order in it reads the book first -- terminal ones
    included, because a fill can land in the instant before a cancel does.

    A book that cannot be read does not stop the round. The round goes on
    from what it last knew, which is conservative by construction: an order
    still open here is still counted as committed, and one that cannot be
    cancelled holds its name.
    """
    live = Order.objects.filter(trading_date=trading_date, stream=stream, mode=OrderMode.LIVE)
    if not live.exists():
        return None

    try:
        result = from_broker(trading_date=trading_date, broker=broker, stream=stream)
    except BrokerError as failure:
        logger.error(
            "the round could not read the order book and goes on from what it last knew",
            extra=fields(trading_date=str(trading_date), stream=stream, error=str(failure)),
        )
        return None

    if stream == DAILY_BUY_STREAM:
        _record_the_days_fills(trading_date=trading_date, broker=broker, result=result)

    # A postback that overtook its placement's response was queued; the row
    # exists now, so it can be matched and the queue kept short.
    from orders import postback

    if postback.unresolved_count():
        try:
            postback.drain_queue(broker=broker)
        except BrokerError as failure:
            logger.warning("could not drain queued postbacks", extra=fields(error=str(failure)))

    return result


def _record_the_days_fills(*, trading_date: date, broker: Broker, result: ReconcileResult) -> None:
    """B2: the trade book's fills, booked and journalled, every live round.

    Kite's trade book holds today's trades only. A live day whose fills
    were written only by the evening settle lost them for good if that
    settle had not succeeded by midnight; written here, they land while
    the day runs -- lots, `buy_fill` entries and Today's spend included --
    and the settle only confirms them. A trade book that cannot be read
    is logged and the round goes on; the next round, or the settle, reads
    it again.
    """
    try:
        result.fills_recorded = record_fills(
            trading_date=trading_date, broker=broker, stream=DAILY_BUY_STREAM
        )
    except BrokerError as failure:
        # record_fills has already logged the failure at ERROR.
        logger.warning(
            "the round could not read the trade book; its fills wait for the next read",
            extra=fields(trading_date=str(trading_date), error=str(failure)),
        )
    except Exception as failure:
        # I-B: anything else -- a trade shape this cannot read, a booking the
        # journal refuses. record_fills is atomic, so its savepoint has rolled
        # back whatever it wrote; the round places on, and the settle, which
        # needs every fill recorded, refuses loudly instead.
        logger.error(
            "the round could not record the trade book",
            extra=fields(
                trading_date=str(trading_date),
                error=f"{failure.__class__.__name__}: {failure}",
            ),
        )


def level_with(order: Order, row: dict) -> bool:
    """Bring one order level with one broker row -- from the book or from its
    history, which carry the same fields. Whether anything changed."""
    return _reconcile_one(order, row, ReconcileResult())


def _reconcile_one(order: Order, row: dict, result: ReconcileResult) -> bool:
    """Bring one order level with its row in the broker's book."""
    changed = False

    broker_order_id = str(row.get("order_id") or "")
    if broker_order_id and order.kite_order_id != broker_order_id:
        order.kite_order_id = broker_order_id
        changed = True

    exchange_order_id = str(row.get("exchange_order_id") or "")
    if exchange_order_id and order.exchange_order_id != exchange_order_id:
        order.exchange_order_id = exchange_order_id
        changed = True

    filled = int(row.get("filled_quantity") or 0)
    filled = min(filled, order.quantity)
    if filled != order.filled_quantity:
        order.filled_quantity = filled
        changed = True

    if changed:
        order.save(
            update_fields=["kite_order_id", "exchange_order_id", "filled_quantity", "updated_at"]
        )

    status = row.get("status", "")
    event = event_for_status(status)

    if event is None:
        # A status this system has never seen is not one to act on: it is
        # reported, and the order is left exactly as it was.
        result.unrecognised_status.append(f"{order.tag}: {status}")
        logger.warning(
            "unrecognised order status",
            extra=fields(tag=order.tag, status=status),
        )
        return changed

    # A partly filled order that the broker still calls open is partially
    # filled here too -- the status alone would lose that.
    if event == OrderEvent.OPENED and 0 < filled < order.quantity:
        event = OrderEvent.PARTIAL_FILL

    return _apply(order, event, detail=f"reconciled from status {status}") or changed


def _apply(order: Order, event: str, *, detail: str) -> bool:
    from orders.placement import apply_event

    before = order.state
    try:
        apply_event(order, event, detail=detail)
    except IllegalTransition:
        # The broker says something this machine cannot reach from where the
        # order is. Reported rather than forced: forcing it would hide the
        # disagreement that matters.
        logger.error(
            "the broker's state cannot be reached from ours",
            extra=fields(tag=order.tag, state=before, event=event),
        )
        record(
            "orders.state_conflict",
            entity=order,
            before={"state": before},
            after={"event": event, "detail": detail},
        )
        return False

    return order.state != before


@transaction.atomic
def record_fills(
    *,
    trading_date: date,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
) -> int:
    """Write any broker trades this system has not recorded yet.

    Each one is booked against the lots as it is written -- a buy creates its
    lot, a sell consumes them FIFO (M35, M36). This is the live day's only
    path to a fill, and without the booking it wrote rows the portfolio was
    never reconciled from.

    The `kite_trade_id` unique constraint carries the idempotency: a trade
    already recorded is skipped rather than duplicated, which is what stops a
    replayed postback consuming lots twice (D11).
    """
    try:
        trades = broker.trades()
    except BrokerError as failure:
        logger.error("could not read the trade book", extra=fields(error=str(failure)))
        raise

    orders_by_id = {
        order.kite_order_id: order
        for order in Order.objects.filter(trading_date=trading_date, stream=stream)
        if order.kite_order_id
    }

    recorded = 0
    for trade in trades:
        trade_id = str(trade.get("trade_id") or "")
        order = orders_by_id.get(str(trade.get("order_id") or ""))

        if not trade_id or order is None:
            continue
        exchange_order_id = str(trade.get("exchange_order_id") or "")
        if exchange_order_id and not order.exchange_order_id:
            order.exchange_order_id = exchange_order_id
            order.save(update_fields=["exchange_order_id", "updated_at"])
        held = Fill.objects.filter(kite_trade_id=trade_id).select_related("order").first()
        if held is not None:
            if held.order_id != order.pk:
                # Nm2: the same trade id on another order is not this trade
                # recorded already -- skipping it would drop a real fill.
                logger.error(
                    "a trade id another order already holds; the trade is not booked",
                    extra=fields(
                        trade_id=trade_id,
                        order=order.tag,
                        held_by=held.order.tag,
                        detail="another order holds this trade id",
                    ),
                )
            continue

        quantity = int(trade.get("quantity") or 0)
        price = _to_decimal(trade.get("average_price") or trade.get("price"))
        if quantity <= 0 or price <= 0:
            # Nm1: never booked as a zero -- a trade without a price or a
            # quantity is a shape this does not read. The settle refuses
            # while the order's fills fall short of what it filled.
            logger.error(
                "a trade without a positive price and quantity is not booked",
                extra=fields(
                    trade_id=trade_id, order=order.tag, quantity=quantity, price=str(price)
                ),
            )
            continue

        fill = Fill.objects.create(
            order=order,
            kite_trade_id=trade_id,
            quantity=quantity,
            price=price,
            executed_at=_aware(trade.get("fill_timestamp")) or timezone.now(),
            trace_id=order.trace_id,
        )
        booking.book(fill)
        recorded += 1

    if recorded:
        logger.info("fills recorded from the trade book", extra=fields(recorded=recorded))

    return recorded
