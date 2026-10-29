"""The order state machine.

Enumerated rather than asserted: every state is asked about every event, so
the pairs nobody thought about are covered by construction rather than by
whoever remembered to write the test.
"""

import pytest

from orders.state import (
    TRANSITIONS,
    IllegalTransition,
    OrderEvent,
    OrderState,
    event_for_status,
    is_terminal,
    next_state,
)

EVERY_STATE = list(OrderState)
EVERY_EVENT = list(OrderEvent)


# -- the legal path --------------------------------------------------------


def test_a_placed_order_opens():
    assert next_state(OrderState.PLACED, OrderEvent.OPENED) == OrderState.OPEN


def test_an_open_order_fills():
    assert next_state(OrderState.OPEN, OrderEvent.FILLED) == OrderState.COMPLETE


def test_an_open_order_fills_in_part():
    assert next_state(OrderState.OPEN, OrderEvent.PARTIAL_FILL) == OrderState.PARTIALLY_FILLED


def test_a_partially_filled_order_completes():
    assert next_state(OrderState.PARTIALLY_FILLED, OrderEvent.FILLED) == OrderState.COMPLETE


def test_a_partially_filled_order_can_be_cancelled():
    """The fills stand; what was left goes."""
    assert next_state(OrderState.PARTIALLY_FILLED, OrderEvent.CANCELLED) == OrderState.CANCELLED


def test_a_placed_order_can_be_rejected():
    assert next_state(OrderState.PLACED, OrderEvent.REJECTED) == OrderState.REJECTED


def test_a_fill_can_arrive_before_the_acceptance():
    """Postbacks overtake each other, and the fill is the more important fact."""
    assert next_state(OrderState.PLACED, OrderEvent.FILLED) == OrderState.COMPLETE


# -- what must not happen --------------------------------------------------


def test_a_complete_order_cannot_reopen():
    with pytest.raises(IllegalTransition):
        next_state(OrderState.COMPLETE, OrderEvent.OPENED)


def test_a_complete_order_cannot_be_cancelled():
    """It has already filled. There is nothing to cancel, and pretending
    otherwise would unwind a purchase that really happened."""
    with pytest.raises(IllegalTransition):
        next_state(OrderState.COMPLETE, OrderEvent.CANCELLED)


def test_a_cancelled_order_cannot_fill():
    with pytest.raises(IllegalTransition):
        next_state(OrderState.CANCELLED, OrderEvent.FILLED)


def test_a_rejected_order_cannot_open():
    with pytest.raises(IllegalTransition):
        next_state(OrderState.REJECTED, OrderEvent.OPENED)


def test_an_unknown_state_is_refused():
    with pytest.raises(IllegalTransition):
        next_state("INVENTED", OrderEvent.OPENED)


# -- duplicates and reordering (D8) ---------------------------------------


def test_a_repeated_fill_on_a_complete_order_is_a_no_op():
    """A duplicate postback is a repeat of the news, not a contradiction."""
    assert next_state(OrderState.COMPLETE, OrderEvent.FILLED) == OrderState.COMPLETE


def test_a_repeated_cancellation_is_a_no_op():
    assert next_state(OrderState.CANCELLED, OrderEvent.CANCELLED) == OrderState.CANCELLED


def test_a_repeated_rejection_is_a_no_op():
    assert next_state(OrderState.REJECTED, OrderEvent.REJECTED) == OrderState.REJECTED


def test_a_second_open_leaves_an_open_order_open():
    assert next_state(OrderState.OPEN, OrderEvent.OPENED) == OrderState.OPEN


def test_more_of_a_partial_fill_stays_partially_filled():
    assert (
        next_state(OrderState.PARTIALLY_FILLED, OrderEvent.PARTIAL_FILL)
        == OrderState.PARTIALLY_FILLED
    )


# -- the table as a whole --------------------------------------------------


@pytest.mark.parametrize("state", EVERY_STATE)
@pytest.mark.parametrize("event", EVERY_EVENT)
def test_every_pair_either_transitions_or_raises(state, event):
    """No pair is undefined: the machine has an answer for everything, and
    that answer is either a state or a refusal."""
    try:
        result = next_state(state, event)
    except IllegalTransition:
        return
    assert result in set(OrderState)


@pytest.mark.parametrize("state", EVERY_STATE)
def test_every_state_is_in_the_table(state):
    assert state in TRANSITIONS


def test_terminal_states_accept_nothing_new():
    for state in (OrderState.COMPLETE, OrderState.CANCELLED, OrderState.REJECTED):
        assert TRANSITIONS[state] == {}
        assert is_terminal(state) is True


def test_live_states_are_not_terminal():
    for state in (OrderState.PLACED, OrderState.OPEN, OrderState.PARTIALLY_FILLED):
        assert is_terminal(state) is False


# -- reading Kite's own statuses ------------------------------------------


@pytest.mark.parametrize(
    ("status", "event"),
    [
        ("OPEN", OrderEvent.OPENED),
        ("COMPLETE", OrderEvent.FILLED),
        ("CANCELLED", OrderEvent.CANCELLED),
        ("REJECTED", OrderEvent.REJECTED),
        ("PUT ORDER REQ RECEIVED", OrderEvent.OPENED),
        ("VALIDATION PENDING", OrderEvent.OPENED),
        # Still on its way to the exchange, or resting there: open, never an
        # answer, and never a status to stop reconciling over.
        ("OPEN PENDING", OrderEvent.OPENED),
        ("AMO REQ RECEIVED", OrderEvent.OPENED),
        ("MODIFY PENDING", OrderEvent.OPENED),
        ("CANCEL PENDING", OrderEvent.OPENED),
        ("TRIGGER PENDING", OrderEvent.OPENED),
    ],
)
def test_kite_statuses_map_to_events(status, event):
    assert event_for_status(status) == event


def test_status_matching_ignores_case_and_padding():
    assert event_for_status("  complete ") == OrderEvent.FILLED


def test_an_unknown_status_is_not_guessed_at():
    """A status this system has never seen is not one it should act on."""
    assert event_for_status("SOMETHING NEW") is None


def test_an_empty_status_is_not_an_event():
    assert event_for_status("") is None
