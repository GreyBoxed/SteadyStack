"""Idempotency, the tradesheet lock, overrides and reconciliation.

These are the parts that decide what happens when something has already gone
wrong -- a task fired twice, a placement whose outcome nobody knows, an
operator changing their mind mid-day. The rule running through all of them is
that a fill is a fact: it is never unwound, never re-placed, and never
reconciled away.
"""

from datetime import date, datetime, time
from decimal import Decimal

import pytest
from django.utils import timezone

from audit.models import AuditEvent
from engine.lock import TradesheetLocked, is_locked, require_unlocked
from engine.models import (
    OverrideKind,
    SkipReason,
    TradeSheet,
    TradesheetOverride,
    TradeSheetState,
)
from orders import idempotency, overrides, reconcile
from orders.models import Fill, Order, OrderType, TaskRun
from orders.overrides import OverrideRefused
from orders.state import OrderState
from portfolio.instruments import refresh
from portfolio.models import Instrument

TUESDAY = date(2026, 9, 22)


def master(symbol="INFY", token=408065):
    return {
        "tradingsymbol": symbol,
        "instrument_token": token,
        "name": symbol,
        "exchange": "NSE",
        "instrument_type": "EQ",
        "segment": "NSE",
        "tick_size": 0.05,
        "lot_size": 1,
    }


class StubBroker:
    def __init__(self, *, book=None, trades=None):
        self.book = book or []
        self._trades = trades or []
        self.cancelled = []

    def place_order(self, **kwargs):
        return "kite-1"

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return order_id

    def orders(self):
        return self.book

    def trades(self):
        return self._trades


@pytest.fixture
def instrument(db):
    refresh([master()])
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
        quantity=6,
        spendable_cash=Decimal("12000"),
        reference_price=Decimal("1650.50"),
    )


def an_order(
    sheet, instrument, *, tick_no, quantity, state=OrderState.OPEN, filled=0, mode="paper"
):
    return Order.objects.create(
        tag=f"tx-test-{tick_no}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        tick_no=tick_no,
        mode=mode,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        filled_quantity=filled,
        limit_price=Decimal("1650.00"),
        state=state,
        kite_order_id=f"kite-{tick_no}",
    )


# -- idempotency (M24) -----------------------------------------------------


@pytest.mark.django_db
def test_a_task_can_be_claimed_once():
    run = idempotency.claim(trading_date=TUESDAY, task="ladder", tick_no=3)
    assert isinstance(run, TaskRun)


@pytest.mark.django_db
def test_the_same_tick_cannot_be_claimed_twice():
    """A tick that fires twice must not buy twice."""
    idempotency.claim(trading_date=TUESDAY, task="ladder", tick_no=3)

    with pytest.raises(idempotency.AlreadyRan):
        idempotency.claim(trading_date=TUESDAY, task="ladder", tick_no=3)


@pytest.mark.django_db
def test_different_ticks_are_different_claims():
    idempotency.claim(trading_date=TUESDAY, task="ladder", tick_no=3)
    idempotency.claim(trading_date=TUESDAY, task="ladder", tick_no=4)

    assert TaskRun.objects.count() == 2


@pytest.mark.django_db
def test_a_rebalance_batch_does_not_collide_with_the_ladder():
    """The reason `stream` is in the key: a sell batch runs alongside the buy."""
    idempotency.claim(trading_date=TUESDAY, task="ladder", tick_no=3)
    idempotency.claim(trading_date=TUESDAY, task="ladder", tick_no=3, stream="rebalance-1")

    assert TaskRun.objects.count() == 2


@pytest.mark.django_db
def test_tick_zero_is_a_real_claim_not_a_null():
    """Postgres does not collide NULLs, so a nullable tick would protect
    nothing -- every run would insert alongside every other."""
    idempotency.claim(trading_date=TUESDAY, task="eod")

    with pytest.raises(idempotency.AlreadyRan):
        idempotency.claim(trading_date=TUESDAY, task="eod")


@pytest.mark.django_db
def test_a_claim_records_whether_it_succeeded():
    with idempotency.once(trading_date=TUESDAY, task="ladder", tick_no=1) as run:
        pass

    run.refresh_from_db()
    assert run.succeeded is True
    assert run.finished_at is not None


@pytest.mark.django_db
def test_a_failed_run_is_recorded_as_failed():
    with pytest.raises(RuntimeError):
        with idempotency.once(trading_date=TUESDAY, task="ladder", tick_no=1):
            raise RuntimeError("the tick exploded")

    run = TaskRun.objects.get()
    assert run.succeeded is False
    assert "exploded" in run.detail


@pytest.mark.django_db
def test_a_crashed_run_still_holds_its_claim():
    """A half-finished task must not be silently retried by the next tick."""
    with pytest.raises(RuntimeError):
        with idempotency.once(trading_date=TUESDAY, task="ladder", tick_no=1):
            raise RuntimeError("boom")

    with pytest.raises(idempotency.AlreadyRan):
        idempotency.claim(trading_date=TUESDAY, task="ladder", tick_no=1)


