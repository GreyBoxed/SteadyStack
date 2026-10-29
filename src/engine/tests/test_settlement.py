"""Closing a day out.

The figure under test is carry: it comes from summing fill rows across every
order of the day, at the prices that actually filled, never from the planned
price and never from a broker average (D6, M17). Everything else here follows
from getting that one number right.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from audit.models import AuditEvent
from engine import settlement
from engine.ledger import opening_for
from engine.models import CashLedger, SkipReason, TradeSheet, TradeSheetState
from journal.cash import allocate_daily
from journal.models import Book
from journal.trades import post_fill
from orders.models import Fill, Order, OrderMode, OrderType
from orders.state import OrderState
from portfolio import lots
from portfolio.instruments import refresh
from portfolio.models import Holding, Instrument, Lot

MON = date(2026, 9, 21)
TUE = date(2026, 9, 22)

pytestmark = pytest.mark.django_db


class StubBroker:
    def __init__(self, book=None, trades=None):
        self.book = book or []
        self._trades = trades or []

    def orders(self):
        return self.book

    def trades(self):
        return self._trades

    def place_order(self, **kwargs):
        return "kite-1"

    def cancel_order(self, order_id):
        return order_id


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
    # Planning a day allocates its daily amount; a sheet written directly
    # has to be given it, or the day would have nothing to have spent from.
    allocate_daily(TUE, book=Book.PAPER)
    return TradeSheet.objects.create(
        trading_date=TUE,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=4,
        spendable_cash=Decimal("7000.00"),
        reference_price=Decimal("1650.50"),
    )


def an_order(
    sheet, instrument, *, tick_no=0, quantity=2, mode=OrderMode.PAPER, state=OrderState.OPEN
):
    return Order.objects.create(
        tag=f"tx-settle-{tick_no}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        tick_no=tick_no,
        mode=mode,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=Decimal("1650.00"),
        state=state,
    )


def a_fill(order, *, quantity=2, price=Decimal("1649.00"), charges=Decimal("2.50")):
    order.filled_quantity += quantity
    order.state = OrderState.COMPLETE if order.filled_quantity >= order.quantity else order.state
    order.save(update_fields=["filled_quantity", "state"])

    fill = Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-{order.tag}-{quantity}-{price}",
        quantity=quantity,
        price=price,
        charges=charges,
        executed_at=timezone.now(),
    )
    lots.from_fill(fill)
    post_fill(fill)
    return fill


# -- the day's spend -------------------------------------------------------


def test_spend_is_summed_from_fills(sheet, instrument):
    order = an_order(sheet, instrument, quantity=2)
    a_fill(order, quantity=2, price=Decimal("1649.00"), charges=Decimal("2.50"))

    spent, charges = settlement.spend_and_charges(TUE)

    assert spent == Decimal("3298.00")
    assert charges == Decimal("2.50")


def test_spend_uses_the_price_that_filled_not_the_price_planned(sheet, instrument):
    """M17: carry comes from what happened, not from what was intended."""
    order = an_order(sheet, instrument, quantity=2)
    a_fill(order, quantity=2, price=Decimal("1600.00"), charges=Decimal("2.00"))

    spent, _ = settlement.spend_and_charges(TUE)

    assert spent == Decimal("3200.00")


def test_spend_sums_across_every_order_of_the_day(sheet, instrument):
    """A day places many orders; the only figure that means anything is the
    total."""
    first = an_order(sheet, instrument, tick_no=0, quantity=1)
    second = an_order(sheet, instrument, tick_no=1, quantity=1)
    a_fill(first, quantity=1, price=Decimal("1649.00"), charges=Decimal("1.25"))
    a_fill(second, quantity=1, price=Decimal("1651.00"), charges=Decimal("1.25"))

    spent, charges = settlement.spend_and_charges(TUE)

    assert spent == Decimal("3300.00")
    assert charges == Decimal("2.50")


def test_a_day_with_no_fills_spent_nothing(sheet, instrument):
    an_order(sheet, instrument)
    assert settlement.spend_and_charges(TUE) == (Decimal("0.00"), Decimal("0.00"))


# -- settling --------------------------------------------------------------


def test_settling_writes_the_ledger_row(sheet, instrument):
    order = an_order(sheet, instrument, quantity=4)
    a_fill(order, quantity=4, price=Decimal("1500.00"), charges=Decimal("5.00"))

    result = settlement.settle(trading_date=TUE, broker=StubBroker())

    assert result.ledger is not None
    assert CashLedger.objects.count() == 1
    assert result.ledger.cash_spent == Decimal("6000.00")


def test_the_unspent_remainder_carries(sheet, instrument):
    """The whole point of the ledger: tomorrow starts with what today left."""
    order = an_order(sheet, instrument, quantity=4)
    a_fill(order, quantity=1, price=Decimal("500.00"), charges=Decimal("1.00"))

    settlement.settle(trading_date=TUE, broker=StubBroker())

    # 1,000 allocated, less 500 gross and the fill's stored 1.00 of charges:
    # the journal posts the fill at its stored total.
    assert opening_for(date(2026, 9, 23)).carry == Decimal("499")


def test_a_fully_filled_day_is_filled(sheet, instrument):
    order = an_order(sheet, instrument, quantity=4)
    a_fill(order, quantity=4, price=Decimal("1000.00"))

    settlement.settle(trading_date=TUE, broker=StubBroker())

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.FILLED


def test_a_partly_filled_day_is_partial(sheet, instrument):
    order = an_order(sheet, instrument, quantity=4)
    a_fill(order, quantity=2, price=Decimal("1000.00"))

    settlement.settle(trading_date=TUE, broker=StubBroker())

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.PARTIAL


def test_a_day_that_filled_nothing_is_unfilled(sheet, instrument):
    an_order(sheet, instrument, quantity=4)

    settlement.settle(trading_date=TUE, broker=StubBroker())

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.UNFILLED


def test_a_skipped_day_keeps_its_reason(sheet, instrument):
    """It already explains itself; settlement must not overwrite that."""
    sheet.state = TradeSheetState.SKIPPED
    sheet.skip_reason = SkipReason.RESERVE_ENGAGED
    sheet.save()

    settlement.settle(trading_date=TUE, broker=StubBroker())

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.SKIPPED
    assert sheet.skip_reason == SkipReason.RESERVE_ENGAGED


def test_a_skipped_day_still_gets_a_ledger_row(sheet, instrument):
    """It accrued its deposit and spent none of it. That is a fact about the
    day, and the ledger is where facts about days live."""
    sheet.state = TradeSheetState.SKIPPED
    sheet.skip_reason = SkipReason.RESERVE_ENGAGED
    sheet.save()

    settlement.settle(trading_date=TUE, broker=StubBroker())

    assert CashLedger.objects.get(trading_date=TUE).closing_carry == Decimal("1000.00")


def test_settling_twice_writes_one_row(sheet, instrument):
    order = an_order(sheet, instrument, quantity=4)
    a_fill(order, quantity=4, price=Decimal("1000.00"))

    settlement.settle(trading_date=TUE, broker=StubBroker())
    settlement.settle(trading_date=TUE, broker=StubBroker())

    assert CashLedger.objects.count() == 1


def test_settling_is_audited(sheet, instrument):
    an_order(sheet, instrument)

    settlement.settle(trading_date=TUE, broker=StubBroker())

    assert AuditEvent.objects.filter(action="day.settled").exists()


def test_a_day_with_no_tradesheet_settles_nothing_and_says_so():
    """NI3: raised, not returned, so the run's claim is recorded failed and
    a later settle -- once the day is planned -- may still have it."""
    with pytest.raises(settlement.NothingToSettle):
        settlement.settle(trading_date=TUE, broker=StubBroker())

    assert CashLedger.objects.count() == 0


# -- holdings in paper mode ------------------------------------------------


def test_a_paper_day_grows_the_holding_it_bought(sheet, instrument):
    """Without this the engine ranks the same name as unheld every morning
    and buys it for ever -- a paper portfolio would never converge."""
    order = an_order(sheet, instrument, quantity=4, mode=OrderMode.PAPER)
    a_fill(order, quantity=4, price=Decimal("1000.00"))

    settlement.settle(trading_date=TUE, broker=StubBroker())

    assert Holding.objects.get(instrument_id="INFY").quantity == 4


def test_the_paper_holding_matches_the_lots(sheet, instrument):
    order = an_order(sheet, instrument, quantity=4, mode=OrderMode.PAPER)
    a_fill(order, quantity=2, price=Decimal("1000.00"))
    a_fill(order, quantity=2, price=Decimal("1010.00"))

    settlement.settle(trading_date=TUE, broker=StubBroker())

    assert Holding.objects.get(instrument_id="INFY").quantity == lots.held_quantity("INFY")
    assert Lot.objects.count() == 2


def test_settling_a_paper_day_twice_does_not_double_the_holding(sheet, instrument):
    order = an_order(sheet, instrument, quantity=4, mode=OrderMode.PAPER)
    a_fill(order, quantity=4, price=Decimal("1000.00"))

    settlement.settle(trading_date=TUE, broker=StubBroker())
    settlement.settle(trading_date=TUE, broker=StubBroker())

    assert Holding.objects.get(instrument_id="INFY").quantity == 4


def test_a_live_day_leaves_holdings_to_the_broker(sheet, instrument):
    """The broker knows what a live day bought; this system does not guess."""
    order = an_order(sheet, instrument, quantity=4, mode=OrderMode.LIVE)
    a_fill(order, quantity=4, price=Decimal("1000.00"))

    settlement.settle(trading_date=TUE, broker=StubBroker())

    assert Holding.objects.filter(instrument_id="INFY").exists() is False


# -- the chain across days -------------------------------------------------


def test_two_settled_days_chain(sheet, instrument):
    """Monday's closing carry is Tuesday's opening, and the ledger says so."""
    monday = TradeSheet.objects.create(
        trading_date=MON,
        version=1,
        trace_id="ts-2026-09-21",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=1,
        spendable_cash=Decimal("1000.00"),
    )
    monday_order = Order.objects.create(
        tag="tx-mon-0",
        tradesheet=monday,
        instrument=instrument,
        trading_date=MON,
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=Decimal("400.00"),
        state=OrderState.OPEN,
    )
    a_fill(monday_order, quantity=1, price=Decimal("400.00"), charges=Decimal("1.00"))

    settlement.settle(trading_date=MON, broker=StubBroker())

    tuesday_order = an_order(sheet, instrument, quantity=4)
    a_fill(tuesday_order, quantity=1, price=Decimal("500.00"), charges=Decimal("1.00"))
    settlement.settle(trading_date=TUE, broker=StubBroker())

    rows = list(CashLedger.objects.order_by("trading_date"))
    assert rows[1].opening_carry == rows[0].closing_carry
    assert all(row.reconciles for row in rows)


# -- a day with more than one version that bought --------------------------


@pytest.fixture
def replanned(sheet, instrument):
    """v1 bought 2 and was stopped; v2 was planned from what was left and
    bought 2 more. Both are the day's spend."""
    v1_order = an_order(sheet, instrument, tick_no=0, quantity=2)
    a_fill(v1_order, quantity=2, price=Decimal("1500.00"), charges=Decimal("5.00"))
    TradeSheet.objects.filter(pk=sheet.pk).update(
        state=TradeSheetState.HALTED, skip_reason=SkipReason.OPERATOR
    )
    v2 = TradeSheet.objects.create(
        trading_date=TUE,
        version=2,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=2,
        spendable_cash=Decimal("3995.00"),
        reference_price=Decimal("1000.00"),
    )
    v2_order = an_order(v2, instrument, tick_no=40, quantity=2)
    a_fill(v2_order, quantity=2, price=Decimal("1000.00"), charges=Decimal("3.00"))
    return v2


