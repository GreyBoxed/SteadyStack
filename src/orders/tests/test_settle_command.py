"""The evening's settle, as the command runs it.

Before anything is claimed it sweeps: every order of the day still resting,
in every stream, is cancelled -- each committed on its own, so nothing can
fill after the ledger row is written -- and every rebalance batch of the day
is closed from its fills. A cancel the broker will not confirm refuses the
settle outright: a row written while a sale might still fill would be missing
money it can never be corrected to include.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from broker.exceptions import TransientBrokerError
from engine.models import CashLedger, TradeSheet, TradeSheetState
from orders import idempotency
from orders.models import Order, OrderSide, OrderType, TaskRun
from orders.state import OrderState
from portfolio.instruments import refresh
from portfolio.models import Instrument
from rebalance.models import BatchState, RebalanceSell

pytestmark = pytest.mark.django_db

TUE = date(2026, 9, 22)


class Broker:
    def __init__(self, *, refuses=()):
        self.refuses = set(refuses)

    def cancel_order(self, order_id):
        if order_id in self.refuses:
            raise TransientBrokerError(f"{order_id} could not be cancelled")
        return order_id

    def orders(self):
        return []

    def trades(self):
        return []


@pytest.fixture
def day(settings, tmp_path, monkeypatch):
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
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
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: False)
    return TradeSheet.objects.create(
        trading_date=TUE,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("1000"),
    )


def use(monkeypatch, broker):
    monkeypatch.setattr("orders.management.commands.settle_day.get_broker", lambda: broker)


def resting(sheet, *, stream, side=OrderSide.SELL, tick=1):
    return Order.objects.create(
        tag=f"tx-rest-{stream}-{tick}",
        tradesheet=sheet,
        instrument=Instrument.objects.get(pk="INFY"),
        trading_date=TUE,
        stream=stream,
        tick_no=tick,
        side=side,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=Decimal("1650"),
        state=OrderState.OPEN,
    )


def a_running_batch(stream="rebalance-abc12345"):
    return RebalanceSell.objects.create(
        uploaded_by="operator",
        content_hash="a" * 64,
        state=BatchState.RUNNING,
        trading_date=TUE,
        stream=stream,
    )


def test_what_still_rests_in_any_stream_is_cancelled_before_the_day_settles(day, monkeypatch):
    use(monkeypatch, Broker())
    sale = resting(day, stream="rebalance-abc12345")
    buy = resting(day, stream="daily_buy", side=OrderSide.BUY, tick=2)

    call_command("settle_day", "--date", TUE.isoformat())

    assert Order.objects.get(pk=sale.pk).state == OrderState.CANCELLED
    assert Order.objects.get(pk=buy.pk).state == OrderState.CANCELLED
    assert CashLedger.objects.filter(trading_date=TUE).exists()


def test_a_running_batch_is_closed_by_the_days_settle(day, monkeypatch):
    use(monkeypatch, Broker())
    batch = a_running_batch()

    call_command("settle_day", "--date", TUE.isoformat())

    batch.refresh_from_db()
    assert batch.state == BatchState.CANCELLED
    assert batch.cancelled_reason == "day ended"


def test_an_unconfirmed_cancel_refuses_the_settle_and_claims_nothing(day, monkeypatch):
    stubborn = resting(day, stream="rebalance-abc12345")
    use(monkeypatch, Broker(refuses=[stubborn.tag]))

    with pytest.raises(CommandError, match="could not be confirmed cancelled"):
        call_command("settle_day", "--date", TUE.isoformat())

    assert not CashLedger.objects.exists()
    assert not TaskRun.objects.filter(task=idempotency.SETTLE).exists()


def test_a_live_order_the_broker_already_filled_is_reconciled_not_cancelled(day, monkeypatch):
    """A postback lost in the afternoon leaves an order OPEN here that Kite
    completed. Cancelling it would fail and refuse the settle every evening;
    the book is read first, and there is then nothing left to cancel."""
    from engine.models import CashLedger as Ledger

    order = resting(day, stream="daily_buy", side=OrderSide.BUY)
    order.mode = "live"
    order.kite_order_id = "K1"
    order.save(update_fields=["mode", "kite_order_id"])

    class Filled(Broker):
        def orders(self):
            return [
                {"tag": order.tag, "order_id": "K1", "status": "COMPLETE", "filled_quantity": 1}
            ]

        def trades(self):
            return [{"trade_id": "T1", "order_id": "K1", "quantity": 1, "average_price": 1650}]

        def holdings(self):
            return [{"tradingsymbol": "INFY", "quantity": 7, "t1_quantity": 0}]

    use(monkeypatch, Filled(refuses=["K1"]))
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    call_command("settle_day", "--date", TUE.isoformat())

    assert Order.objects.get(pk=order.pk).state == OrderState.COMPLETE
    assert Ledger.objects.filter(trading_date=TUE).exists()
    # The evening's scan judges against the broker as it is now, not as it
    # was at 09:45: a sale made at the broker during the day shows tonight.
    from portfolio.models import Holding

    assert Holding.objects.get(instrument_id="INFY").quantity == 7


def test_a_failed_settle_can_be_retried(day, monkeypatch):
    use(monkeypatch, Broker())
    TaskRun.objects.create(
        trading_date=TUE, task=idempotency.SETTLE, tick_no=0, succeeded=False, trace_id="t"
    )

    call_command("settle_day", "--date", TUE.isoformat(), "--retry")

    assert CashLedger.objects.filter(trading_date=TUE).exists()


def test_a_settle_that_died_part_way_can_be_retried(day, monkeypatch):
    """Killed mid-transaction, a settle leaves a claim that never finished."""
    use(monkeypatch, Broker())
    TaskRun.objects.create(
        trading_date=TUE, task=idempotency.SETTLE, tick_no=0, succeeded=None, trace_id="t"
    )

    call_command("settle_day", "--date", TUE.isoformat(), "--retry")

    assert CashLedger.objects.filter(trading_date=TUE).exists()


def test_settling_an_earlier_day_stamps_that_days_trace(day, monkeypatch):
    """Run on the 26th for the 25th, the sweep's records are still the 25th's."""
    from audit.models import AuditEvent

    use(monkeypatch, Broker())
    a_running_batch()

    call_command("settle_day", "--date", TUE.isoformat())

    closed = AuditEvent.objects.get(action="rebalance.closed")
    assert closed.trace_id == "ts-2026-09-22"


def test_retry_never_releases_a_day_that_settled(day, monkeypatch):
    use(monkeypatch, Broker())
    call_command("settle_day", "--date", TUE.isoformat())

    with pytest.raises(CommandError, match="already settled"):
        call_command("settle_day", "--date", TUE.isoformat(), "--retry")


# -- the evening's retries (B2) --------------------------------------------


def test_settle_if_unsettled_retries_a_failed_live_settle_and_is_idempotent(day, monkeypatch):
    """The 15:45 settle of a live day failed -- the trade book lagged the
    order book. The retry timer settles it once the trades are there, and
    every later retry is a quiet no-op."""
    order = resting(day, stream="daily_buy", side=OrderSide.BUY)
    Order.objects.filter(pk=order.pk).update(mode="live", kite_order_id="K1")

    class Lagging(Broker):
        trade_book: list = []

        def orders(self):
            return [
                {"tag": order.tag, "order_id": "K1", "status": "COMPLETE", "filled_quantity": 1}
            ]

        def trades(self):
            return self.trade_book

        def holdings(self):
            return [{"tradingsymbol": "INFY", "quantity": 1, "t1_quantity": 0}]

    broker = Lagging()
    use(monkeypatch, broker)
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    with pytest.raises(CommandError, match="trade book is incomplete"):
        call_command("settle_day", "--date", TUE.isoformat())
    assert TaskRun.objects.get(task=idempotency.SETTLE).succeeded is False

    broker.trade_book = [{"trade_id": "T1", "order_id": "K1", "quantity": 1, "average_price": 1650}]
    call_command("settle_day", "--date", TUE.isoformat(), "--if-unsettled")
    call_command("settle_day", "--date", TUE.isoformat(), "--if-unsettled")

    assert CashLedger.objects.filter(trading_date=TUE).count() == 1


def test_settle_if_unsettled_settles_a_day_the_close_never_reached(day, monkeypatch):
    use(monkeypatch, Broker())

    call_command("settle_day", "--date", TUE.isoformat(), "--if-unsettled")

    assert CashLedger.objects.filter(trading_date=TUE).exists()


def test_settle_if_unsettled_is_quiet_once_the_row_exists(day, monkeypatch, capsys):
    use(monkeypatch, Broker())
    call_command("settle_day", "--date", TUE.isoformat())

    call_command("settle_day", "--date", TUE.isoformat(), "--if-unsettled")

    assert "already settled" in capsys.readouterr().out


# -- a past live day, after Kite has forgotten it (NI1) ---------------------


def test_a_past_live_day_settles_with_an_unfilled_order_kite_no_longer_knows(day, monkeypatch):
    """Kite's order book and history hold today's orders only. A DAY order of
    a past date that is still open here has ended at the exchange; asking
    Kite to cancel it fails, and so does reading its history. Once Console's
    tradebook shows it traded nothing, --level-unseen levels it from its
    fills without a broker call, audited with who said so, and the day
    settles (I-1)."""
    from audit.models import AuditEvent

    order = resting(day, stream="daily_buy", side=OrderSide.BUY)
    Order.objects.filter(pk=order.pk).update(mode="live", kite_order_id="K1")

    class Forgotten(Broker):
        cancels = 0

        def cancel_order(self, order_id):
            Forgotten.cancels += 1
            raise TransientBrokerError("no such order")

        def order_history(self, order_id):
            raise TransientBrokerError("no such order")

        def holdings(self):
            return []

    use(monkeypatch, Forgotten())
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    call_command("settle_day", "--date", TUE.isoformat(), "--level-unseen", "--actor", "operator")

    order.refresh_from_db()
    assert order.state == OrderState.CANCELLED
    assert Forgotten.cancels == 0
    levelled = AuditEvent.objects.get(action="order.expired_unseen")
    assert levelled.actor == "operator"
    assert CashLedger.objects.filter(trading_date=TUE).exists()


def test_a_past_orders_fills_beyond_its_record_still_refuse_the_settle(day, monkeypatch):
    """The order book said a share filled that no fill records: levelling
    keeps that count, so the settle refuses and the tradebook is imported,
    rather than the row being written without the share's cost."""
    order = resting(day, stream="daily_buy", side=OrderSide.BUY)
    Order.objects.filter(pk=order.pk).update(
        mode="live", kite_order_id="K1", filled_quantity=1, state=OrderState.PARTIALLY_FILLED
    )
    order = Order.objects.get(pk=order.pk)
    Order.objects.filter(pk=order.pk).update(quantity=2)

    use(monkeypatch, Broker())
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    with pytest.raises(CommandError, match="trade book is incomplete"):
        call_command(
            "settle_day", "--date", TUE.isoformat(), "--level-unseen", "--actor", "operator"
        )

    order.refresh_from_db()
    assert order.state == OrderState.CANCELLED
    assert order.filled_quantity == 1
    assert not CashLedger.objects.exists()