@pytest.mark.django_db
def test_the_order_lock_excludes_a_second_holder(settings, tmp_path):
    """The 15:00 tick and the 15:00 fallback must not interleave."""
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")

    with idempotency.order_lock(), pytest.raises(idempotency.Busy):
        with idempotency.order_lock():
            pass


@pytest.mark.django_db
def test_an_operator_can_wait_for_a_round_to_finish(settings, tmp_path):
    """Stop today waits a few seconds for a round rather than refusing: the
    person pressing it wants the day stopped, not told to try again."""
    import threading
    import time

    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
    held, release = threading.Event(), threading.Event()

    def a_round():
        with idempotency.order_lock():
            held.set()
            release.wait(5)

    runner = threading.Thread(target=a_round)
    runner.start()
    held.wait(5)
    threading.Timer(0.3, release.set).start()

    started = time.monotonic()
    with idempotency.order_lock(wait_seconds=5):
        waited = time.monotonic() - started
    runner.join()

    assert 0.2 < waited < 5


@pytest.mark.django_db
def test_waiting_for_the_lock_gives_up_in_the_end(settings, tmp_path):
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")

    with idempotency.order_lock(), pytest.raises(idempotency.Busy):
        with idempotency.order_lock(wait_seconds=0.3):
            pass


@pytest.mark.django_db
def test_a_run_that_cannot_get_the_lock_leaves_no_claim(settings, tmp_path):
    """m-2: the lock is taken before the claim is written, so a claim that is
    unfinished while the lock is free belongs to a run that died -- never to
    one still waiting its turn."""
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")

    with idempotency.order_lock(), pytest.raises(idempotency.Busy):
        with idempotency.once(trading_date=TUESDAY, task="settle", lock_wait=0.2):
            pass

    assert not TaskRun.objects.exists()


@pytest.mark.django_db
def test_a_run_that_cannot_get_the_lock_logs_an_error(settings, tmp_path, caplog):
    """Minor 1 (wave H): Busy leaves no claim and no failed TaskRun, so this
    line is what still makes a hung lock holder alarm as `task_failed`."""
    import logging

    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")

    with idempotency.order_lock():
        with caplog.at_level(logging.INFO, logger="steadystack.orders"):
            with pytest.raises(idempotency.Busy):
                with idempotency.once(
                    trading_date=TUESDAY, task="settle", stream="daily", lock_wait=0.2
                ):
                    pass

    busy = [r for r in caplog.records if r.getMessage() == "scheduled task busy"]
    assert len(busy) == 1
    assert busy[0].levelname == "ERROR"
    logged = _logged(busy[0])
    assert logged["task"] == "settle"
    assert logged["trading_date"] == str(TUESDAY)
    assert logged["stream"] == "daily"


def _logged(record) -> dict:
    from audit.logging import _FIELDS_KEY

    return getattr(record, _FIELDS_KEY, {})


@pytest.mark.django_db
def test_an_unfinished_claim_is_held_under_the_lock(settings, tmp_path):
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")

    with idempotency.once(trading_date=TUESDAY, task="settle"):
        assert TaskRun.objects.get().succeeded is None
        with pytest.raises(idempotency.Busy):
            with idempotency.order_lock():
                pass

    assert TaskRun.objects.get().succeeded is True


@pytest.mark.django_db
def test_the_lock_is_released_afterwards(settings, tmp_path):
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")

    with idempotency.order_lock():
        pass

    with idempotency.order_lock():
        pass  # does not raise


# -- the tradesheet lock (D13) ---------------------------------------------


@pytest.mark.django_db
def test_a_day_with_no_orders_is_unlocked():
    assert is_locked(TUESDAY) is False
    require_unlocked(TUESDAY)


@pytest.mark.django_db
def test_a_live_order_locks_the_day(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=2)

    assert is_locked(TUESDAY) is True
    with pytest.raises(TradesheetLocked, match="live orders"):
        require_unlocked(TUESDAY)


@pytest.mark.django_db
def test_a_terminal_order_does_not_lock_the_day(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.CANCELLED)

    assert is_locked(TUESDAY) is False


@pytest.mark.django_db
def test_a_day_whose_buy_filled_stays_locked_after_every_order_is_done(sheet, instrument):
    """The double buy: every order complete, nothing live -- and a new version
    would start with nothing committed, so the ladder would buy the day again."""
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)

    assert is_locked(TUESDAY) is True
    with pytest.raises(TradesheetLocked, match="already bought"):
        require_unlocked(TUESDAY)


@pytest.mark.django_db
def test_a_cancelled_order_that_part_filled_still_locks_the_day(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.CANCELLED, filled=1)

    assert is_locked(TUESDAY) is True


@pytest.mark.django_db
def test_a_settled_day_is_locked():
    from engine.models import CashLedger

    CashLedger.objects.create(book="paper", trading_date=TUESDAY, daily_allocation=Decimal("2600"))

    with pytest.raises(TradesheetLocked, match="settled"):
        require_unlocked(TUESDAY)