def test_a_replanned_day_settles_every_versions_fills_into_one_row(replanned):
    result = settlement.settle(trading_date=TUE, broker=StubBroker())

    assert CashLedger.objects.count() == 1
    assert result.ledger.cash_spent == Decimal("5000.00")
    assert result.ledger.charges == Decimal("8.00")
    assert result.ledger.reconciles


def test_the_spend_of_a_day_is_the_spend_of_all_its_versions(replanned):
    assert settlement.spend_and_charges(TUE) == (Decimal("5000.00"), Decimal("8.00"))


def test_the_latest_version_settles_on_its_own_plan_and_the_stopped_one_stays_stopped(
    sheet, replanned
):
    settlement.settle(trading_date=TUE, broker=StubBroker())

    sheet.refresh_from_db()
    replanned.refresh_from_db()
    assert replanned.state == TradeSheetState.FILLED
    assert sheet.state == TradeSheetState.HALTED


def test_the_ledger_note_names_each_version(replanned):
    result = settlement.settle(trading_date=TUE, broker=StubBroker())

    assert "v1" in result.ledger.note and "v2" in result.ledger.note


def test_paper_holdings_follow_a_name_only_an_earlier_version_bought(sheet, instrument):
    """v2 bought nothing of INFY; the holding still has to reflect v1's fill."""
    v1_order = an_order(sheet, instrument, tick_no=0, quantity=2)
    a_fill(v1_order, quantity=2, price=Decimal("1500.00"))
    TradeSheet.objects.create(
        trading_date=TUE,
        version=2,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("0.00"),
    )

    settlement.settle(trading_date=TUE, broker=StubBroker())

    assert Holding.objects.get(instrument=instrument).quantity == 2