# -- a past live day is not levelled without its tradebook (I-1) --------------


def _a_past_live_order(day, *, tag_tick=1, kite_order_id="K1"):
    order = resting(day, stream="daily_buy", side=OrderSide.BUY, tick=tag_tick)
    Order.objects.filter(pk=order.pk).update(mode="live", kite_order_id=kite_order_id)
    return Order.objects.get(pk=order.pk)


class NextMorning(Broker):
    """Kite the day after, with a valid session: its books hold nothing of
    Tuesday's, so an order that filled while the box was down looks exactly
    like one that never filled."""

    def holdings(self):
        return [{"tradingsymbol": "INFY", "quantity": 1, "t1_quantity": 0}]


def test_a_past_live_day_with_an_open_order_is_not_settled_without_its_tradebook(
    day, monkeypatch, tmp_path
):
    order = _a_past_live_order(day)
    use(monkeypatch, NextMorning())
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    with pytest.raises(CommandError) as refused:
        call_command("settle_day", "--date", TUE.isoformat())

    message = str(refused.value)
    assert "import" in message and "tradebook" in message
    assert "--level-unseen" in message
    order.refresh_from_db()
    assert order.state == OrderState.OPEN
    assert not CashLedger.objects.exists()
    assert not TaskRun.objects.filter(task=idempotency.SETTLE).exists()

    # The order filled while nobody watched; Console's tradebook says so,
    # and the import is still accepted.
    path = tmp_path / "tradebook.csv"
    path.write_text(
        "symbol,trade_date,exchange,trade_type,quantity,price,trade_id,order_id,"
        "order_execution_time\n"
        f"INFY,{TUE.isoformat()},NSE,buy,1,1650.00,T1,K1,{TUE.isoformat()}T14:55:00\n"
    )
    call_command("import_tradebook", str(path), "--actor", "operator")

    order.refresh_from_db()
    assert order.state == OrderState.COMPLETE
    assert order.filled_quantity == 1


