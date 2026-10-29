"""Rebalance sells, and the pause.

The two things worth protecting: a sale cannot happen by accident, and a buy
and a sell of the same name cannot collide on the same day. The second one
matters because CNC nets them off -- the shares never arrive, and a lot is
created and consumed without anything moving.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from audit.models import AuditEvent
from configuration import pause as pause_module
from configuration.pause import PauseError
from engine.models import TradeSheet, TradeSheetState
from portfolio.instruments import refresh
from portfolio.models import BasisState, Instrument, OpeningImport
from rebalance import batches
from rebalance.batches import ExecutionRefused, UploadRefused
from rebalance.models import BatchState, RebalanceSell
from rebalance.tests.conftest import paper_lot

pytestmark = pytest.mark.django_db

TUE = date(2026, 9, 22)
JAN = date(2024, 1, 15)


@pytest.fixture
def universe(db):
    refresh(
        [
            {
                "tradingsymbol": symbol,
                "instrument_token": token,
                "name": symbol,
                "exchange": "NSE",
                "instrument_type": "EQ",
                "segment": "NSE",
                "tick_size": 0.05,
                "lot_size": 1,
            }
            for symbol, token in (("INFY", 408065), ("TCS", 2953217))
        ]
    )
    # A rebalance sell is refused until the lot history is whole.
    OpeningImport.objects.create(
        imported_by="operator", source="contract notes", completed_at=timezone.now()
    )
    for symbol in ("INFY", "TCS"):
        paper_lot(
            Instrument.objects.get(pk=symbol),
            acquired_on=JAN,
            quantity=10,
            cost=Decimal("1000"),
            basis_state=BasisState.FINAL,
        )


@pytest.fixture(autouse=True)
def a_trading_morning(db):
    """Tuesday at 10:00, after the day's plan was made: the only time a batch
    may be executed. The plan is version 50 so a test can still create the
    day's own versions from 1."""
    import time_machine

    TradeSheet.objects.create(
        trading_date=TUE, version=50, trace_id="ts-2026-09-22", state=TradeSheetState.RUNNING
    )
    with time_machine.travel("2026-09-22 10:00:00 +05:30", tick=True):
        yield


class StubBroker:
    def __init__(self):
        self.cancelled = []

    def place_order(self, **kwargs):
        return "kite-1"

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return order_id


A_FILE = b"symbol,quantity\nINFY,4\n"


# -- validation ------------------------------------------------------------


def test_a_clean_file_uploads(universe):
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)

    assert batch.state == BatchState.DRAFT
    assert batch.rows.count() == 1


def test_an_upload_defaults_to_reinvesting(universe):
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)

    assert batch.reinvest is True


def test_an_upload_can_opt_out_of_reinvesting(universe):
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE, reinvest=False)

    assert batch.reinvest is False


def test_selling_more_than_the_lots_hold_is_refused(universe):
    with pytest.raises(UploadRefused, match="not the 99"):
        batches.upload(b"symbol,quantity\nINFY,99\n", uploaded_by="operator", trading_date=TUE)


def test_an_unknown_symbol_is_refused(universe):
    with pytest.raises(UploadRefused, match="instrument master"):
        batches.upload(b"symbol,quantity\nNOSUCHCO,1\n", uploaded_by="operator", trading_date=TUE)


def test_a_duplicate_symbol_is_refused(universe):
    with pytest.raises(UploadRefused, match="appears twice"):
        batches.upload(
            b"symbol,quantity\nINFY,1\nINFY,2\n", uploaded_by="operator", trading_date=TUE
        )


def test_a_zero_quantity_is_refused(universe):
    with pytest.raises(UploadRefused, match="quantity of 0"):
        batches.upload(b"symbol,quantity\nINFY,0\n", uploaded_by="operator", trading_date=TUE)


def test_a_refused_upload_stores_nothing(universe):
    with pytest.raises(UploadRefused):
        batches.upload(b"symbol,quantity\nINFY,99\n", uploaded_by="operator", trading_date=TUE)

    assert RebalanceSell.objects.count() == 0


