"""What a fill does to the lot history, on every path that makes one.

A fill is not the record. The record is the lot it bought or the lots it
consumed, and until one of those is written the shares exist only as a row in
a table nobody reconciles against. Two paths create fills -- the paper feed
and the broker's trade book -- and both have to reach the same place, or the
first live day writes fills with no basis behind them.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from gains.models import RealisedGain
from orders import booking, paperfill, reconcile
from orders.models import DAILY_BUY_STREAM, Fill, Order, OrderMode, OrderSide, OrderType
from orders.state import OrderState
from portfolio.charges import DEFAULT_RATES, cost_of
from portfolio.instruments import refresh
from portfolio.models import BasisState, Instrument, Lot

pytestmark = pytest.mark.django_db

TUESDAY = date(2026, 9, 22)
JANUARY = date(2024, 1, 15)


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
    from engine.models import TradeSheet, TradeSheetState

    return TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=4,
        spendable_cash=Decimal("7000"),
        reference_price=Decimal("1650.50"),
    )


def an_order(sheet, instrument, **kwargs):
    fields = {
        "tick_no": 0,
        "quantity": 4,
        "limit_price": Decimal("1650.00"),
        "order_type": OrderType.LIMIT,
        "side": OrderSide.BUY,
        "mode": OrderMode.PAPER,
        "state": OrderState.OPEN,
        "stream": DAILY_BUY_STREAM,
    }
    fields.update(kwargs)
    return Order.objects.create(
        tag=f"tx-{fields['side'].lower()}-{fields['tick_no']}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        trace_id=sheet.trace_id,
        **fields,
    )


_BOUGHT = iter(range(1000, 2000))


def _paper_buy_fill(instrument, quantity, cost):
    """The paper buy a paper lot came from: FIFO consumes only its own book's lots."""
    from engine.models import TradeSheet

    sheet, _ = TradeSheet.objects.get_or_create(
        trading_date=JANUARY, version=1, defaults={"instrument": instrument, "quantity": 0}
    )
    n = next(_BOUGHT)
    order = Order.objects.create(
        tag=f"bought-{n}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=JANUARY,
        tick_no=n,
        quantity=quantity,
        filled_quantity=quantity,
        limit_price=cost,
        order_type=OrderType.LIMIT,
        side=OrderSide.BUY,
        mode=OrderMode.PAPER,
        state=OrderState.COMPLETE,
        stream=DAILY_BUY_STREAM,
    )
    return Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-bought-{n}",
        quantity=quantity,
        price=cost,
        executed_at=timezone.now(),
    )


def a_lot(instrument, *, quantity=10, cost=Decimal("1000"), mode=OrderMode.PAPER):
    """A lot of *mode*'s book: a paper one comes from a paper buy fill."""
    return Lot.objects.create(
        instrument=instrument,
        acquired_on=JANUARY,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=cost,
        basis_state=BasisState.FINAL,
        created_from_fill=(
            _paper_buy_fill(instrument, quantity, cost) if mode == OrderMode.PAPER else None
        ),
    )


# -- a sell consumes lots, on every path (M36) -----------------------------


def test_a_paper_sell_fill_consumes_lots_fifo(instrument, sheet):
    """The rebalance sell is the deliberate way shares leave (M32, M34).

    Until this consumed lots, a sale filled, the order completed, and the lot
    history still said every share was held.
    """
    older = a_lot(instrument, quantity=6, cost=Decimal("1000"))
    newer = a_lot(instrument, quantity=6, cost=Decimal("1500"))
    an_order(
        sheet,
        instrument,
        side=OrderSide.SELL,
        quantity=8,
        limit_price=Decimal("1900"),
        stream="rebalance-1",
    )

    fills = paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1950"))
    assert len(fills) == 1

    older.refresh_from_db()
    newer.refresh_from_db()
    assert older.remaining_quantity == 0, "the oldest lot goes first"
    assert newer.remaining_quantity == 4, "and only the remainder comes from the next"

    realisations = list(RealisedGain.objects.order_by("acquired_on", "created_at"))
    assert [row.quantity for row in realisations] == [6, 2]
    assert all(row.fill_id == fills[0].fill.id for row in realisations)


def test_a_sell_fill_carries_its_charges_into_the_proceeds(instrument, sheet):
    """A charge is part of what the sale returned, exactly as it is part of
    what a purchase cost (D6)."""
    a_lot(instrument, quantity=10, cost=Decimal("1000"))
    an_order(
        sheet,
        instrument,
        side=OrderSide.SELL,
        quantity=4,
        limit_price=Decimal("1900"),
        stream="rebalance-1",
    )

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1900"))

    _gross, charges = cost_of(
        quantity=4, price=Decimal("1900"), side=OrderSide.SELL, exchange="NSE"
    )
    realisation = RealisedGain.objects.get()
    expected = (Decimal("1900") * 4) - charges.total
    assert realisation.net_proceeds == pytest.approx(expected, abs=Decimal("0.01"))
    assert realisation.gain == realisation.net_proceeds - realisation.cost