def test_a_live_earlier_version_still_stops_settling_against_a_guess(sheet, instrument):
    """A live order on v1 and none yet on v2: an unreachable broker must still
    refuse to settle, because v1's fills are the day's too."""
    from broker.exceptions import TransientBrokerError

    an_order(sheet, instrument, tick_no=0, quantity=2, mode=OrderMode.LIVE)
    TradeSheet.objects.create(
        trading_date=TUE,
        version=2,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("0.00"),
    )

    class Unreachable(StubBroker):
        def orders(self):
            raise TransientBrokerError("down")

    with pytest.raises(TransientBrokerError):
        settlement.settle(trading_date=TUE, broker=Unreachable())

    assert not CashLedger.objects.exists()


def test_day_spend_says_what_is_left_of_the_day(replanned):
    spend = settlement.day_spend(TUE)

    assert spend.spent == Decimal("5000.00")
    assert spend.charges == Decimal("8.00")
    assert spend.fills == {"INFY": 4}
    assert spend.remaining == max(Decimal("0.00"), spend.opening - Decimal("5008.00"))


@pytest.mark.django_db
def test_what_a_replan_may_spend_is_the_engine_balance_left(sheet, instrument):
    from engine.ledger import spendable_on
    from engine.settlement import day_spend
    from journal.cash import allocate_daily
    from journal.models import Book

    allocate_daily(TUE, book=Book.PAPER)
    a_fill(
        an_order(sheet, instrument, quantity=1),
        quantity=1,
        price=Decimal("100"),
        charges=Decimal("0.50"),
    )

    assert spendable_on(TUE) == Decimal("899.50")
    assert day_spend(TUE).remaining == spendable_on(TUE)