def test_the_unseen_refusal_names_retry_when_a_failed_claim_exists(day, monkeypatch):
    """m-3: the 15:45 settle failed first, so its claim is held failed; the
    route the refusal names must release it in the same step."""
    _a_past_live_order(day)
    TaskRun.objects.create(
        trading_date=TUE, task=idempotency.SETTLE, tick_no=0, succeeded=False, trace_id="t"
    )
    use(monkeypatch, NextMorning())
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    with pytest.raises(CommandError) as refused:
        call_command("settle_day", "--date", TUE.isoformat())

    assert f"settle_day --date {TUE} --retry --level-unseen --actor NAME" in str(refused.value)


def test_the_unseen_refusal_omits_retry_without_a_claim(day, monkeypatch):
    _a_past_live_order(day)
    use(monkeypatch, NextMorning())
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    with pytest.raises(CommandError) as refused:
        call_command("settle_day", "--date", TUE.isoformat())

    assert f"settle_day --date {TUE} --level-unseen --actor NAME" in str(refused.value)


def test_retry_with_level_unseen_settles_a_day_whose_settle_failed(day, monkeypatch):
    """The route the refusal names works in one step."""
    order = _a_past_live_order(day)
    TaskRun.objects.create(
        trading_date=TUE, task=idempotency.SETTLE, tick_no=0, succeeded=False, trace_id="t"
    )
    use(monkeypatch, NextMorning())
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    call_command(
        "settle_day", "--date", TUE.isoformat(), "--retry", "--level-unseen", "--actor", "operator"
    )

    order.refresh_from_db()
    assert order.state == OrderState.CANCELLED
    assert CashLedger.objects.filter(trading_date=TUE).exists()


