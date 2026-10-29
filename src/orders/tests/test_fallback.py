"""The 15:00 fallback.

Two behaviours carry the day's money. The market order is sized from fills
re-read *after* every cancellation is confirmed, never from the snapshot
before them -- an order that fills in the moments before its cancel lands is
exactly the case that over-buys. And the order is withheld entirely when the
market has moved outside the sanity band, because an unfilled day is cheaper
than a day's cash spent into a circuit move.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from audit.models import AuditEvent
from configuration.store import update as configure
from engine.models import TradeSheet, TradeSheetState
from orders import fallback
from orders.models import Order, OrderType, Tick
from orders.state import OrderState
from portfolio.instruments import refresh
from portfolio.models import Instrument

TUESDAY = date(2026, 9, 22)
REFERENCE = Decimal("1650.50")


def moment(hour, minute, second=0):
    from datetime import datetime, time

    return timezone.make_aware(
        datetime.combine(TUESDAY, time(hour, minute, second)), timezone.get_current_timezone()
    )


def close_out(**kwargs):
    """One round of the close; the last one unless a moment is given, so the
    whole remainder is due and the tests below read as they did before pacing."""
    kwargs.setdefault("moment", moment(15, 25))
    return fallback.run(**kwargs)


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


class StubBroker:
    """A broker that can be told what its order book says, and can fail."""

    def __init__(self, *, book=None, cancel_fails=False):
        self.book = book or []
        self.cancel_fails = cancel_fails
        self.cancelled = []
        self.placed = []

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return f"kite-{len(self.placed)}"

    def cancel_order(self, order_id):
        if self.cancel_fails:
            from broker.exceptions import TransientBrokerError

            raise TransientBrokerError("the cancel did not confirm")
        self.cancelled.append(order_id)
        return order_id

    def orders(self):
        return self.book


@pytest.fixture
def instrument(db):
    refresh([master("INFY", 408065)])
    return Instrument.objects.get(pk="INFY")


@pytest.fixture
def sheet(instrument):
    return TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        authorized_at=timezone.now(),
        authorized_by="operator",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=4,
        spendable_cash=Decimal("7000"),
        reference_price=REFERENCE,
    )


def an_order(sheet, instrument, *, tick_no, quantity, state=OrderState.OPEN, filled=0):
    return Order.objects.create(
        tag=f"tx-test-{tick_no}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        tick_no=tick_no,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=quantity,
        filled_quantity=filled,
        limit_price=Decimal("1650.00"),
        state=state,
    )


# -- the sanity band (M22a) ------------------------------------------------


def test_a_price_at_the_reference_is_within_the_band():
    assert within(Decimal("1650.50")) is True


def test_a_small_move_is_within_the_band():
    assert within(Decimal("1700.00")) is True


def test_a_large_move_up_is_outside_the_band():
    assert within(Decimal("1800.00")) is False


def test_a_large_move_down_is_outside_the_band():
    """A circuit move either way is a reason not to buy blind."""
    assert within(Decimal("1400.00")) is False


def test_a_missing_reference_is_never_within_the_band():
    assert (
        fallback.within_band(
            last_price=Decimal("1650"), reference=Decimal("0"), band_pct=Decimal("5")
        )
        is False
    )


def test_a_zero_last_price_is_never_within_the_band():
    assert within(Decimal("0")) is False


def within(last_price: Decimal) -> bool:
    return fallback.within_band(
        last_price=last_price, reference=REFERENCE, band_pct=Decimal("5.00")
    )


# -- cancelling ------------------------------------------------------------


@pytest.mark.django_db
def test_every_open_order_is_cancelled(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=1)
    an_order(sheet, instrument, tick_no=1, quantity=1)
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_price=REFERENCE)

    assert outcome.cancelled == 2
    assert Order.objects.filter(state=OrderState.CANCELLED).count() == 2


@pytest.mark.django_db
def test_a_terminal_order_is_not_cancelled_again(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=1, state=OrderState.COMPLETE, filled=1)
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_price=REFERENCE)

    assert outcome.cancelled == 0


@pytest.mark.django_db
def test_nothing_is_sized_while_a_cancellation_is_unconfirmed(sheet, instrument):
    """An order believed cancelled that is in fact resting is a share bought
    twice -- once by it, once by the market order sized as though it were
    gone."""
    an_order(sheet, instrument, tick_no=0, quantity=2)
    broker = StubBroker(cancel_fails=True)

    with pytest.raises(fallback.CancellationsUnconfirmed):
        close_out(tradesheet=sheet, broker=broker, last_price=REFERENCE)

    assert broker.placed == []


# -- sizing from fills read after the cancels ------------------------------


@pytest.mark.django_db
def test_the_market_order_covers_only_what_is_unfilled(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)
    an_order(sheet, instrument, tick_no=1, quantity=2)
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_price=REFERENCE)

    assert outcome.quantity == 2


@pytest.mark.django_db
def test_a_fill_landing_during_cancellation_is_not_bought_twice(sheet, instrument):
    """The reason the fills are re-read after the cancels rather than before:
    this order fills in the moment between the two."""
    order = an_order(sheet, instrument, tick_no=0, quantity=4)
    broker = StubBroker(book=[{"tag": order.tag, "filled_quantity": 3, "status": "CANCELLED"}])

    outcome = close_out(tradesheet=sheet, broker=broker, last_price=REFERENCE)

    assert outcome.quantity == 1


@pytest.mark.django_db
def test_a_day_that_filled_in_full_places_no_market_order(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=4, state=OrderState.COMPLETE, filled=4)
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_price=REFERENCE)

    assert outcome.placed is False
    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.FILLED


@pytest.mark.django_db
def test_the_market_order_is_a_market_order(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=4)
    broker = StubBroker()

    close_out(tradesheet=sheet, broker=broker, last_price=REFERENCE)

    market = Order.objects.get(is_fallback=True)
    assert market.order_type == OrderType.MARKET
    assert market.limit_price is None


def test_the_market_order_is_protected_by_the_sanity_band(sheet, instrument, monkeypatch):
    """The exchange rejects a market order without market protection; the
    band is what the day already allows the price to move, so it is the value."""
    sent = []
    real = fallback.placement.paper.place

    def spy(request, *, broker):
        sent.append(request)
        return real(request, broker=broker)

    monkeypatch.setattr(fallback.placement.paper, "place", spy)
    an_order(sheet, instrument, tick_no=0, quantity=4)

    close_out(tradesheet=sheet, broker=StubBroker(), last_price=REFERENCE)

    assert sent[-1].order_type == OrderType.MARKET
    assert sent[-1].market_protection == Decimal("5.00")


# -- withholding -----------------------------------------------------------


@pytest.mark.django_db
def test_a_market_order_is_withheld_outside_the_band(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=4)
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_price=Decimal("1900"))

    assert outcome.withheld is True
    assert Order.objects.filter(is_fallback=True).count() == 0


@pytest.mark.django_db
def test_the_cancellations_still_stand_when_the_order_is_withheld(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=4)
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_price=Decimal("1900"))

    assert outcome.cancelled == 1
    assert Order.objects.filter(state=OrderState.CANCELLED).count() == 1


@pytest.mark.django_db
def test_a_withheld_day_with_no_fills_is_unfilled(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=4)

    close_out(tradesheet=sheet, broker=StubBroker(), last_price=Decimal("1900"))

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.UNFILLED


@pytest.mark.django_db
def test_a_withheld_day_with_some_fills_is_partial(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)
    an_order(sheet, instrument, tick_no=1, quantity=2)

    close_out(tradesheet=sheet, broker=StubBroker(), last_price=Decimal("1900"))

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.PARTIAL


@pytest.mark.django_db
def test_withholding_raises_an_alert(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=4)

    close_out(tradesheet=sheet, broker=StubBroker(), last_price=Decimal("1900"))

    event = AuditEvent.objects.get(action="fallback.withheld")
    assert "sanity" in event.after["reason"] or "reference" in event.after["reason"]


@pytest.mark.django_db
def test_the_band_is_configurable(sheet, instrument):
    configure(changed_by="operator", fallback_band_pct=Decimal("20.00"))
    an_order(sheet, instrument, tick_no=0, quantity=4)

    outcome = close_out(tradesheet=sheet, broker=StubBroker(), last_price=Decimal("1900"))

    assert outcome.placed is True


# -- the day's record ------------------------------------------------------


@pytest.mark.django_db
def test_the_fallback_writes_its_own_tick_row(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=4)

    close_out(tradesheet=sheet, broker=StubBroker(), last_price=REFERENCE)

    tick = Tick.objects.get(tick_no__gte=fallback.FALLBACK_TICK)
    assert tick.slice_qty == 4


@pytest.mark.django_db
def test_a_withheld_fallback_records_why(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=4)

    close_out(tradesheet=sheet, broker=StubBroker(), last_price=Decimal("1900"))

    tick = Tick.objects.get(tick_no__gte=fallback.FALLBACK_TICK)
    assert "reference" in tick.detail


# -- a day that has already decided not to buy -----------------------------


def a_line(sheet, instrument, *, quantity=4):
    from engine.models import TradeSheetLine

    return TradeSheetLine.objects.create(
        tradesheet=sheet,
        instrument=instrument,
        rank=1,
        quantity=quantity,
        reference_price=REFERENCE,
    )


@pytest.mark.django_db
def test_a_skipped_day_is_not_bought_at_market(sheet, instrument):
    """The operator's skip has to survive the timer that runs after it.

    An override never rewrites the ranking snapshot (M15a), so the sheet keeps
    its lines -- which left the fallback everything it needed to buy the whole
    day at market, and it did.
    """
    from engine.models import SkipReason

    a_line(sheet, instrument)
    sheet.state = TradeSheetState.SKIPPED
    sheet.skip_reason = SkipReason.OPERATOR
    sheet.save(update_fields=["state", "skip_reason"])
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_prices={"INFY": REFERENCE})

    sheet.refresh_from_db()
    assert not outcome.placed
    assert broker.placed == []
    assert sheet.state == TradeSheetState.SKIPPED
    assert sheet.skip_reason == SkipReason.OPERATOR


@pytest.mark.django_db
def test_a_skipped_day_still_has_its_open_orders_cancelled(sheet, instrument):
    """Not buying is not the same as leaving orders resting overnight."""
    from engine.models import SkipReason

    a_line(sheet, instrument)
    order = an_order(sheet, instrument, tick_no=0, quantity=2)
    sheet.state = TradeSheetState.SKIPPED
    sheet.skip_reason = SkipReason.OPERATOR
    sheet.save(update_fields=["state", "skip_reason"])
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_prices={"INFY": REFERENCE})

    order.refresh_from_db()
    assert outcome.cancelled == 1
    assert order.state == OrderState.CANCELLED
    assert broker.placed == []


@pytest.mark.django_db
def test_a_halted_day_is_not_bought_at_market(sheet, instrument):
    """HALTED means the broker kept refusing this day's orders.

    Following that with one unbounded market order is the single worst thing
    available, so the fallback does not.
    """
    a_line(sheet, instrument)
    sheet.state = TradeSheetState.HALTED
    sheet.save(update_fields=["state"])
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_prices={"INFY": REFERENCE})

    sheet.refresh_from_db()
    assert not outcome.placed
    assert broker.placed == []
    assert sheet.state == TradeSheetState.HALTED


@pytest.mark.django_db
def test_a_partly_filled_skipped_day_keeps_its_fills(sheet, instrument):
    """A skip is refused once anything has filled, so this is the shape a
    reduction to what filled leaves behind: the fills stand, nothing more is
    bought, and the day is not called skipped."""
    a_line(sheet, instrument, quantity=2)
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)
    broker = StubBroker()

    outcome = close_out(tradesheet=sheet, broker=broker, last_prices={"INFY": REFERENCE})

    sheet.refresh_from_db()
    assert not outcome.placed
    assert broker.placed == []


# -- pacing (M22) ------------------------------------------------------------


def test_the_first_round_buys_a_share_of_the_remainder_not_all_of_it(sheet, instrument):
    """Fifty rounds from 15:00 to 15:25: a fifty-share remainder is one a round."""
    sheet.quantity = 50
    sheet.save(update_fields=["quantity"])
    configure(changed_by="test", max_orders_per_window=0)

    outcome = close_out(
        tradesheet=sheet, broker=StubBroker(), last_price=REFERENCE, moment=moment(15, 0)
    )

    assert outcome.quantity == 1
    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.RUNNING, "not settled while rounds remain"


def test_a_later_round_leaves_the_closes_own_orders_alone(sheet, instrument):
    sheet.quantity = 50
    sheet.save(update_fields=["quantity"])
    configure(changed_by="test", max_orders_per_window=0)
    close_out(tradesheet=sheet, broker=StubBroker(), last_price=REFERENCE, moment=moment(15, 0))
    market = Order.objects.get(is_fallback=True)

    close_out(tradesheet=sheet, broker=StubBroker(), last_price=REFERENCE, moment=moment(15, 0, 30))

    market.refresh_from_db()
    assert market.state != OrderState.CANCELLED
    assert Order.objects.filter(is_fallback=True).count() == 2


def test_the_close_places_no_more_than_the_rate_limit_leaves(db):
    """Fifteen names at 15:00 with room for five: five now, the rest wait."""
    from engine.models import TradeSheetLine

    symbols = [f"N{i:02d}" for i in range(15)]
    refresh([master(s, 500 + i) for i, s in enumerate(symbols)])
    configure(changed_by="test", max_orders_per_window=5, order_rate_window_seconds=30)
    sheet = TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        authorized_at=timezone.now(),
        authorized_by="operator",
        state=TradeSheetState.RUNNING,
        instrument_id=symbols[0],
        quantity=15,
        spendable_cash=Decimal("50000"),
        reference_price=Decimal("100"),
    )
    for rank, symbol in enumerate(symbols, start=1):
        TradeSheetLine.objects.create(
            tradesheet=sheet,
            instrument_id=symbol,
            rank=rank,
            quantity=1,
            reference_price=Decimal("100"),
        )

    outcome = close_out(
        tradesheet=sheet,
        broker=StubBroker(),
        last_prices={s: Decimal("100") for s in symbols},
        moment=moment(15, 0),
    )

    assert Order.objects.filter(is_fallback=True).count() == 5
    assert outcome.waiting == 10


def test_a_withheld_name_is_said_once_not_every_round(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=4)
    far = REFERENCE * Decimal("1.2")

    for minute_second in ((0, 0), (0, 30), (1, 0)):
        close_out(
            tradesheet=sheet, broker=StubBroker(), last_price=far, moment=moment(15, *minute_second)
        )

    assert AuditEvent.objects.filter(action="fallback.withheld").count() == 1


def test_a_close_run_by_hand_before_its_window_buys_the_whole_remainder(sheet, instrument):
    sheet.quantity = 50
    sheet.save(update_fields=["quantity"])
    configure(changed_by="test", max_orders_per_window=0)

    outcome = close_out(
        tradesheet=sheet, broker=StubBroker(), last_price=REFERENCE, moment=moment(12, 0)
    )

    assert outcome.quantity == 50
