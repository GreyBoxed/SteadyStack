"""The two ledger surfaces (M40a, M40b).

The cash ledger shows the identity D7's property tests already assert --
contributions minus deployed minus cash held equals zero -- rather than asking
to be trusted on it. The share ledger does the same for quantities, by source,
so that when the system and the broker disagree the disagreement can be
diagnosed instead of merely alerted.
"""

from datetime import date
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

MON = date(2026, 9, 21)
TUE = date(2026, 9, 22)


def a_ledger_row(day, **kwargs):
    from engine.models import CashLedger

    defaults = {
        "opening_carry": Decimal("0"),
        "daily_allocation": Decimal("1000"),
        "extra_allocation": Decimal("0"),
        "cash_spent": Decimal("900"),
        "charges": Decimal("2"),
        "closing_carry": Decimal("98"),
    }
    return CashLedger.objects.create(book="paper", trading_date=day, **{**defaults, **kwargs})


# -- cash (M40a) ---------------------------------------------------------


def test_every_day_since_inception_is_listed():
    from performance import ledgers

    a_ledger_row(MON)
    a_ledger_row(TUE, opening_carry=Decimal("98"), closing_carry=Decimal("196"))

    view = ledgers.cash()

    assert [row.trading_date for row in view.rows] == [MON, TUE]


def test_the_range_narrows_it():
    from performance import ledgers

    a_ledger_row(MON)
    a_ledger_row(TUE, opening_carry=Decimal("98"), closing_carry=Decimal("196"))

    assert [row.trading_date for row in ledgers.cash(start=TUE).rows] == [TUE]


def test_totals_are_summed_over_the_range():
    from performance import ledgers

    a_ledger_row(MON)
    a_ledger_row(TUE, opening_carry=Decimal("98"), closing_carry=Decimal("196"))

    totals = ledgers.cash().totals

    assert totals.daily_allocation == Decimal("2000.00")
    assert totals.cash_spent == Decimal("1800.00")
    assert totals.charges == Decimal("4.00")


def test_the_totals_span_the_range_rather_than_summing_carry():
    """Opening and closing carry are positions, not flows. Adding them up
    would produce a number that means nothing."""
    from performance import ledgers

    a_ledger_row(MON)
    a_ledger_row(TUE, opening_carry=Decimal("98"), closing_carry=Decimal("196"))

    totals = ledgers.cash().totals

    assert totals.opening_carry == Decimal("0.00")
    assert totals.closing_carry == Decimal("196.00")


def test_the_surface_carries_the_reconciliation():
    """D7's identity, shown rather than trusted. The row is written from the
    journal, since the reconciliation also checks it against the journal."""
    from engine.ledger import settle_day
    from journal import accounts
    from journal.cash import allocate_daily
    from journal.models import Book, Component, EntryKind
    from journal.posting import Line, post
    from performance import ledgers

    allocate_daily(MON, book=Book.PAPER)
    post(
        book=Book.PAPER,
        kind=EntryKind.BUY_FILL,
        occurred_on=MON,
        description="buy",
        lines=[
            Line(accounts.INVESTMENTS, Decimal("900"), component=Component.GROSS),
            Line(accounts.INVESTMENTS, Decimal("2"), component=Component.STT),
            Line(accounts.SETTLE_ENGINE, Decimal("-902")),
        ],
    )
    settle_day(MON, book=Book.PAPER)

    view = ledgers.cash()

    assert view.identity_holds is True
    assert "chain_breaks" in view.reconciliation
    assert view.reconciliation["journal_breaks"] == []


def test_a_row_the_journal_does_not_back_is_surfaced():
    from performance import ledgers

    a_ledger_row(MON)

    view = ledgers.cash()

    assert view.identity_holds is False
    assert view.reconciliation["journal_breaks"][0]["date"] == str(MON)


def test_a_broken_chain_is_surfaced_not_hidden():
    from performance import ledgers

    a_ledger_row(MON, closing_carry=Decimal("98"))
    a_ledger_row(TUE, opening_carry=Decimal("5"), closing_carry=Decimal("103"))

    view = ledgers.cash()

    assert view.identity_holds is False
    assert view.reconciliation["chain_breaks"][0]["date"] == str(TUE)


def test_an_empty_ledger_does_not_explode():
    from performance import ledgers

    view = ledgers.cash()

    assert view.rows == []
    assert view.totals.closing_carry == Decimal("0")


# -- shares (M40b) -------------------------------------------------------


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


