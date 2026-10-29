"""The placing rounds: the schedule, the price, the shared rate limit, and
the rounds that place nothing.

The arithmetic is tested on its own in `test_schedule`. Here it is tested
against real rows, because the promises are about the day's record: it never
buys more than it decided to, it never leaves a gap that cannot be explained,
and on a day of many names it never starves one.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from configuration.store import current as current_configuration
from configuration.store import update as configure
from engine.models import TradeSheet, TradeSheetLine, TradeSheetState
from orders import ladder
from orders.models import DAILY_BUY_STREAM, NoPlaceReason, Order, Tick
from orders.state import OrderState
from portfolio.instruments import refresh
from portfolio.models import Instrument

TUESDAY = date(2026, 9, 22)


def master(symbol, token, tick="0.05"):
    return {
        "tradingsymbol": symbol,
        "instrument_token": token,
        "name": symbol,
        "exchange": "NSE",
        "instrument_type": "EQ",
        "segment": "NSE",
        "tick_size": float(tick),
        "lot_size": 1,
    }


def quote(bid="1650.00", ask="1650.50", last=None):
    return {
        "last_price": float(last if last is not None else bid),
        "depth": {
            "buy": [{"price": float(bid), "quantity": 100}] if bid else [],
            "sell": [{"price": float(ask), "quantity": 100}] if ask else [],
        },
    }


def depth(bid="1650.00", ask="1650.50"):
    """Just the book, for callers that build their own quote."""
    return quote(bid, ask)["depth"]


def at(hour, minute, second=0) -> datetime:
    return timezone.make_aware(
        datetime.combine(TUESDAY, time(hour, minute, second)), timezone.get_current_timezone()
    )


class RecordingBroker:
    def __init__(self):
        self.placed = []

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return f"kite-{len(self.placed)}"

    def cancel_order(self, order_id):
        return order_id


@pytest.fixture(autouse=True)
def no_rate_limit(db):
    """Most tests are about one name; the shared limit has tests of its own."""
    configure(changed_by="test", max_orders_per_window=0)


@pytest.fixture
def instrument(db):
    refresh([master("INFY", 408065, tick="0.05")])
    return Instrument.objects.get(pk="INFY")


@pytest.fixture
def sheet(instrument):
    return TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        authorized_at=at(9, 0),
        authorized_by="operator",
        state=TradeSheetState.DRAFT,
        instrument=instrument,
        quantity=4,
        spendable_cash=Decimal("7000"),
        reference_price=Decimal("1650.50"),
    )


def run(sheet, moment, *, quotes=None, stream=DAILY_BUY_STREAM, broker=None, rounds_run=None):
    return ladder.run_round(
        tradesheet=sheet,
        quotes=quotes if quotes is not None else {"INFY": quote()},
        broker=broker or RecordingBroker(),
        moment=moment,
        stream=stream,
        rounds_run=rounds_run,
    )


def fill_everything():
    for order in Order.objects.exclude(state__in=[OrderState.COMPLETE, OrderState.CANCELLED]):
        Order.objects.filter(pk=order.pk).update(
            state=OrderState.COMPLETE, filled_quantity=order.quantity
        )


def age(order_qs, seconds):
    """Move orders into the past; `placed_at` is auto_now_add."""
    for order in order_qs:
        Order.objects.filter(pk=order.pk).update(
            placed_at=order.placed_at - timedelta(seconds=seconds)
        )


def placed_at(order_qs, moment):
    """Pin when orders were placed, on the test's own clock."""
    order_qs.update(placed_at=moment)


# -- the round number comes from the clock --------------------------------


@pytest.mark.django_db
def test_round_zero_opens_the_window():
    assert ladder.round_number(at(9, 45), current_configuration()) == 0


@pytest.mark.django_db
def test_a_round_every_thirty_seconds():
    config = current_configuration()
    assert ladder.round_number(at(9, 45, 29), config) == 0
    assert ladder.round_number(at(9, 45, 30), config) == 1
    assert ladder.round_number(at(11, 20), config) == 190


@pytest.mark.django_db
def test_before_the_window_the_round_is_zero():
    assert ladder.round_number(at(9, 0), current_configuration()) == 0


# -- the schedule places what is owed --------------------------------------


def test_the_first_round_places_the_first_share_at_the_bid(sheet):
    [outcome] = run(sheet, at(9, 45))

    assert outcome.placed
    order = Order.objects.get()
    assert order.quantity == 1
    assert order.limit_price == Decimal("1650.00")


