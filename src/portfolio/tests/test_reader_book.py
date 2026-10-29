"""Which book's lots a reader sees (I1, M-1).

Live, every reader sees the live lots only: a paper lot is shares that were
never bought, and counting them raises divergences the operator would
"fix" with live journal entries. Paper, the readers see every lot as they
always have -- except the paper engine's own portfolio (ranking, and the
holdings a paper settle writes), which is the paper lots and nothing else.
"""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from orders.models import OrderMode
from portfolio.models import BasisState, Holding, Instrument, Lot

pytestmark = pytest.mark.django_db

IST = ZoneInfo("Asia/Kolkata")
LONG_AGO = date(2025, 3, 3)
DAY = date(2026, 9, 22)


@pytest.fixture
def names(db):
    for symbol, token in (("INFY", 408065), ("TCS", 2953217)):
        Instrument.objects.create(tradingsymbol=symbol, instrument_token=token, name=symbol)


def a_live_lot(symbol, quantity, *, cost="100", acquired_on=LONG_AGO):
    """A lot with no fill behind it -- an opening import or an external buy,
    both of which are live."""
    return Lot.objects.create(
        instrument_id=symbol,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal(cost),
        basis_state=BasisState.FINAL,
    )


def a_paper_lot(symbol, quantity, *, price=Decimal("100"), on=LONG_AGO):
    """A lot as a paper fill makes one."""
    from engine.models import TradeSheet, TradeSheetState
    from orders.models import Fill, Order, OrderType
    from portfolio import lots

    sheet = TradeSheet.objects.create(
        trading_date=on,
        version=TradeSheet.objects.filter(trading_date=on).count() + 1,
        trace_id=f"ts-{on}",
        state=TradeSheetState.RUNNING,
        instrument_id=symbol,
        quantity=quantity,
    )
    order = Order.objects.create(
        tag=f"tx-paper-{symbol}-{on}",
        tradesheet=sheet,
        instrument_id=symbol,
        trading_date=on,
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=price,
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-{symbol}-{on}",
        quantity=quantity,
        price=price,
        executed_at=datetime.combine(on, datetime.min.time(), tzinfo=IST).replace(hour=10),
    )
    return lots.from_fill(fill)


def test_the_reader_book_is_live_in_live_mode_and_every_book_in_paper(already_live):
    from portfolio.lots import reader_book

    assert reader_book() is None
    already_live()
    assert reader_book() == OrderMode.LIVE


def test_divergence_scan_ignores_paper_lots_in_live_mode(names, already_live):
    """The T1 probe inverted: paper lots, then the broker's holdings synced
    live, and the evening scan finds nothing to raise."""
    from records import divergence

    a_paper_lot("INFY", 3)
    Holding.objects.create(instrument_id="INFY", quantity=0)
    already_live()

    result = divergence.scan(detected_on=DAY)

    assert result.opened == []


def test_the_live_scan_still_raises_a_real_difference(names, already_live):
    from records import divergence

    a_paper_lot("INFY", 3)
    a_live_lot("INFY", 5)
    Holding.objects.create(instrument_id="INFY", quantity=4)
    already_live()

    result = divergence.scan(detected_on=DAY)

    assert [(d.expected_quantity, d.actual_quantity) for d in result.opened] == [(5, 4)]


def test_gains_and_weights_read_live_lots_only_in_live_mode(names, already_live):
    from gains import fifo
    from performance import gains, valuation

    paper = a_paper_lot("INFY", 3)
    a_live_lot("TCS", 2, cost="300")
    fifo.consume(
        instrument_id="INFY",
        quantity=1,
        proceeds_per_unit=Decimal("150"),
        disposed_on=DAY,
        fill=_a_paper_sell(paper),
    )
    already_live()
    prices = {"INFY": Decimal("150"), "TCS": Decimal("400")}

    assert valuation.held_quantities() == {"TCS": 2}
    assert valuation.invested_to_date() == Decimal("600.0000")
    assert set(valuation.value_now(prices).per_instrument) == {"TCS"}
    assert [row.instrument_id for row in gains.unrealised(prices).rows] == ["TCS"]
    assert gains.realised().rows == []


def test_paper_mode_still_reads_every_lot(names):
    from performance import valuation

    a_paper_lot("INFY", 3)
    a_live_lot("TCS", 2)

    assert valuation.held_quantities() == {"INFY": 3, "TCS": 2}


def test_paper_holdings_are_set_from_paper_lots_only(names):
    """An opening lot of the same name is real shares, which the broker's
    record already holds; the paper settle writes what the paper book holds."""
    from engine.settlement import _apply_paper_holdings

    a_live_lot("INFY", 5)
    a_paper_lot("INFY", 2, on=DAY)

    _apply_paper_holdings(DAY)

    assert Holding.objects.get(instrument_id="INFY").quantity == 2


def test_the_paper_ranking_counts_paper_lots_only(names):
    """M-1: a name held in opening lots is not held by the paper book -- it
    could not be sold there either."""
    from engine.book import book_for_ranking

    a_live_lot("INFY", 5)
    a_paper_lot("TCS", 2)

    assert book_for_ranking(["INFY", "TCS"]) == {"TCS": 2}


def _a_paper_sell(lot):
    from orders.models import Fill, Order, OrderSide, OrderType

    order = Order.objects.create(
        tag=f"tx-paper-sell-{lot.instrument_id}",
        tradesheet=lot.created_from_fill.order.tradesheet,
        instrument_id=lot.instrument_id,
        trading_date=DAY,
        tick_no=0,
        mode=OrderMode.PAPER,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=Decimal("150"),
    )
    return Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-sell-{lot.instrument_id}",
        quantity=1,
        price=Decimal("150"),
        executed_at=datetime.combine(DAY, datetime.min.time(), tzinfo=IST).replace(hour=11),
    )
