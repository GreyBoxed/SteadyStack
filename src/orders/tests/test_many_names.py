"""A day that buys several names (M15): the placing rounds, the close and
the per-name overrides, each working one line at a time."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from engine.models import TradeSheet, TradeSheetLine, TradeSheetState
from orders import fallback, ladder, overrides, placement
from orders.models import Order, OrderState, Tick
from orders.tests.test_fallback import StubBroker
from orders.tests.test_ladder import RecordingBroker, master, quote
from portfolio.instruments import refresh
from portfolio.models import Instrument

pytestmark = pytest.mark.django_db

TUESDAY = date(2026, 9, 22)


def at(hour, minute, second=0):
    return timezone.make_aware(datetime.combine(TUESDAY, time(hour, minute, second)))


@pytest.fixture
def day(db):
    refresh([master("INFY", 1), master("ITC", 2)])
    sheet = TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        authorized_at=at(9, 0),
        authorized_by="operator",
        state=TradeSheetState.DRAFT,
        instrument_id="INFY",
        quantity=5,
        spendable_cash=Decimal("6000"),
        reference_price=Decimal("1650.50"),
    )
    TradeSheetLine.objects.create(
        tradesheet=sheet,
        instrument_id="INFY",
        rank=1,
        quantity=2,
        reference_price=Decimal("1650.50"),
    )
    TradeSheetLine.objects.create(
        tradesheet=sheet,
        instrument_id="ITC",
        rank=2,
        quantity=3,
        reference_price=Decimal("450.00"),
        price_source="close",
    )
    return sheet


QUOTES = {"INFY": quote("1650.00", "1650.50"), "ITC": quote("449.90", "450.00")}


def round_all(sheet, broker, moment):
    return ladder.run_round(tradesheet=sheet, quotes=QUOTES, broker=broker, moment=moment)


def fill_everything():
    for order in Order.objects.exclude(state__in=[OrderState.COMPLETE, OrderState.CANCELLED]):
        Order.objects.filter(pk=order.pk).update(
            state=OrderState.COMPLETE, filled_quantity=order.quantity
        )


def test_the_first_round_places_every_name(day):
    outcomes = round_all(day, RecordingBroker(), at(9, 45))

    assert all(outcome.placed for outcome in outcomes)
    assert sorted(o.instrument_id for o in Order.objects.all()) == ["INFY", "ITC"]
    assert Tick.objects.filter(tick_no=0).count() == 2  # one row per name


def test_a_name_that_is_fully_committed_stops_while_the_others_carry_on(day):
    broker = RecordingBroker()
    round_all(day, broker, at(12, 0))  # two of INFY's two, two of ITC's three
    fill_everything()

    last = {o.symbol: o for o in round_all(day, broker, at(13, 59, 30))}

    assert placement.committed_quantity(day, instrument=Instrument.objects.get(pk="INFY")) == 2
    assert last["INFY"].reason == "fully_committed"
    assert last["ITC"].placed


def test_the_fallback_buys_each_names_remainder_against_its_own_price(day):
    broker = StubBroker()

    outcome = fallback.run(
        tradesheet=day,
        broker=broker,
        last_prices={"INFY": Decimal("1651"), "ITC": Decimal("451")},
        moment=at(15, 25),
    )

    market = Order.objects.filter(is_fallback=True).order_by("instrument_id")
    assert [(o.instrument_id, o.quantity) for o in market] == [("INFY", 2), ("ITC", 3)]
    assert outcome.quantity == 5


def test_a_name_whose_price_ran_away_is_withheld_and_the_others_still_bought(day):
    broker = StubBroker()

    outcome = fallback.run(
        tradesheet=day,
        broker=broker,
        last_prices={"INFY": Decimal("1900"), "ITC": Decimal("451")},
        moment=at(15, 25),
    )

    assert [o.instrument_id for o in Order.objects.filter(is_fallback=True)] == ["ITC"]
    assert "INFY" in outcome.withheld_reason


def test_one_name_can_be_lowered_without_touching_the_others(day):
    overrides.adjust_line(
        day, symbol="ITC", quantity=1, actor="op", reason="too much ITC", broker=RecordingBroker()
    )

    day.refresh_from_db()
    assert {ln.instrument_id: ln.quantity for ln in day.buy_lines()} == {"INFY": 2, "ITC": 1}
    assert day.quantity == 3


def test_one_name_can_be_raised_while_the_cash_covers_it(day):
    overrides.adjust_line(
        day, symbol="ITC", quantity=4, actor="op", reason="more ITC", broker=RecordingBroker()
    )
    assert TradeSheetLine.objects.get(instrument_id="ITC").quantity == 4

    with pytest.raises(overrides.OverrideRefused, match="more than"):
        overrides.adjust_line(
            day, symbol="ITC", quantity=10, actor="op", reason="all in", broker=RecordingBroker()
        )


def test_skipping_a_name_cancels_its_open_orders_and_keeps_its_fills(day):
    broker = RecordingBroker()
    round_all(day, broker, at(9, 45))
    itc = Order.objects.get(instrument_id="ITC")
    Order.objects.filter(pk=itc.pk).update(state=OrderState.OPEN)

    overrides.adjust_line(
        day, symbol="ITC", quantity=0, actor="op", reason="not today", broker=broker, skip=True
    )

    itc.refresh_from_db()
    assert itc.state == OrderState.CANCELLED
    assert TradeSheetLine.objects.get(instrument_id="ITC").quantity == 0
    assert Order.objects.get(instrument_id="INFY").state != OrderState.CANCELLED


def test_a_name_the_day_does_not_buy_cannot_be_adjusted(day):
    Instrument.objects.create(tradingsymbol="TCS", instrument_token=3)
    with pytest.raises(overrides.OverrideRefused, match="not one of the names"):
        overrides.adjust_line(
            day, symbol="TCS", quantity=1, actor="op", reason="x", broker=RecordingBroker()
        )


def test_a_twelve_name_day_gets_every_name_out_within_the_limit(db):
    """Five orders per thirty seconds, twelve names: three rounds' worth of
    room, and every name placed without the breaker ever tripping."""
    import time_machine

    from configuration.store import update as configure

    symbols = [f"N{i:02d}" for i in range(12)]
    refresh([master(s, 100 + i) for i, s in enumerate(symbols)])
    configure(changed_by="op", max_orders_per_window=5, order_rate_window_seconds=30)
    sheet = TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.DRAFT,
        instrument_id=symbols[0],
        quantity=12,
        spendable_cash=Decimal("50000"),
        authorized_at=at(9, 0),
        authorized_by="op",
    )
    for rank, s in enumerate(symbols, start=1):
        TradeSheetLine.objects.create(
            tradesheet=sheet,
            instrument_id=s,
            rank=rank,
            quantity=1,
            reference_price=Decimal("100"),
        )
    quotes = {s: quote("99.95", "100.00") for s in symbols}

    placed_per_round = []
    for second in (0, 31, 62):
        moment = at(9, 45) + timedelta(seconds=second)
        with time_machine.travel(moment, tick=False):
            outcomes = ladder.run_round(
                tradesheet=sheet, quotes=quotes, broker=RecordingBroker(), moment=moment
            )
        placed_per_round.append(sum(o.placed for o in outcomes))

    assert placed_per_round == [5, 5, 2]
    assert Order.objects.count() == 12


def test_a_runaway_still_trips_the_breaker(day):
    """Placing past the limit outside the rounds is a loop, and the breaker
    stops it at the limit -- no allowance per name any more."""
    from configuration.store import update as configure

    configure(changed_by="op", max_orders_per_window=2, order_rate_window_seconds=30)
    line = day.buy_lines()[0]

    import time_machine

    with time_machine.travel(at(10, 30), tick=False), pytest.raises(placement.RateBreakerTripped):
        for tick_no in range(10):
            placement.place(
                tradesheet=day,
                instrument=line.instrument,
                tick_no=tick_no,
                quantity=1,
                broker=RecordingBroker(),
                limit_price=Decimal("1650"),
            )

    assert Order.objects.count() == 2


def test_the_command_reports_a_tripped_breaker_without_a_traceback(day, monkeypatch):
    from django.core.management import CommandError, call_command

    def trip(**_kwargs):
        raise placement.RateBreakerTripped(placed=9, allowed=5, window=30)

    monkeypatch.setattr("orders.management.commands.ladder_tick.session.attach", lambda b: True)
    monkeypatch.setattr("orders.management.commands.ladder_tick.get_broker", lambda: object())
    monkeypatch.setattr(
        "orders.management.commands.ladder_tick.Command._quotes_for", lambda self, lines, b: {}
    )
    monkeypatch.setattr("orders.ladder.run_round", trip)

    import time_machine

    with time_machine.travel(at(10, 30), tick=False):
        with pytest.raises(CommandError, match="Rate breaker tripped: 9 orders"):
            call_command("ladder_tick", "--date", TUESDAY.isoformat())