def test_a_round_keeps_the_days_vwap_from_the_quote(sheet):
    """Kite's quote carries the day's volume-weighted average; the board is
    priced from what rounds stored, so the round stores it with the book."""
    with_vwap = quote() | {"average_price": 1648.25}

    run(sheet, at(9, 45), quotes={"INFY": with_vwap})

    assert Tick.objects.get(order__isnull=False).vwap == Decimal("1648.25")


def test_a_quote_without_a_vwap_stores_none(sheet):
    run(sheet, at(9, 45))

    assert Tick.objects.get(order__isnull=False).vwap is None


def test_a_placed_round_records_the_book_it_read(sheet):
    run(sheet, at(9, 45))

    tick = Tick.objects.get(instrument_id="INFY")
    assert tick.best_bid == Decimal("1650.00")
    assert tick.best_ask == Decimal("1650.50")
    assert tick.depth_snapshot["buy"][0]["price"] == 1650.0


def test_the_day_starts_running_once_a_round_places(sheet):
    run(sheet, at(9, 45))
    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.RUNNING


def test_an_order_is_paper_by_default_and_never_reaches_the_broker(sheet):
    broker = RecordingBroker()
    run(sheet, at(9, 45), broker=broker)
    assert broker.placed == []
    assert Order.objects.get().mode == "paper"


def test_a_resting_order_holds_the_name(sheet):
    run(sheet, at(9, 45))
    [outcome] = run(sheet, at(9, 46))

    assert not outcome.placed
    assert outcome.reason == NoPlaceReason.RESTING
    assert Order.objects.count() == 1


def test_a_name_that_has_filled_its_schedule_waits(sheet):
    run(sheet, at(9, 45))
    fill_everything()

    [outcome] = run(sheet, at(9, 46))

    assert outcome.reason == NoPlaceReason.ON_SCHEDULE
    assert Order.objects.count() == 1


def test_a_name_behind_places_what_it_owes_and_pays_up(sheet):
    """Nothing filled by noon: the schedule says two of four are owed, and two
    hours behind is well past catching up, so it pays the ask."""
    [outcome] = run(sheet, at(12, 0))

    assert outcome.placed
    order = Order.objects.get()
    assert order.quantity == 3  # ceil(4 x 0.53) with one round's lookahead
    assert order.limit_price == Decimal("1650.50")


def test_a_little_behind_prices_between_the_bid_and_the_ask(sheet):
    """Behind by about half of `catch_up_minutes` bids about half the spread."""
    configure(changed_by="test", catch_up_minutes=30, max_orders_per_window=0)
    sheet.quantity = 255  # one share a minute across 09:45-14:00
    sheet.save(update_fields=["quantity"])

    run(sheet, at(10, 0), quotes={"INFY": quote(bid="1650.00", ask="1651.00")})

    price = Order.objects.get().limit_price
    assert Decimal("1650.00") < price < Decimal("1651.00")


def test_the_price_never_leaves_the_otr_band(sheet):
    """A wide book is still priced within 0.75% of the last trade."""
    run(sheet, at(12, 0), quotes={"INFY": quote(bid="1600.00", ask="1700.00", last="1650.00")})

    assert Order.objects.get().limit_price <= Decimal("1662.35")


def test_authorizing_late_spreads_what_is_left_rather_than_paying_up(sheet):
    sheet.authorized_at = at(12, 0)
    sheet.save(update_fields=["authorized_at"])

    run(sheet, at(12, 0))

    order = Order.objects.get()
    assert order.quantity == 1
    assert order.limit_price == Decimal("1650.00")


# -- an outage is re-spread; a market that will not fill is chased ---------


@pytest.fixture
def an_hour_in(sheet):
    """1,020 shares, two a round; the first hour's rounds ran and filled."""
    sheet.quantity = 1020
    sheet.save(update_fields=["quantity"])
    run(sheet, at(10, 45), rounds_run=set(range(121)))
    fill_everything()
    assert Order.objects.get().quantity == 242
    return sheet


def test_rounds_that_never_ran_are_spread_over_what_is_left(an_hour_in):
    """Down from 10:45 to 11:45: the restart owes a round's worth at the bid,
    not the missed hour at the ask."""
    run(an_hour_in, at(11, 45), rounds_run=set(range(121)) | {240})

    resumed = Order.objects.order_by("placed_at").last()
    assert resumed.quantity <= 5
    assert resumed.limit_price == Decimal("1650.00")


