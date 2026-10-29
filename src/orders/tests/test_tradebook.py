"""Importing a past live day's fills from Zerodha Console's tradebook (B2).

Kite's trade book holds today's trades only. A live day that did not settle
before midnight can take its fills from nowhere else. The fixtures are
synthetic: the column set is Console's documented tradebook export, and the
first real file is to be checked against it.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from engine.models import CashLedger, TradeSheet, TradeSheetState
from journal.models import Book, EntryKind, JournalEntry
from orders.models import DAILY_BUY_STREAM, Fill, Order, OrderSide, OrderType
from orders.state import OrderState
from portfolio.instruments import refresh
from portfolio.models import Instrument, Lot

pytestmark = pytest.mark.django_db

TUE = date(2026, 9, 22)

HEADER = (
    "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,"
    "trade_id,order_id,order_execution_time\n"
)


def row(trade_id, order_id, quantity, price, *, day=TUE, symbol="INFY", side="buy"):
    return (
        f"{symbol},INE009A01021,{day.isoformat()},NSE,EQ,EQ,{side},false,{quantity},{price},"
        f"{trade_id},{order_id},{day.isoformat()}T10:15:03\n"
    )


@pytest.fixture
def tradebook(tmp_path):
    def write(*rows, header=HEADER):
        path = tmp_path / "tradebook.csv"
        path.write_text(header + "".join(rows))
        return str(path)

    return write


@pytest.fixture
def live_order(db, settings, tmp_path):
    """A live buy of 3 INFY whose postbacks and trade book the day never saw."""
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
    sheet = TradeSheet.objects.create(
        trading_date=TUE,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("6000"),
        book="live",  # a live day, planned in the live book
    )
    return Order.objects.create(
        tag="tx-live-1",
        tradesheet=sheet,
        instrument=Instrument.objects.get(pk="INFY"),
        trading_date=TUE,
        stream=DAILY_BUY_STREAM,
        tick_no=1,
        side=OrderSide.BUY,
        mode="live",
        order_type=OrderType.LIMIT,
        quantity=3,
        limit_price=Decimal("1650"),
        state=OrderState.OPEN,
        kite_order_id="250922000111",
    )


def test_import_tradebook_records_a_past_live_day_then_settle_writes_its_row(
    live_order, tradebook, monkeypatch, already_live
):
    already_live()
    path = tradebook(
        row("T1", "250922000111", 2, "1649.95"),
        row("T2", "250922000111", 1, "1650.00"),
    )

    call_command("import_tradebook", path, "--actor", "operator")

    fills = Fill.objects.filter(order=live_order).order_by("kite_trade_id")
    assert [(f.kite_trade_id, f.quantity, f.price) for f in fills] == [
        ("T1", 2, Decimal("1649.95")),
        ("T2", 1, Decimal("1650.00")),
    ]
    assert Lot.objects.filter(created_from_fill__in=fills).count() == 2
    assert (
        JournalEntry.objects.filter(
            book=Book.LIVE, kind=EntryKind.BUY_FILL, occurred_on=TUE
        ).count()
        == 2
    )
    live_order.refresh_from_db()
    assert live_order.filled_quantity == 3
    assert live_order.state == OrderState.COMPLETE

    class NextMorning:
        """Kite the day after: its books hold nothing of Tuesday's."""

        def orders(self):
            return []

        def trades(self):
            return []

        def holdings(self):
            return [{"tradingsymbol": "INFY", "quantity": 3, "t1_quantity": 0}]

    monkeypatch.setattr("orders.management.commands.settle_day.get_broker", lambda: NextMorning())
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: True)
    call_command("settle_day", "--date", TUE.isoformat())

    row_ = CashLedger.objects.get(trading_date=TUE, book="live")
    assert row_.cash_spent == Decimal("4949.90")


def test_import_tradebook_skips_trade_ids_already_recorded(live_order, tradebook):
    first = tradebook(row("T1", "250922000111", 2, "1649.95"))
    call_command("import_tradebook", first, "--actor", "operator")

    both = tradebook(
        row("T1", "250922000111", 2, "1649.95"),
        row("T2", "250922000111", 1, "1650.00"),
    )
    call_command("import_tradebook", both, "--actor", "operator")

    assert sorted(Fill.objects.values_list("kite_trade_id", flat=True)) == ["T1", "T2"]
    assert Lot.objects.count() == 2
    live_order.refresh_from_db()
    assert live_order.filled_quantity == 3