@pytest.mark.django_db
def test_regeneration_is_refused_while_an_order_is_live(sheet, instrument):
    from engine.selection import Quote
    from engine.tradesheet import generate

    an_order(sheet, instrument, tick_no=0, quantity=2)

    with pytest.raises(TradesheetLocked):
        generate(
            trading_date=TUESDAY,
            quotes={"INFY": Quote("INFY", Decimal("1650"), Decimal("1650.5"))},
        )


# -- overrides (M15a) ------------------------------------------------------


@pytest.mark.django_db
def test_the_quantity_can_be_reduced(sheet, instrument):
    outcome = overrides.reduce_quantity(
        sheet, quantity=3, actor="operator", reason="market looks thin", broker=StubBroker()
    )

    sheet.refresh_from_db()
    assert sheet.quantity == 3
    assert outcome.override.kind == OverrideKind.REDUCE_QUANTITY


@pytest.mark.django_db
def test_a_reduction_cancels_the_newest_orders_first(sheet, instrument):
    """Newest are highest up the ladder and least likely to have filled."""
    an_order(sheet, instrument, tick_no=0, quantity=2)
    an_order(sheet, instrument, tick_no=1, quantity=2)
    an_order(sheet, instrument, tick_no=2, quantity=2)
    broker = StubBroker()

    outcome = overrides.reduce_quantity(
        sheet, quantity=2, actor="operator", reason="enough", broker=broker
    )

    assert outcome.cancelled == ["tx-test-2", "tx-test-1"]
    assert Order.objects.get(tick_no=0).state == OrderState.OPEN


@pytest.mark.django_db
def test_a_reduction_below_what_filled_is_refused(sheet, instrument):
    """There is no way to un-buy shares."""
    an_order(sheet, instrument, tick_no=0, quantity=4, state=OrderState.COMPLETE, filled=4)

    with pytest.raises(OverrideRefused, match="cannot be un-bought"):
        overrides.reduce_quantity(
            sheet, quantity=2, actor="operator", reason="too late", broker=StubBroker()
        )


@pytest.mark.django_db
def test_the_quantity_cannot_be_raised(sheet, instrument):
    """An override is a reduction; raising it would be a different decision
    from the one the engine explained."""
    with pytest.raises(OverrideRefused, match="only reduce"):
        overrides.reduce_quantity(
            sheet, quantity=99, actor="operator", reason="more", broker=StubBroker()
        )


@pytest.mark.django_db
def test_an_override_does_not_rewrite_the_ranking(sheet, instrument):
    before = dict(sheet.ranking_snapshot)

    overrides.reduce_quantity(
        sheet, quantity=1, actor="operator", reason="thin", broker=StubBroker()
    )

    sheet.refresh_from_db()
    assert sheet.ranking_snapshot == before


@pytest.mark.django_db
def test_an_override_records_actor_and_reason(sheet, instrument):
    overrides.reduce_quantity(
        sheet, quantity=1, actor="operator", reason="market looks thin", broker=StubBroker()
    )

    override = TradesheetOverride.objects.get()
    assert override.actor == "operator"
    assert override.reason == "market looks thin"
    assert override.before["quantity"] == 6


@pytest.mark.django_db
def test_a_tick_can_be_excluded(sheet):
    overrides.exclude_tick(sheet, tick_no=5, actor="operator", reason="spread is wide")

    assert overrides.is_excluded(sheet, 5) is True
    assert overrides.excluded_ticks(sheet) == {5}


@pytest.mark.django_db
def test_excluding_a_tick_does_not_reduce_the_quantity(sheet):
    """Otherwise "don't place at 11:05" silently means "buy less today"."""
    overrides.exclude_tick(sheet, tick_no=5, actor="operator", reason="wide")

    sheet.refresh_from_db()
    assert sheet.quantity == 6


@pytest.mark.django_db
def test_excluding_a_tick_twice_is_refused(sheet):
    overrides.exclude_tick(sheet, tick_no=5, actor="operator", reason="wide")

    with pytest.raises(OverrideRefused, match="already excluded"):
        overrides.exclude_tick(sheet, tick_no=5, actor="operator", reason="wide again")


@pytest.mark.django_db
def test_an_excluded_tick_places_nothing(sheet, instrument):
    from datetime import datetime, time

    from django.utils import timezone

    from orders import ladder
    from orders.models import NoPlaceReason

    overrides.exclude_tick(sheet, tick_no=0, actor="operator", reason="wide")

    [outcome] = ladder.run_round(
        tradesheet=sheet,
        quotes={"INFY": {"depth": {"buy": [{"price": 1650.0}], "sell": [{"price": 1650.5}]}}},
        broker=StubBroker(),
        moment=timezone.make_aware(
            datetime.combine(TUESDAY, time(9, 45)), timezone.get_current_timezone()
        ),
    )

    assert outcome.reason == NoPlaceReason.EXCLUDED


@pytest.mark.django_db
def test_a_day_can_be_skipped(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=2)
    broker = StubBroker()

    overrides.skip_day(sheet, actor="operator", reason="stepping back", broker=broker)

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.SKIPPED
    assert sheet.skip_reason == SkipReason.OPERATOR
    assert Order.objects.get().state == OrderState.CANCELLED


