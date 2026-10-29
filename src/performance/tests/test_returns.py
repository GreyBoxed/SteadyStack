"""Portfolio performance (M41).

The flows are read from the journal: every rupee that left for shares is an
outflow on its date, whether the engine bought it, it was bought directly, or
it arrived as an IPO; net sale proceeds and dividends come back as inflows.
Money sitting in carry is never a flow at all, and what is still held is the
terminal inflow.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

START = date(2025, 9, 1)
TODAY = date(2026, 9, 22)


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


@pytest.fixture
def live_mode(monkeypatch):
    """The engine in live mode, without the static-IP checks going live needs.

    Only `current().order_mode` is read by the code under test, so the
    configuration object is wrapped rather than a live version written.
    Every flow-producing test needs this: external transactions -- direct
    buys, IPOs, sales -- always post to the live book (spec), so the
    performance book has to be live for those flows to be the ones read.
    """
    from configuration import store

    real = store.current

    class Live:
        def __init__(self, config):
            self._config = config

        def __getattr__(self, name):
            return getattr(self._config, name)

        order_mode = "live"

    monkeypatch.setattr(store, "current", lambda: Live(real()))


def a_live_buy(instrument, *, quantity, price, on=START, charges="0"):
    """A direct buy at Zerodha: an outflow in the journal and a live lot."""
    from records.external import record_external
    from records.models import ExternalSide

    return record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=quantity,
        occurred_on=on,
        actor="t",
        reason="test buy",
        price=Decimal(price),
        charges=Decimal(charges),
    )


def a_live_sell(instrument, *, quantity, price, on, charges="0"):
    from records.external import record_external
    from records.models import ExternalSide

    return record_external(
        instrument=instrument,
        side=ExternalSide.SELL,
        quantity=quantity,
        occurred_on=on,
        actor="t",
        reason="test sell",
        price=Decimal(price),
        charges=Decimal(charges),
    )


def a_lot_via_fill(instrument, *, quantity, cost, mode, on=START):
    """A lot with real provenance: an order and a fill in the given book.

    Built directly rather than through `orders.booking.book()`, since only
    the lot's `created_from_fill__order__mode` matters here -- not the
    journal entry a real booking would also post.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from engine.models import TradeSheet
    from orders.models import DAILY_BUY_STREAM, Fill, Order, OrderSide, OrderType
    from portfolio.models import Lot

    sheet, _ = TradeSheet.objects.get_or_create(trading_date=on, version=1)
    tag = f"pi-{mode}-{instrument.tradingsymbol}-{quantity}-{cost}"[:32]
    order = Order.objects.create(
        tag=tag,
        tradesheet=sheet,
        instrument=instrument,
        trading_date=on,
        stream=DAILY_BUY_STREAM,
        tick_no=1,
        side=OrderSide.BUY,
        mode=mode,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        filled_quantity=quantity,
        limit_price=Decimal(cost),
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id=tag,
        quantity=quantity,
        price=Decimal(cost),
        executed_at=datetime(on.year, on.month, on.day, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    return Lot.objects.create(
        instrument=instrument,
        acquired_on=on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal(cost),
        created_from_fill=fill,
    )


# -- flows ---------------------------------------------------------------


def test_each_acquisition_is_an_outflow(instrument, live_mode):
    from performance import returns

    a_live_buy(instrument, quantity=2, price="500", charges="2", on=START)

    flows = returns.contribution_flows(TODAY, terminal_value=Decimal("1500"))

    assert flows[0].on == START
    assert flows[0].amount == Decimal("-1002.00")


def test_no_activity_is_not_a_flow(live_mode):
    """Nothing recorded moved no money; a zero flow would drag the date range
    without changing the arithmetic."""
    from performance import returns

    assert returns.contribution_flows(TODAY, terminal_value=Decimal("0")) == []


def test_what_is_still_held_is_the_terminal_inflow(instrument, live_mode):
    from performance import returns

    a_live_buy(instrument, quantity=2, price="500", on=START)

    flows = returns.contribution_flows(TODAY, terminal_value=Decimal("1500"))

    assert flows[-1].on == TODAY
    assert flows[-1].amount == Decimal("1500.00")


def test_a_disposal_is_an_inflow_on_the_day_it_settled(instrument, live_mode):
    from performance import returns

    a_live_buy(instrument, quantity=1, price="1000", on=START)
    a_live_sell(instrument, quantity=1, price="1200", on=date(2026, 3, 1))

    flows = returns.contribution_flows(TODAY, terminal_value=Decimal("500"))
    inflow = next(flow for flow in flows if flow.on == date(2026, 3, 1))

    assert inflow.amount == Decimal("1200.00")


def test_an_ipo_costs_what_it_is_worth_so_xirr_is_flat(instrument, live_mode):
    """Before this fix, an IPO's value counted with no outflow, so XIRR came
    out absurdly large. Now the acquisition is booked as the outflow it is,
    and a price that has not moved gives a flat return."""
    from performance import returns
    from records.external import record_external
    from records.models import ExternalSide, PaidFrom

    record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=10,
        occurred_on=START,
        actor="o",
        reason="IPO",
        price=Decimal("50"),
        paid_from=PaidFrom.BANK,
    )

    result = returns.measure({"INFY": Decimal("50")}, as_of=TODAY)

    assert result.invested == Decimal("500.00")
    assert result.current_value == Decimal("500.00")
    assert result.xirr is not None
    assert abs(result.xirr) < Decimal("0.01")


def an_opening_lot(instrument, *, quantity=10, cost="100", acquired_on=date(2024, 1, 1)):
    """A lot from the opening import: held and valued, and in no fill."""
    from portfolio.models import BasisState, Lot, OpeningImport

    opening = OpeningImport.objects.create(imported_by="operator", source="statement")
    return Lot.objects.create(
        instrument=instrument,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal(cost),
        basis_state=BasisState.IMPORTED,
        opening_import=opening,
    )


def test_an_opening_lot_is_an_outflow_on_the_day_it_was_bought(instrument, live_mode):
    """Shares held before this system are in the terminal value, so their
    cost must be in the flows, or they arrive free and XIRR reads their whole
    value as return. In the journal they are the opening entry's Investments
    lines, one per lot, at the cost of what is still held -- and each is dated
    by its lot's `acquired_on`, not by go-live (I10)."""
    from journal.external import open_books
    from performance import returns

    an_opening_lot(instrument, quantity=10, cost="100.5", acquired_on=date(2024, 1, 1))
    open_books(cash=Decimal("0"), on=START, actor="operator")

    flows = returns.contribution_flows(TODAY, terminal_value=Decimal("1100"))

    assert flows[0].on == date(2024, 1, 1)
    assert flows[0].amount == Decimal("-1005.00")
    assert flows[-1].amount == Decimal("1100.00")


def test_a_cheap_opening_lot_does_not_count_its_old_gain_as_new_return(instrument, live_mode):
    """I10 / I-3: bought five years before go-live at 100, worth 300 at go-live
    and 315 now. Dated at go-live, the flows read a 215% gain in a year; dated
    at purchase they read about 21% a year, which is what it earned."""
    from journal.external import open_books
    from performance import returns

    an_opening_lot(instrument, quantity=10, cost="100", acquired_on=date(2020, 9, 1))
    open_books(cash=Decimal("0"), on=START, actor="operator")

    result = returns.measure({"INFY": Decimal("315")}, as_of=TODAY)

    assert result.xirr is not None
    assert Decimal("0.15") < result.xirr < Decimal("0.30")


def test_a_lot_already_in_the_journal_is_not_opened_twice(instrument, live_mode):
    """A direct buy already posted its outflow; the opening entry leaves it out."""
    from journal.external import open_books
    from performance import returns

    a_live_buy(instrument, quantity=2, price="1000", on=START)
    open_books(cash=Decimal("0"), on=START + timedelta(days=1), actor="operator")

    outflows = [
        flow
        for flow in returns.contribution_flows(TODAY, terminal_value=Decimal("2400"))
        if flow.amount < 0
    ]

    assert [flow.amount for flow in outflows] == [Decimal("-2000.00")]


def test_opening_positions_do_not_overstate_xirr(instrument, live_mode):
    """Opening shares and a later buy, up 5% in all: a few per cent a year,
    not the hundred a free lot would report."""
    from journal.external import open_books
    from performance import returns

    an_opening_lot(instrument, quantity=10, cost="100", acquired_on=date(2024, 9, 22))
    open_books(cash=Decimal("0"), on=START, actor="operator")
    a_live_buy(instrument, quantity=10, price="100", on=START + timedelta(days=30))

    result = returns.measure({"INFY": Decimal("105")}, as_of=TODAY)

    assert result.xirr is not None
    assert Decimal("0") < result.xirr < Decimal("0.10")


# -- the headline numbers ------------------------------------------------


def test_performance_reports_invested_value_and_return(instrument, live_mode):
    from performance import returns

    a_live_buy(instrument, quantity=2, price="1000", on=START)

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.invested == Decimal("2000.00")
    assert result.current_value == Decimal("2400.00")
    assert result.absolute_return == Decimal("400.00")
    assert result.percent_return == Decimal("20.00")


def test_xirr_is_reported_over_a_long_enough_history(instrument, live_mode):
    from performance import returns

    a_live_buy(instrument, quantity=2, price="1000", on=START)

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.xirr is not None
    assert result.xirr_unavailable == ""


def test_a_short_history_says_so_instead_of_printing_a_number(instrument, live_mode):
    """One contribution and a one-week move annualises into nonsense."""
    from performance import returns

    recent = TODAY - timedelta(days=7)
    a_live_buy(instrument, quantity=2, price="1000", on=recent)

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.xirr is None
    assert "history" in result.xirr_unavailable


def test_per_instrument_contribution_is_reported(instrument, live_mode):
    from performance import returns

    a_live_buy(instrument, quantity=2, price="1000", on=START)

    contribution = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY).per_instrument[0]

    assert contribution.instrument_id == "INFY"
    assert contribution.gain == Decimal("400.00")
    assert contribution.share_of_gain == Decimal("100.00")