def test_a_refusal_is_audited(universe):
    with pytest.raises(UploadRefused):
        batches.upload(b"symbol,quantity\nINFY,99\n", uploaded_by="operator", trading_date=TUE)

    assert AuditEvent.objects.filter(action="rebalance.rejected").exists()


def test_a_sell_is_refused_before_the_opening_import(db):
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
    from portfolio.opening import OpeningImportIncomplete

    with pytest.raises(OpeningImportIncomplete):
        batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)


# -- the interlock, both ways ---------------------------------------------


def test_selling_a_name_the_day_is_buying_is_refused(universe):
    """CNC nets them off: the shares never arrive and a lot is created and
    consumed without anything moving."""
    TradeSheet.objects.create(
        trading_date=TUE,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=Instrument.objects.get(pk="INFY"),
        quantity=2,
    )

    with pytest.raises(UploadRefused, match="being bought today"):
        batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)


def test_a_name_the_day_skipped_can_still_be_sold(universe):
    TradeSheet.objects.create(
        trading_date=TUE,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.SKIPPED,
        skip_reason="reserve_engaged",
        instrument=Instrument.objects.get(pk="INFY"),
        quantity=0,
    )

    assert batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE) is not None


def test_a_running_sell_takes_its_name_out_of_the_universe(universe):
    """The other half of the interlock (M34)."""
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)
    batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)

    assert batches.instruments_with_a_live_sell(TUE) == {"INFY"}


def test_selection_excludes_a_name_being_sold(universe):
    from engine.selection import EXCLUDED_BEING_SOLD, Quote, rank

    ranking = rank(
        weights={"INFY": Decimal("50"), "TCS": Decimal("50")},
        holdings={},
        quotes={
            "INFY": Quote("INFY", Decimal("1650"), Decimal("1650.5")),
            "TCS": Quote("TCS", Decimal("3400"), Decimal("3401")),
        },
        being_sold={"INFY"},
    )

    assert [c.tradingsymbol for c in ranking.candidates] == ["TCS"]
    assert ranking.excluded[0].reason == EXCLUDED_BEING_SOLD


# -- the one-shot token ---------------------------------------------------


def test_a_batch_starts_with_its_token(universe):
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)

    started = batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)

    assert started.state == BatchState.RUNNING
    assert started.stream.startswith("rebalance-")


def test_a_wrong_token_is_refused(universe):
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)

    with pytest.raises(ExecutionRefused, match="does not match"):
        batches.start(batch, token="not-the-token", actor="operator", trading_date=TUE)


def test_a_batch_cannot_start_while_trading_is_stopped(universe):
    """Stopping has to mean stopping. The ladder and the fallback already
    refuse while paused; starting a sale was the way round them."""
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)
    pause_module.pause(actor="operator", reason="stepping away")

    with pytest.raises(ExecutionRefused, match="paused"):
        batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)

    batch.refresh_from_db()
    assert batch.state == BatchState.DRAFT


def test_a_batch_starts_once_trading_resumes(universe):
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)
    pause_module.pause(actor="operator", reason="stepping away")
    pause_module.resume(actor="operator")

    started = batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)

    assert started.state == BatchState.RUNNING


def test_a_token_is_single_use(universe):
    """A double submit cannot sell twice."""
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)
    batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)

    with pytest.raises(ExecutionRefused):
        batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)


def test_an_expired_token_is_refused(universe):
    """A stale tab cannot sell a portfolio somebody looked at an hour ago."""
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)
    RebalanceSell.objects.filter(pk=batch.pk).update(
        token_expires_at=timezone.now() - timedelta(minutes=1)
    )
    batch.refresh_from_db()

    with pytest.raises(ExecutionRefused, match="expired"):
        batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)


def test_the_token_is_bound_to_the_content_that_was_previewed(universe):
    """Two uploads of different files have different hashes and tokens."""
    first = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)
    second = batches.upload(b"symbol,quantity\nTCS,2\n", uploaded_by="operator", trading_date=TUE)

    assert first.content_hash != second.content_hash
    assert first.execute_token != second.execute_token


def test_starting_is_audited(universe):
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)
    batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)

    assert AuditEvent.objects.filter(action="rebalance.started").exists()