@pytest.fixture
def live(already_live):
    """The order mode live: the share ledger nets the live book and compares
    it with the broker's holdings (m-3)."""
    already_live()


def a_fill(instrument, *, quantity=2, side=None, day=TUE, mode="paper"):
    from django.utils import timezone

    from engine.models import TradeSheet, TradeSheetState
    from orders.models import Fill, Order, OrderSide, OrderType

    sheet, _ = TradeSheet.objects.get_or_create(
        trading_date=day,
        version=1,
        defaults={
            "state": TradeSheetState.RUNNING,
            "instrument": instrument,
            "quantity": quantity,
            "trace_id": f"ts-{day}",
        },
    )
    order = Order.objects.create(
        tradesheet=sheet,
        tag=f"tx-{quantity}-{side or 'buy'}-{day}-{mode}",
        instrument=instrument,
        trading_date=day,
        tick_no=0,
        side=side or OrderSide.BUY,
        mode=mode,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=Decimal("1000"),
    )
    return Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-{order.tag}",
        quantity=quantity,
        price=Decimal("1000"),
        executed_at=timezone.now(),
    )


def test_an_engine_buy_is_a_positive_movement(instrument):
    from performance import ledgers

    a_fill(instrument, quantity=2)

    movement = ledgers.shares().movements[0]

    assert movement.quantity == 2
    assert movement.source == "engine_buy"
    assert movement.instrument_id == "INFY"


def test_a_sell_is_a_negative_movement(instrument):
    from orders.models import OrderSide
    from performance import ledgers

    a_fill(instrument, quantity=3, side=OrderSide.SELL)

    movement = ledgers.shares().movements[0]

    assert movement.quantity == -3
    assert movement.source == "rebalance_sell"


def test_an_external_transaction_is_its_own_source(instrument, live):
    from performance import ledgers
    from records.models import ExternalSide, ExternalTransaction

    ExternalTransaction.objects.create(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=5,
        price=Decimal("1000"),
        occurred_on=TUE,
        acquired_on=TUE,
        reason="bought at the broker",
        actor="operator",
    )

    movement = ledgers.shares().movements[0]

    assert movement.source == "external"
    assert movement.quantity == 5


def test_movements_net_to_the_quantity_held(instrument):
    from orders.models import OrderSide
    from performance import ledgers

    a_fill(instrument, quantity=5)
    a_fill(instrument, quantity=2, side=OrderSide.SELL)

    assert ledgers.shares().by_instrument["INFY"] == 3


def test_a_split_is_a_movement_even_though_no_share_was_traded(instrument, live):
    """A corporate action supersedes lots and writes no fill. Without a source
    of its own the ledger would net to the pre-split quantity and report a
    divergence against the broker every day after a split."""
    from performance import ledgers
    from portfolio.models import Lot
    from records import corporate_actions

    Lot.objects.create(
        instrument=instrument,
        acquired_on=MON,
        quantity=5,
        remaining_quantity=5,
        cost_per_unit=Decimal("1000"),
    )
    a_fill(instrument, quantity=5, mode="live")

    corporate_actions.split(instrument=instrument, factor=5, effective_on=TUE, actor="operator")

    view = ledgers.shares()

    assert view.by_instrument["INFY"] == 25
    assert any(movement.source == "corporate_action" for movement in view.movements)


def test_finalising_a_basis_is_not_a_share_movement(instrument):
    """A contract note supersedes a lot too, but the quantity does not move --
    only what it cost. A ledger of quantities must not notice."""
    from performance import ledgers
    from portfolio import contract_note
    from portfolio.models import BasisState, Lot

    a_fill(instrument, quantity=2)
    lot = Lot.objects.create(
        instrument=instrument,
        acquired_on=MON,
        quantity=2,
        remaining_quantity=2,
        cost_per_unit=Decimal("1001"),
        basis_state=BasisState.ESTIMATED,
    )

    contract_note.finalise(lot, actual_charges=Decimal("5"), actor="operator")

    assert all(movement.source != "corporate_action" for movement in ledgers.shares().movements)


def test_the_ledger_names_a_disagreement_with_the_broker(instrument, live):
    """Between the two ledgers a divergence is diagnosed, not merely alerted."""
    from performance import ledgers
    from portfolio.models import Holding

    a_fill(instrument, quantity=5, mode="live")
    Holding.objects.create(instrument=instrument, quantity=4, average_cost=Decimal("1000"))

    divergence = ledgers.shares().divergences[0]

    assert divergence["instrument_id"] == "INFY"
    assert divergence["ours"] == 5
    assert divergence["broker"] == 4