# -- a rebalance sale's money (D-S1) ---------------------------------------


def a_sale(sheet, instrument, *, quantity, price, charges="4.00", stream="rebalance-1", tick=90):
    from orders.models import OrderSide

    order = Order.objects.create(
        tag=f"tx-sale-{stream}-{tick}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        stream=stream,
        tick_no=tick,
        side=OrderSide.SELL,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=Decimal(price),
        state=OrderState.COMPLETE,
        filled_quantity=quantity,
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-{order.tag}",
        quantity=quantity,
        price=Decimal(price),
        charges=Decimal(charges),
        executed_at=timezone.now(),
    )
    from orders import booking

    # Consumes the lots FIFO and posts the sale to the journal, as a real
    # sell fill does.
    booking.book(fill)
    fill.refresh_from_db()
    return fill


def a_batch(*, stream="rebalance-1", reinvest=True):
    """The rebalance batch a sale belongs to, running on the day."""
    from rebalance.models import BatchState, RebalanceSell

    return RebalanceSell.objects.create(
        uploaded_by="operator",
        content_hash=stream,
        state=BatchState.RUNNING,
        trading_date=TUE,
        stream=stream,
        reinvest=reinvest,
    )


def closed(batch):
    """The batch closed at day end, which gives its proceeds to the engine."""
    from rebalance.ladder import close_batch

    return close_batch(batch, reason="day ended")