def test_per_instrument_invested_matches_the_values_book(instrument):
    """A paper lot and a live lot of the same symbol: paper mode's
    per-instrument invested counts only the paper lot -- the same book the
    value side is already filtered to (spec §8), or the two figures would be
    about two different portfolios."""
    from performance import returns

    a_lot_via_fill(instrument, quantity=2, cost="1000", mode="paper", on=START)
    a_lot_via_fill(instrument, quantity=3, cost="2000", mode="live", on=START)

    contribution = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY).per_instrument[0]

    assert contribution.instrument_id == "INFY"
    assert contribution.invested == Decimal("2000.00")  # 2 x 1000, the paper lot only
    assert contribution.value == Decimal("2400.00")  # 2 x 1200, the paper lot only


def test_portfolio_figures_are_flagged_not_withheld_under_a_divergence(instrument, live_mode):
    """M31b: the instrument's own figures go, the portfolio's stay and carry
    a caveat naming what is in doubt."""
    from performance import returns
    from records.models import Divergence

    a_live_buy(instrument, quantity=2, price="1000", on=START)
    Divergence.objects.create(
        instrument=instrument, detected_on=TODAY, expected_quantity=2, actual_quantity=1
    )

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.current_value == Decimal("2400.00")
    assert result.caveats != []