def test_an_hour_of_rounds_without_a_fill_still_catches_up(an_hour_in):
    """Every round ran and the market did not fill: that is behind, not an
    outage, so what is owed goes out and pays up."""
    run(an_hour_in, at(11, 45), rounds_run=set(range(241)))

    chased = Order.objects.order_by("placed_at").last()
    assert chased.quantity == 240
    assert chased.limit_price == Decimal("1650.50")


def test_missed_rounds_are_the_ones_with_no_claim():
    config = current_configuration()
    gaps = ladder.missed_rounds(
        start=at(9, 45), moment=at(9, 47), config=config, rounds_run={0, 1, 3}
    )
    assert gaps == [(at(9, 46), at(9, 46, 30))]


def test_consecutive_missed_rounds_are_one_gap():
    config = current_configuration()
    gaps = ladder.missed_rounds(start=at(9, 45), moment=at(9, 50), config=config, rounds_run={0})
    assert gaps == [(at(9, 45, 30), at(9, 50))]


# -- stale orders are cancelled, never modified ----------------------------


def test_a_stale_order_is_cancelled_and_replaced_next_round(sheet):
    run(sheet, at(9, 45))
    placed_at(Order.objects.all(), at(9, 45))

    [outcome] = run(sheet, at(9, 51))

    assert outcome.reason == NoPlaceReason.RESTING
    assert Order.objects.get().state == OrderState.CANCELLED

    [outcome] = run(sheet, at(9, 51, 30))
    assert outcome.placed
    assert Order.objects.count() == 2


def test_the_last_round_cancels_nothing_it_could_not_replace(sheet):
    run(sheet, at(13, 50))
    placed_at(Order.objects.all(), at(13, 50))

    run(sheet, at(13, 59, 30))

    assert Order.objects.get().state != OrderState.CANCELLED


def test_an_order_is_never_modified(sheet):
    run(sheet, at(9, 45))
    first = Order.objects.get()
    placed_at(Order.objects.all(), at(9, 45))
    run(sheet, at(9, 51))
    run(sheet, at(9, 51, 30))

    first.refresh_from_db()
    assert first.quantity == 1
    assert first.limit_price == Decimal("1650.00")


# -- the record ------------------------------------------------------------


def test_a_second_round_at_the_same_moment_writes_one_row(sheet):
    run(sheet, at(9, 45))
    run(sheet, at(9, 45))
    assert Tick.objects.filter(tick_no=0, instrument_id="INFY").count() == 1


def test_an_unchanged_answer_is_written_once(sheet):
    run(sheet, at(9, 45))
    for second in (30, 60, 90):
        run(sheet, at(9, 45) + timedelta(seconds=second))

    reasons = list(Tick.objects.order_by("tick_no").values_list("no_place_reason", flat=True))
    assert reasons == ["", NoPlaceReason.RESTING]


def test_a_locked_book_places_nothing_and_says_so(sheet):
    [outcome] = run(sheet, at(9, 45), quotes={"INFY": quote(bid="1650.00", ask="1650.00")})
    assert outcome.reason == NoPlaceReason.LOCKED_BOOK
    assert Order.objects.count() == 0


def test_a_one_sided_book_places_nothing(sheet):
    [outcome] = run(sheet, at(9, 45), quotes={"INFY": quote(bid="1650.00", ask=None)})
    assert outcome.reason == NoPlaceReason.LOCKED_BOOK


def test_no_quote_at_all_places_nothing(sheet):
    [outcome] = run(sheet, at(9, 45), quotes={})
    assert outcome.reason == NoPlaceReason.NO_DEPTH