def test_a_preview_shows_what_would_be_sold(universe):
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)

    rows = batches.preview(batch)

    assert rows[0]["symbol"] == "INFY"
    assert rows[0]["held"] == 10
    assert rows[0]["oldest_lot"] == JAN.isoformat()


# -- cancelling a running batch (M32a) ------------------------------------


def test_a_running_batch_can_be_cancelled(universe):
    batch = batches.upload(A_FILE, uploaded_by="operator", trading_date=TUE)
    batches.start(batch, token=batch.execute_token, actor="operator", trading_date=TUE)

    cancelled = batches.cancel(
        batch, actor="operator", reason="changed my mind", broker=StubBroker()
    )

    assert cancelled.state == BatchState.CANCELLED
    assert AuditEvent.objects.filter(action="rebalance.cancelled").exists()


# -- pause and resume ------------------------------------------------------


def test_the_system_starts_running(db):
    assert pause_module.is_paused() is False


def test_pausing_stops_the_day(db):
    pause_module.pause(actor="operator", reason="away for a fortnight")

    assert pause_module.is_paused() is True
    with pytest.raises(PauseError, match="paused"):
        pause_module.require_running("Generating a tradesheet")


def test_a_paused_day_plans_nothing(universe):
    from engine.selection import Quote
    from engine.tradesheet import generate
    from model_portfolio.uploads import store

    store("symbol,target_pct\nINFY,50\nTCS,50\n", uploaded_by="operator")
    pause_module.pause(actor="operator", reason="away")

    sheet = generate(
        trading_date=TUE,
        quotes={
            "INFY": Quote("INFY", Decimal("1650"), Decimal("1650.5")),
            "TCS": Quote("TCS", Decimal("3400"), Decimal("3401")),
        },
    )

    assert sheet.state == TradeSheetState.SKIPPED
    assert sheet.skip_reason == "paused"


def test_carry_does_not_accrue_while_paused(db):
    """A month away must not come back as one large purchase."""
    from engine.ledger import opening_for

    pause_module.pause(actor="operator", reason="away")

    assert opening_for(TUE).daily_allocation == Decimal("0.00")


def test_resuming_starts_accrual_again(db):
    from engine.ledger import opening_for

    pause_module.pause(actor="operator", reason="away")
    pause_module.resume(actor="operator")

    assert opening_for(TUE).daily_allocation == Decimal("1000.00")


def test_a_pause_can_carry_a_resume_date(db):
    """Away for three weeks, back on the 14th."""
    pause_module.pause(actor="operator", reason="away", resume_on=date(2026, 10, 14))

    assert pause_module.is_paused(on=date(2026, 10, 1)) is True
    assert pause_module.is_paused(on=date(2026, 10, 14)) is False


def test_pausing_twice_is_refused(db):
    pause_module.pause(actor="operator", reason="away")

    with pytest.raises(PauseError, match="already paused"):
        pause_module.pause(actor="operator", reason="away again")


def test_resuming_when_running_is_refused(db):
    with pytest.raises(PauseError, match="not paused"):
        pause_module.resume(actor="operator")


def test_a_resume_date_in_the_past_is_refused(db):
    with pytest.raises(PauseError, match="before it began"):
        pause_module.pause(actor="operator", reason="away", resume_on=date(2020, 1, 1))


def test_pausing_and_resuming_are_audited(db):
    pause_module.pause(actor="operator", reason="away")
    pause_module.resume(actor="operator", note="back")

    assert AuditEvent.objects.filter(action="system.paused").exists()
    assert AuditEvent.objects.filter(action="system.resumed").exists()


# -- executing: when a batch may start (S7) ---------------------------------


def uploaded(content=A_FILE):
    return batches.upload(content, uploaded_by="operator", trading_date=TUE)


def execute(batch, **kwargs):
    return batches.start(batch, token=batch.execute_token, actor="operator", **kwargs)


def test_a_batch_cannot_start_on_a_day_the_market_is_shut(universe):
    batch = uploaded()

    with pytest.raises(ExecutionRefused, match="not a trading day"):
        execute(batch, trading_date=date(2026, 9, 26))