def test_a_settle_after_the_import_levels_a_second_trade_less_order_proceeds(
    day, monkeypatch, tmp_path, already_live
):
    already_live()
    TradeSheet.objects.filter(pk=day.pk).update(book="live")
    traded = _a_past_live_order(day, tag_tick=1, kite_order_id="K1")
    idle = _a_past_live_order(day, tag_tick=2, kite_order_id="K2")
    use(monkeypatch, NextMorning())
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    path = tmp_path / "tradebook.csv"
    path.write_text(
        "symbol,trade_date,exchange,trade_type,quantity,price,trade_id,order_id,"
        "order_execution_time\n"
        f"INFY,{TUE.isoformat()},NSE,buy,1,1650.00,T1,K1,{TUE.isoformat()}T14:55:00\n"
    )
    call_command("import_tradebook", str(path), "--actor", "operator")
    call_command("settle_day", "--date", TUE.isoformat())

    traded.refresh_from_db()
    idle.refresh_from_db()
    assert traded.state == OrderState.COMPLETE
    assert idle.state == OrderState.CANCELLED
    assert CashLedger.objects.get(trading_date=TUE, book="live").cash_spent == Decimal("1650.00")


def test_the_boot_catch_up_refuses_an_open_past_live_order_and_keeps_its_claim(day, monkeypatch):
    import time_machine

    order = _a_past_live_order(day)
    use(monkeypatch, NextMorning())
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)

    with time_machine.travel("2026-09-23 01:30 +05:30", tick=False):
        with pytest.raises(CommandError, match="--level-unseen"):
            call_command("settle_day", "--previous", "--if-unsettled")

    order.refresh_from_db()
    assert order.state == OrderState.OPEN
    assert not CashLedger.objects.exists()
    assert not TaskRun.objects.filter(task=idempotency.SETTLE).exists()