@pytest.mark.django_db
def test_a_day_with_fills_cannot_be_skipped(sheet, instrument):
    """A day with fills is PARTIAL. Calling it skipped would be a record that
    says nothing was bought on a day when something was."""
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)

    with pytest.raises(OverrideRefused, match="cannot be skipped"):
        overrides.skip_day(sheet, actor="operator", reason="too late", broker=StubBroker())


@pytest.mark.django_db
def test_stopping_a_day_with_nothing_filled_skips_it(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=2)

    overrides.stop_day(sheet, actor="operator", reason="price looks broken", broker=StubBroker())

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.SKIPPED
    assert Order.objects.get().state == OrderState.CANCELLED


@pytest.mark.django_db
def test_stopping_a_day_with_fills_halts_it_and_cancels_what_rests(sheet, instrument):
    """The fills stand; what is still resting goes, and nothing more is placed."""
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)
    an_order(sheet, instrument, tick_no=1, quantity=2)

    outcome = overrides.stop_day(sheet, actor="operator", reason="on a train", broker=StubBroker())

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.HALTED
    assert sheet.skip_reason == SkipReason.OPERATOR
    assert sheet.quantity == 6  # the plan is not rewritten; settlement calls it PARTIAL
    assert Order.objects.get(tick_no=1).state == OrderState.CANCELLED
    assert outcome.cancelled == ["tx-test-1"]
    override = TradesheetOverride.objects.get()
    assert override.kind == OverrideKind.STOP_DAY
    assert override.reason == "on a train"


@pytest.mark.django_db
def test_a_cancel_that_fails_mid_stop_undoes_neither_the_stop_nor_the_cancels_before_it(
    sheet, instrument
):
    """The first order is cancelled at the broker before the second refuses.
    Rolling the stop back with it would leave that order open here while it is
    cancelled there, and the day running -- so the next round places again."""
    from broker.exceptions import TransientBrokerError

    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)
    an_order(sheet, instrument, tick_no=1, quantity=2)
    an_order(sheet, instrument, tick_no=2, quantity=2)

    class SecondCancelFails(StubBroker):
        def cancel_order(self, order_id):
            if order_id == "kite-2":
                raise TransientBrokerError("the cancel did not confirm")
            return super().cancel_order(order_id)

    outcome = overrides.stop_day(
        sheet, actor="operator", reason="on a train", broker=SecondCancelFails()
    )

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.HALTED
    assert Order.objects.get(tick_no=1).state == OrderState.CANCELLED
    assert Order.objects.get(tick_no=2).state == OrderState.OPEN
    assert outcome.cancelled == ["tx-test-1"]
    assert outcome.unconfirmed == ["tx-test-2"]
    override = TradesheetOverride.objects.get()
    assert override.after["unconfirmed"] == ["tx-test-2"]


@pytest.mark.django_db
def test_a_cancel_that_fails_mid_skip_still_skips_the_day(sheet, instrument):
    from broker.exceptions import TransientBrokerError

    an_order(sheet, instrument, tick_no=1, quantity=2)
    an_order(sheet, instrument, tick_no=2, quantity=2)

    class SecondCancelFails(StubBroker):
        def cancel_order(self, order_id):
            if order_id == "kite-2":
                raise TransientBrokerError("the cancel did not confirm")
            return super().cancel_order(order_id)

    with pytest.raises(overrides.CancellationsIncomplete, match="tx-test-2"):
        overrides.skip_day(sheet, actor="operator", reason="r", broker=SecondCancelFails())

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.SKIPPED
    assert Order.objects.get(tick_no=1).state == OrderState.CANCELLED
    assert TradesheetOverride.objects.filter(kind=OverrideKind.SKIP_DAY).exists()


@pytest.mark.django_db
def test_stopping_waits_for_a_round_in_flight_and_changes_nothing_if_it_cannot(
    sheet, instrument, settings, tmp_path, monkeypatch
):
    """A round that has already read the sheet as running would place after a
    stop that did not wait for it -- an order resting on a stopped day. The
    stop takes the same lock the round holds."""
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
    monkeypatch.setattr(overrides, "STOP_WAIT_SECONDS", 0.2)
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)
    resting = an_order(sheet, instrument, tick_no=1, quantity=2)

    with idempotency.order_lock(), pytest.raises(idempotency.Busy):
        overrides.stop_day(sheet, actor="operator", reason="on a train", broker=StubBroker())

    sheet.refresh_from_db()
    assert sheet.state == TradeSheetState.RUNNING
    assert Order.objects.get(tick_no=1).state == resting.state
    assert not TradesheetOverride.objects.exists()


@pytest.fixture
def superseded(sheet):
    """v1, after a v2 of the same day exists: what a stale tab still shows."""
    TradeSheet.objects.create(
        trading_date=sheet.trading_date,
        version=2,
        trace_id=sheet.trace_id,
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("100"),
    )
    return sheet