def test_a_batch_cannot_start_in_live_mode_yet(already_live, universe):
    batch = uploaded()
    already_live()

    with pytest.raises(ExecutionRefused, match="paper"):
        execute(batch, trading_date=TUE)


def test_a_batch_cannot_start_once_the_day_has_settled(universe):
    from engine.models import CashLedger

    batch = uploaded()
    CashLedger.objects.create(book="paper", trading_date=TUE, daily_allocation=Decimal("1000"))

    with pytest.raises(ExecutionRefused, match="settled"):
        execute(batch, trading_date=TUE)


def test_a_batch_cannot_start_after_the_close(universe):
    import time_machine

    with time_machine.travel("2026-09-22 15:20:00 +05:30", tick=False):
        batch = uploaded()

    with (
        time_machine.travel("2026-09-22 15:26:00 +05:30", tick=False),
        pytest.raises(ExecutionRefused, match="close"),
    ):
        execute(batch, trading_date=TUE)


def test_a_batch_cannot_start_before_the_days_plan_exists(universe):
    TradeSheet.objects.filter(trading_date=TUE).delete()
    batch = uploaded()

    with pytest.raises(ExecutionRefused, match="09:45 plan"):
        execute(batch, trading_date=TUE)


def test_a_batch_cannot_start_for_a_name_the_plan_bought_since_it_was_uploaded(universe):
    """Uploaded before the plan named INFY; executed after."""
    batch = uploaded()
    TradeSheet.objects.create(
        trading_date=TUE,
        version=51,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=Instrument.objects.get(pk="INFY"),
        quantity=2,
    )

    with pytest.raises(ExecutionRefused, match="INFY"):
        execute(batch, trading_date=TUE)


def test_two_batches_cannot_sell_more_than_is_held_between_them(universe):
    """Ten INFY held; one running batch already means to sell eight."""
    execute(uploaded(b"symbol,quantity\nINFY,8\n"), trading_date=TUE)
    second = uploaded(b"symbol,quantity\nINFY,4\n")

    with pytest.raises(ExecutionRefused, match="INFY"):
        execute(second, trading_date=TUE)


def test_execute_is_refused_when_the_books_own_lots_fall_short(universe):
    """M7: a paper sale takes paper lots only, and one with none to take
    posts nothing, so its proceeds would never be reinvested. Execute
    counts the sale's own book's lots, however many real shares there are."""
    from portfolio.models import Lot

    batch = uploaded(b"symbol,quantity\nINFY,4\n")
    Lot.objects.filter(instrument_id="INFY").update(remaining_quantity=2)
    Lot.objects.create(
        instrument=Instrument.objects.get(pk="INFY"),
        acquired_on=JAN,
        quantity=50,
        remaining_quantity=50,
        cost_per_unit=Decimal("900"),
        basis_state=BasisState.FINAL,
    )

    with pytest.raises(ExecutionRefused, match=r"INFY \(holds 2"):
        execute(batch, trading_date=TUE)


def test_starting_stamps_the_moment_and_the_trading_days_trace(universe):
    from datetime import datetime

    moment = timezone.make_aware(datetime(2026, 9, 22, 10, 15))
    started = execute(uploaded(), trading_date=TUE, moment=moment)

    assert started.started_at == moment
    assert started.trace_id == "ts-2026-09-22"


# -- what the day may buy around a sale (S8) --------------------------------


def a_sell_order(batch, *, state, filled=0, symbol="INFY"):
    from orders.models import Fill, Order, OrderSide, OrderType

    order = Order.objects.create(
        tag=f"tx-{batch.stream[-8:]}-{symbol}-{state[:4]}",
        tradesheet=TradeSheet.objects.get(trading_date=TUE, version=50),
        instrument=Instrument.objects.get(pk=symbol),
        trading_date=TUE,
        stream=batch.stream,
        tick_no=1,
        side=OrderSide.SELL,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=2,
        filled_quantity=filled,
        limit_price=Decimal("1650"),
        state=state,
    )
    if filled:
        Fill.objects.create(
            order=order,
            kite_trade_id=f"paper-{order.tag}",
            quantity=filled,
            price=Decimal("1650"),
            charges=Decimal("1.00"),
            executed_at=timezone.now(),
        )
    return order