def test_import_tradebook_refuses_an_order_id_that_is_not_ours(live_order, tradebook):
    path = tradebook(
        row("T1", "250922000111", 2, "1649.95"),
        row("T9", "250922999999", 5, "300.00", symbol="ITC"),
    )

    with pytest.raises(CommandError, match="250922999999"):
        call_command("import_tradebook", path, "--actor", "operator")

    assert not Fill.objects.exists()


def test_import_tradebook_refuses_unknown_or_missing_columns(live_order, tradebook):
    unknown = tradebook(
        row("T1", "250922000111", 2, "1649.95"),
        header=HEADER.replace("order_execution_time", "executed"),
    )

    with pytest.raises(CommandError, match="order_execution_time") as refused:
        call_command("import_tradebook", unknown, "--actor", "operator")

    assert "executed" in str(refused.value)
    assert not Fill.objects.exists()


def test_import_tradebook_refuses_a_trade_that_disagrees_with_its_order(live_order, tradebook):
    path = tradebook(row("T1", "250922000111", 2, "1649.95", side="sell"))

    with pytest.raises(CommandError, match="sell"):
        call_command("import_tradebook", path, "--actor", "operator")


def test_import_tradebook_refuses_more_than_the_order_asked_for(live_order, tradebook):
    path = tradebook(row("T1", "250922000111", 4, "1649.95"))

    with pytest.raises(CommandError, match="more than"):
        call_command("import_tradebook", path, "--actor", "operator")


def test_import_tradebook_refuses_a_day_that_has_settled(live_order, tradebook):
    CashLedger.objects.create(book="live", trading_date=TUE)
    path = tradebook(row("T1", "250922000111", 2, "1649.95"))

    with pytest.raises(CommandError, match="settled"):
        call_command("import_tradebook", path, "--actor", "operator")


def test_import_tradebook_refuses_today(live_order, tradebook):
    """Today's trades are still in Kite's own trade book, which the rounds
    and the settle read."""
    from audit.context import today_ist

    Order.objects.filter(pk=live_order.pk).update(trading_date=today_ist())
    path = tradebook(row("T1", "250922000111", 2, "1649.95", day=today_ist()))

    with pytest.raises(CommandError, match="today"):
        call_command("import_tradebook", path, "--actor", "operator")


def test_a_dry_run_writes_nothing(live_order, tradebook, capsys):
    path = tradebook(row("T1", "250922000111", 2, "1649.95"))

    call_command("import_tradebook", path, "--actor", "operator", "--dry-run")

    assert not Fill.objects.exists()
    assert "T1" in capsys.readouterr().out


def test_an_old_day_is_not_confused_with_yesterday(live_order, tradebook):
    """The trade date must be the order's own."""
    path = tradebook(row("T1", "250922000111", 2, "1649.95", day=TUE - timedelta(days=1)))

    with pytest.raises(CommandError, match="2026-09-21"):
        call_command("import_tradebook", path, "--actor", "operator")


# -- what a real Console file may carry (NI1) --------------------------------


def test_import_matches_the_exchange_order_id(live_order, tradebook):
    """Console's `order_id` is probably the exchange's order number, not
    Kite's; either names the order."""
    Order.objects.filter(pk=live_order.pk).update(exchange_order_id="1100000012345678")
    path = tradebook(row("T1", "1100000012345678", 3, "1649.95"))

    call_command("import_tradebook", path, "--actor", "operator")

    fill = Fill.objects.get()
    assert fill.order_id == live_order.pk
    live_order.refresh_from_db()
    assert live_order.state == OrderState.COMPLETE


def test_import_reads_a_decimal_quantity_and_an_empty_expiry_date(live_order, tradebook):
    header = HEADER.replace("order_execution_time\n", "order_execution_time,expiry_date\n")
    line = row("T1", "250922000111", "2.000000", "1649.95").replace("\n", ",\n")
    path = tradebook(line, header=header)

    call_command("import_tradebook", path, "--actor", "operator")

    assert Fill.objects.get().quantity == 2