def test_a_sell_booked_twice_consumes_once(instrument, sheet):
    """Booking is idempotent: a replay must not consume the lots again."""
    a_lot(instrument, quantity=10)
    order = an_order(
        sheet,
        instrument,
        side=OrderSide.SELL,
        quantity=4,
        limit_price=Decimal("1900"),
        stream="rebalance-1",
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id="T-replay",
        quantity=4,
        price=Decimal("1900"),
        executed_at=timezone.now(),
    )

    booking.book(fill)
    booking.book(fill)

    assert RealisedGain.objects.count() == 1
    assert Lot.objects.get(instrument=instrument).remaining_quantity == 6


# -- a buy creates a lot, on every path (M35) ------------------------------


def test_a_live_buy_fill_from_the_trade_book_creates_a_lot(instrument, sheet):
    """`record_fills` is how a live day's fills arrive.

    It wrote the fill and stopped, so the first live day would have bought
    shares with no cost basis and left FIFO nothing to consume.
    """
    an_order(
        sheet,
        instrument,
        mode=OrderMode.LIVE,
        quantity=2,
        kite_order_id="250922000000001",
    )

    class Broker:
        def trades(self):
            return [
                {
                    "trade_id": "T1",
                    "order_id": "250922000000001",
                    "quantity": 2,
                    "average_price": 1500.0,
                    "fill_timestamp": timezone.now(),
                }
            ]

    assert reconcile.record_fills(trading_date=TUESDAY, broker=Broker()) == 1

    lot = Lot.objects.get(instrument=instrument)
    assert lot.quantity == 2
    assert lot.remaining_quantity == 2
    assert lot.basis_state == BasisState.ESTIMATED
    gross, charges = cost_of(quantity=2, price=Decimal("1500"), exchange="NSE")
    assert lot.cost_per_unit == pytest.approx((gross + charges.total) / 2, abs=Decimal("0.0001"))


def test_a_live_sell_fill_from_the_trade_book_consumes_lots(instrument, sheet):
    a_lot(instrument, quantity=10, cost=Decimal("1000"), mode=OrderMode.LIVE)
    an_order(
        sheet,
        instrument,
        side=OrderSide.SELL,
        mode=OrderMode.LIVE,
        quantity=3,
        limit_price=Decimal("1900"),
        stream="rebalance-1",
        kite_order_id="250922000000002",
    )

    class Broker:
        def trades(self):
            return [
                {
                    "trade_id": "T2",
                    "order_id": "250922000000002",
                    "quantity": 3,
                    "average_price": 1900.0,
                    "fill_timestamp": timezone.now(),
                }
            ]

    assert reconcile.record_fills(trading_date=TUESDAY, broker=Broker(), stream="rebalance-1") == 1
    assert Lot.objects.get(instrument=instrument).remaining_quantity == 7
    assert RealisedGain.objects.count() == 1


def test_a_fill_records_the_charges_it_was_booked_with(instrument, sheet):
    """The components stay on the fill, so a contract note can be checked
    against them line by line (S5)."""
    an_order(sheet, instrument, quantity=2)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1600"))

    fill = Fill.objects.get()
    _gross, charges = cost_of(quantity=2, price=Decimal("1600"), exchange="NSE")
    assert fill.charges == charges.total


# -- the depository charge is per scrip per day, not per fill --------------


def a_sell_fill(sheet, instrument, *, tick_no, quantity=1, price=Decimal("1900")):
    order = an_order(
        sheet,
        instrument,
        side=OrderSide.SELL,
        tick_no=tick_no,
        quantity=quantity,
        limit_price=price,
        stream="rebalance-1",
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id=f"T-dp-{tick_no}",
        quantity=quantity,
        price=price,
        executed_at=timezone.now(),
    )
    booking.book(fill)
    fill.refresh_from_db()
    return fill


def test_only_the_first_sell_fill_of_the_day_pays_the_depository_charge(instrument, sheet):
    """The sell ladder fills one slice at a time by design, so charging it
    per fill would bill a three-slice sale three times over."""
    a_lot(instrument, quantity=10)

    first = a_sell_fill(sheet, instrument, tick_no=0)
    second = a_sell_fill(sheet, instrument, tick_no=1)
    third = a_sell_fill(sheet, instrument, tick_no=2)

    dp = DEFAULT_RATES.dp_charge
    assert first.charges - second.charges == dp
    assert second.charges == third.charges


def test_the_days_sell_charges_carry_exactly_one_depository_charge(instrument, sheet):
    a_lot(instrument, quantity=10)

    fills = [a_sell_fill(sheet, instrument, tick_no=n) for n in range(4)]

    total = sum(fill.charges for fill in fills)
    without_dp = sum(
        cost_of(quantity=1, price=Decimal("1900"), side=OrderSide.SELL, dp_charged=True)[1].total
        for _ in fills
    )
    assert total - without_dp == DEFAULT_RATES.dp_charge


def test_a_buy_never_pays_a_depository_charge_however_many_slices(instrument, sheet):
    an_order(sheet, instrument, quantity=3)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1600"))

    fill = Fill.objects.get()
    _gross, charges = cost_of(quantity=3, price=Decimal("1600"), exchange="NSE")
    assert charges.dp_charge == Decimal("0.00")
    assert fill.charges == charges.total