# -- the benchmark (M41a) ------------------------------------------------


def a_benchmark_level(day, level):
    from performance.models import DailyValuation

    return DailyValuation.objects.create(
        trading_date=day,
        portfolio_value=Decimal("0"),
        benchmark_level=Decimal(level),
    )


def test_the_benchmark_buys_units_on_the_same_dates(instrument):
    """The index rose 20% over the period, so putting the same money in on the
    same days returns about 20% -- which is the number the portfolio has to
    beat to have been worth the trouble."""
    from performance import returns
    from performance.xirr import Flow

    a_benchmark_level(START, "20000")
    a_benchmark_level(TODAY, "24000")

    flows = [Flow(START, Decimal("-2000")), Flow(TODAY, Decimal("2400"))]
    rate, unavailable = returns.benchmark_xirr(flows, as_of=TODAY)

    assert unavailable == ""
    assert Decimal("0.15") < rate < Decimal("0.25")


def test_the_benchmark_is_independent_of_what_the_portfolio_did(instrument):
    """It is the counterfactual, so a portfolio that did far worse must not
    drag the number it is being judged against."""
    from performance import returns
    from performance.xirr import Flow

    a_benchmark_level(START, "20000")
    a_benchmark_level(TODAY, "24000")

    good = returns.benchmark_xirr(
        [Flow(START, Decimal("-2000")), Flow(TODAY, Decimal("9999"))], as_of=TODAY
    )[0]
    bad = returns.benchmark_xirr(
        [Flow(START, Decimal("-2000")), Flow(TODAY, Decimal("10"))], as_of=TODAY
    )[0]

    assert good == bad


def test_a_contribution_date_with_no_level_makes_the_benchmark_unavailable(instrument):
    """Guessing a level would invent the comparison the operator is relying on."""
    from performance import returns
    from performance.xirr import Flow

    a_benchmark_level(TODAY, "24000")

    flows = [Flow(START, Decimal("-2000")), Flow(TODAY, Decimal("2400"))]
    rate, unavailable = returns.benchmark_xirr(flows, as_of=TODAY)

    assert rate is None
    assert str(START) in unavailable


def test_the_benchmark_is_absent_before_any_history_exists(instrument):
    from performance import returns
    from performance.xirr import Flow

    rate, unavailable = returns.benchmark_xirr(
        [Flow(START, Decimal("-2000")), Flow(TODAY, Decimal("2400"))], as_of=TODAY
    )

    assert rate is None
    assert unavailable != ""


def test_measure_reports_the_benchmark_alongside_the_portfolio(instrument, live_mode):
    from performance import returns

    a_live_buy(instrument, quantity=2, price="1000", on=START)
    a_benchmark_level(START, "20000")
    a_benchmark_level(TODAY, "24000")

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.benchmark_xirr is not None
    assert result.benchmark_unavailable == ""