def test_a_fully_committed_name_places_nothing(sheet):
    run(sheet, at(12, 0))
    fill_everything()
    run(sheet, at(13, 59))
    fill_everything()

    [outcome] = run(sheet, at(13, 59, 30))

    assert outcome.reason == NoPlaceReason.FULLY_COMMITTED
    assert outcome.tick is None, "a finished name writes no row; its orders say it"
    assert not Tick.objects.filter(no_place_reason=NoPlaceReason.FULLY_COMMITTED).exists()
    assert sum(Order.objects.values_list("quantity", flat=True)) == 4


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (TradeSheetState.SKIPPED, NoPlaceReason.DAY_SKIPPED),
        (TradeSheetState.HALTED, NoPlaceReason.DAY_HALTED),
        (TradeSheetState.AWAITING_SESSION, NoPlaceReason.NO_SESSION),
    ],
)
def test_a_stopped_or_parked_day_places_nothing(sheet, state, reason):
    sheet.state = state
    sheet.save(update_fields=["state"])

    [outcome] = run(sheet, at(9, 45))

    assert outcome.reason == reason
    assert outcome.tick.instrument_id is None
    assert Order.objects.count() == 0


def test_an_unauthorized_day_places_nothing(sheet):
    sheet.authorized_at = None
    sheet.save(update_fields=["authorized_at"])

    [outcome] = run(sheet, at(9, 45))

    assert outcome.reason == NoPlaceReason.NOT_AUTHORIZED


def test_a_round_outside_the_session_places_nothing(sheet):
    [outcome] = run(sheet, at(8, 0))
    assert outcome.reason == NoPlaceReason.OUTSIDE_SESSION


def test_a_round_after_the_window_closes_places_nothing(sheet):
    [outcome] = run(sheet, at(14, 0))
    assert outcome.reason == NoPlaceReason.OUTSIDE_SESSION
    assert "window closed" in outcome.tick.detail


def test_the_stream_separates_a_batch_from_the_daily_buy(sheet):
    run(sheet, at(9, 45))
    run(sheet, at(9, 45), stream="rebalance-1")

    assert Tick.objects.filter(tick_no=0).count() == 2
    assert Tick.objects.filter(stream=DAILY_BUY_STREAM).count() == 1


# -- the whole day ---------------------------------------------------------


def test_a_days_worth_of_rounds_buys_exactly_the_quantity_and_finishes_in_time(sheet):
    """The property that keeps the day from over-buying, and the one that
    keeps the spread honest: the last share goes out before the window shuts."""
    moment = at(9, 45)
    last_placed = None
    while moment < at(14, 0):
        outcomes = run(sheet, moment)
        if any(o.placed for o in outcomes):
            last_placed = moment
        fill_everything()
        moment += timedelta(seconds=30)

    assert sum(Order.objects.values_list("quantity", flat=True)) == 4
    assert last_placed is not None and last_placed < at(14, 0)
    placed_at = sorted(Tick.objects.filter(order__isnull=False).values_list("tick_no", flat=True))
    assert len(placed_at) == 4
    assert placed_at[-1] - placed_at[0] > 300  # spread across the day, not bunched


def test_a_large_day_places_more_than_one_share_a_round(sheet):
    sheet.quantity = 2000
    sheet.save(update_fields=["quantity"])

    run(sheet, at(9, 45))

    assert Order.objects.get().quantity == 4  # ceil(2000 x 30 / 15300)


# -- the shared rate limit -------------------------------------------------


@pytest.fixture
def seven_names(db):
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]
    refresh([master(symbol, 1000 + n) for n, symbol in enumerate(symbols)])
    sheet = TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        authorized_at=at(9, 0),
        authorized_by="operator",
        state=TradeSheetState.RUNNING,
        instrument_id="AAA",
        quantity=14,
        spendable_cash=Decimal("50000"),
        reference_price=Decimal("100"),
    )
    for rank, symbol in enumerate(symbols, start=1):
        TradeSheetLine.objects.create(
            tradesheet=sheet,
            instrument_id=symbol,
            rank=rank,
            quantity=2,
            reference_price=Decimal("100"),
        )
    configure(changed_by="test", max_orders_per_window=5, order_rate_window_seconds=30)
    return sheet, {symbol: quote(bid="100.00", ask="100.10") for symbol in symbols}


def test_a_round_places_no_more_than_the_limit_leaves(seven_names):
    sheet, quotes = seven_names

    outcomes = run(sheet, at(9, 45), quotes=quotes)

    assert sum(o.placed for o in outcomes) == 5
    waiting = [o for o in outcomes if o.reason == NoPlaceReason.WAITING_TURN]
    assert len(waiting) == 2


def test_the_names_that_waited_go_first_next_round(seven_names):
    sheet, quotes = seven_names
    first = run(sheet, at(9, 45), quotes=quotes)
    waited = {o.symbol for o in first if o.reason == NoPlaceReason.WAITING_TURN}
    fill_everything()
    age(Order.objects.all(), 31)

    second = run(sheet, at(9, 45, 30), quotes=quotes)

    assert waited <= {o.symbol for o in second if o.placed}


