"""Reversing the move to a journal-backed cash ledger restores what it replaced."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = ("engine", "0010_cashledger_proceeds")


def _has_append_only_trigger() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_trigger WHERE tgname = 'engine_cashledger_append_only' "
            "AND NOT tgisinternal"
        )
        return cursor.fetchone() is not None


@pytest.mark.django_db(transaction=True)
def test_reversing_0011_puts_the_old_ledgers_append_only_trigger_back():
    """M8: the old CashLedger is recreated from state; its trigger must be too."""
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([BEFORE])
        assert _has_append_only_trigger()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
    assert _has_append_only_trigger()


@pytest.mark.django_db(transaction=True)
def test_0011_refuses_to_drop_the_old_ledger_once_a_live_order_exists():
    """M-8: the old ledger is paper history, which 0011 drops. A database that
    ever placed a live order holds a real one, and 0011 must not eat it."""
    from datetime import date
    from decimal import Decimal

    from orders.models import Order, OrderType
    from portfolio.models import Instrument

    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    executor.migrate([BEFORE])
    # The sheet as the table stands at 0010: no book column yet (0012).
    TradeSheet = executor.loader.project_state([BEFORE]).apps.get_model("engine", "TradeSheet")
    try:
        day = date(2026, 9, 22)
        order = Order.objects.create(
            tag="tx-live-0",
            tradesheet_id=TradeSheet.objects.create(trading_date=day, version=1).pk,
            instrument=Instrument.objects.create(
                tradingsymbol="INFY", instrument_token=408065, name="Infosys"
            ),
            trading_date=day,
            tick_no=0,
            mode="live",
            order_type=OrderType.LIMIT,
            quantity=1,
            limit_price=Decimal("1650"),
        )

        with pytest.raises(RuntimeError, match="live order"):
            MigrationExecutor(connection).migrate(leaves)

        order.delete()
    finally:
        MigrationExecutor(connection).migrate(leaves)
    assert _has_append_only_trigger()


@pytest.mark.django_db(transaction=True)
def test_0012_files_each_sheet_under_its_orders_book_and_reverses():
    """M1: a sheet already written takes its orders' mode, and one with no
    order the mode in force; taken back, the column goes."""
    from datetime import date
    from decimal import Decimal

    from configuration.models import ConfigurationVersion

    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("engine", "0011_cash_ledger_from_journal")])
        old = (
            MigrationExecutor(connection)
            .loader.project_state(
                [("engine", "0011_cash_ledger_from_journal"), ("orders", "0007_feed_heartbeat")]
            )
            .apps
        )
        sheets = old.get_model("engine", "TradeSheet")
        placed = sheets.objects.create(trading_date=date(2026, 9, 21), version=1)
        unplaced = sheets.objects.create(trading_date=date(2026, 9, 22), version=1)
        instrument = old.get_model("portfolio", "Instrument").objects.create(
            tradingsymbol="INFY", instrument_token=408065, name="Infosys"
        )
        old.get_model("orders", "Order").objects.create(
            tag="tx-0012",
            tradesheet_id=placed.pk,
            instrument_id=instrument.pk,
            trading_date=date(2026, 9, 21),
            tick_no=0,
            mode="paper",
            order_type="LIMIT",
            quantity=1,
            limit_price=Decimal("100"),
        )
        ConfigurationVersion.objects.create(version=1, changed_by="operator", order_mode="live")

        executor = MigrationExecutor(connection)
        executor.migrate([("engine", "0012_tradesheet_book")])
        apps = executor.loader.project_state([("engine", "0012_tradesheet_book")]).apps
        books = dict(apps.get_model("engine", "TradeSheet").objects.values_list("pk", "book"))
        assert books == {placed.pk: "paper", unplaced.pk: "live"}

        MigrationExecutor(connection).migrate([("engine", "0011_cash_ledger_from_journal")])
        with connection.cursor() as cursor:
            columns = [
                c.name
                for c in connection.introspection.get_table_description(cursor, "engine_tradesheet")
            ]
        assert "book" not in columns
    finally:
        MigrationExecutor(connection).migrate(leaves)