def test_a_single_instrument_can_be_asked_about(instrument):
    from performance import ledgers

    a_fill(instrument, quantity=5)

    assert len(ledgers.shares(instrument_id="INFY").movements) == 1
    assert ledgers.shares(instrument_id="TCS").movements == []


def test_asking_about_one_instrument_does_not_accuse_the_others(instrument, live):
    """Narrowed to INFY, TCS has no movements on screen to agree with its
    holding -- that is the filter, not a divergence."""
    from performance import ledgers
    from portfolio.models import Holding, Instrument

    tcs = Instrument.objects.create(tradingsymbol="TCS", instrument_token=2, name="TCS")
    a_fill(instrument, quantity=5, mode="live")
    a_fill(tcs, quantity=2, mode="live")
    Holding.objects.create(instrument=instrument, quantity=5, average_cost=Decimal("1000"))
    Holding.objects.create(instrument=tcs, quantity=3, average_cost=Decimal("3000"))

    assert ledgers.shares(instrument_id="INFY").divergences == []
    assert [d["instrument_id"] for d in ledgers.shares().divergences] == ["TCS"]


def test_a_window_narrows_the_list_but_never_the_reconciliation(instrument, live):
    """A month of movements does not add up to what the broker holds. Comparing
    the two would report a divergence for every instrument the window cuts
    across -- an alert wrong by construction is worse than no alert."""
    from performance import ledgers
    from portfolio.models import Holding

    a_fill(instrument, quantity=5, day=MON, mode="live")
    a_fill(instrument, quantity=3, day=TUE, mode="live")
    Holding.objects.create(instrument=instrument, quantity=8, average_cost=Decimal("1000"))

    view = ledgers.shares(start=TUE)

    assert len(view.movements) == 1  # the window
    assert view.by_instrument["INFY"] == 8  # the whole history
    assert view.divergences == []


# -- the share ledger reads one book (m-3) -----------------------------------


def an_opening_lot(instrument, *, quantity, on=MON):
    from portfolio.models import Lot, OpeningImport

    opening = OpeningImport.objects.create(imported_by="operator", source="contract notes")
    return Lot.objects.create(
        instrument=instrument,
        acquired_on=on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal("900"),
        opening_import=opening,
    )


def test_live_counts_opening_lots_on_the_day_they_were_acquired(instrument, live):
    """After go-live every name held in opening lots would otherwise read
    "ours 0, broker N"."""
    from performance import ledgers
    from portfolio.models import Holding

    an_opening_lot(instrument, quantity=10)
    a_fill(instrument, quantity=2, mode="live")
    Holding.objects.create(instrument=instrument, quantity=12, average_cost=Decimal("950"))

    view = ledgers.shares()

    opening = [m for m in view.movements if m.source == "opening"]
    assert [(m.on, m.quantity) for m in opening] == [(MON, 10)]
    assert opening[0].label == "Opening position"
    assert view.by_instrument["INFY"] == 12
    assert view.divergences == []


def test_live_leaves_paper_fills_out(instrument, live):
    """A stint back in paper must not inflate what the live book holds."""
    from performance import ledgers
    from portfolio.models import Holding

    a_fill(instrument, quantity=3, day=MON)  # paper, before the switch
    a_fill(instrument, quantity=2, mode="live")
    Holding.objects.create(instrument=instrument, quantity=2, average_cost=Decimal("1000"))

    view = ledgers.shares()

    assert view.by_instrument["INFY"] == 2
    assert all(m.source != "engine_buy" or m.quantity == 2 for m in view.movements)
    assert view.divergences == []
    assert view.compared_with == "broker"


def test_paper_shows_paper_lots_against_paper_holdings(instrument):
    """In paper the broker's holdings are real shares the paper book never
    bought: the paper fills are netted against the paper lots, not them."""
    from orders.models import OrderSide
    from performance import ledgers
    from portfolio import lots
    from portfolio.models import Holding

    fill = a_fill(instrument, quantity=4)
    lots.from_fill(fill)
    a_fill(instrument, quantity=1, side=OrderSide.SELL)  # no lot consumed: disagrees
    an_opening_lot(instrument, quantity=10)
    Holding.objects.create(instrument=instrument, quantity=10, average_cost=Decimal("900"))

    view = ledgers.shares()

    assert view.compared_with == "paper lots"
    assert view.by_instrument["INFY"] == 3
    assert all(m.source != "opening" for m in view.movements)
    assert view.divergences == [{"instrument_id": "INFY", "ours": 3, "broker": 4}]


