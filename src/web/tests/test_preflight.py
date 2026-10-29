"""What the server says on the way up.

Every one of these has cost somebody an hour: a database that was not up,
migrations nobody applied, a session that expired at 06:00. The point of the
severity split is that a warning must never train somebody to ignore the line
that mattered.
"""

import pytest

from web import preflight

pytestmark = pytest.mark.django_db


def test_it_reports_the_things_worth_knowing():
    labels = [check.label for check in preflight.run()]

    for expected in (
        "database",
        "migrations",
        "order mode",
        "kite session",
        "model portfolio",
        "holiday calendar",
    ):
        assert expected in labels


def test_a_missing_session_blocks_a_day():
    """The tradesheet needs a live price to size against the ask."""
    checks = preflight.run()
    session = next(c for c in checks if c.label == "kite session")

    assert session.blocking is True
    assert session.problem


def test_an_unloaded_calendar_warns_but_does_not_block():
    """Weekends are still skipped. Treating this like a dead session would
    train somebody to ignore both."""
    checks = preflight.run()
    calendar = next(c for c in checks if c.label == "holiday calendar")

    assert calendar.problem
    assert calendar.blocking is False
    assert calendar.problem in preflight.warnings(checks)
    assert calendar.problem not in preflight.blockers(checks)


def test_paper_mode_is_not_a_problem():
    mode = next(c for c in preflight.run() if c.label == "order mode")

    assert mode.ok is True
    assert "never sent" in mode.value


def test_live_without_a_registered_address_blocks(no_outbound_lookup):
    """Every order would be refused, so saying it on the way up is cheaper
    than discovering it at 09:46."""
    from configuration.models import ConfigurationVersion, OrderMode

    ConfigurationVersion.objects.create(version=1, changed_by="operator", order_mode=OrderMode.LIVE)

    mode = next(c for c in preflight.run() if c.label == "order mode")

    assert mode.blocking is True


def live_from(registered, monkeypatch, *, observed):
    from configuration.models import ConfigurationVersion, OrderMode

    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: observed)
    ConfigurationVersion.objects.create(
        version=1,
        changed_by="operator",
        order_mode=OrderMode.LIVE,
        registered_static_ip=registered,
    )
    return next(c for c in preflight.run() if c.label == "order mode")


def test_live_from_a_different_address_blocks(monkeypatch):
    """A recorded address is not a matching one. The elastic IP lost and
    reassigned (D12) refuses every order; LIVE on the way up would be a lie."""
    mode = live_from("203.0.113.7", monkeypatch, observed="198.51.100.42")

    assert mode.blocking is True
    assert "198.51.100.42" in mode.problem
    assert "LIVE —" not in mode.value


def test_live_from_an_undeterminable_address_blocks(monkeypatch):
    mode = live_from("203.0.113.7", monkeypatch, observed=None)

    assert mode.blocking is True


def test_live_from_the_registered_address_is_live(monkeypatch):
    mode = live_from("203.0.113.7", monkeypatch, observed="203.0.113.7")

    assert mode.ok is True
    assert mode.value.startswith("LIVE")


def test_nothing_below_the_database_is_guessed_at(monkeypatch):
    """Guessing would be worse than saying the database is down once."""

    def refuse():
        return preflight.Check("database", "unreachable", "down")

    monkeypatch.setattr(preflight, "_database", refuse)

    checks = preflight.run()

    assert len(checks) == 1
    assert checks[0].problem


def test_the_report_marks_blockers_and_warnings_differently():
    from web.management.commands.preflight import render

    text = render(
        [
            preflight.Check("fine", "yes"),
            preflight.Check("bad", "no", "stops it", preflight.BLOCKS),
            preflight.Check("iffy", "maybe", "worth knowing", preflight.WARNS),
        ]
    )

    assert "! bad" in text
    assert "~ iffy" in text
    assert "  fine" in text


@pytest.fixture
def no_outbound_lookup(monkeypatch):
    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: None)


# -- what to do about it -------------------------------------------------


def test_the_script_is_always_offered():
    """The other half of what somebody starting the server wants is the
    command they were about to go looking for."""
    steps = "\n".join(preflight.next_steps(preflight.run()))

    assert "./ops/local/run_day.sh" in steps
    assert "--watch" in steps
    assert "--settle" in steps


def test_a_blocker_is_offered_before_the_script():
    """Running a day before signing in would only fail."""
    steps = preflight.next_steps(preflight.run(), base_url="http://localhost:8000")

    sign_in = next(i for i, s in enumerate(steps) if "kite/login" in s)
    run_day = next(i for i, s in enumerate(steps) if "run_day.sh" in s)

    assert sign_in < run_day


def test_a_dead_database_offers_nothing_else(monkeypatch):
    """Every other suggestion would be a guess."""
    monkeypatch.setattr(
        preflight, "_database", lambda: preflight.Check("database", "unreachable", "down")
    )

    steps = preflight.next_steps(preflight.run())

    assert steps == ["docker compose up -d postgres", "manage.py migrate"]


def test_the_columns_are_measured_not_guessed():
    """A URL longer than a hardcoded pad runs into its own description."""
    steps = preflight.next_steps(
        preflight.run(), base_url="http://a-very-long-development-hostname.local:8000"
    )

    starts = {line.index("  ", line.index(" ")) for line in steps if "  " in line}
    assert all("  " in line for line in steps)
    assert len(starts) >= 1