@pytest.mark.django_db
@pytest.mark.parametrize(
    "lever",
    [
        lambda s: overrides.reduce_quantity(
            s, quantity=1, actor="operator", reason="r", broker=StubBroker()
        ),
        lambda s: overrides.adjust_line(
            s, symbol="INFY", quantity=1, actor="operator", reason="r", broker=StubBroker()
        ),
        lambda s: overrides.exclude_tick(s, tick_no=3, actor="operator", reason="r"),
        lambda s: overrides.skip_day(s, actor="operator", reason="r", broker=StubBroker()),
        lambda s: overrides.stop_day(s, actor="operator", reason="r", broker=StubBroker()),
    ],
    ids=["reduce", "adjust_line", "exclude_tick", "skip_day", "stop_day"],
)
def test_no_lever_acts_on_a_version_that_has_been_superseded(superseded, lever, settings, tmp_path):
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")

    with pytest.raises(OverrideRefused, match="later version"):
        lever(superseded)

    assert not TradesheetOverride.objects.exists()


@pytest.fixture
def replanned_after_a_fill(sheet, instrument):
    """v1 bought and was stopped; v2 is the day's latest and has bought nothing
    of its own. The day has bought, whichever version is asked."""
    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)
    TradeSheet.objects.filter(pk=sheet.pk).update(state=TradeSheetState.HALTED)
    return TradeSheet.objects.create(
        trading_date=sheet.trading_date,
        version=2,
        trace_id=sheet.trace_id,
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=2,
        spendable_cash=Decimal("5000"),
    )


@pytest.mark.django_db
def test_a_day_that_bought_under_an_earlier_version_cannot_be_skipped(replanned_after_a_fill):
    """D13: a skip is refused once anything has filled -- the day's fills, not
    the version's."""
    with pytest.raises(OverrideRefused, match="already filled"):
        overrides.skip_day(
            replanned_after_a_fill, actor="operator", reason="r", broker=StubBroker()
        )


@pytest.mark.django_db
def test_stopping_a_replanned_version_that_has_not_bought_halts_rather_than_skips(
    replanned_after_a_fill, settings, tmp_path
):
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")

    overrides.stop_day(replanned_after_a_fill, actor="operator", reason="r", broker=StubBroker())

    replanned_after_a_fill.refresh_from_db()
    assert replanned_after_a_fill.state == TradeSheetState.HALTED


@pytest.mark.django_db
def test_a_replanned_version_never_authorized_does_not_make_the_day_skipped(
    replanned_after_a_fill,
):
    from orders.fallback import _never_authorized

    _never_authorized(replanned_after_a_fill, broker=StubBroker(), stream="daily_buy")

    replanned_after_a_fill.refresh_from_db()
    assert replanned_after_a_fill.state != TradeSheetState.SKIPPED


@pytest.mark.django_db
def test_a_stopped_day_settles_as_partial(sheet, instrument):
    from engine.settlement import _settle_state

    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)
    overrides.stop_day(sheet, actor="operator", reason="enough", broker=StubBroker())

    sheet.refresh_from_db()
    _settle_state(sheet, filled=2)

    assert sheet.state == TradeSheetState.PARTIAL


@pytest.mark.django_db
def test_a_halted_day_places_nothing_and_says_the_operator_stopped_it(sheet, instrument):
    from orders import ladder
    from orders.models import NoPlaceReason

    an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2)
    overrides.stop_day(sheet, actor="operator", reason="enough", broker=StubBroker())
    sheet.refresh_from_db()

    [outcome] = ladder.run_round(
        tradesheet=sheet,
        quotes={"INFY": {"depth": {"buy": [{"price": 1650.0}], "sell": [{"price": 1650.5}]}}},
        broker=StubBroker(),
        moment=timezone.make_aware(
            datetime.combine(TUESDAY, time(10, 0)), timezone.get_current_timezone()
        ),
    )

    assert not outcome.placed
    assert outcome.reason == NoPlaceReason.DAY_HALTED
    assert "operator" in outcome.tick.detail.lower()


@pytest.mark.django_db
def test_a_day_that_has_ended_cannot_be_stopped(sheet):
    sheet.state = TradeSheetState.FILLED
    sheet.save()

    with pytest.raises(OverrideRefused, match="already ended"):
        overrides.stop_day(sheet, actor="operator", reason="late", broker=StubBroker())


# -- reconciliation (D8) ---------------------------------------------------


@pytest.mark.django_db
def test_an_order_is_matched_by_tag(sheet, instrument):
    order = an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.PLACED, mode="live")
    broker = StubBroker(
        book=[
            {
                "tag": order.tag,
                "order_id": "251122000123456",
                "status": "OPEN",
                "filled_quantity": 0,
            }
        ]
    )

    result = reconcile.from_broker(trading_date=TUESDAY, broker=broker)

    order.refresh_from_db()
    assert result.matched == 1
    assert order.state == OrderState.OPEN
    assert order.kite_order_id == "251122000123456"


@pytest.mark.django_db
def test_the_brokers_fill_quantity_wins(sheet, instrument):
    order = an_order(sheet, instrument, tick_no=0, quantity=4, mode="live")
    broker = StubBroker(book=[{"tag": order.tag, "status": "COMPLETE", "filled_quantity": 4}])

    reconcile.from_broker(trading_date=TUESDAY, broker=broker)

    order.refresh_from_db()
    assert order.filled_quantity == 4
    assert order.state == OrderState.COMPLETE