def test_import_refuses_a_fractional_quantity(live_order, tradebook):
    path = tradebook(row("T1", "250922000111", "2.5", "1649.95"))

    with pytest.raises(CommandError, match="whole"):
        call_command("import_tradebook", path, "--actor", "operator")


def test_import_refuses_a_known_empty_column_that_is_not_empty(live_order, tradebook):
    """expiry_date is empty for equity; a value there is not a trade this
    command knows how to read."""
    header = HEADER.replace("order_execution_time\n", "order_execution_time,expiry_date\n")
    line = row("T1", "250922000111", 2, "1649.95").replace("\n", ",2026-09-24\n")
    path = tradebook(line, header=header)

    with pytest.raises(CommandError, match="expiry_date"):
        call_command("import_tradebook", path, "--actor", "operator")


# -- the rest of the day, its trace and its lock (NI1, m-2) --------------------


def a_second_order(live_order, *, tag="tx-live-2", quantity=2):
    """Another live order of the same day that Console's file never names:
    it rested until the close and nothing filled."""
    return Order.objects.create(
        tag=tag,
        tradesheet=live_order.tradesheet,
        instrument=live_order.instrument,
        trading_date=TUE,
        stream=DAILY_BUY_STREAM,
        tick_no=2,
        side=OrderSide.BUY,
        mode="live",
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=Decimal("1650"),
        state=OrderState.OPEN,
        kite_order_id="250922000222",
    )


def test_import_levels_every_open_live_order_of_the_imported_day(live_order, tradebook):
    """A DAY order still open here after its day ended at the exchange has
    ended there too. Kite no longer holds it, so the import levels it rather
    than leaving settle_day to ask Kite to cancel an order it cannot find."""
    from audit.models import AuditEvent

    other = a_second_order(live_order)
    path = tradebook(row("T1", "250922000111", 3, "1649.95"))

    call_command("import_tradebook", path, "--actor", "operator")

    other.refresh_from_db()
    assert other.state == OrderState.CANCELLED
    assert other.filled_quantity == 0
    assert AuditEvent.objects.filter(
        action="order.expired_unseen", entity_id=str(other.pk)
    ).exists()


def test_import_stamps_the_imported_days_trace(live_order, tradebook):
    from audit.models import AuditEvent

    path = tradebook(row("T1", "250922000111", 3, "1649.95"))

    call_command("import_tradebook", path, "--actor", "operator")

    assert AuditEvent.objects.get(action="orders.tradebook_imported").trace_id == "ts-2026-09-22"
    entry = JournalEntry.objects.get(kind=EntryKind.BUY_FILL)
    assert entry.trace_id == "ts-2026-09-22"


def test_import_waits_its_turn_behind_an_order_touching_task(live_order, tradebook):
    """A settle_day --date of the same day reading the fills half-written
    would settle on a guess."""
    from orders import idempotency

    path = tradebook(row("T1", "250922000111", 3, "1649.95"))

    with idempotency.order_lock(), pytest.raises(CommandError, match="Another order-touching"):
        call_command("import_tradebook", path, "--actor", "operator")

    assert not Fill.objects.exists()


def test_import_refuses_a_trade_id_another_order_already_holds(live_order, tradebook):
    """Nm2: the same trade id on a different order is not "already recorded"."""
    other = a_second_order(live_order)
    first = tradebook(row("T1", "250922000222", 2, "1649.95"))
    call_command("import_tradebook", first, "--actor", "operator")

    again = tradebook(row("T1", "250922000111", 2, "1649.95"))
    with pytest.raises(CommandError, match="another order"):
        call_command("import_tradebook", again, "--actor", "operator")

    assert Fill.objects.get().order_id == other.pk


# -- an order the box never saw in the order book (I-1) ----------------------