def test_level_unseen_names_a_day_and_who_checked_console(day, monkeypatch):
    use(monkeypatch, Broker())

    with pytest.raises(CommandError, match="--date or --previous"):
        call_command("settle_day", "--level-unseen", "--actor", "operator")
    with pytest.raises(CommandError, match="--actor"):
        call_command("settle_day", "--date", TUE.isoformat(), "--level-unseen")


def test_level_unseen_is_refused_for_today(day, monkeypatch):
    """Kite still holds today's orders: they are cancelled there, not levelled."""
    import time_machine

    use(monkeypatch, Broker())
    with time_machine.travel("2026-09-22 16:00 +05:30", tick=False):
        with pytest.raises(CommandError, match="past day"):
            call_command(
                "settle_day", "--date", TUE.isoformat(), "--level-unseen", "--actor", "operator"
            )


def test_a_day_behind_the_next_settles_its_trades_only(day, monkeypatch, capsys):
    """Wednesday's 15:45 settle wrote its row while Tuesday's recovery was
    still pending: Tuesday's settle is refused and points at --trades-only,
    which posts what its trades owe and writes no row."""
    from engine.ledger import settle_day as write_row

    use(monkeypatch, Broker())
    write_row(date(2026, 9, 23), book="paper")

    with pytest.raises(CommandError, match="--trades-only"):
        call_command("settle_day", "--date", TUE.isoformat())

    call_command("settle_day", "--date", TUE.isoformat(), "--trades-only")

    assert "trades settled" in capsys.readouterr().out
    assert not CashLedger.objects.filter(trading_date=TUE).exists()


def test_trades_only_needs_a_date(day, monkeypatch):
    use(monkeypatch, Broker())

    with pytest.raises(CommandError, match="--date"):
        call_command("settle_day", "--trades-only")


# -- a settle run before the day is planned (NI3) ----------------------------


@pytest.fixture
def unplanned(settings, tmp_path, monkeypatch):
    """Tuesday, with no tradesheet yet: what a caught-up timer or an early
    hand-run settle finds on a morning boot."""
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: False)
    use(monkeypatch, Broker())


def test_a_settle_before_the_day_is_planned_does_not_use_up_its_claim(unplanned, capsys):
    call_command("settle_day", "--date", TUE.isoformat())

    assert "No tradesheet" in capsys.readouterr().out
    assert TaskRun.objects.get(task=idempotency.SETTLE).succeeded is False

    TradeSheet.objects.create(
        trading_date=TUE,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("1000"),
    )
    call_command("settle_day", "--date", TUE.isoformat(), "--if-unsettled")

    assert CashLedger.objects.filter(trading_date=TUE).exists()


def test_settle_without_a_date_refuses_before_the_close(unplanned):
    import time_machine

    with time_machine.travel("2026-09-22 09:10 +05:30", tick=False):
        with pytest.raises(CommandError, match="before the close"):
            call_command("settle_day")

    assert not TaskRun.objects.exists()


def test_settle_without_a_date_runs_after_the_close(unplanned, capsys):
    import time_machine

    with time_machine.travel("2026-09-22 15:45 +05:30", tick=False):
        call_command("settle_day")

    assert "No tradesheet" in capsys.readouterr().out


def test_settle_previous_settles_the_last_trading_day_before_today(day, monkeypatch):
    """The boot-time catch-up: a box down at Tuesday's 15:45 that comes back
    on Wednesday morning settles Tuesday, and never Wednesday."""
    import time_machine

    use(monkeypatch, Broker())

    with time_machine.travel("2026-09-23 08:30 +05:30", tick=False):
        call_command("settle_day", "--previous", "--if-unsettled")
        call_command("settle_day", "--previous", "--if-unsettled")

    assert list(CashLedger.objects.values_list("trading_date", flat=True)) == [TUE]


def test_previous_and_date_are_not_both_given(unplanned):
    with pytest.raises(CommandError, match="--previous"):
        call_command("settle_day", "--previous", "--date", TUE.isoformat())