def test_a_name_sold_or_being_sold_today_is_left_out_of_every_plan(universe):
    from orders.state import OrderState

    batch = execute(uploaded(b"symbol,quantity\nINFY,2\nTCS,2\n"), trading_date=TUE)
    a_sell_order(batch, state=OrderState.COMPLETE, filled=2, symbol="INFY")
    a_sell_order(batch, state=OrderState.CANCELLED, filled=0, symbol="TCS")

    assert batches.names_sold_today(TUE) == {"INFY"}


def test_a_cancelled_batchs_sold_name_stays_out_of_a_regenerated_plan(universe):
    from engine.selection import Quote
    from engine.tradesheet import generate
    from model_portfolio.uploads import store
    from orders.state import OrderState

    store("symbol,target_pct\nINFY,50\nTCS,50\n", uploaded_by="operator")
    batch = execute(uploaded(), trading_date=TUE)
    a_sell_order(batch, state=OrderState.COMPLETE, filled=2)
    batches.cancel(batch, actor="operator", reason="changed my mind", broker=StubBroker())

    sheet = generate(
        trading_date=TUE,
        quotes={
            "INFY": Quote("INFY", Decimal("1650"), Decimal("1650.5")),
            "TCS": Quote("TCS", Decimal("3400"), Decimal("3401")),
        },
        has_session=True,
    )

    assert "INFY" not in {line.instrument_id for line in sheet.buy_lines()}


# -- cancelling and stopping (S9, S10) --------------------------------------


def test_cancelling_a_batch_that_sold_leaves_it_partial(universe):
    from orders.state import OrderState

    batch = execute(uploaded(), trading_date=TUE)
    a_sell_order(batch, state=OrderState.COMPLETE, filled=2)

    cancelled = batches.cancel(batch, actor="operator", reason="enough", broker=StubBroker())

    assert cancelled.state == BatchState.PARTIAL
    assert cancelled.rows.get().sold_quantity == 2


def test_only_a_running_batch_can_be_cancelled(universe):
    batch = uploaded()

    with pytest.raises(ExecutionRefused, match="Only a running batch"):
        batches.cancel(batch, actor="operator", reason="r", broker=StubBroker())


def test_cancelling_waits_for_a_round_in_flight(universe, settings, tmp_path, monkeypatch):
    from orders import idempotency, overrides

    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
    monkeypatch.setattr(overrides, "STOP_WAIT_SECONDS", 0.2)
    batch = execute(uploaded(), trading_date=TUE)

    with idempotency.order_lock(), pytest.raises(idempotency.Busy):
        batches.cancel(batch, actor="operator", reason="r", broker=StubBroker())

    batch.refresh_from_db()
    assert batch.state == BatchState.RUNNING


def test_stopping_everything_closes_a_batch_from_its_fills(universe, settings, tmp_path):
    from orders import halt
    from orders.state import OrderState

    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
    batch = execute(uploaded(), trading_date=TUE)
    a_sell_order(batch, state=OrderState.COMPLETE, filled=2)

    halt.stop_trading(actor="operator", reason="stop", broker=StubBroker())

    batch.refresh_from_db()
    assert batch.state == BatchState.PARTIAL


def test_executing_waits_for_a_round_in_flight(universe, settings, tmp_path, monkeypatch):
    """Two executes at once, or one racing the evening's settle, are taken in
    turn under the order lock -- each sees what the other did."""
    from orders import idempotency, overrides

    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
    monkeypatch.setattr(overrides, "STOP_WAIT_SECONDS", 0.2)
    batch = uploaded()

    with idempotency.order_lock(), pytest.raises(idempotency.Busy):
        execute(batch, trading_date=TUE)

    batch.refresh_from_db()
    assert batch.state == BatchState.DRAFT


def test_the_batch_page_shows_what_has_sold_while_it_runs(universe):
    """From the fills, during the day -- not from a count written at settle."""
    from orders.state import OrderState

    batch = execute(uploaded(), trading_date=TUE)
    a_sell_order(batch, state=OrderState.COMPLETE, filled=2)

    assert batches.preview(batch)[0]["sold"] == 2