@pytest.fixture
def held(instrument):
    """Ten INFY bought by an earlier paper day, so a paper sale has lots to
    consume (a paper sale consumes only paper lots, I6)."""
    from rebalance.tests.conftest import paper_lot

    return paper_lot(
        instrument, acquired_on=MON, quantity=10, cost=Decimal("1500"), basis_state="final"
    )


def test_a_days_proceeds_are_its_sale_fills_net_of_their_charges(sheet, instrument, held):
    batch = a_batch()
    a_sale(sheet, instrument, quantity=3, price="1600.00", charges="4.00")
    closed(batch)

    assert settlement.day_proceeds(TUE) == Decimal("4796.00")  # 3 x 1600 - 4


def test_a_batch_that_does_not_reinvest_leaves_the_day_no_proceeds(sheet, instrument, held):
    batch = a_batch(reinvest=False)
    a_sale(sheet, instrument, quantity=3, price="1600.00", charges="4.00")
    closed(batch)

    assert settlement.day_proceeds(TUE) == Decimal("0.00")


def test_the_ledger_row_carries_the_proceeds_and_charges_stay_the_buys(sheet, instrument, held):
    batch = a_batch()
    order = an_order(sheet, instrument, quantity=1)
    a_fill(order, quantity=1, price=Decimal("1500.00"), charges=Decimal("2.00"))
    a_sale(sheet, instrument, quantity=3, price="1600.00", charges="4.00")
    closed(batch)

    result = settlement.settle(trading_date=TUE, broker=StubBroker())

    assert result.ledger.proceeds == Decimal("4796.00")
    assert result.ledger.extra_allocation == Decimal("0")
    assert result.ledger.cash_spent == Decimal("1500.00")
    assert result.ledger.charges == Decimal("2.00")
    assert result.ledger.reconciles
    assert opening_for(date(2026, 9, 23)).carry == result.ledger.closing_carry


def test_a_sale_in_the_daily_buy_stream_is_not_counted_twice(sheet, instrument, held):
    """The daily stream nets its own sells into spend; proceeds are the
    rebalance streams' alone."""
    batch = a_batch()
    a_sale(sheet, instrument, quantity=1, price="1600.00", stream="daily_buy")
    closed(batch)

    assert settlement.day_proceeds(TUE) == Decimal("0.00")


def test_a_sold_names_holding_follows_its_lots_after_settlement(sheet, instrument, held):
    Holding.objects.create(instrument=instrument, quantity=10)
    a_sale(sheet, instrument, quantity=10, price="1600.00")

    settlement.settle(trading_date=TUE, broker=StubBroker())

    assert Holding.objects.get(instrument=instrument).quantity == 0
    assert lots.held_quantity("INFY") == 0


# -- a live day settles only against the broker ------------------------------