@pytest.mark.django_db
def test_a_partly_filled_open_order_is_partially_filled_here(sheet, instrument):
    """The status alone says OPEN and would lose the fill."""
    order = an_order(sheet, instrument, tick_no=0, quantity=4, mode="live")
    broker = StubBroker(book=[{"tag": order.tag, "status": "OPEN", "filled_quantity": 2}])

    reconcile.from_broker(trading_date=TUESDAY, broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.PARTIALLY_FILLED


@pytest.mark.django_db
def test_an_order_the_broker_never_saw_is_rejected_not_replaced(sheet, instrument):
    """The placement never landed. Nothing was bought, and nothing is placed
    again to find out. No broker id, and long enough ago that its placement
    call cannot still be on the wire."""
    from datetime import timedelta

    order = an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.PLACED, mode="live")
    Order.objects.filter(pk=order.pk).update(
        kite_order_id="", placed_at=order.placed_at - timedelta(minutes=5)
    )
    broker = StubBroker(book=[])

    result = reconcile.from_broker(trading_date=TUESDAY, broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.REJECTED
    assert order.tag in result.unknown_to_broker


@pytest.mark.django_db
def test_a_paper_order_is_not_expected_in_the_book(sheet, instrument):
    order = an_order(sheet, instrument, tick_no=0, quantity=2, state=OrderState.OPEN)

    result = reconcile.from_broker(trading_date=TUESDAY, broker=StubBroker(book=[]))

    order.refresh_from_db()
    assert order.state == OrderState.OPEN
    assert result.unknown_to_broker == []


@pytest.mark.django_db
def test_an_unrecognised_status_is_reported_not_acted_on(sheet, instrument):
    order = an_order(sheet, instrument, tick_no=0, quantity=2, mode="live")
    broker = StubBroker(book=[{"tag": order.tag, "status": "SOMETHING NEW", "filled_quantity": 0}])

    result = reconcile.from_broker(trading_date=TUESDAY, broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.OPEN
    assert result.unrecognised_status


@pytest.mark.django_db
def test_an_impossible_transition_is_reported_rather_than_forced(sheet, instrument):
    """The broker says open; we say complete. Forcing it would hide the
    disagreement that matters."""
    order = an_order(
        sheet, instrument, tick_no=0, quantity=2, state=OrderState.COMPLETE, filled=2, mode="live"
    )
    broker = StubBroker(book=[{"tag": order.tag, "status": "OPEN", "filled_quantity": 2}])

    reconcile.from_broker(trading_date=TUESDAY, broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.COMPLETE
    assert AuditEvent.objects.filter(action="orders.state_conflict").exists()


@pytest.mark.django_db
def test_reconciliation_is_audited(sheet, instrument):
    an_order(sheet, instrument, tick_no=0, quantity=2, mode="live")

    reconcile.from_broker(trading_date=TUESDAY, broker=StubBroker(book=[]))

    assert AuditEvent.objects.filter(action="orders.reconciled").exists()


# -- fills from the trade book --------------------------------------------


@pytest.mark.django_db
def test_fills_are_recorded_from_the_trade_book(sheet, instrument):
    order = an_order(sheet, instrument, tick_no=0, quantity=2, mode="live")
    broker = StubBroker(
        trades=[
            {
                "trade_id": "10000001",
                "order_id": order.kite_order_id,
                "quantity": 2,
                "average_price": 1650.25,
            }
        ]
    )

    recorded = reconcile.record_fills(trading_date=TUESDAY, broker=broker)

    assert recorded == 1
    assert Fill.objects.get().quantity == 2


@pytest.mark.django_db
def test_a_trade_already_recorded_is_not_recorded_twice(sheet, instrument):
    """The unique trade id is what stops a replayed postback consuming lots
    twice (D11)."""
    order = an_order(sheet, instrument, tick_no=0, quantity=2, mode="live")
    trade = {
        "trade_id": "10000001",
        "order_id": order.kite_order_id,
        "quantity": 2,
        "average_price": 1650.25,
    }
    broker = StubBroker(trades=[trade])

    reconcile.record_fills(trading_date=TUESDAY, broker=broker)
    recorded = reconcile.record_fills(trading_date=TUESDAY, broker=broker)

    assert recorded == 0
    assert Fill.objects.count() == 1


@pytest.mark.django_db
def test_a_trade_for_an_unknown_order_is_ignored(sheet, instrument):
    broker = StubBroker(
        trades=[{"trade_id": "1", "order_id": "not-ours", "quantity": 1, "average_price": 10}]
    )

    assert reconcile.record_fills(trading_date=TUESDAY, broker=broker) == 0


# -- the order row is committed before the broker is called ----------------


@pytest.mark.django_db
def test_placing_inside_a_transaction_is_refused(sheet, instrument):
    """The worst failure available is an order the broker accepts whose local
    row never commits: money moving with no record of it.

    The row is written first *and committed*, so the tag can be looked for
    afterwards. Nested in a caller's transaction that write is a savepoint
    rather than a commit and the guarantee quietly evaporates -- so nesting
    raises, rather than being left to every caller to remember.
    """
    from django.db import transaction

    from orders import placement

    with pytest.raises(RuntimeError, match="durable"), transaction.atomic():
        placement.place(
            tradesheet=sheet,
            instrument=instrument,
            tick_no=0,
            quantity=1,
            broker=StubBroker(),
            limit_price=Decimal("1650.00"),
        )


@pytest.mark.django_db
def test_placing_outside_a_transaction_is_allowed(sheet, instrument):
    from orders import placement

    outcome = placement.place(
        tradesheet=sheet,
        instrument=instrument,
        tick_no=0,
        quantity=1,
        broker=StubBroker(),
        limit_price=Decimal("1650.00"),
    )

    assert outcome.placed
    assert Order.objects.filter(tag=outcome.order.tag).exists()


@pytest.mark.django_db
def test_an_order_at_the_broker_we_have_no_row_for_is_reported(sheet, instrument):
    """Reconciling only the local rows cannot ask this question at all, and it
    is the worse one to get wrong."""
    an_order(sheet, instrument, tick_no=0, quantity=2, mode="live")
    broker = StubBroker(
        book=[
            {"tag": "tx-test-0", "order_id": "kite-0", "status": "OPEN", "filled_quantity": 0},
            {"tag": "txdeadbeefdead", "order_id": "kite-9", "status": "OPEN", "filled_quantity": 0},
        ]
    )

    result = reconcile.from_broker(trading_date=TUESDAY, broker=broker)

    assert result.unknown_to_us == ["txdeadbeefdead"]
    assert result.ok is False
    assert AuditEvent.objects.filter(action="orders.unknown_to_us").exists()


@pytest.mark.django_db
def test_an_order_this_system_did_not_tag_is_not_claimed_as_ours(sheet, instrument):
    """Only this system's tag prefix counts. The account belongs to a person
    who may place their own orders in it (M34)."""
    an_order(sheet, instrument, tick_no=0, quantity=2, mode="live")
    broker = StubBroker(
        book=[
            {"tag": "tx-test-0", "order_id": "kite-0", "status": "OPEN", "filled_quantity": 0},
            {"tag": "holiday-fund", "order_id": "kite-9", "status": "OPEN", "filled_quantity": 0},
        ]
    )

    result = reconcile.from_broker(trading_date=TUESDAY, broker=broker)

    assert result.unknown_to_us == []


# -- the rate breaker ------------------------------------------------------
#
# A rail against this program rather than against the market. Everything else
# in this file assumes the code is doing roughly what it means to; this one
# assumes it is not, and counts.


def place_one(sheet, instrument, *, tick_no):
    from orders import placement

    return placement.place(
        tradesheet=sheet,
        instrument=instrument,
        tick_no=tick_no,
        quantity=1,
        broker=StubBroker(),
        limit_price=Decimal("1650.00"),
    )


@pytest.mark.django_db
def test_placing_under_the_limit_is_allowed(sheet, instrument):
    from configuration.store import update as configure

    configure(changed_by="operator", max_orders_per_window=5, order_rate_window_seconds=30)

    for tick_no in range(4):
        assert place_one(sheet, instrument, tick_no=tick_no).placed

    assert Order.objects.count() == 4


@pytest.mark.django_db
def test_the_breaker_trips_at_the_limit(sheet, instrument):
    """Five in thirty seconds is a rate the schedule cannot produce."""
    from configuration.store import update as configure
    from orders.placement import RateBreakerTripped

    configure(changed_by="operator", max_orders_per_window=5, order_rate_window_seconds=30)

    for tick_no in range(5):
        place_one(sheet, instrument, tick_no=tick_no)

    with pytest.raises(RateBreakerTripped, match="the limit is 5"):
        place_one(sheet, instrument, tick_no=5)


@pytest.mark.django_db
def test_a_tripped_breaker_leaves_no_order_behind(sheet, instrument):
    """Checked before the row is reserved, so there is nothing for
    reconciliation to explain: nothing was written and nothing was sent."""
    from configuration.store import update as configure
    from orders.placement import RateBreakerTripped

    configure(changed_by="operator", max_orders_per_window=2, order_rate_window_seconds=30)
    for tick_no in range(2):
        place_one(sheet, instrument, tick_no=tick_no)

    with pytest.raises(RateBreakerTripped):
        place_one(sheet, instrument, tick_no=2)

    assert Order.objects.count() == 2


@pytest.mark.django_db
def test_the_breaker_is_audited(sheet, instrument):
    from configuration.store import update as configure
    from orders.placement import RateBreakerTripped

    configure(changed_by="operator", max_orders_per_window=1, order_rate_window_seconds=30)
    place_one(sheet, instrument, tick_no=0)

    with pytest.raises(RateBreakerTripped):
        place_one(sheet, instrument, tick_no=1)

    assert AuditEvent.objects.filter(action="orders.rate_breaker_tripped").exists()


@pytest.mark.django_db
def test_orders_outside_the_window_do_not_count(sheet, instrument):
    """It is a rate, not a total. Yesterday's orders are not this minute's."""
    from datetime import timedelta

    from django.utils import timezone

    from configuration.store import update as configure
    from orders import placement

    configure(changed_by="operator", max_orders_per_window=2, order_rate_window_seconds=30)
    for tick_no in range(3):
        an_order(sheet, instrument, tick_no=tick_no, quantity=1)

    # placed_at is auto_now_add, so the rows land now; ask about a moment far
    # enough ahead that the window has moved past them.
    later = timezone.now() + timedelta(seconds=120)

    assert placement.orders_in_window(window_seconds=30, now=later) == 0
    placement.check_rate(now=later)  # does not raise


@pytest.mark.django_db
def test_the_breaker_can_be_turned_off(sheet, instrument):
    """Zero means no limit, for an operator who has decided otherwise. It is
    configuration rather than a code change, and the change is audited."""
    from configuration.store import update as configure

    configure(changed_by="operator", max_orders_per_window=0)

    for tick_no in range(8):
        assert place_one(sheet, instrument, tick_no=tick_no).placed


QUOTES = {
    "INFY": {
        "last_price": 1650.00,
        "depth": {
            "buy": [{"price": 1650.00, "quantity": 100}],
            "sell": [{"price": 1650.50, "quantity": 100}],
        },
    }
}


def ten_oclock():
    return timezone.make_aware(
        datetime.combine(TUESDAY, time(10, 0)), timezone.get_current_timezone()
    )


@pytest.mark.django_db
def test_a_full_window_holds_the_round_instead_of_tripping(sheet, instrument):
    """Why there is no queue: the schedule already is one.

    A round with no room places nothing and loses nothing. The name's
    uncommitted quantity is untouched, so the next round has the same work in
    front of it, priced from the book as it stands then -- and whatever is
    still unplaced at 15:00 the close sweeps up.
    """
    from configuration.store import update as configure
    from orders import ladder, placement
    from orders.models import NoPlaceReason

    configure(changed_by="operator", max_orders_per_window=1, order_rate_window_seconds=30)
    sheet.authorized_at = ten_oclock().replace(hour=9)
    sheet.save(update_fields=["authorized_at"])
    place_one(sheet, instrument, tick_no=0)
    placement.Order.objects.update(state="COMPLETE", filled_quantity=1)
    before = placement.committed_quantity(sheet)

    # By noon the schedule owes more than the one share already bought.
    [outcome] = ladder.run_round(
        tradesheet=sheet, quotes=QUOTES, broker=StubBroker(), moment=ten_oclock().replace(hour=12)
    )

    assert outcome.reason == NoPlaceReason.WAITING_TURN
    assert placement.committed_quantity(sheet) == before
    assert sheet.quantity - before > 0, "the day still has shares left to buy"


@pytest.mark.django_db
def test_a_breaker_trip_inside_a_round_records_the_tick_and_stops(sheet, instrument, monkeypatch):
    """If something placed outside the round's count -- a race, a bug -- the
    breaker still trips. The row is written, and then it propagates: a rate
    this system cannot explain is not something to carry on through."""
    from configuration.store import update as configure
    from orders import ladder
    from orders.models import NoPlaceReason, Tick
    from orders.placement import RateBreakerTripped

    configure(changed_by="operator", max_orders_per_window=1, order_rate_window_seconds=30)
    place_one(sheet, instrument, tick_no=0)
    placement_state = sheet.orders.update(state="COMPLETE", filled_quantity=1)
    assert placement_state == 1
    monkeypatch.setattr(ladder, "_room", lambda config: 5)  # the round miscounts

    with pytest.raises(RateBreakerTripped):
        ladder.run_round(tradesheet=sheet, quotes=QUOTES, broker=StubBroker(), moment=ten_oclock())

    tick = Tick.objects.filter(no_place_reason=NoPlaceReason.RATE_BREAKER).first()
    assert tick is not None
    assert "the limit is 1" in tick.detail


@pytest.mark.django_db
def test_the_next_round_places_again_once_the_window_has_passed(sheet, instrument):
    """The limit is a rate, so it clears by itself: no operator action, no
    drain step, no queue."""
    import time_machine
    from django.db.models import F as models_F

    from configuration.store import update as configure
    from orders import ladder
    from orders.models import Order

    configure(changed_by="operator", max_orders_per_window=1, order_rate_window_seconds=30)
    sheet.authorized_at = ten_oclock().replace(hour=9)
    sheet.save(update_fields=["authorized_at"])

    with time_machine.travel("2026-09-22 10:00:00+05:30", tick=False) as traveller:
        [first] = ladder.run_round(
            tradesheet=sheet, quotes=QUOTES, broker=StubBroker(), moment=ten_oclock()
        )
        Order.objects.update(state="COMPLETE", filled_quantity=models_F("quantity"))

        [held] = ladder.run_round(
            tradesheet=sheet,
            quotes=QUOTES,
            broker=StubBroker(),
            moment=ten_oclock().replace(minute=30),
        )

        traveller.move_to("2026-09-22 10:31:00+05:30")
        [again] = ladder.run_round(
            tradesheet=sheet,
            quotes=QUOTES,
            broker=StubBroker(),
            moment=ten_oclock().replace(minute=31),
        )

    assert first.placed is True
    assert held.placed is False
    assert again.placed is True