# -- the books (I7, M4) ----------------------------------------------------


def _paper_lot_without_its_entry():
    """A paper lot written before the journal existed: no `fill` entry behind it."""
    from datetime import datetime
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from engine.models import TradeSheet
    from orders.models import DAILY_BUY_STREAM, Fill, Order, OrderMode, OrderSide, OrderType
    from orders.state import OrderState
    from portfolio.lots import from_fill
    from portfolio.models import Instrument

    day = datetime(2026, 9, 1, 10, tzinfo=ZoneInfo("Asia/Kolkata"))
    instrument = Instrument.objects.create(
        tradingsymbol="INFY", instrument_token=408065, name="Infosys"
    )
    order = Order.objects.create(
        tag="old-0",
        tradesheet=TradeSheet.objects.create(trading_date=day.date(), version=1),
        instrument=instrument,
        trading_date=day.date(),
        stream=DAILY_BUY_STREAM,
        tick_no=0,
        side=OrderSide.BUY,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=1,
        filled_quantity=1,
        limit_price=Decimal("100"),
        state=OrderState.COMPLETE,
    )
    fill = Fill.objects.create(
        order=order, kite_trade_id="paper-old", quantity=1, price=Decimal("100"), executed_at=day
    )
    return fill, from_fill(fill)


def _check(label):
    return next(c for c in preflight.run() if c.label == label)


def test_paper_lots_that_predate_the_journal_are_worth_knowing():
    _paper_lot_without_its_entry()

    check = _check("paper history")

    assert check.problem and not check.blocking
    assert "seed_demo --reset" in check.problem


def test_paper_lots_the_journal_holds_are_not_a_problem():
    from journal.trades import post_fill

    fill, _lot = _paper_lot_without_its_entry()
    post_fill(fill)

    assert _check("paper history").ok
    assert _check("investments").ok


def test_investments_that_disagree_with_the_lots_are_worth_knowing():
    _paper_lot_without_its_entry()

    check = _check("investments")

    assert check.problem and not check.blocking
    assert "INFY" in check.problem


def test_preflight_blocks_live_mode_without_an_opening_entry(monkeypatch):
    """Live with no opening entry: the balance check and funding warnings
    are silent, and every allocation overdraws a book that holds nothing."""
    live_from("203.0.113.7", monkeypatch, observed="203.0.113.7")

    check = _check("live book")

    assert check.blocking is True
    assert "open_books" in check.problem


def test_an_opened_live_book_does_not_block_live_mode(monkeypatch, live_book_open):
    live_from("203.0.113.7", monkeypatch, observed="203.0.113.7")

    assert _check("live book").ok


def test_paper_mode_does_not_need_the_live_book_opened():
    assert _check("live book").ok


# -- settlement history (Minor 4, wave H) -----------------------------------


def test_a_tradesheet_before_today_with_no_ledger_row_is_worth_knowing():
    """gap m2: the boot catch-up reaches only one trading day back, so an
    older unsettled sheet -- paper-era history, most likely -- sits silent
    until someone notices it here."""
    from datetime import date

    from engine.models import TradeSheet

    TradeSheet.objects.create(trading_date=date(2026, 9, 1), version=1)

    check = _check("settlement history")

    assert check.problem and not check.blocking
    assert "2026-09-01" in check.problem
    assert "settle_day" in check.problem


def test_a_settled_day_before_today_is_not_a_problem():
    from datetime import date

    from engine.models import CashLedger, TradeSheet

    TradeSheet.objects.create(trading_date=date(2026, 9, 1), version=1)
    CashLedger.objects.create(book="paper", trading_date=date(2026, 9, 1))

    assert _check("settlement history").ok


def test_todays_own_tradesheet_is_not_counted():
    from django.utils import timezone

    from engine.models import TradeSheet

    TradeSheet.objects.create(trading_date=timezone.localdate(), version=1)

    assert _check("settlement history").ok


def test_no_tradesheet_history_at_all_is_not_a_problem():
    assert _check("settlement history").ok


# -- the runtime role's privileges (NB1) ----------------------------------


def test_preflight_blocks_when_the_runtime_role_may_update_the_journal():
    """The suite connects as the owner, a superuser: exactly the connection
    a recreated database without its grants -- or a DATABASE_URL pointed at
    the owner to get past "permission denied" -- leaves the app on. The
    append-only tables would then take an UPDATE, and nothing else says so."""
    check = _check("runtime role")

    assert check.blocking is True
    assert "journal_journalentry" in check.problem
    assert "UPDATE" in check.problem


def test_preflight_passes_under_the_runtime_role(runtime_role):
    check = _check("runtime role")

    assert check.ok, check.problem
    assert runtime_role in check.value


def test_preflight_blocks_a_runtime_role_that_cannot_read_the_journal(runtime_role):
    """The other half of a recreated database without its default grants:
    the role may do nothing at all, and every page and timer fails."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(f"REVOKE SELECT, INSERT ON journal_journalentry FROM {runtime_role}")
        cursor.execute(f"SET ROLE {runtime_role}")

    check = _check("runtime role")

    assert check.blocking is True
    assert "SELECT" in check.problem