# -- a plain --date on a failed claim (Nm1) ------------------------------------


@pytest.mark.parametrize("succeeded", [False, None])
def test_a_date_settle_after_a_failed_one_says_retry(day, monkeypatch, succeeded):
    """The evening's settle failed, so its claim is recorded failed (or it
    died part way). A plain --date must not answer "already run" and exit 0
    with no row: it says to run with --retry."""
    use(monkeypatch, Broker())
    TaskRun.objects.create(
        trading_date=TUE, task=idempotency.SETTLE, tick_no=0, succeeded=succeeded, trace_id="t"
    )

    with pytest.raises(CommandError, match="a failed settle holds the claim; run with --retry"):
        call_command("settle_day", "--date", TUE.isoformat())

    assert not CashLedger.objects.exists()


def a_settle_running():
    """Another settle's claim, unfinished, with the order lock that run holds."""
    TaskRun.objects.create(
        trading_date=TUE, task=idempotency.SETTLE, tick_no=0, succeeded=None, trace_id="t"
    )
    return idempotency.order_lock()


def test_a_second_settle_during_a_running_one_is_busy_not_failed(day, monkeypatch):
    """m-2: the running settle holds the lock from before its claim, so a
    second one is told to wait -- not that a failed settle holds the claim."""
    use(monkeypatch, Broker())
    monkeypatch.setattr(
        "orders.management.commands.settle_day.Command._sweep", lambda *a, **k: None
    )

    with a_settle_running(), pytest.raises(CommandError) as refused:
        call_command("settle_day", "--date", TUE.isoformat())

    assert "Another order-touching task is running" in str(refused.value)
    assert "failed settle" not in str(refused.value)
    assert TaskRun.objects.get().succeeded is None


def test_an_unfinished_claim_whose_lock_is_held_names_the_lock_not_a_running_settle(
    day, monkeypatch
):
    """Minor 2 (wave H): the claim was lost to whoever holds the lock right
    now -- a round, a stop -- not necessarily a settle still running, so the
    message says the lock is held rather than implying a settle is."""
    from contextlib import contextmanager

    use(monkeypatch, Broker())
    monkeypatch.setattr(
        "orders.management.commands.settle_day.Command._sweep", lambda *a, **k: None
    )

    @contextmanager
    def lost(**_):
        raise idempotency.AlreadyRan("settle has already run")
        yield

    monkeypatch.setattr(idempotency, "once", lost)

    with a_settle_running(), pytest.raises(CommandError) as refused:
        call_command("settle_day", "--date", TUE.isoformat())

    assert "the order lock is held by another task; try again shortly" in str(refused.value)
    assert "may still be running" not in str(refused.value)
    assert "failed settle" not in str(refused.value)


def test_a_date_settle_after_a_settled_one_is_still_a_quiet_no_op(day, monkeypatch, capsys):
    use(monkeypatch, Broker())
    call_command("settle_day", "--date", TUE.isoformat())

    call_command("settle_day", "--date", TUE.isoformat())

    assert "already run" in capsys.readouterr().out


# -- the boot catch-up's reach (m-1, Nm4) -------------------------------------


def test_previous_after_todays_close_settles_today(day, monkeypatch):
    """A box back after the 22:30 retry, with today unsettled, settles today:
    the latest trading day whose close has passed."""
    import time_machine

    use(monkeypatch, Broker())

    with time_machine.travel("2026-09-22 23:10 +05:30", tick=False):
        call_command("settle_day", "--previous", "--if-unsettled")

    assert list(CashLedger.objects.values_list("trading_date", flat=True)) == [TUE]


def test_previous_before_todays_close_settles_the_day_before(unplanned, capsys):
    import time_machine

    with time_machine.travel("2026-09-23 14:00 +05:30", tick=False):
        call_command("settle_day", "--previous", "--if-unsettled")

    assert "2026-09-22" in capsys.readouterr().out


