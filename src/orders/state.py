"""The order state machine, as a table.

Every (state, event) pair is enumerated, including the ones that must never
happen -- because "must never happen" is a claim, and a table is a claim that
can be tested. An illegal transition raises rather than being absorbed: an
order that has already filled cannot go back to open, and code that tries is
wrong in a way that will cost money if it is allowed to continue.

Two kinds of non-transition are tolerated deliberately, and only two:

  * an event that restates the state an order is already in. Postbacks arrive
    twice and out of order (D8), and a second "this filled" must be a no-op,
    not an error;
  * an event on a terminal order that would leave it where it is.

Anything else raises.
"""

from django.db import models


class OrderState(models.TextChoices):
    PLACED = "PLACED", "Placed"
    OPEN = "OPEN", "Open at the exchange"
    PARTIALLY_FILLED = "PARTIALLY_FILLED", "Partially filled"
    COMPLETE = "COMPLETE", "Complete"
    CANCELLED = "CANCELLED", "Cancelled"
    REJECTED = "REJECTED", "Rejected"


class OrderEvent(models.TextChoices):
    OPENED = "OPENED", "Accepted by the exchange"
    PARTIAL_FILL = "PARTIAL_FILL", "Filled in part"
    FILLED = "FILLED", "Filled in full"
    CANCELLED = "CANCELLED", "Cancelled"
    REJECTED = "REJECTED", "Rejected"


TERMINAL = frozenset({OrderState.COMPLETE, OrderState.CANCELLED, OrderState.REJECTED})

#: The whole machine. A state missing from this table is not a state.
TRANSITIONS: dict[str, dict[str, str]] = {
    OrderState.PLACED: {
        OrderEvent.OPENED: OrderState.OPEN,
        # A fill can be reported before the acceptance is: the postback for
        # one can overtake the other, and the fill is the more important fact.
        OrderEvent.PARTIAL_FILL: OrderState.PARTIALLY_FILLED,
        OrderEvent.FILLED: OrderState.COMPLETE,
        OrderEvent.CANCELLED: OrderState.CANCELLED,
        OrderEvent.REJECTED: OrderState.REJECTED,
    },
    OrderState.OPEN: {
        OrderEvent.OPENED: OrderState.OPEN,
        OrderEvent.PARTIAL_FILL: OrderState.PARTIALLY_FILLED,
        OrderEvent.FILLED: OrderState.COMPLETE,
        OrderEvent.CANCELLED: OrderState.CANCELLED,
        OrderEvent.REJECTED: OrderState.REJECTED,
    },
    OrderState.PARTIALLY_FILLED: {
        # More of the same order filling. It stays where it is.
        OrderEvent.PARTIAL_FILL: OrderState.PARTIALLY_FILLED,
        OrderEvent.FILLED: OrderState.COMPLETE,
        # Cancelled with part of it filled: the fills stand, the rest is gone.
        OrderEvent.CANCELLED: OrderState.CANCELLED,
    },
    OrderState.COMPLETE: {},
    OrderState.CANCELLED: {},
    OrderState.REJECTED: {},
}


class IllegalTransition(Exception):
    """An event that would move an order somewhere it cannot go."""

    def __init__(self, state: str, event: str) -> None:
        super().__init__(f"An order in {state} cannot handle {event}.")
        self.state = state
        self.event = event


def is_terminal(state: str) -> bool:
    return state in TERMINAL


def next_state(state: str, event: str) -> str:
    """Where *event* takes an order currently in *state*.

    Raises on anything the table does not allow. The one exception is an event
    restating a terminal state the order is already in -- a duplicate "this
    was cancelled" is a repeat of the news, not a contradiction of it.
    """
    if state not in TRANSITIONS:
        raise IllegalTransition(state, event)

    allowed = TRANSITIONS[state]
    if event in allowed:
        return allowed[event]

    if _restates(state, event):
        return state

    raise IllegalTransition(state, event)


def _restates(state: str, event: str) -> bool:
    """Whether *event* is simply telling us what we already know."""
    return (
        (state == OrderState.COMPLETE and event == OrderEvent.FILLED)
        or (state == OrderState.CANCELLED and event == OrderEvent.CANCELLED)
        or (state == OrderState.REJECTED and event == OrderEvent.REJECTED)
    )


#: Kite's own order statuses, mapped to the events this machine understands.
#: Anything unrecognised is not guessed at -- it is reported, because a status
#: this system has never seen is not one it should act on.
KITE_STATUS_EVENTS = {
    "OPEN": OrderEvent.OPENED,
    # Received and on its way to the exchange, or an after-market order
    # waiting for the open. Neither is an answer yet, so both read as open:
    # reported as unrecognised, they would leave the order unreconciled.
    "OPEN PENDING": OrderEvent.OPENED,
    "AMO REQ RECEIVED": OrderEvent.OPENED,
    "TRIGGER PENDING": OrderEvent.OPENED,
    "VALIDATION PENDING": OrderEvent.OPENED,
    "PUT ORDER REQ RECEIVED": OrderEvent.OPENED,
    "MODIFY PENDING": OrderEvent.OPENED,
    "MODIFY VALIDATION PENDING": OrderEvent.OPENED,
    "COMPLETE": OrderEvent.FILLED,
    "CANCELLED": OrderEvent.CANCELLED,
    "CANCEL PENDING": OrderEvent.OPENED,
    "REJECTED": OrderEvent.REJECTED,
}


def event_for_status(status: str) -> str | None:
    """The event a Kite status means, or None if this system does not know it."""
    return KITE_STATUS_EVENTS.get((status or "").strip().upper())
