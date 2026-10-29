"""The daily value series.

`Lot` records what a share cost and `CashLedger` what a day spent; neither
records what the portfolio was worth on a given evening. Every chart, the
benchmark comparison and any future backtest need that, and it cannot be
recovered after the fact -- a price not written down on the day is gone.
"""

from datetime import date
from decimal import Decimal

import pytest

from performance.models import DailyValuation

pytestmark = pytest.mark.django_db

SEP = date(2026, 9, 22)


def test_a_day_is_stored_once():
    DailyValuation.objects.create(
        trading_date=SEP,
        portfolio_value=Decimal("1000"),
        invested_to_date=Decimal("900"),
        cash_carry=Decimal("50"),
        benchmark_level=Decimal("24000"),
    )

    assert DailyValuation.objects.get(trading_date=SEP).portfolio_value == Decimal("1000.0000")


def test_a_day_cannot_be_stored_twice():
    from django.db import IntegrityError

    DailyValuation.objects.create(trading_date=SEP, portfolio_value=Decimal("1"))

    with pytest.raises(IntegrityError):
        DailyValuation.objects.create(trading_date=SEP, portfolio_value=Decimal("2"))


def test_the_benchmark_level_may_be_missing():
    """The index is on the same feed as everything else, and a feed can be
    down. A day with no level is recorded rather than skipped, because the
    portfolio value is still worth having."""
    row = DailyValuation.objects.create(trading_date=SEP, portfolio_value=Decimal("1"))

    assert row.benchmark_level is None


# -- pricing what is held ------------------------------------------------


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


def a_lot(instrument, *, quantity, cost, acquired_on=SEP):
    from portfolio.models import Lot

    return Lot.objects.create(
        instrument=instrument,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=cost,
    )