# -- the live ledger's two exemptions, as the divergence scan has them (gap m3)


def test_live_does_not_flag_todays_undelivered_buys(instrument, live):
    """Tonight's resync reads Kite's holdings, and a CNC buy made today is a
    position there until the next morning: the broker is short by exactly
    the shares settlement has not delivered."""
    import time_machine

    from performance import ledgers
    from portfolio import lots
    from portfolio.models import Holding

    an_opening_lot(instrument, quantity=10)
    Holding.objects.create(instrument=instrument, quantity=10, average_cost=Decimal("900"))
    with time_machine.travel("2026-09-22 16:00 +05:30", tick=False):
        lots.from_fill(a_fill(instrument, quantity=2, mode="live"))

        view = ledgers.shares()

    assert view.by_instrument["INFY"] == 12
    assert view.divergences == []


def test_live_leaves_holdings_that_are_not_the_portfolios_out(instrument, live):
    """The adult's own shares in the same demat: no lot, not in the model,
    no movement. The divergence scan lists them apart; so does the ledger."""
    from performance import ledgers
    from portfolio.models import Holding, Instrument

    personal = Instrument.objects.create(tradingsymbol="TCS", instrument_token=2, name="TCS")
    an_opening_lot(instrument, quantity=10)
    Holding.objects.create(instrument=instrument, quantity=10, average_cost=Decimal("900"))
    Holding.objects.create(instrument=personal, quantity=5, average_cost=Decimal("3000"))

    assert ledgers.shares().divergences == []


def test_live_still_flags_a_share_missing_beyond_settlement(instrument, live):
    """Monday's buy was due on Tuesday: by Wednesday the broker holding one
    fewer is a real difference, not settlement lag."""
    import time_machine

    from performance import ledgers
    from portfolio import lots
    from portfolio.models import Holding

    with time_machine.travel("2026-09-21 16:00 +05:30", tick=False):
        lots.from_fill(a_fill(instrument, quantity=4, day=MON, mode="live"))
    Holding.objects.create(instrument=instrument, quantity=3, average_cost=Decimal("1000"))

    with time_machine.travel("2026-09-23 16:00 +05:30", tick=False):
        view = ledgers.shares()

    assert view.divergences == [{"instrument_id": "INFY", "ours": 4, "broker": 3}]


# -- the day not yet settled -----------------------------------------------


def a_lumpsum(day, amount, *, allocated_on=None):
    from journal import cash
    from journal.models import Book

    return cash.allocate(
        Decimal(amount),
        book=Book.PAPER,
        on=allocated_on or day,
        actor="operator",
        description="lumpsum",
        source_type=cash.LUMPSUM,
    )


def test_an_unsettled_day_is_read_live_from_the_journal():
    from performance import ledgers

    a_lumpsum(TUE, "500000")

    row = ledgers.provisional(TUE)

    assert row.trading_date == TUE
    assert row.extra_allocation == Decimal("500000.00")
    assert row.closing_carry == Decimal("500000.00")


def test_a_settled_day_has_no_provisional_row():
    from performance import ledgers

    a_ledger_row(TUE)

    assert ledgers.provisional(TUE) is None


def test_a_day_that_does_not_trade_has_no_provisional_row():
    from performance import ledgers

    assert ledgers.provisional(date(2026, 9, 26)) is None  # a Saturday


def test_money_dated_a_later_day_is_that_days_not_todays():
    from performance import ledgers

    a_lumpsum(TUE, "100000", allocated_on=date(2026, 9, 28))

    assert ledgers.provisional(TUE).extra_allocation == Decimal("0.00")
    assert ledgers.allocated_ahead(TUE) == [(date(2026, 9, 28), Decimal("100000.00"))]


def test_the_provisional_row_stays_out_of_the_rows_and_totals(monkeypatch):
    from performance import ledgers

    monkeypatch.setattr("performance.ledgers.today_ist", lambda: TUE)
    a_ledger_row(MON)
    a_lumpsum(TUE, "500000")

    view = ledgers.cash()

    assert [row.trading_date for row in view.rows] == [MON]
    assert view.totals.extra_allocation == Decimal("0.00")
    assert view.provisional.extra_allocation == Decimal("500000.00")


def test_a_range_that_ends_before_today_has_no_provisional_row(monkeypatch):
    from performance import ledgers

    monkeypatch.setattr("performance.ledgers.today_ist", lambda: TUE)
    a_lumpsum(TUE, "500000")

    assert ledgers.cash(end=MON).provisional is None
