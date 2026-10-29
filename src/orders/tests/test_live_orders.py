"""The live order path, where the broker's book is the only witness.

A paper order fills against the tick feed inside this process, so a paper day
never has to ask the broker what happened. A live day always does: a postback
may never arrive, a placement may be refused outright or answered late, and a
cancel may fail because the order it names has already filled. Each test here
is one of those days, and each asks the same question -- does the record end
up saying what the broker says, without placing a share twice to find out?
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
import time_machine
from django.core.management import call_command
from django.utils import timezone

from audit.models import AuditEvent
from broker import paper
from broker.exceptions import (
    BrokerError,
    BrokerInputError,
    SessionExpired,
    TransientBrokerError,
)
from configuration.store import update as configure
from engine.models import TradeSheet, TradeSheetState
from orders import fallback, ladder, placement, reconcile
from orders.models import NoPlaceReason, Order, OrderType, QueuedPostback, Tick
from orders.state import OrderState
from portfolio.instruments import refresh
from portfolio.models import Instrument

pytestmark = pytest.mark.django_db

DAY = date(2026, 9, 23)  # a Wednesday
REFERENCE = Decimal("1650.50")


def at(hour, minute, second=0) -> datetime:
    return timezone.make_aware(
        datetime.combine(DAY, time(hour, minute, second)), timezone.get_current_timezone()
    )


class LiveBroker:
    """A broker with a book, a history per order, and cancels that can fail."""

    def __init__(
        self, *, book=None, history=None, cancel_fails=None, read_fails=False, trades=None
    ):
        self.book = book or []
        self.trade_book = trades or []
        self.trade_reads = 0
        self.history = history or {}
        self.cancel_fails = cancel_fails
        self.read_fails = read_fails
        self.cancelled: list[str] = []
        self.placed: list[dict] = []
        self.reads = 0

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return f"kite-{len(self.placed)}"

    def cancel_order(self, order_id):
        if self.cancel_fails is not None:
            raise self.cancel_fails
        self.cancelled.append(order_id)
        return order_id

    def orders(self):
        self.reads += 1
        if self.read_fails:
            raise TransientBrokerError("the order book did not answer")
        return self.book

    def order_history(self, order_id):
        return self.history.get(order_id, [])

    def trades(self):
        self.trade_reads += 1
        if isinstance(self.trade_book, Exception):
            raise self.trade_book
        return self.trade_book

    def quote(self, instruments):
        return {}


@pytest.fixture(autouse=True)
def no_rate_limit(db):
    configure(changed_by="test", max_orders_per_window=0)


@pytest.fixture
def instrument(db):
    refresh(
        [
            {
                "tradingsymbol": "INFY",
                "instrument_token": 408065,
                "name": "INFY",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "segment": "NSE",
                "tick_size": 0.05,
                "lot_size": 1,
            }
        ]
    )
    return Instrument.objects.get(pk="INFY")


@pytest.fixture
def sheet(instrument):
    return TradeSheet.objects.create(
        trading_date=DAY,
        version=1,
        trace_id="ts-2026-09-23",
        authorized_at=at(9, 0),
        authorized_by="operator",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=4,
        spendable_cash=Decimal("7000"),
        reference_price=REFERENCE,
    )


def a_live_order(
    sheet,
    *,
    tick_no=0,
    quantity=2,
    state=OrderState.OPEN,
    kite_order_id="",
    filled=0,
    age_seconds=0,
    mode="live",
):
    order = Order.objects.create(
        tag=f"tx-live-{tick_no}",
        tradesheet=sheet,
        instrument=sheet.instrument,
        trading_date=sheet.trading_date,
        tick_no=tick_no,
        mode=mode,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        filled_quantity=filled,
        limit_price=Decimal("1650.00"),
        state=state,
        kite_order_id=kite_order_id,
    )
    if age_seconds:
        Order.objects.filter(pk=order.pk).update(
            placed_at=order.placed_at - timedelta(seconds=age_seconds)
        )
        order.refresh_from_db()
    return order


def place_one(sheet, *, broker=None):
    return placement.place(
        tradesheet=sheet,
        instrument=sheet.instrument,
        tick_no=0,
        quantity=1,
        broker=broker or LiveBroker(),
        limit_price=Decimal("1650.00"),
    )


def broker_says(monkeypatch, failure):
    """Make the one call that sends raise *failure*."""

    def refuse(request, *, broker):
        raise failure

    monkeypatch.setattr("orders.placement.paper.place", refuse)


# -- a definite refusal is a rejection, not a question ---------------------


def test_a_malformed_order_the_broker_refused_is_rejected(sheet, monkeypatch):
    """Kite's InputException -- a bad tick, an unregistered address -- means
    nothing was placed. Leaving the row PLACED would count it as committed
    and later send its tag to Kite as though it were an order id."""
    broker_says(monkeypatch, BrokerInputError("Tick size for this script is 0.05"))

    outcome = place_one(sheet)

    outcome.order.refresh_from_db()
    assert outcome.order.state == OrderState.REJECTED
    assert outcome.sent
    assert not outcome.placed
    assert "Tick size" in outcome.order.rejection_reason


def test_a_dead_session_refusal_is_rejected(sheet, monkeypatch):
    broker_says(monkeypatch, SessionExpired("Incorrect `api_key` or `access_token`."))

    outcome = place_one(sheet)

    outcome.order.refresh_from_db()
    assert outcome.order.state == OrderState.REJECTED


def test_a_timeout_is_still_a_question_for_reconciliation(sheet, monkeypatch):
    broker_says(monkeypatch, TransientBrokerError("read timed out"))

    outcome = place_one(sheet)

    outcome.order.refresh_from_db()
    assert outcome.order.state == OrderState.PLACED
    assert outcome.order.rejection_reason.startswith("unconfirmed")


def test_repeated_input_refusals_halt_the_day(sheet, monkeypatch):
    broker_says(monkeypatch, BrokerInputError("market protection is invalid"))
    quotes = {
        "INFY": {
            "last_price": 1650.0,
            "depth": {
                "buy": [{"price": 1650.0, "quantity": 10}],
                "sell": [{"price": 1650.5, "quantity": 10}],
            },
        }
    }

    moment = at(9, 45)
    for _ in range(3):
        ladder.run_round(tradesheet=sheet, quotes=quotes, broker=LiveBroker(), moment=moment)
        moment += timedelta(seconds=30)

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.HALTED


# -- a cancel never names an order by its tag ------------------------------


def test_a_live_order_without_an_id_is_found_by_tag_before_it_is_cancelled(sheet):
    order = a_live_order(sheet, state=OrderState.PLACED)
    broker = LiveBroker(book=[{"tag": order.tag, "order_id": "251123000111", "status": "OPEN"}])

    placement.cancel(order, broker=broker)

    order.refresh_from_db()
    assert broker.cancelled == ["251123000111"]
    assert order.kite_order_id == "251123000111"
    assert order.state == OrderState.CANCELLED


def test_a_live_order_that_never_reached_the_broker_is_rejected_not_cancelled(sheet):
    order = a_live_order(sheet, state=OrderState.PLACED, age_seconds=300)
    broker = LiveBroker(book=[])

    placement.cancel(order, broker=broker)

    order.refresh_from_db()
    assert broker.cancelled == []
    assert order.state == OrderState.REJECTED
    assert "never reached the broker" in order.rejection_reason


def test_a_live_order_placed_a_moment_ago_is_not_given_up_on(sheet):
    """Its placement may still be on the wire. Calling it rejected now is
    how the same shares get placed twice."""
    order = a_live_order(sheet, state=OrderState.PLACED)
    broker = LiveBroker(book=[])

    with pytest.raises(BrokerError):
        placement.cancel(order, broker=broker)

    order.refresh_from_db()
    assert broker.cancelled == []
    assert order.state == OrderState.PLACED


def test_a_paper_order_is_cancelled_as_before(sheet):
    order = a_live_order(sheet, mode="paper", kite_order_id="paper-abc")
    broker = LiveBroker()

    placement.cancel(order, broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.CANCELLED
    assert broker.reads == 0


# -- a failed cancel asks the broker what became of the order --------------


def test_a_cancel_refused_because_the_order_filled_records_the_fill(sheet):
    order = a_live_order(sheet, kite_order_id="251123000222")
    broker = LiveBroker(
        cancel_fails=BrokerInputError("Order cannot be cancelled as it is being processed."),
        history={
            "251123000222": [
                {"order_id": "251123000222", "status": "OPEN", "filled_quantity": 0},
                {"order_id": "251123000222", "status": "COMPLETE", "filled_quantity": 2},
            ]
        },
    )

    placement.cancel(order, broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.COMPLETE
    assert order.filled_quantity == 2


def test_a_cancel_that_failed_after_the_broker_cancelled_keeps_the_part_fill(sheet):
    order = a_live_order(sheet, quantity=3, kite_order_id="251123000333")
    broker = LiveBroker(
        cancel_fails=TransientBrokerError("read timed out"),
        history={
            "251123000333": [
                {"order_id": "251123000333", "status": "CANCELLED", "filled_quantity": 1}
            ]
        },
    )

    placement.cancel(order, broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.CANCELLED
    assert order.filled_quantity == 1


def test_a_cancel_that_failed_on_an_order_still_open_is_unconfirmed(sheet):
    order = a_live_order(sheet, kite_order_id="251123000444")
    broker = LiveBroker(
        cancel_fails=TransientBrokerError("read timed out"),
        history={"251123000444": [{"status": "OPEN", "filled_quantity": 0}]},
    )

    with pytest.raises(BrokerError):
        placement.cancel(order, broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.OPEN


# -- reconciliation never races a placement in flight ----------------------


def test_an_order_placed_a_moment_ago_is_not_rejected_for_being_absent(sheet):
    order = a_live_order(sheet, state=OrderState.PLACED)

    result = reconcile.from_broker(trading_date=DAY, broker=LiveBroker(book=[]))

    order.refresh_from_db()
    assert order.state == OrderState.PLACED
    assert order.tag in result.unknown_to_broker


def test_an_order_absent_long_after_it_was_placed_is_rejected(sheet):
    order = a_live_order(sheet, state=OrderState.PLACED, age_seconds=300)

    reconcile.from_broker(trading_date=DAY, broker=LiveBroker(book=[]))

    order.refresh_from_db()
    assert order.state == OrderState.REJECTED


def test_an_order_the_broker_acknowledged_is_reported_not_rejected_when_absent(sheet):
    """It has a broker order id, so it reached the broker. Its absence from
    the book is a disagreement to raise, not a fact to write down."""
    order = a_live_order(
        sheet, state=OrderState.PLACED, kite_order_id="251123000555", age_seconds=300
    )

    result = reconcile.from_broker(trading_date=DAY, broker=LiveBroker(book=[]))

    order.refresh_from_db()
    assert order.state == OrderState.PLACED
    assert order.tag in result.unknown_to_broker
    assert AuditEvent.objects.filter(action="orders.missing_from_book").exists()


def test_a_placement_answered_after_reconciliation_rejected_it_is_restored(sheet, monkeypatch):
    """The call returned an order id: the order is at the exchange, whatever
    was concluded while the call was out. Leaving it REJECTED would size the
    next slice as though it did not exist."""

    def answered_late(request, *, broker):
        Order.objects.filter(tag=request.tag).update(
            state=OrderState.REJECTED, rejection_reason="not present in the order book"
        )
        return paper.Placement(order_id="251123000666", mode="live", sent=True)

    monkeypatch.setattr("orders.placement.paper.place", answered_late)

    outcome = place_one(sheet)

    order = Order.objects.get(pk=outcome.order.pk)
    assert order.state == OrderState.PLACED
    assert order.kite_order_id == "251123000666"
    assert order.rejection_reason == ""
    assert AuditEvent.objects.filter(action="order.rejection_withdrawn").exists()


# -- every round reads the book first --------------------------------------


def test_a_day_with_live_orders_is_levelled_with_the_book(sheet):
    order = a_live_order(sheet, kite_order_id="251123000777")
    broker = LiveBroker(
        book=[
            {
                "tag": order.tag,
                "order_id": "251123000777",
                "status": "COMPLETE",
                "filled_quantity": 2,
            }
        ]
    )

    reconcile.refresh_live_orders(trading_date=DAY, broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.COMPLETE
    assert order.filled_quantity == 2


def a_kite_trade(order, *, trade_id="10000001", quantity=2, price="1649.95"):
    """A trade-book row with the fields Kite Connect documents for `trades()`."""
    return {
        "trade_id": trade_id,
        "order_id": order.kite_order_id,
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "instrument_token": 408065,
        "product": "CNC",
        "average_price": float(price),
        "quantity": quantity,
        "exchange_order_id": "1300000001234567",
        "transaction_type": "BUY",
        "fill_timestamp": datetime(2026, 9, 23, 10, 15, 3),
        "order_timestamp": "10:15:03",
        "exchange_timestamp": datetime(2026, 9, 23, 10, 15, 3),
    }


def test_a_live_round_records_the_trade_books_fills_and_posts_them(sheet):
    """B2: a live day's fills, lots and journal entries land during the day,
    so an evening settle that fails leaves nothing unrecorded."""
    from journal.models import Book, EntryKind, JournalEntry
    from orders.models import Fill
    from portfolio.models import Lot

    order = a_live_order(sheet, kite_order_id="251123000777")
    broker = LiveBroker(
        book=[
            {
                "tag": order.tag,
                "order_id": "251123000777",
                "status": "COMPLETE",
                "filled_quantity": 2,
            }
        ],
        trades=[a_kite_trade(order)],
    )

    reconcile.refresh_live_orders(trading_date=DAY, broker=broker)
    reconcile.refresh_live_orders(trading_date=DAY, broker=broker)

    fill = Fill.objects.get(order=order)
    assert fill.kite_trade_id == "10000001"
    assert fill.quantity == 2
    assert fill.price == Decimal("1649.95")
    assert timezone.localtime(fill.executed_at).time() == time(10, 15, 3)
    assert Lot.objects.get(created_from_fill=fill).quantity == 2
    assert (
        JournalEntry.objects.filter(
            book=Book.LIVE, kind=EntryKind.BUY_FILL, source_id=str(fill.pk)
        ).count()
        == 1
    )
    # M9: Today's intraday cash line reads the day's fills, so it now sees them.
    from engine.settlement import day_spend

    assert day_spend(DAY).spent == Decimal("3299.90")


def test_a_trade_book_that_cannot_be_read_does_not_stop_the_round(sheet):
    order = a_live_order(sheet, kite_order_id="251123000777")
    broker = LiveBroker(
        book=[{"tag": order.tag, "order_id": "251123000777", "status": "OPEN"}],
        trades=TransientBrokerError("the trade book did not answer"),
    )

    result = reconcile.refresh_live_orders(trading_date=DAY, broker=broker)

    assert result is not None
    assert broker.trade_reads == 1


def test_a_paper_day_never_reads_the_book(sheet):
    a_live_order(sheet, mode="paper", kite_order_id="paper-1")
    broker = LiveBroker()

    reconcile.refresh_live_orders(trading_date=DAY, broker=broker)

    assert broker.reads == 0
    assert broker.trade_reads == 0


def test_an_unreadable_book_does_not_stop_the_round(sheet):
    order = a_live_order(sheet, kite_order_id="251123000888")

    result = reconcile.refresh_live_orders(trading_date=DAY, broker=LiveBroker(read_fails=True))

    order.refresh_from_db()
    assert result is None
    assert order.state == OrderState.OPEN


def test_queued_postbacks_are_drained_once_the_book_is_read(sheet):
    order = a_live_order(sheet, kite_order_id="251123000999")
    QueuedPostback.objects.create(
        order_id="251123000999",
        received_at_key="2026-09-23 10:00:00",
        payload={"order_id": "251123000999", "tag": order.tag},
    )
    broker = LiveBroker(book=[{"tag": order.tag, "order_id": "251123000999", "status": "OPEN"}])

    reconcile.refresh_live_orders(trading_date=DAY, broker=broker)

    assert not QueuedPostback.objects.filter(resolved_at__isnull=True).exists()


def commands_use(monkeypatch, broker):
    for command in ("ladder_tick", "market_fallback"):
        monkeypatch.setattr(f"orders.management.commands.{command}.session.attach", lambda b: True)
        monkeypatch.setattr(f"orders.management.commands.{command}.get_broker", lambda: broker)


def test_the_ladder_round_reads_the_book_before_it_sizes(sheet, monkeypatch):
    """A fill the postback never announced must be seen before the round
    decides the order is stale and cancels it."""
    order = a_live_order(sheet, kite_order_id="251123001000")
    broker = LiveBroker(
        book=[
            {
                "tag": order.tag,
                "order_id": "251123001000",
                "status": "COMPLETE",
                "filled_quantity": 2,
            }
        ]
    )
    commands_use(monkeypatch, broker)
    seen = []
    monkeypatch.setattr(
        "orders.ladder.run_round",
        lambda **_: seen.append(Order.objects.get(pk=order.pk).state) or [],
    )

    with time_machine.travel(at(10, 30), tick=False):
        call_command("ladder_tick", "--date", DAY.isoformat())

    assert seen == [OrderState.COMPLETE]


def test_the_ladder_round_runs_when_the_book_cannot_be_read(sheet, monkeypatch):
    a_live_order(sheet, kite_order_id="251123001100")
    commands_use(monkeypatch, LiveBroker(read_fails=True))
    seen = []
    monkeypatch.setattr("orders.ladder.run_round", lambda **_: seen.append(True) or [])

    with time_machine.travel(at(10, 30), tick=False):
        call_command("ladder_tick", "--date", DAY.isoformat())

    assert seen == [True]


def test_the_close_reads_the_book_before_it_cancels(sheet, monkeypatch):
    order = a_live_order(sheet, kite_order_id="251123001200")
    broker = LiveBroker(
        book=[
            {
                "tag": order.tag,
                "order_id": "251123001200",
                "status": "COMPLETE",
                "filled_quantity": 2,
            }
        ]
    )
    commands_use(monkeypatch, broker)
    seen = []

    def run(**_):
        seen.append(Order.objects.get(pk=order.pk).state)
        return fallback.FallbackOutcome(cancelled=0, placed=False)

    monkeypatch.setattr("orders.fallback.run", run)

    with time_machine.travel(at(15, 5), tick=False):
        call_command("market_fallback", "--date", DAY.isoformat())

    assert seen == [OrderState.COMPLETE]


# -- a rejection that arrives late still counts ----------------------------


def rejected_three_times(sheet):
    for tick_no in range(3):
        a_live_order(
            sheet,
            tick_no=tick_no,
            quantity=1,
            state=OrderState.REJECTED,
            kite_order_id=f"25112300130{tick_no}",
        )


def test_a_round_halts_a_day_the_book_says_was_refused_three_times(sheet):
    """Kite returned an id each time and rejected afterwards; the round's own
    placements never saw a refusal, so the round has to count them."""
    rejected_three_times(sheet)
    quotes = {"INFY": {"last_price": 1650.0, "depth": {}}}

    [outcome] = ladder.run_round(
        tradesheet=sheet, quotes=quotes, broker=LiveBroker(), moment=at(10, 30)
    )

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.HALTED
    assert outcome.reason == NoPlaceReason.DAY_HALTED
    assert Order.objects.count() == 3


def test_the_close_does_not_buy_a_day_the_book_says_was_refused_three_times(sheet):
    rejected_three_times(sheet)
    broker = LiveBroker()

    fallback.run(tradesheet=sheet, broker=broker, last_price=REFERENCE, moment=at(15, 25))

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.HALTED
    assert Order.objects.count() == 3
    assert not Tick.objects.filter(order__isnull=False).exists()


# -- the exchange's order number (NI1) --------------------------------------


def test_the_order_book_gives_an_order_its_exchange_order_id(sheet):
    """Console's tradebook names an order by the exchange's number, not
    Kite's; the order keeps both, so a past day's trades can find it."""
    order = a_live_order(sheet, kite_order_id="251123000777")
    broker = LiveBroker(
        book=[
            {
                "tag": order.tag,
                "order_id": "251123000777",
                "exchange_order_id": "1300000001234567",
                "status": "OPEN",
            }
        ]
    )

    reconcile.from_broker(trading_date=DAY, broker=broker)

    order.refresh_from_db()
    assert order.exchange_order_id == "1300000001234567"


def test_the_trade_book_gives_an_order_its_exchange_order_id(sheet):
    order = a_live_order(sheet, kite_order_id="251123000777")

    reconcile.record_fills(trading_date=DAY, broker=LiveBroker(trades=[a_kite_trade(order)]))

    order.refresh_from_db()
    assert order.exchange_order_id == "1300000001234567"


# -- a trade the round cannot book (I-B, Nm1, Nm2) ---------------------------


def test_a_trade_that_cannot_be_booked_does_not_stop_the_round(sheet, caplog):
    """The trade book's shape is unverified until the first live day, which
    is the day the ladder must keep running. A trade it cannot book is logged
    at ERROR and left for the settle, which refuses loudly; the round goes on."""
    import logging

    from orders.models import Fill

    order = a_live_order(sheet, kite_order_id="251123000777")
    broker = LiveBroker(
        book=[{"tag": order.tag, "order_id": "251123000777", "status": "OPEN"}],
        trades=[{**a_kite_trade(order), "quantity": "one"}],
    )

    with caplog.at_level(logging.ERROR, logger="steadystack.orders"):
        result = reconcile.refresh_live_orders(trading_date=DAY, broker=broker)

    assert result is not None
    assert not Fill.objects.exists()
    assert "the round could not record the trade book" in caplog.text


def test_a_trade_with_no_price_or_quantity_is_refused_not_booked(sheet, caplog):
    import logging

    from orders.models import Fill

    order = a_live_order(sheet, kite_order_id="251123000777")
    broker = LiveBroker(
        trades=[
            {**a_kite_trade(order, trade_id="T0"), "average_price": 0, "price": None},
            {**a_kite_trade(order, trade_id="T00"), "quantity": 0},
            a_kite_trade(order, trade_id="T1", quantity=1),
        ]
    )

    with caplog.at_level(logging.ERROR, logger="steadystack.orders"):
        recorded = reconcile.record_fills(trading_date=DAY, broker=broker)

    assert recorded == 1
    assert list(Fill.objects.values_list("kite_trade_id", flat=True)) == ["T1"]
    refused = [_logged(r).get("trade_id") for r in caplog.records if r.levelname == "ERROR"]
    assert refused == ["T0", "T00"]


def test_a_repeated_trade_id_on_another_order_is_refused_not_skipped(sheet, caplog):
    """If exchange trade numbers repeat across days, skipping by id alone
    drops a real fill without a word."""
    import logging

    from orders.models import Fill

    first = a_live_order(sheet, kite_order_id="251123000777")
    reconcile.record_fills(trading_date=DAY, broker=LiveBroker(trades=[a_kite_trade(first)]))
    second = a_live_order(sheet, tick_no=1, kite_order_id="251123000888")

    with caplog.at_level(logging.ERROR, logger="steadystack.orders"):
        recorded = reconcile.record_fills(
            trading_date=DAY, broker=LiveBroker(trades=[a_kite_trade(second)])
        )

    assert recorded == 0
    assert Fill.objects.filter(order=second).count() == 0
    (error,) = [_logged(r) for r in caplog.records if r.levelname == "ERROR"]
    assert error["trade_id"] == "10000001"
    assert error["held_by"] == first.tag


def _logged(record) -> dict:
    from audit.logging import _FIELDS_KEY

    return getattr(record, _FIELDS_KEY, {})