def test_a_trade_naming_no_known_order_goes_to_the_days_single_order_of_its_kind(
    live_order, tradebook, capsys
):
    """The box was down before the order book gave the order its exchange
    number, and Console names the order by that number. The day's only live
    INFY buy is the one it can be, and the import says it matched it so."""
    path = tradebook(row("T1", "1100000012345678", 3, "1649.95"))

    call_command("import_tradebook", path, "--actor", "operator", "--dry-run")
    dry = capsys.readouterr().out
    assert not Fill.objects.exists()
    assert "1100000012345678" in dry and "tx-live-1" in dry and "symbol and side" in dry

    call_command("import_tradebook", path, "--actor", "operator")
    out = capsys.readouterr().out
    assert "tx-live-1" in out and "symbol and side" in out
    fill = Fill.objects.get()
    assert fill.order_id == live_order.pk
    live_order.refresh_from_db()
    assert live_order.state == OrderState.COMPLETE


def test_a_trade_naming_no_known_order_is_refused_when_two_orders_could_take_it(
    live_order, tradebook
):
    """Both orders are open and neither has an exchange number: either could
    be the one, so nothing is guessed. The refusal says how to name it --
    not that the trade was made at the broker."""
    a_second_order(live_order)
    path = tradebook(row("T1", "1100000012345678", 2, "1649.95"))

    with pytest.raises(CommandError, match="1100000012345678") as refused:
        call_command("import_tradebook", path, "--actor", "operator")

    message = str(refused.value)
    assert "exchange_order_id" in message and "import again" in message
    assert "tx-live-1" in message and "tx-live-2" in message
    assert "external transaction" not in message
    assert not Fill.objects.exists()
    # Minor 3 (wave H): named by the command that records it, filled in with
    # the trade's own id, rather than a bare pointer at the field.
    assert "record_exchange_order_id --order TAG --exchange-id 1100000012345678" in message
    assert "--actor NAME" in message


def test_a_trade_naming_no_known_order_goes_to_the_one_unseen_order_despite_cancelled_siblings(
    live_order, tradebook, capsys
):
    """gap m1 (P1): the ladder placed the name's earlier orders, each read in
    the order book and so carrying its exchange number, and cancelled them at
    their TTL. The box died with the last one resting and never read: it is
    the only order the trade can belong to."""
    earlier = a_second_order(live_order, tag="tx-live-0")
    Order.objects.filter(pk=earlier.pk).update(
        state=OrderState.CANCELLED, exchange_order_id="1100000011111111"
    )
    path = tradebook(row("T1", "1100000012345678", 3, "1649.95"))

    call_command("import_tradebook", path, "--actor", "operator")

    assert "tx-live-1" in capsys.readouterr().out
    assert Fill.objects.get().order_id == live_order.pk
    live_order.refresh_from_db()
    assert live_order.state == OrderState.COMPLETE


def test_a_rejected_order_never_takes_a_trade_it_did_not_name(live_order, tradebook):
    """gap m1 (P2): a REJECTED order has no exchange number, because the
    exchange never took it. A Console row naming some other order -- a buy
    made at the broker -- is not attached to it."""
    Order.objects.filter(pk=live_order.pk).update(state=OrderState.REJECTED)
    path = tradebook(row("T1", "1100000012345678", 2, "1649.95"))

    with pytest.raises(CommandError, match="not one of this system's live orders"):
        call_command("import_tradebook", path, "--actor", "operator")

    assert not Fill.objects.exists()
    live_order.refresh_from_db()
    assert live_order.state == OrderState.REJECTED
    assert live_order.filled_quantity == 0


def test_a_trade_naming_no_known_order_is_refused_when_the_order_has_another_number(
    live_order, tradebook
):
    """The one candidate already carries a different exchange number: the
    trade is some other order's -- one made at the broker -- not this one's."""
    Order.objects.filter(pk=live_order.pk).update(exchange_order_id="1100000099999999")
    path = tradebook(row("T1", "1100000012345678", 2, "1649.95"))

    with pytest.raises(CommandError, match="not one of this system's live orders"):
        call_command("import_tradebook", path, "--actor", "operator")


def test_the_import_hint_names_retry(live_order, tradebook, capsys):
    """Nm1: the usual reason for an import is a settle that failed, and a
    failed settle holds the claim."""
    path = tradebook(row("T1", "250922000111", 3, "1649.95"))

    call_command("import_tradebook", path, "--actor", "operator")

    assert "--retry" in capsys.readouterr().out
