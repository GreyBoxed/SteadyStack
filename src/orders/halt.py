"""Stop trading, now, and cancel everything that is out there.

Pausing and cancelling are two different things and the operator almost never
wants one without the other. A pause stops the system *deciding* to buy: no
tradesheet, no tick, no fallback. It does nothing at all about the orders the
ladder has already placed, which are DAY validity and sit at the exchange
filling for the rest of the session. So "I have paused it" and "it has stopped
buying" are not the same sentence, and the gap between them is hours.

This is the one action that means both, and it is arranged around the
assumption that something is already wrong:

  * **pause first.** If it is going to fail anywhere it will fail at the
    broker, and a system that cancelled everything and then failed to pause
    would place again five minutes later. The half that cannot fail goes
    first;
  * **try every order.** A broker that refuses one cancellation must not leave
    the others unexamined -- stopping at the first failure is how an emergency
    stop leaves orders resting that nobody has even looked at;
  * **say what is still live.** An order this could not cancel is the single
    most important thing the operator needs to know, so it is returned, logged
    at error and audited. Reporting success with an order still out there
    would be worse than failing loudly.

Nothing here un-stops. Resuming is its own audited decision (`pause.resume`),
which is what makes this safe to press.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from audit.events import record
from audit.logging import fields
from broker.base import Broker
from broker.exceptions import BrokerError
from configuration import pause as pause_module
from configuration.models import SystemPause
from orders.models import Order
from orders.state import TERMINAL

logger = logging.getLogger("steadystack.orders")


@dataclass
class Stop:
    """What the stop managed to do."""

    stopped_at: datetime
    cancelled: list[str] = field(default_factory=list)
    #: Orders the broker would not confirm cancelled. These are still live.
    unconfirmed: list[str] = field(default_factory=list)
    batches_closed: int = 0
    pause: SystemPause | None = None
    was_already_paused: bool = False

    @property
    def clean(self) -> bool:
        """Whether everything that was out there is now cancelled."""
        return not self.unconfirmed

    @property
    def summary(self) -> str:
        """One line, for a command's output and a page's banner."""
        parts = [f"Trading is stopped. {len(self.cancelled)} order(s) cancelled"]
        if self.batches_closed:
            parts.append(f"{self.batches_closed} rebalance batch(es) closed")

        line = ", ".join(parts) + "."
        if self.unconfirmed:
            line += (
                f" {len(self.unconfirmed)} order(s) could NOT be confirmed cancelled and "
                f"may still be live at the broker: {', '.join(self.unconfirmed)}. "
                "Check the broker's own order book."
            )
        return line


def open_orders():
    """Every order still capable of filling, across every stream and mode.

    Not scoped to a tradesheet or a day: a stop is about what is live at the
    broker, and an order left over from a day nobody settled is exactly the
    one worth catching.
    """
    return Order.objects.exclude(state__in=list(TERMINAL)).order_by("trading_date", "tick_no")


def stop_trading(*, actor: str, reason: str, broker: Broker) -> Stop:
    """Pause the system and cancel every order that is still live, after any
    round in flight has finished.

    Under the order lock, waiting a few seconds for a buy or sell round, so a
    round that had already read the day as running cannot place after the
    cancels. This is the emergency stop, so it never refuses for want of the
    lock: if a round will not finish in time it stops anyway, and the pause
    still keeps every later round from placing.
    """
    from orders.idempotency import Busy, order_lock
    from orders.overrides import STOP_WAIT_SECONDS

    try:
        with order_lock(wait_seconds=STOP_WAIT_SECONDS):
            return _stop_trading(actor=actor, reason=reason, broker=broker)
    except Busy:
        logger.error("stopping without the order lock: a round did not finish in time")
        return _stop_trading(actor=actor, reason=reason, broker=broker)


def _stop_trading(*, actor: str, reason: str, broker: Broker) -> Stop:
    """Pause the system and cancel every order that is still live.

    Deliberately not one transaction. Each cancellation that succeeds is
    committed as it happens, because the alternative is that a failure at the
    tenth order rolls back the nine that genuinely were cancelled at the
    broker -- leaving the database claiming they are open when they are not,
    which is the one disagreement this system works hardest to avoid.
    """
    from orders import placement

    result = Stop(stopped_at=timezone.now())

    result.was_already_paused = pause_module.is_paused()
    if not result.was_already_paused:
        result.pause = pause_module.pause(actor=actor, reason=reason)

    for order in list(open_orders()):
        try:
            placement.cancel(order, broker=broker)
        except BrokerError:
            # Logged by `cancel` already. Collected rather than raised: the
            # next order still has to be tried.
            result.unconfirmed.append(order.tag)
        else:
            result.cancelled.append(order.tag)

    result.batches_closed = _close_running_batches(actor=actor, reason=reason)

    record(
        "system.trading_stopped",
        actor=actor,
        entity_type="configuration.SystemPause",
        entity_id=str(result.pause.id) if result.pause else "",
        after={
            "reason": reason,
            "cancelled": result.cancelled,
            "unconfirmed": result.unconfirmed,
            "batches_closed": result.batches_closed,
            "was_already_paused": result.was_already_paused,
        },
    )

    if result.clean:
        logger.warning(
            "trading stopped and every open order cancelled",
            extra=fields(
                actor=actor,
                reason=reason,
                cancelled=len(result.cancelled),
                batches_closed=result.batches_closed,
            ),
        )
    else:
        logger.error(
            "trading stopped, but some orders could not be cancelled and may still be live",
            extra=fields(
                actor=actor,
                reason=reason,
                cancelled=len(result.cancelled),
                unconfirmed=result.unconfirmed,
            ),
        )

    return result


def _close_running_batches(*, actor: str, reason: str) -> int:
    """Close any rebalance batch that was mid-flight.

    Its orders have already been cancelled above, so this is the bookkeeping:
    a batch left RUNNING with nothing running is a row that lies, and the
    interlock that keeps the daily buy off a name being sold reads that state.
    Fills already taken stand -- the batch closes partial, never pretending it
    did not run (M32a).
    """
    from rebalance.ladder import close_batch
    from rebalance.models import BatchState, RebalanceSell

    closed = 0
    for batch in RebalanceSell.objects.filter(state=BatchState.RUNNING):
        # From the fills, not from a sold count only settlement ever wrote:
        # a batch that sold and is stopped closes partial, not cancelled.
        closed_batch = close_batch(batch, reason=f"trading stopped: {reason}"[:255], actor=actor)
        record(
            "rebalance.cancelled",
            actor=actor,
            entity=closed_batch,
            after={"reason": closed_batch.cancelled_reason, "state": closed_batch.state},
        )
        closed += 1

    return closed