def a_lot_via_fill(instrument, *, quantity, cost, mode, acquired_on=SEP):
    """A lot with real provenance: an order and a fill in the given book."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from engine.models import TradeSheet
    from orders.models import DAILY_BUY_STREAM, Fill, Order, OrderSide, OrderType
    from portfolio.models import Lot

    sheet, _ = TradeSheet.objects.get_or_create(trading_date=acquired_on, version=1)
    tag = f"pi-{mode}-{instrument.tradingsymbol}-{quantity}-{cost}"[:32]
    order = Order.objects.create(
        tag=tag,
        tradesheet=sheet,
        instrument=instrument,
        trading_date=acquired_on,
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
        executed_at=datetime(
            acquired_on.year,
            acquired_on.month,
            acquired_on.day,
            10,
            0,
            tzinfo=ZoneInfo("Asia/Kolkata"),
        ),
    )
    return Lot.objects.create(
        instrument=instrument,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal(cost),
        created_from_fill=fill,
    )


def test_the_portfolio_is_worth_what_its_open_lots_are_worth(instrument):
    from performance import valuation

    a_lot(instrument, quantity=3, cost=Decimal("1000"))

    value = valuation.value_now({"INFY": Decimal("1100")})

    assert value.total == Decimal("3300.0000")
    assert value.per_instrument == {"INFY": Decimal("3300.0000")}


def test_a_consumed_lot_is_not_counted(instrument):
    from performance import valuation

    lot = a_lot(instrument, quantity=3, cost=Decimal("1000"))
    lot.remaining_quantity = 1
    lot.save(update_fields=["remaining_quantity"])

    assert valuation.value_now({"INFY": Decimal("1100")}).total == Decimal("1100.0000")


def test_an_unpriced_name_is_named_rather_than_valued_at_nothing(instrument):
    """Valuing a name at zero because the feed was silent understates the
    portfolio and would quietly poison XIRR. It is reported instead."""
    from performance import valuation

    a_lot(instrument, quantity=3, cost=Decimal("1000"))

    value = valuation.value_now({})

    assert value.total == Decimal("0.0000")
    assert value.unpriced == {"INFY"}


def test_invested_to_date_is_the_basis_of_what_is_still_held(instrument):
    from performance import valuation

    a_lot(instrument, quantity=2, cost=Decimal("1000.5000"))

    assert valuation.invested_to_date() == Decimal("2001.0000")


# -- the snapshot --------------------------------------------------------


def test_a_snapshot_records_the_day(instrument):
    from performance import valuation

    a_lot_via_fill(instrument, quantity=2, cost="1000", mode="paper")

    row = valuation.snapshot(
        SEP, prices={"INFY": Decimal("1100")}, benchmark_level=Decimal("24000")
    )

    assert row.portfolio_value == Decimal("2200.0000")
    assert row.invested_to_date == Decimal("2000.0000")
    assert row.benchmark_level == Decimal("24000.0000")
    assert row.holdings_snapshot["INFY"] == {"quantity": 2, "price": "1100"}


def test_snapshotting_a_day_twice_does_not_rewrite_it(instrument):
    from performance import valuation

    a_lot_via_fill(instrument, quantity=2, cost="1000", mode="paper")
    valuation.snapshot(SEP, prices={"INFY": Decimal("1100")})

    assert valuation.snapshot(SEP, prices={"INFY": Decimal("9999")}) is None
    assert DailyValuation.objects.get(trading_date=SEP).portfolio_value == Decimal("2200.0000")


def test_a_snapshot_in_one_book_excludes_the_other_books_lot(instrument):
    """A paper snapshot must not mix in a live lot's value and cost, or the
    stored row disagrees with what `measure()` reports for the same day."""
    from performance import valuation

    a_lot_via_fill(instrument, quantity=2, cost="1000", mode="paper", acquired_on=SEP)
    a_lot_via_fill(instrument, quantity=3, cost="500", mode="live", acquired_on=SEP)

    row = valuation.snapshot(SEP, prices={"INFY": Decimal("1200")}, book="paper")

    assert row.portfolio_value == Decimal("2400.0000")  # 2 x 1200, the paper lot only
    assert row.invested_to_date == Decimal("2000.0000")  # 2 x 1000, the paper lot only


# -- the command ---------------------------------------------------------


def test_the_command_values_nothing_on_a_closed_day(db):
    """A weekend has no close to value, and calling the broker to find that
    out would be a broker call made for nothing."""
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("snapshot_valuation", date="2026-09-20", stdout=out)  # a Sunday

    assert "not a trading day" in out.getvalue()
    assert DailyValuation.objects.count() == 0


# -- one series per book (M13) ---------------------------------------------


def test_each_book_keeps_its_own_evening(instrument):
    """The paper series before go-live and the live one after are two
    portfolios, and the same date may be valued in both."""
    from performance import valuation

    a_lot_via_fill(instrument, quantity=2, cost="1000", mode="paper", acquired_on=SEP)
    a_lot_via_fill(instrument, quantity=3, cost="500", mode="live", acquired_on=SEP)

    paper = valuation.snapshot(SEP, prices={"INFY": Decimal("1200")}, book="paper")
    live = valuation.snapshot(SEP, prices={"INFY": Decimal("1200")}, book="live")

    assert (paper.book, paper.portfolio_value) == ("paper", Decimal("2400.0000"))
    assert (live.book, live.portfolio_value) == ("live", Decimal("3600.0000"))
    assert valuation.snapshot(SEP, prices={"INFY": Decimal("9")}, book="live") is None


def test_a_snapshot_without_a_book_is_the_performance_books(instrument, already_live):
    from performance import valuation

    a_lot_via_fill(instrument, quantity=3, cost="500", mode="live", acquired_on=SEP)
    already_live()

    row = valuation.snapshot(SEP, prices={"INFY": Decimal("600")})

    assert row.book == "live"
    assert row.portfolio_value == Decimal("1800.0000")


def test_the_value_series_and_drift_read_the_performance_book_only(db, already_live):
    """Joined, the paper evenings before go-live and the live ones after
    would read as one portfolio that jumped on the day of the switch."""
    from performance import series

    def evening(day, book, value):
        DailyValuation.objects.create(
            trading_date=day,
            book=book,
            portfolio_value=Decimal(value),
            invested_to_date=Decimal(value),
            holdings_snapshot={"INFY": {"quantity": 1, "price": value}},
        )

    evening(date(2026, 9, 18), "paper", "900")
    evening(date(2026, 9, 21), "paper", "950")
    evening(date(2026, 9, 21), "live", "5000")
    evening(SEP, "live", "5100")
    already_live()

    assert [(p.on, p.value) for p in series.value_history()] == [
        (date(2026, 9, 21), Decimal("5000.00")),
        (SEP, Decimal("5100.00")),
    ]
    assert len(series.drift_history({"INFY": Decimal("100")})) == 2


def test_the_command_reads_no_quotes_once_the_day_is_valued(instrument, monkeypatch):
    """m-7: the retry timer runs the snapshot four times an evening; a day
    already valued must not cost a Kite quote call each time."""
    from io import StringIO

    from django.core.management import call_command

    from journal.performance import book_for_performance
    from performance import valuation

    valuation.snapshot(SEP, prices={"INFY": Decimal("1200")}, book=book_for_performance())

    def no_quotes(*args, **kwargs):
        raise AssertionError("quotes read for a day already valued")

    monkeypatch.setattr("engine.quotes.fetch", no_quotes)
    monkeypatch.setattr(
        "performance.management.commands.snapshot_valuation.get_broker", lambda: object()
    )
    out = StringIO()
    call_command("snapshot_valuation", date=SEP.isoformat(), stdout=out)

    assert "already valued" in out.getvalue()
