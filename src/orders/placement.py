"""Placing one order, in the order that survives a failure.

The sequence matters more than anything else in this module:

  1. generate a tag;
  2. write the local `Order` row with it, and commit;
  3. call the broker with that tag.

The worst failure available to a system like this is an order the broker
accepts whose local write fails -- money moving with no record of it. Writing
first turns that into the opposite failure, a local row with no broker order,
which is both visible and harmless: it reconciles to REJECTED and nothing was
bought.

Which leaves the ambiguous case, where the call times out and nobody knows
whether the exchange has it. The answer is never to place again. It is to
fetch the order book and look for the tag, which is why the tag exists and why
it is written before the call rather than after it.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from broker import paper
from broker.base import Broker
from broker.exceptions import (
    BrokerError,
    BrokerInputError,
    LiveOrdersRefused,
    OrderRejected,
    SessionExpired,
)
from broker.mode import configured_mode
from configuration.store import current as current_configuration
from engine.models import TradeSheet
from orders.models import DAILY_BUY_STREAM, Order, OrderMode, OrderSide, OrderType
from orders.state import TERMINAL, OrderEvent, OrderState, event_for_status, next_state
from portfolio.models import Instrument

logger = logging.getLogger("steadystack.orders")

#: Kite caps the tag at 20 characters. Short, unique, and recognisable in the
#: broker's own order book without a lookup.
TAG_PREFIX = "tx"

#: How long a live order with no broker id is given before its absence from
#: the broker's book is taken to mean it never arrived. Its placement call may
#: still be on the wire until then -- the row is committed before the call is
#: made -- and concluding "rejected" while it is would size the same shares
#: again. Far longer than the client's own timeout, far shorter than a TTL.
UNCONFIRMED_GRACE = timedelta(minutes=2)


class PlacementFailed(BrokerError):
    """The order could not be placed. The local row records why."""


class NotYetAtBroker(BrokerError):
    """A live order with no broker id that the book does not show yet.

    Its placement may still be on its way, so it is neither cancellable -- there
    is no id to name -- nor safely called rejected. Raised as a `BrokerError`
    so every caller already treats it as a cancellation it cannot confirm.
    """


class RateBreakerTripped(Exception):
    """This system has placed orders faster than it can possibly mean to.

    Deliberately not a `BrokerError`: nothing about the market caused it and
    nothing about the market will clear it. Every other refusal in this
    module is a fact about the day that the ladder carries on past; this one
    says the program is not doing what it thinks it is, and it propagates
    until something stops.
    """

    def __init__(self, *, placed: int, allowed: int, window: int) -> None:
        super().__init__(
            f"{placed} orders have been placed in the last {window} seconds and "
            f"the limit is {allowed}. Nothing further will be placed until the "
            f"window clears; if this is legitimate, raise the limit in "
            f"configuration rather than removing the check."
        )
        self.placed = placed
        self.allowed = allowed
        self.window = window


@dataclass(frozen=True)
class PlacementOutcome:
    order: Order
    sent: bool
    failure: Exception | None = None

    @property
    def placed(self) -> bool:
        return self.failure is None


def new_tag() -> str:
    """A tag unique to this order, short enough for Kite to accept."""
    return f"{TAG_PREFIX}{uuid.uuid4().hex[:14]}"


@transaction.atomic(durable=True)
def _reserve(
    *,
    tradesheet: TradeSheet,
    instrument: Instrument,
    tick_no: int,
    quantity: int,
    order_type: str,
    limit_price: Decimal | None,
    side: str,
    stream: str,
    is_fallback: bool,
) -> Order:
    """Write the local row before anything is sent, and *commit* it.

    `durable=True` is the whole point. Nested inside a caller's transaction
    this would be a savepoint rather than a commit, the row would not exist
    outside the process until the caller finished, and a crash between the
    broker accepting the order and that commit would leave money moving with
    no record of it -- the one failure this module is arranged to avoid. So
    rather than trusting every caller to stay out of a transaction, nesting
    raises here and the mistake cannot be made quietly.
    """
    return Order.objects.create(
        tag=new_tag(),
        tradesheet=tradesheet,
        instrument=instrument,
        trading_date=tradesheet.trading_date,
        stream=stream,
        tick_no=tick_no,
        side=side,
        mode=OrderMode.LIVE if configured_mode() == OrderMode.LIVE else OrderMode.PAPER,
        order_type=order_type,
        is_fallback=is_fallback,
        quantity=quantity,
        limit_price=limit_price,
        state=OrderState.PLACED,
        trace_id=current_trace_id(),
    )


def orders_in_window(*, window_seconds: int, now=None) -> int:
    """How many orders this system has placed in the last *window_seconds*.

    Every order, across every stream and both modes. A runaway is a runaway
    whatever it is placing, and counting paper too is what makes the breaker
    exercisable before the first live order rather than on the day it is most
    needed -- which is the same reasoning M19c applies to fills.
    """
    now = now or timezone.now()
    return Order.objects.filter(placed_at__gt=now - timedelta(seconds=window_seconds)).count()


def room(config) -> int | None:
    """How many orders the rate limit leaves right now, or None for no limit.

    Counted by the breaker's own function, so whatever places within this
    number -- the buy round, the close, a sell round -- can never place the
    order that trips it.
    """
    if config.max_orders_per_window <= 0 or config.order_rate_window_seconds <= 0:
        return None
    used = orders_in_window(window_seconds=config.order_rate_window_seconds)
    return max(0, config.max_orders_per_window - used)


def stream_orders(trading_date, *, stream: str, instrument=None):
    """A stream's orders for the day, whichever sheet version they hang off.

    A rebalance batch's orders attach to the day's latest sheet as it was when
    each was placed, so after a replan they span versions; the stream is what
    they have in common.
    """
    orders = Order.objects.filter(trading_date=trading_date, stream=stream)
    if instrument is not None:
        orders = orders.filter(instrument=instrument)
    return orders


def check_rate(*, now=None) -> None:
    """Refuse to place if the recent rate says something is looping (the breaker).

    Checked before the row is reserved, so a tripped breaker leaves no order
    behind at all -- there is nothing for reconciliation to explain, because
    nothing was written and nothing was sent.
    """
    config = current_configuration()
    allowed = config.max_orders_per_window
    window = config.order_rate_window_seconds
    if allowed <= 0 or window <= 0:
        return
    # The placing rounds and the close stay under this limit by construction:
    # each places only what `orders_in_window` leaves room for. So a trip here
    # means something placed that was not counting -- which is the point.

    placed = orders_in_window(window_seconds=window, now=now)
    if placed < allowed:
        return

    breaker = RateBreakerTripped(placed=placed, allowed=allowed, window=window)
    record(
        "orders.rate_breaker_tripped",
        entity_type="orders.Order",
        after={"placed": placed, "allowed": allowed, "window_seconds": window},
    )
    logger.error(
        "the order rate breaker tripped; nothing further will be placed",
        extra=fields(placed=placed, allowed=allowed, window_seconds=window),
    )
    raise breaker


def place(
    *,
    tradesheet: TradeSheet,
    instrument: Instrument,
    tick_no: int,
    quantity: int,
    broker: Broker,
    order_type: str = OrderType.LIMIT,
    limit_price: Decimal | None = None,
    side: str = OrderSide.BUY,
    stream: str = DAILY_BUY_STREAM,
    is_fallback: bool = False,
    market_protection: Decimal | None = None,
) -> PlacementOutcome:
    """Place one order and return what happened.

    Never raises for an ordinary rejection: a rejected order is a fact about
    the day, recorded on the order and on the tick, and the ladder carries on.
    It does raise when the engine refuses a live placement, because that is a
    configuration problem rather than a market one and should stop the day
    rather than repeat every five minutes. The rate breaker raises for the
    same reason and more so.
    """
    if quantity <= 0:
        raise ValueError("An order for no shares is not an order.")

    check_rate()

    order = _reserve(
        tradesheet=tradesheet,
        instrument=instrument,
        tick_no=tick_no,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        side=side,
        stream=stream,
        is_fallback=is_fallback,
    )

    request = paper.PlacementRequest(
        tradingsymbol=instrument.tradingsymbol,
        exchange=instrument.exchange,
        transaction_type=side,
        quantity=quantity,
        order_type=order_type,
        price=limit_price,
        tag=order.tag,
        market_protection=market_protection,
    )

    try:
        placement = paper.place(request, broker=broker)
    except LiveOrdersRefused:
        # The engine's own refusal (M12a, M19b). The local row is marked and
        # the exception propagates: a misconfigured system should stop, not
        # quietly place nothing every five minutes for the rest of the day.
        _mark_failed(order, reason="live placement refused by the engine")
        raise
    except (OrderRejected, BrokerInputError, SessionExpired) as rejection:
        # Definite refusals. Kite's InputException -- a tick size, a market
        # protection, an address not registered -- and a dead token both mean
        # the order was not taken. Left PLACED they would count as committed
        # all day and be "cancelled" by their tag, which Kite has never seen.
        _mark_rejected(order, reason=str(rejection))
        return PlacementOutcome(order=order, sent=True, failure=rejection)
    except BrokerError as failure:
        # Ambiguous: the exchange may or may not have it. Reconciliation by
        # tag decides, and nothing here places again.
        _mark_unconfirmed(order, reason=str(failure))
        return PlacementOutcome(order=order, sent=False, failure=failure)

    order = _accepted(order, order_id=placement.order_id, sent=placement.sent)

    record(
        "order.placed",
        entity=order,
        after={
            "tag": order.tag,
            "kite_order_id": placement.order_id,
            "mode": order.mode,
            "symbol": instrument.tradingsymbol,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": str(limit_price) if limit_price is not None else None,
            "tick_no": tick_no,
            "is_fallback": is_fallback,
        },
    )
    logger.info(
        "order placed",
        extra=fields(
            tag=order.tag,
            mode=order.mode,
            symbol=instrument.tradingsymbol,
            quantity=quantity,
            limit_price=str(limit_price) if limit_price is not None else None,
            tick_no=tick_no,
        ),
    )
    return PlacementOutcome(order=order, sent=placement.sent)


def _accepted(order: Order, *, order_id: str, sent: bool) -> Order:
    """Write the broker's id onto the row, read afresh under a lock.

    The row was committed before the call, so while the call was out the
    reconciliation could see it, find nothing in the book, and call it
    rejected. An order id in hand says otherwise: the order is at the
    exchange. A rejection written with no broker id was this system's guess,
    not the broker's answer, and it is withdrawn -- left standing, the next
    round would size those shares again and buy them twice. A rejection that
    came with an id came from the broker's book and stands.
    """
    with transaction.atomic():
        current = Order.objects.select_for_update().get(pk=order.pk)
        guessed = current.state == OrderState.REJECTED and not current.kite_order_id
        current.kite_order_id = order_id
        current.mode = OrderMode.LIVE if sent else OrderMode.PAPER
        update = ["kite_order_id", "mode", "updated_at"]

        if guessed:
            # Deliberately around the state machine, which rightly has no way
            # out of REJECTED: this is not an event on the order but the
            # correction of a record that was never true.
            withdrawn = current.rejection_reason
            current.state = OrderState.PLACED
            current.rejection_reason = ""
            update += ["state", "rejection_reason"]
            record(
                "order.rejection_withdrawn",
                entity=current,
                before={"state": OrderState.REJECTED, "reason": withdrawn},
                after={"state": OrderState.PLACED, "kite_order_id": order_id},
            )
            logger.error(
                "an order called rejected while its placement was out was accepted; "
                "the rejection is withdrawn",
                extra=fields(tag=current.tag, kite_order_id=order_id, reason=withdrawn),
            )

        current.save(update_fields=update)
    return current


def _mark_rejected(order: Order, *, reason: str) -> None:
    order.state = next_state(order.state, OrderEvent.REJECTED)
    order.rejection_reason = reason[:255]
    order.save(update_fields=["state", "rejection_reason", "updated_at"])

    record("order.rejected", entity=order, after={"tag": order.tag, "reason": reason})
    logger.warning("order rejected", extra=fields(tag=order.tag, reason=reason))


def _mark_failed(order: Order, *, reason: str) -> None:
    """A placement that never reached the broker at all."""
    order.state = next_state(order.state, OrderEvent.REJECTED)
    order.rejection_reason = reason[:255]
    order.save(update_fields=["state", "rejection_reason", "updated_at"])

    record("order.not_placed", entity=order, after={"tag": order.tag, "reason": reason})
    logger.warning("order not placed", extra=fields(tag=order.tag, reason=reason))


def _mark_unconfirmed(order: Order, *, reason: str) -> None:
    """The call failed and the outcome is unknown.

    The row is left in PLACED deliberately. It is neither rejected nor open,
    and only the broker's order book can say which -- so it stays as a
    question for reconciliation to answer, rather than being given an answer
    here that might be wrong.
    """
    order.rejection_reason = f"unconfirmed: {reason}"[:255]
    order.save(update_fields=["rejection_reason", "updated_at"])

    record(
        "order.unconfirmed",
        entity=order,
        after={"tag": order.tag, "reason": reason},
    )
    logger.error(
        "placement outcome unknown; reconcile by tag rather than placing again",
        extra=fields(tag=order.tag, reason=reason),
    )


@transaction.atomic
def apply_event(order: Order, event: str, *, detail: str = "") -> Order:
    """Move an order through the state machine, refusing illegal moves."""
    before = order.state
    order.state = next_state(before, event)

    if order.state != before:
        order.save(update_fields=["state", "updated_at"])
        record(
            "order.state_changed",
            entity=order,
            before={"state": before},
            after={"state": order.state, "event": event, "detail": detail},
        )
    return order


def cancel(order: Order, *, broker: Broker) -> Order:
    """Cancel one order and record it.

    A paper order has nothing to cancel at the broker, but runs the same
    transition, so callers need no branch of their own.

    A live order is only ever cancelled by its broker id. One without an id
    is looked for by tag first (`_find_by_tag`), and a cancel the broker
    refuses is not taken at its word: the order's history is read, because
    the usual reason a cancel fails is that the order has already filled or
    gone (`_ended_at_broker`).
    """
    if order.is_terminal:
        return order

    if not order.is_paper and not order.kite_order_id:
        _find_by_tag(order, broker=broker)
        order.refresh_from_db()
        if order.is_terminal:
            return order

    try:
        paper.cancel(order.kite_order_id or order.tag, broker=broker)
    except BrokerError as failure:
        if not order.is_paper and _ended_at_broker(order, broker=broker):
            order.refresh_from_db()
            return order
        # The fallback must know the cancellation is unconfirmed before it
        # sizes a market order against what it believes is unfilled.
        logger.error(
            "cancellation failed",
            extra=fields(tag=order.tag, reason=str(failure)),
        )
        raise

    # Read again under a row lock before writing. The paper filler commits a
    # fill from the tick-feed process on its own, and a copy of this order
    # read a moment ago would otherwise have CANCELLED written over COMPLETE
    # -- a record saying shares were not bought when they were.
    with transaction.atomic():
        current = Order.objects.select_for_update().get(pk=order.pk)
        if not current.is_terminal:
            apply_event(current, OrderEvent.CANCELLED, detail="cancelled")
            record("order.cancelled", entity=current, after={"tag": current.tag})

    order.refresh_from_db()
    return order


def expire_unseen(order: Order, *, detail: str, actor: str | None = None) -> Order:
    """Level a live order of a past date that is still open here (NI1).

    Every order this system places is DAY validity, so by the next morning
    it has ended at the exchange: filled, or cancelled at the close with
    whatever it filled standing. Kite's order book and history hold today's
    orders only, so a cancel or a history read for it fails, and nothing at
    the broker can be asked. It is levelled without a broker call: COMPLETE
    when its shares are all accounted for, CANCELLED otherwise.

    Its fills are what it filled -- unless the order book already said more,
    which is kept: the settle then refuses on the trade book being
    incomplete, and Console's tradebook supplies the missing fills, rather
    than a row being written without their cost.

    Called by the tradebook import, for the imported day's other orders, and
    by `settle_day --level-unseen`, once *actor* has read Console's
    tradebook and found none of them traded (I-1) -- never on a settle's
    own say-so, which cannot tell unfilled from filled while unseen.
    """
    from audit.events import SYSTEM_ACTOR
    from orders.models import Fill

    with transaction.atomic():
        current = Order.objects.select_for_update().get(pk=order.pk)
        if current.is_terminal or current.is_paper:
            return current
        before = {"state": current.state, "filled_quantity": current.filled_quantity}
        recorded = Fill.objects.filter(order=current).aggregate(total=Sum("quantity"))["total"] or 0
        filled = min(max(recorded, current.filled_quantity), current.quantity)
        if filled != current.filled_quantity:
            current.filled_quantity = filled
            current.save(update_fields=["filled_quantity", "updated_at"])
        event = OrderEvent.FILLED if filled >= current.quantity else OrderEvent.CANCELLED
        apply_event(current, event, detail=detail)
        record(
            "order.expired_unseen",
            actor=actor or SYSTEM_ACTOR,
            entity=current,
            before=before,
            after={"state": current.state, "filled_quantity": filled, "recorded": recorded},
        )
        logger.warning(
            "a past day's live order was levelled without asking the broker",
            extra=fields(
                tag=current.tag,
                trading_date=str(current.trading_date),
                state=current.state,
                filled=filled,
            ),
        )
    order.refresh_from_db()
    return order


def _find_by_tag(order: Order, *, broker: Broker) -> None:
    """Give a live order with no broker id one, or an answer.

    Its placement went unanswered, so the only name Kite knows it by is the
    tag. Found in the book, it takes the book's id and state. Absent, it is
    rejected once `UNCONFIRMED_GRACE` has passed -- it never reached the
    broker -- and before that the question stays open and `NotYetAtBroker`
    says so. Nothing is ever sent to Kite with the tag standing in for an id.
    """
    from orders.reconcile import level_with

    try:
        book = broker.orders()
    except BrokerError as failure:
        logger.error(
            "could not look for an unconfirmed order by its tag",
            extra=fields(tag=order.tag, reason=str(failure)),
        )
        raise

    row = next((row for row in book if row.get("tag") == order.tag), None)

    with transaction.atomic():
        current = Order.objects.select_for_update().get(pk=order.pk)
        if current.is_terminal or current.kite_order_id:
            return
        if row is not None:
            level_with(current, row)
            return
        if current.state != OrderState.PLACED or (
            timezone.now() - current.placed_at < UNCONFIRMED_GRACE
        ):
            raise NotYetAtBroker(
                f"{current.tag} has no broker id and is not in the order book yet; "
                "its placement may still be on the way."
            )

        reason = "never reached the broker: not in the order book by its tag"
        current.state = next_state(current.state, OrderEvent.REJECTED)
        current.rejection_reason = reason
        current.save(update_fields=["state", "rejection_reason", "updated_at"])
        record("order.not_placed", entity=current, after={"tag": current.tag, "reason": reason})
        logger.warning("order not placed", extra=fields(tag=current.tag, reason=reason))


#: What a cancel that failed can find the order already became.
_ENDED = frozenset({OrderEvent.FILLED, OrderEvent.CANCELLED, OrderEvent.REJECTED})


def _ended_at_broker(order: Order, *, broker: Broker) -> bool:
    """After a cancel failed, whether the broker says the order has ended.

    Kite refuses to cancel an order that has filled, been cancelled or been
    rejected, and a cancel that timed out may have landed. Its history says
    which; an ended order takes that state and that fill quantity -- a part
    fill on a cancelled order is shares bought -- and there is nothing left
    to cancel. Anything else, or a history that cannot be read, is still an
    unconfirmed cancellation.
    """
    from orders.reconcile import level_with

    try:
        history = broker.order_history(order.kite_order_id)
    except BrokerError as failure:
        logger.error(
            "could not read the history of an order whose cancel failed",
            extra=fields(tag=order.tag, reason=str(failure)),
        )
        return False

    latest = history[-1] if history else {}
    if event_for_status(latest.get("status", "")) not in _ENDED:
        return False

    with transaction.atomic():
        current = Order.objects.select_for_update().get(pk=order.pk)
        level_with(current, latest)

    if not current.is_terminal:
        return False

    record(
        "order.cancel_overtaken",
        entity=current,
        after={
            "tag": current.tag,
            "status": latest.get("status"),
            "filled_quantity": current.filled_quantity,
        },
    )
    logger.warning(
        "a cancel failed because the order had already ended at the broker",
        extra=fields(tag=current.tag, state=current.state, filled=current.filled_quantity),
    )
    return True


def _orders(tradesheet: TradeSheet, *, stream: str, instrument=None):
    orders = Order.objects.filter(tradesheet=tradesheet, stream=stream)
    if instrument is not None:
        orders = orders.filter(instrument=instrument)
    return orders


def committed_quantity(
    tradesheet: TradeSheet, *, stream: str = DAILY_BUY_STREAM, instrument=None
) -> int:
    """Filled plus still-open, across every order the day has placed.

    The day's number, not any one order's: with several orders resting at once
    the only figure that can size the next slice is the total (M27). A day
    buys several names, so the figure is per name when one is given.
    """
    orders = _orders(tradesheet, stream=stream, instrument=instrument)
    return sum(order.filled_quantity if order.is_terminal else order.quantity for order in orders)


def filled_quantity(
    tradesheet: TradeSheet, *, stream: str = DAILY_BUY_STREAM, instrument=None
) -> int:
    total = _orders(tradesheet, stream=stream, instrument=instrument).aggregate(
        total=Sum("filled_quantity")
    )["total"]
    return total or 0


def day_has_bought(trading_date, *, stream: str = DAILY_BUY_STREAM) -> bool:
    """Whether any version of the day has had a fill in *stream*.

    The question "may this day be skipped?" is about the day: a replanned
    version with no fills of its own belongs to a day that has bought, and a
    day that has bought is PARTIAL, never SKIPPED.
    """
    return Order.objects.filter(
        trading_date=trading_date, stream=stream, filled_quantity__gt=0
    ).exists()


def open_orders(tradesheet: TradeSheet, *, stream: str = DAILY_BUY_STREAM, instrument=None):
    """Orders that can still fill, and are therefore still committing cash."""
    return _orders(tradesheet, stream=stream, instrument=instrument).exclude(
        state__in=list(TERMINAL)
    )