def a_live_complete_order(sheet, instrument, *, quantity=4, kite_order_id=""):
    order = an_order(sheet, instrument, quantity=quantity, mode=OrderMode.LIVE)
    order.kite_order_id = kite_order_id
    order.filled_quantity = quantity
    order.state = OrderState.COMPLETE
    order.save(update_fields=["kite_order_id", "filled_quantity", "state"])
    return order


def test_a_live_day_without_a_session_is_not_settled(sheet, instrument):
    """With no session there is nothing to reconcile against, and the row is
    written once: settling on postbacks alone would carry money already spent."""
    from broker.exceptions import BrokerError

    a_live_complete_order(sheet, instrument)

    with pytest.raises(BrokerError, match="session"):
        settlement.settle(trading_date=TUE, broker=StubBroker(), reconcile_first=False)

    assert not CashLedger.objects.exists()


def test_a_live_day_whose_fills_are_not_all_recorded_is_not_settled(sheet, instrument):
    """Kite says four filled; the trade book gave none. A row written now would
    say nothing was spent, and the carry would spend it again tomorrow."""
    from broker.exceptions import BrokerError

    a_live_complete_order(sheet, instrument, kite_order_id="kite-1")

    with pytest.raises(BrokerError, match="fills"):
        settlement.settle(trading_date=TUE, broker=StubBroker())

    assert not CashLedger.objects.exists()


def test_a_live_trade_with_a_naive_timestamp_is_recorded_in_ist(sheet, instrument):
    """pykiteconnect hands back naive datetimes, in exchange time."""
    from datetime import datetime

    from orders import reconcile

    order = an_order(sheet, instrument, quantity=2, mode=OrderMode.LIVE)
    order.kite_order_id = "kite-1"
    order.save(update_fields=["kite_order_id"])
    trade = {
        "trade_id": "T1",
        "order_id": "kite-1",
        "quantity": 2,
        "average_price": 1649.0,
        "fill_timestamp": datetime(2026, 9, 22, 10, 15, 0),
    }

    reconcile.record_fills(trading_date=TUE, broker=StubBroker(trades=[trade]))

    fill = Fill.objects.get(kite_trade_id="T1")
    assert timezone.is_aware(fill.executed_at)
    assert timezone.localtime(fill.executed_at).hour == 10


# -- the day's own book (M1, M2) ---------------------------------------------


def test_a_tradesheet_written_directly_takes_the_mode_in_force(instrument, already_live):
    paper = TradeSheet.objects.create(trading_date=MON, version=1)
    already_live()
    live = TradeSheet.objects.create(trading_date=TUE, version=1)

    assert (paper.book, live.book) == (Book.PAPER, Book.LIVE)


def test_a_paper_day_settled_after_the_switch_writes_a_paper_row(sheet, instrument, already_live):
    """The day was planned and funded in paper; switching to live before its
    settle must not summarise the live book into its row."""
    a_fill(an_order(sheet, instrument), quantity=2)
    already_live()

    result = settlement.settle(trading_date=TUE, broker=StubBroker(), reconcile_first=False)

    assert result.ledger.book == Book.PAPER
    assert result.ledger.cash_spent == Decimal("3298.00")
    assert not CashLedger.objects.filter(book=Book.LIVE).exists()


def test_settle_takes_book_locks_live_then_paper(sheet, instrument, monkeypatch):
    """The same order as record_lumpsum, so the two never deadlock (N1)."""
    calls = []
    monkeypatch.setattr("journal.trades.settle_trades", lambda day, *, book: calls.append(book))
    an_order(sheet, instrument, tick_no=1, state=OrderState.CANCELLED)
    an_order(sheet, instrument, tick_no=2, mode=OrderMode.LIVE, state=OrderState.CANCELLED)

    settlement.settle(trading_date=TUE, broker=StubBroker(), reconcile_first=True)

    assert calls == [Book.LIVE, Book.PAPER]


# -- a day settled after the next (NI1) --------------------------------------


