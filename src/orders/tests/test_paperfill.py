"""Paper fills against the live feed, and the lots they create.

The point of these is that a paper day exercises the same path a live day
will: a fill row, a state transition, a lot with a real basis. Nothing here
invents a price -- every fill is caused by a price the market actually
traded.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from orders import paperfill
from orders.models import Fill, Order, OrderMode, OrderSide, OrderType
from orders.state import OrderState
from portfolio import lots
from portfolio.charges import cost_of, estimate
from portfolio.instruments import refresh
from portfolio.models import BasisState, Lot

TUESDAY = date(2026, 9, 22)


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
    from portfolio.models import Instrument

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


def an_order(
    sheet,
    instrument,
    *,
    tick_no=0,
    quantity=2,
    limit=Decimal("1650.00"),
    order_type=OrderType.LIMIT,
    side=OrderSide.BUY,
    mode=OrderMode.PAPER,
    state=OrderState.OPEN,
):
    return Order.objects.create(
        tag=f"tx-paper-{tick_no}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        tick_no=tick_no,
        side=side,
        mode=mode,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit,
        state=state,
    )


# -- would it have filled? -------------------------------------------------


@pytest.mark.django_db
def test_a_buy_fills_when_the_price_reaches_its_limit(sheet, instrument):
    order = an_order(sheet, instrument, limit=Decimal("1650.00"))
    assert paperfill.would_fill(order, Decimal("1650.00")) is True


@pytest.mark.django_db
def test_a_buy_fills_when_the_price_is_better(sheet, instrument):
    order = an_order(sheet, instrument, limit=Decimal("1650.00"))
    assert paperfill.would_fill(order, Decimal("1649.00")) is True


@pytest.mark.django_db
def test_a_buy_does_not_fill_above_its_limit(sheet, instrument):
    order = an_order(sheet, instrument, limit=Decimal("1650.00"))
    assert paperfill.would_fill(order, Decimal("1650.05")) is False


@pytest.mark.django_db
def test_a_sell_mirrors_the_buy(sheet, instrument):
    order = an_order(sheet, instrument, side=OrderSide.SELL, limit=Decimal("1650.00"))

    assert paperfill.would_fill(order, Decimal("1650.50")) is True
    assert paperfill.would_fill(order, Decimal("1649.50")) is False


@pytest.mark.django_db
def test_a_market_order_fills_at_whatever_traded(sheet, instrument):
    order = an_order(sheet, instrument, order_type=OrderType.MARKET, limit=None)
    assert paperfill.would_fill(order, Decimal("9999")) is True


@pytest.mark.django_db
def test_a_terminal_order_never_fills(sheet, instrument):
    order = an_order(sheet, instrument, state=OrderState.CANCELLED)
    assert paperfill.would_fill(order, Decimal("1")) is False


# -- what it fills at ------------------------------------------------------


@pytest.mark.django_db
def test_a_buy_never_pays_more_than_its_limit(sheet, instrument):
    order = an_order(sheet, instrument, limit=Decimal("1650.00"))
    assert paperfill.fill_price_for(order, Decimal("1649.00")) == Decimal("1649.00")


@pytest.mark.django_db
def test_a_market_order_pays_what_traded(sheet, instrument):
    """Which is why the fallback can overspend, and why carry may go
    negative."""
    order = an_order(sheet, instrument, order_type=OrderType.MARKET, limit=None)
    assert paperfill.fill_price_for(order, Decimal("1700")) == Decimal("1700")


# -- applying a tick -------------------------------------------------------


@pytest.mark.django_db
def test_a_tick_fills_a_matching_order(sheet, instrument):
    an_order(sheet, instrument, quantity=2, limit=Decimal("1650.00"))

    filled = paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.50"))

    assert len(filled) == 1
    assert Fill.objects.count() == 1


@pytest.mark.django_db
def test_a_filled_order_becomes_complete(sheet, instrument):
    order = an_order(sheet, instrument, quantity=2)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))

    order.refresh_from_db()
    assert order.state == OrderState.COMPLETE
    assert order.filled_quantity == 2


@pytest.mark.django_db
def test_a_tick_that_does_not_reach_the_limit_fills_nothing(sheet, instrument):
    an_order(sheet, instrument, limit=Decimal("1650.00"))

    filled = paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1655.00"))

    assert filled == []
    assert Fill.objects.count() == 0


@pytest.mark.django_db
def test_a_second_tick_does_not_fill_the_same_order_twice(sheet, instrument):
    """Two fills of one order would buy the shares twice."""
    an_order(sheet, instrument, quantity=2)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))
    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))

    assert Fill.objects.count() == 1


@pytest.mark.django_db
def test_several_resting_orders_fill_on_one_tick(sheet, instrument):
    """The ladder leaves orders resting; a move through them fills them all."""
    an_order(sheet, instrument, tick_no=0, quantity=1, limit=Decimal("1650.00"))
    an_order(sheet, instrument, tick_no=1, quantity=1, limit=Decimal("1650.50"))

    filled = paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))

    assert len(filled) == 2


@pytest.mark.django_db
def test_a_live_order_is_not_filled_by_the_paper_path(sheet, instrument):
    """A live order fills at the exchange, and is reconciled from it."""
    an_order(sheet, instrument, mode=OrderMode.LIVE)

    assert paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1000")) == []


@pytest.mark.django_db
def test_a_paper_trade_id_cannot_be_mistaken_for_a_brokers(sheet, instrument):
    an_order(sheet, instrument)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))

    assert Fill.objects.get().kite_trade_id.startswith(paperfill.PAPER_TRADE_PREFIX)


# -- the lot a fill creates (D11) -----------------------------------------


@pytest.mark.django_db
def test_a_buy_fill_creates_a_lot(sheet, instrument):
    an_order(sheet, instrument, quantity=2)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))

    lot = Lot.objects.get()
    assert lot.quantity == 2
    assert lot.remaining_quantity == 2
    assert lot.instrument_id == "INFY"


@pytest.mark.django_db
def test_the_lot_carries_the_charges_in_its_basis(sheet, instrument):
    """D6: there is one cost number, and every charge is part of it."""
    an_order(sheet, instrument, quantity=2)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))

    lot = Lot.objects.get()
    gross, charges = cost_of(quantity=2, price=Decimal("1649.00"))
    assert lot.cost_per_unit > Decimal("1649.00")
    assert lot.cost_per_unit == ((gross + charges.total) / 2).quantize(Decimal("0.0001"))


@pytest.mark.django_db
def test_the_lot_starts_estimated_and_waits_for_the_contract_note(sheet, instrument):
    an_order(sheet, instrument, quantity=2)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))

    assert Lot.objects.get().basis_state == BasisState.ESTIMATED


@pytest.mark.django_db
def test_the_lot_is_dated_by_the_fill_not_by_today(sheet, instrument):
    an_order(sheet, instrument, quantity=2)
    executed = timezone.make_aware(
        timezone.datetime(2026, 9, 22, 10, 5), timezone.get_current_timezone()
    )

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"), at=executed)

    assert Lot.objects.get().acquired_on == TUESDAY


@pytest.mark.django_db
def test_one_lot_per_fill_not_per_order(sheet, instrument):
    """An order filling in parts at different prices is several bases, which
    is what actually happened."""
    an_order(sheet, instrument, tick_no=0, quantity=1, limit=Decimal("1650.00"))
    an_order(sheet, instrument, tick_no=1, quantity=1, limit=Decimal("1651.00"))

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))

    assert Lot.objects.count() == 2


@pytest.mark.django_db
def test_lots_are_readable_in_fifo_order(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=1)
    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1649.00"))

    assert lots.held_quantity("INFY") == 1
    assert lots.total_basis("INFY") > Decimal("1649")


@pytest.mark.django_db
def test_a_sell_fill_consumes_lots_rather_than_creating_one(sheet, instrument):
    """Sells consume lots; that is the FIFO path, not this one."""
    # A paper lot -- bought by an earlier paper fill -- since a paper sale
    # consumes only its own book's lots.
    bought = an_order(sheet, instrument, tick_no=9, quantity=5, state=OrderState.COMPLETE)
    held = Lot.objects.create(
        instrument=instrument,
        acquired_on=date(2024, 1, 15),
        quantity=5,
        remaining_quantity=5,
        cost_per_unit=Decimal("1000"),
        basis_state=BasisState.FINAL,
        created_from_fill=Fill.objects.create(
            order=bought,
            kite_trade_id="paper-bought",
            quantity=5,
            price=Decimal("1000"),
            executed_at=timezone.now(),
        ),
    )
    an_order(sheet, instrument, side=OrderSide.SELL, limit=Decimal("1600.00"), quantity=2)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1650.00"))

    held.refresh_from_db()
    assert Lot.objects.count() == 1, "no new lot: a sell does not buy anything"
    assert held.remaining_quantity == 3
    assert Fill.objects.filter(order__side=OrderSide.SELL).count() == 1


def test_a_sell_with_nothing_behind_it_still_records_the_fill(sheet, instrument):
    """The exchange did it, so the fill is a fact.

    The lots not covering it is a divergence for the operator to classify
    (M31a), found by the evening scan -- not a reason to refuse the record and
    retry for ever.
    """
    an_order(sheet, instrument, side=OrderSide.SELL, limit=Decimal("1600.00"), quantity=2)

    paperfill.apply_tick(instrument_id="INFY", traded_price=Decimal("1650.00"))

    assert Fill.objects.count() == 1
    assert Lot.objects.count() == 0


# -- charges ---------------------------------------------------------------


def test_a_buy_pays_stamp_duty_and_no_depository_charge():
    charges = estimate(turnover=Decimal("100000"), side="BUY")
    assert charges.stamp_duty > 0
    assert charges.dp_charge == Decimal("0.00")


def test_a_sell_pays_the_depository_charge_and_no_stamp_duty():
    charges = estimate(turnover=Decimal("100000"), side="SELL")
    assert charges.stamp_duty == Decimal("0.00")
    assert charges.dp_charge > 0


def test_gst_falls_on_the_service_charges_not_on_the_taxes():
    charges = estimate(turnover=Decimal("100000"), side="BUY")
    expected = (charges.brokerage + charges.exchange_txn + charges.sebi_turnover) * Decimal("0.18")
    assert charges.gst == expected.quantize(Decimal("0.01"))


def test_the_components_are_kept_separately():
    """A basis that can be taken apart is one that can be checked against a
    contract note line by line (S5)."""
    charges = estimate(turnover=Decimal("100000"), side="BUY")
    assert set(charges.as_dict()) >= {"stamp_duty", "gst", "securities_transaction_tax", "total"}


def test_a_zero_turnover_costs_nothing():
    assert estimate(turnover=Decimal("0"), side="BUY").total == Decimal("0.00")


# -- the published rate card ----------------------------------------------
#
# These pin the figures to the broker's schedule as read on 22 September 2026.
# Every one of them moves -- with a Union Budget, an exchange circular, a GST
# change or a depository revision -- and when one does, this file is where the
# change is noticed. A failing test here is the point: it means somebody must
# look at the schedule and decide, rather than the basis drifting quietly.


def test_delivery_brokerage_is_nil():
    assert estimate(turnover=Decimal("100000"), side="BUY").brokerage == Decimal("0.00")


def test_stt_is_a_tenth_of_a_percent_on_both_sides():
    on_buy = estimate(turnover=Decimal("100000"), side="BUY")
    on_sell = estimate(turnover=Decimal("100000"), side="SELL")

    assert on_buy.securities_transaction_tax == Decimal("100.00")
    assert on_sell.securities_transaction_tax == Decimal("100.00")


def test_the_nse_transaction_charge():
    """0.00307% of turnover."""
    assert estimate(turnover=Decimal("100000"), side="BUY").exchange_txn == Decimal("3.07")


def test_the_bse_transaction_charge_differs():
    """0.00375%. The instrument knows which exchange it is on."""
    charges = estimate(turnover=Decimal("100000"), side="BUY", exchange="BSE")
    assert charges.exchange_txn == Decimal("3.75")


def test_sebi_charges_are_ten_rupees_per_crore():
    assert estimate(turnover=Decimal("10000000"), side="BUY").sebi_turnover == Decimal("10.00")


def test_stamp_duty_is_fifteen_hundred_per_crore_on_the_buy():
    on_buy = estimate(turnover=Decimal("10000000"), side="BUY")
    on_sell = estimate(turnover=Decimal("10000000"), side="SELL")

    assert on_buy.stamp_duty == Decimal("1500.00")
    assert on_sell.stamp_duty == Decimal("0.00")


def test_the_depository_charge_is_flat_and_only_on_a_sale():
    """Rs 3.50 CDSL + Rs 9.50 broker + Rs 2.34 GST, per scrip."""
    on_sell = estimate(turnover=Decimal("100000"), side="SELL")

    assert on_sell.dp_charge == Decimal("15.34")
    assert estimate(turnover=Decimal("1"), side="SELL").dp_charge == Decimal("15.34")


def test_the_depository_charge_is_not_taxed_twice():
    """Its GST is already inside the figure, so it is excluded from the 18%."""
    charges = estimate(turnover=Decimal("100000"), side="SELL")
    expected_gst = (charges.brokerage + charges.exchange_txn + charges.sebi_turnover) * Decimal(
        "0.18"
    )

    assert charges.gst == expected_gst.quantize(Decimal("0.01"))


def test_a_realistic_delivery_buy_adds_up():
    """One INFY share at 1,029.40, the price the live feed showed."""
    gross, charges = cost_of(quantity=1, price=Decimal("1029.40"))

    assert gross == Decimal("1029.40")
    assert charges.securities_transaction_tax == Decimal("1.03")
    assert charges.stamp_duty == Decimal("0.15")
    assert charges.total == (
        charges.brokerage
        + charges.securities_transaction_tax
        + charges.exchange_txn
        + charges.sebi_turnover
        + charges.stamp_duty
        + charges.gst
        + charges.dp_charge
    )
