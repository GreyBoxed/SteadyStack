"""The command that runs a rebalance batch's rounds and its close.

It runs before the buy round, and takes at most one order: the buy round's own
count of the rate window then leaves it the rest, so a sale is never starved
and the two together never exceed the limit. Each round of each batch is
claimed once, under task names of its own, so the buy's round counts stay the
buy's. Nothing places once the day's ledger row exists.
"""

from decimal import Decimal

import pytest
import time_machine
from django.core.management import call_command

from engine.models import CashLedger
from orders import idempotency
from orders.models import Order, OrderSide, OrderType, TaskRun
from rebalance.tests.test_batches import StubBroker, universe  # noqa: F401
from rebalance.tests.test_ladder import QUOTES, a_running_batch, at

pytestmark = pytest.mark.django_db


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(
        "rebalance.management.commands.rebalance_tick.session.attach", lambda b: True
    )
    monkeypatch.setattr(
        "rebalance.management.commands.rebalance_tick.get_broker", lambda: StubBroker()
    )
    monkeypatch.setattr(
        "rebalance.management.commands.rebalance_tick.Command._quotes_for",
        lambda self, batch, broker: QUOTES,
    )


def tick(moment):
    with time_machine.travel(moment, tick=False):
        call_command("rebalance_tick", "--date", "2026-09-22")


def sells(**filters):
    return Order.objects.filter(side=OrderSide.SELL, **filters)


def test_a_round_places_at_most_one_sell(universe, wired):  # noqa: F811
    a_running_batch()

    tick(at(9, 45))

    assert sells().count() == 1


def test_a_round_is_claimed_once_under_the_rebalance_task(universe, wired):  # noqa: F811
    batch = a_running_batch()

    tick(at(9, 45))
    tick(at(9, 45, 10))  # the same round again

    assert sells().count() == 1
    run = TaskRun.objects.get(stream=batch.stream)
    assert run.task == idempotency.REBALANCE


def test_nothing_happens_without_a_running_batch(universe, wired):  # noqa: F811
    tick(at(9, 45))

    assert not TaskRun.objects.exists()


def test_nothing_is_placed_once_the_day_has_settled(universe, wired):  # noqa: F811
    a_running_batch()
    CashLedger.objects.create(
        book="paper", trading_date=at(9, 45).date(), daily_allocation=Decimal("1000")
    )

    tick(at(9, 45))

    assert not sells().exists()


@pytest.mark.usefixtures("live_book_open")
def test_nothing_sells_once_the_system_is_switched_to_live(universe, wired, monkeypatch):  # noqa: F811
    """Paper only is checked every round, not just at execute: a live sale's
    fills are not yet recorded."""
    from configuration.models import OrderMode
    from configuration.store import update

    a_running_batch()
    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: "203.0.113.7")
    update(changed_by="operator", order_mode=OrderMode.LIVE, registered_static_ip="203.0.113.7")

    tick(at(9, 45))

    assert not sells().exists()


def test_several_batches_share_one_order_a_round(universe, wired):  # noqa: F811
    """At most one sell a round across every batch, so buys are never starved."""
    a_running_batch(b"symbol,quantity\nINFY,4\n")
    a_running_batch(b"symbol,quantity\nTCS,4\n")

    tick(at(9, 45))

    assert sells().count() == 1


def test_a_quote_failure_costs_that_batch_its_round_and_no_more(universe, wired, monkeypatch):  # noqa: F811
    from broker.exceptions import TransientBrokerError

    first = a_running_batch(b"symbol,quantity\nINFY,4\n")
    a_running_batch(b"symbol,quantity\nTCS,4\n")

    def quotes(self, batch, broker):
        if batch.pk == first.pk:
            raise TransientBrokerError("quote timed out")
        return QUOTES

    monkeypatch.setattr("rebalance.management.commands.rebalance_tick.Command._quotes_for", quotes)

    tick(at(9, 45))

    assert list(sells().values_list("instrument_id", flat=True)) == ["TCS"]


def test_the_close_needs_no_quotes(universe, wired, monkeypatch):  # noqa: F811
    def no_quotes(self, batch, broker):
        raise AssertionError("the close asked for quotes")

    monkeypatch.setattr(
        "rebalance.management.commands.rebalance_tick.Command._quotes_for", no_quotes
    )
    a_running_batch(b"symbol,quantity\nINFY,4\n")

    tick(at(15, 0))

    assert sells(order_type=OrderType.MARKET).count() == 1


def test_a_busy_lock_is_not_my_turn(universe, wired, capsys):  # noqa: F811
    a_running_batch()

    with idempotency.order_lock():
        tick(at(9, 45))

    assert "Another order-touching task is running." in capsys.readouterr().out
    assert not sells().exists()


def test_between_the_placement_close_and_the_close_nothing_happens(universe, wired):  # noqa: F811
    a_running_batch()

    tick(at(14, 30))

    assert not sells().exists()


def test_the_close_window_sells_at_market_under_its_own_task(universe, wired):  # noqa: F811
    batch = a_running_batch(b"symbol,quantity\nINFY,4\n")

    tick(at(15, 0))

    market = sells(order_type=OrderType.MARKET).get()
    assert market.quantity == 4
    assert TaskRun.objects.get(stream=batch.stream).task == idempotency.REBALANCE_CLOSE