def test_the_most_behind_name_goes_first(seven_names):
    """With room for one order, the round picks the name furthest behind its
    schedule -- not the first in rank order."""
    sheet, quotes = seven_names
    TradeSheetLine.objects.filter(instrument_id__in=["FFF", "GGG"]).update(quantity=40)
    for symbol, filled in (
        ("AAA", 1),
        ("BBB", 1),
        ("CCC", 1),
        ("DDD", 1),
        ("EEE", 1),
        ("FFF", 10),
        ("GGG", 5),
    ):
        Order.objects.create(
            tag=f"done-{symbol}",
            tradesheet=sheet,
            instrument_id=symbol,
            trading_date=TUESDAY,
            tick_no=0,
            mode="paper",
            order_type="LIMIT",
            quantity=filled,
            filled_quantity=filled,
            limit_price=Decimal("100"),
            state=OrderState.COMPLETE,
        )
    age(Order.objects.all(), 60)
    configure(changed_by="test", max_orders_per_window=1, order_rate_window_seconds=30)

    outcomes = run(sheet, at(11, 0), quotes=quotes)

    [placed] = [o for o in outcomes if o.placed]
    assert placed.symbol == "GGG"
    assert {o.symbol for o in outcomes if o.reason == NoPlaceReason.WAITING_TURN} == {"FFF"}


def test_a_full_window_places_nothing_and_says_why(seven_names):
    sheet, quotes = seven_names
    run(sheet, at(9, 45), quotes=quotes)
    fill_everything()

    # By noon every name owes another share, and the window is still full.
    outcomes = run(sheet, at(12, 0), quotes=quotes)

    assert not any(o.placed for o in outcomes)
    assert {o.reason for o in outcomes if o.tick} >= {NoPlaceReason.WAITING_TURN}


def test_the_round_never_trips_the_breaker(seven_names):
    sheet, quotes = seven_names
    moment = at(9, 45)
    for _ in range(20):
        run(sheet, moment, quotes=quotes)  # raises if the breaker trips
        fill_everything()
        moment += timedelta(seconds=10)


# -- halting a day the broker keeps refusing (§11) -------------------------


@pytest.fixture
def refusing(monkeypatch):
    """Make the broker refuse the next `n` placements, at the one call that sends."""
    from broker import paper
    from broker.exceptions import OrderRejected

    state = {"refusals": 0, "sent": 0}
    real = paper.place

    def refuse(request, *, broker):
        state["sent"] += 1
        if state["refusals"] > 0:
            state["refusals"] -= 1
            raise OrderRejected("price is out of the current band")
        return real(request, broker=broker)

    monkeypatch.setattr("orders.placement.paper.place", refuse)

    def refuse_next(count):
        state["refusals"] = count
        return state

    return refuse_next


def rounds(sheet, count, start=(9, 45)):
    moment = at(*start)
    for _ in range(count):
        run(sheet, moment)
        moment += timedelta(seconds=30)


def test_two_rejections_do_not_halt_the_day(sheet, refusing):
    refusing(2)
    rounds(sheet, 2)

    sheet.refresh_from_db()
    assert sheet.state != TradeSheetState.HALTED


def test_three_consecutive_rejections_halt_the_day(sheet, refusing):
    refusing(3)
    rounds(sheet, 3)

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.HALTED


def test_a_halted_day_places_nothing_further(sheet, refusing):
    state = refusing(3)
    rounds(sheet, 5)

    assert state["sent"] == 3
    assert Tick.objects.filter(no_place_reason=NoPlaceReason.DAY_HALTED).exists()


def test_a_rejection_run_broken_by_an_accepted_order_does_not_halt(sheet, refusing):
    refusing(2)
    rounds(sheet, 3)  # rejected, rejected, accepted
    fill_everything()
    refusing(2)
    rounds(sheet, 2, start=(12, 0))

    sheet.refresh_from_db()
    assert sheet.state != TradeSheetState.HALTED


def test_halting_is_audited(sheet, refusing):
    from audit.models import AuditEvent

    refusing(3)
    rounds(sheet, 3)

    assert AuditEvent.objects.filter(action="tradesheet.halted").exists()