def test_a_day_settled_after_the_next_posts_its_settlement_entry(instrument):
    """D+1's 15:45 settle wrote its row before D's did. D can no longer have
    a row -- D+1's already counted D's lines -- but its trades still owe
    their settlement entry, or they sit in the settlement account for good
    and the statement's line for D has nothing to match."""
    from engine.ledger import LedgerError
    from engine.ledger import settle_day as write_row
    from journal.accounts import SETTLE_ENGINE
    from journal.balances import balance
    from journal.models import EntryKind, JournalEntry

    allocate_daily(MON, book=Book.PAPER)
    monday = TradeSheet.objects.create(
        trading_date=MON,
        version=1,
        trace_id="ts-2026-09-21",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=2,
        spendable_cash=Decimal("7000.00"),
        reference_price=Decimal("1650.50"),
    )
    a_fill(an_order(monday, instrument, quantity=2), quantity=2)
    allocate_daily(TUE, book=Book.PAPER)
    write_row(TUE, book=Book.PAPER)

    with pytest.raises(LedgerError, match="already summarised"):
        settlement.settle(trading_date=MON, broker=StubBroker())
    assert not JournalEntry.objects.filter(kind=EntryKind.SETTLEMENT, occurred_on=MON).exists()

    posted = settlement.settle_trades_behind(MON)

    assert [entry.kind for entry in posted] == [EntryKind.SETTLEMENT]
    assert posted[0].occurred_on == MON
    assert balance(SETTLE_ENGINE, book=Book.PAPER, on=MON) == 0
    assert settlement.settle_trades_behind(MON) == []


def test_settling_only_the_trades_names_both_routes_while_an_order_is_open(sheet, instrument):
    """I-1: an order of the day still open is levelled by the day's tradebook
    import, or by --level-unseen once Console shows it traded nothing."""
    from engine.ledger import LedgerError
    from engine.ledger import settle_day as write_row

    an_order(sheet, instrument, quantity=2)
    write_row(date(2026, 9, 23), book=Book.PAPER)

    with pytest.raises(LedgerError) as refused:
        settlement.settle_trades_behind(TUE)

    assert "import" in str(refused.value) and "tradebook" in str(refused.value)
    assert "--level-unseen" in str(refused.value)


def test_settling_only_the_trades_is_refused_while_the_day_can_settle_whole(sheet, instrument):
    from engine.ledger import LedgerError

    a_fill(an_order(sheet, instrument, quantity=2), quantity=2)

    with pytest.raises(LedgerError, match="settle_day --date"):
        settlement.settle_trades_behind(TUE)


# -- go-live round 2, wave E: the day's own book (m-6) ---------------------


def _paper_carry_on_monday():
    from journal.cash import allocate

    allocate(
        Decimal("500"),
        book=Book.PAPER,
        on=MON,
        actor="o",
        description="paper carry",
        source_type="lumpsum",
    )


def test_a_paper_day_read_after_the_switch_reads_its_own_book(sheet, instrument, already_live):
    """m-6: day_spend, the deck's cash and carry read the book the day was planned in."""
    from engine.ledger import spendable_on
    from web.deck import holdings_stats, snapshot

    _paper_carry_on_monday()
    paper = spendable_on(TUE, book=Book.PAPER)
    already_live()

    assert spendable_on(TUE, book=Book.LIVE) != paper
    assert settlement.day_spend(TUE).opening == paper
    cash = next(stat for stat in snapshot({}, TUE) if stat.label == "CASH TO DEPLOY")
    assert cash.value == f"₹{paper:.2f}"
    carry = next(stat for stat in holdings_stats({}, TUE) if stat.label == "CARRY")
    assert carry.value == "₹500.00"


def test_a_paper_days_proceeds_are_read_after_the_switch(sheet, instrument, held, already_live):
    batch = a_batch()
    a_sale(sheet, instrument, quantity=3, price="1600.00", charges="4.00")
    closed(batch)
    already_live()

    assert settlement.day_proceeds(TUE) == Decimal("4796.00")