def test_the_catch_up_exits_quietly_without_claiming_a_day_with_no_sheet(unplanned, capsys):
    """The box was down all of Tuesday, or the database was just recreated:
    no sheet, nothing to settle, and no claim or failure recorded for a
    day already alarmed at 09:45."""
    import time_machine

    with time_machine.travel("2026-09-23 08:30 +05:30", tick=False):
        call_command("settle_day", "--previous", "--if-unsettled")

    assert "no tradesheet" in capsys.readouterr().out
    assert not TaskRun.objects.exists()


def test_the_catch_up_after_the_next_close_settles_the_unsettled_day_before_an_unplanned_one(
    day, monkeypatch, capsys
):
    """gap m2 (P3): down from Tuesday's close until after Wednesday's, and
    Wednesday was never planned. The latest closed day has no sheet, so the
    catch-up falls back once to Tuesday, which has a sheet and no row."""
    import time_machine

    use(monkeypatch, Broker())

    with time_machine.travel("2026-09-23 16:00 +05:30", tick=False):
        call_command("settle_day", "--previous", "--if-unsettled")

    assert list(CashLedger.objects.values_list("trading_date", flat=True)) == [TUE]
    assert "2026-09-23 has no tradesheet" in capsys.readouterr().out


def test_the_catch_up_names_an_older_unsettled_day_beyond_its_reach(unplanned):
    """Monday is planned and unsettled; Tuesday and Wednesday were never
    planned. One day back is as far as the catch-up reaches: it names the
    command for Monday and fails, so the unit alarms."""
    import time_machine

    TradeSheet.objects.create(
        trading_date=date(2026, 9, 21),
        version=1,
        trace_id="ts-2026-09-21",
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("1000"),
    )

    with time_machine.travel("2026-09-23 16:00 +05:30", tick=False):
        with pytest.raises(CommandError, match="settle_day --date 2026-09-21 --retry"):
            call_command("settle_day", "--previous", "--if-unsettled")

    assert not CashLedger.objects.exists()
    assert not TaskRun.objects.exists()


def test_the_catch_up_does_not_fall_back_to_a_settled_day(day, monkeypatch, capsys):
    import time_machine

    use(monkeypatch, Broker())
    call_command("settle_day", "--date", TUE.isoformat())

    with time_machine.travel("2026-09-23 16:00 +05:30", tick=False):
        call_command("settle_day", "--previous", "--if-unsettled")

    assert "2026-09-23 has no tradesheet; nothing to settle" in capsys.readouterr().out
    assert CashLedger.objects.count() == 1


def test_the_catch_up_waits_for_the_order_lock_rather_than_giving_up(day, monkeypatch):
    """At boot a round may hold the lock for a moment; the catch-up is not
    retried, so it waits its turn (bounded) instead of raising Busy."""
    import threading

    import time_machine

    use(monkeypatch, Broker())
    TaskRun.objects.create(
        trading_date=TUE, task=idempotency.SETTLE, tick_no=0, succeeded=False, trace_id="t"
    )
    held = threading.Event()
    release = threading.Event()

    def hold():
        with idempotency.order_lock():
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    threading.Timer(0.5, release.set).start()
    try:
        with time_machine.travel("2026-09-23 08:30 +05:30", tick=True):
            call_command("settle_day", "--previous", "--if-unsettled")
    finally:
        release.set()
        holder.join()

    assert CashLedger.objects.filter(trading_date=TUE).exists()


# -- a fresh database's first evenings (Nm3) ----------------------------------


def test_nothing_to_settle_on_a_fresh_database_is_not_an_error(unplanned, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="steadystack.orders"):
        call_command("settle_day", "--date", TUE.isoformat())

    failed = [r for r in caplog.records if r.getMessage() == "scheduled task failed"]
    assert failed and all(r.levelno == logging.INFO for r in failed)


def test_nothing_to_settle_after_earlier_days_is_still_an_error(unplanned, caplog):
    import logging

    TradeSheet.objects.create(
        trading_date=date(2026, 9, 21),
        version=1,
        trace_id="ts-2026-09-21",
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("1000"),
    )
    with caplog.at_level(logging.INFO, logger="steadystack.orders"):
        call_command("settle_day", "--date", TUE.isoformat())

    failed = [r for r in caplog.records if r.getMessage() == "scheduled task failed"]
    assert failed and all(r.levelno == logging.ERROR for r in failed)
