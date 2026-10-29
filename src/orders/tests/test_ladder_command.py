"""The ladder command's clock guard.

Before the tradesheet time the clock says tick 0. A tick fired then -- a run
at one in the morning -- used to claim tick 0 for the whole day, so the real
09:45 tick found it already taken and never fired.
"""

from datetime import date, datetime, time
from decimal import Decimal

import pytest
import time_machine
from django.core.management import call_command
from django.utils import timezone

pytestmark = pytest.mark.django_db

DAY = date(2026, 9, 23)  # a Wednesday


def test_a_tick_before_the_tradesheet_time_uses_nothing_up(monkeypatch):
    from engine.models import TradeSheet, TradeSheetState
    from orders.models import TaskRun, Tick

    TradeSheet.objects.create(
        trading_date=DAY,
        version=1,
        trace_id="ts-2026-09-23",
        state=TradeSheetState.DRAFT,
        spendable_cash=Decimal("2600"),
    )
    monkeypatch.setattr("orders.management.commands.ladder_tick.session.attach", lambda b: True)
    monkeypatch.setattr("orders.management.commands.ladder_tick.get_broker", lambda: object())

    one_am = timezone.make_aware(datetime.combine(DAY, time(1, 24)))
    with time_machine.travel(one_am, tick=False):
        call_command("ladder_tick", "--date", DAY.isoformat())

    assert TaskRun.objects.count() == 0
    assert Tick.objects.count() == 0


def test_the_card_counts_only_the_current_versions_ticks():
    from engine.models import TradeSheet, TradeSheetState
    from orders.models import NoPlaceReason, Tick
    from web.deck import health

    old = TradeSheet.objects.create(
        trading_date=DAY, version=1, trace_id="t", state=TradeSheetState.SKIPPED
    )
    TradeSheet.objects.create(
        trading_date=DAY, version=2, trace_id="t", state=TradeSheetState.DRAFT
    )
    Tick.objects.create(
        tradesheet=old, trading_date=DAY, tick_no=0, no_place_reason=NoPlaceReason.DAY_SKIPPED
    )

    sheet_card, ticks_card = health(DAY)[:2]

    assert sheet_card.value == "Draft"
    assert sheet_card.tone == ""
    assert "not authorized" in sheet_card.sub
    assert ticks_card.value == "0"
    assert "1 under an earlier version" in ticks_card.sub


# -- the version acted on is the one that is latest under the lock ----------


def a_later_version_lands_as_the_lock_is_taken(monkeypatch):
    """Replace `idempotency.once` with one that writes v2 on entry: the
    interleaving where a replan or a stop lands between the command reading
    the sheet and the command getting the lock."""
    from contextlib import contextmanager

    from engine.models import TradeSheet, TradeSheetState
    from orders import idempotency

    real_once = idempotency.once

    @contextmanager
    def once(**kwargs):
        with real_once(**kwargs) as run:
            TradeSheet.objects.create(
                trading_date=DAY,
                version=2,
                trace_id="ts-2026-09-23",
                state=TradeSheetState.RUNNING,
                spendable_cash=Decimal("100"),
            )
            yield run

    monkeypatch.setattr(idempotency, "once", once)


def running_sheet():
    from engine.models import TradeSheet, TradeSheetState

    return TradeSheet.objects.create(
        trading_date=DAY,
        version=1,
        trace_id="ts-2026-09-23",
        state=TradeSheetState.RUNNING,
        spendable_cash=Decimal("2600"),
    )


def test_a_round_acts_on_the_version_that_is_latest_once_it_holds_the_lock(monkeypatch):
    running_sheet()
    seen = []
    monkeypatch.setattr("orders.management.commands.ladder_tick.session.attach", lambda b: True)
    monkeypatch.setattr("orders.management.commands.ladder_tick.get_broker", lambda: object())
    monkeypatch.setattr(
        "orders.management.commands.ladder_tick.Command._quotes_for", lambda self, lines, b: {}
    )
    monkeypatch.setattr(
        "orders.ladder.run_round", lambda *, tradesheet, **_: seen.append(tradesheet.version) or []
    )
    a_later_version_lands_as_the_lock_is_taken(monkeypatch)

    with time_machine.travel(timezone.make_aware(datetime.combine(DAY, time(10, 30))), tick=False):
        call_command("ladder_tick", "--date", DAY.isoformat())

    assert seen == [2]


def test_the_close_acts_on_the_version_that_is_latest_once_it_holds_the_lock(monkeypatch):
    from engine.models import TradeSheet
    from orders.fallback import FallbackOutcome

    sheet = running_sheet()
    TradeSheet.objects.filter(pk=sheet.pk).update(instrument=infy())
    seen = []
    monkeypatch.setattr("orders.management.commands.market_fallback.session.attach", lambda b: True)
    monkeypatch.setattr("orders.management.commands.market_fallback.get_broker", lambda: object())
    monkeypatch.setattr(
        "orders.management.commands.market_fallback.Command._last_prices", lambda self, s, b: {}
    )

    def run(*, tradesheet, **_):
        seen.append(tradesheet.version)
        return FallbackOutcome(cancelled=0, placed=False)

    monkeypatch.setattr("orders.fallback.run", run)
    a_later_version_lands_as_the_lock_is_taken(monkeypatch)

    with time_machine.travel(timezone.make_aware(datetime.combine(DAY, time(15, 5))), tick=False):
        call_command("market_fallback", "--date", DAY.isoformat())

    assert seen == [2]


def test_the_close_says_it_is_not_its_turn_when_the_lock_is_held(monkeypatch, capsys):
    """Busy is "not my turn", as the ladder treats it -- not a traceback."""
    from contextlib import contextmanager

    from engine.models import TradeSheet
    from orders import idempotency

    sheet = running_sheet()
    TradeSheet.objects.filter(pk=sheet.pk).update(instrument=infy())
    monkeypatch.setattr("orders.management.commands.market_fallback.session.attach", lambda b: True)
    monkeypatch.setattr("orders.management.commands.market_fallback.get_broker", lambda: object())

    @contextmanager
    def busy(**_):
        raise idempotency.Busy("Another order-touching task is running.")
        yield

    monkeypatch.setattr(idempotency, "once", busy)

    with time_machine.travel(timezone.make_aware(datetime.combine(DAY, time(15, 5))), tick=False):
        call_command("market_fallback", "--date", DAY.isoformat())

    assert "Another order-touching task is running." in capsys.readouterr().out


def infy():
    from portfolio.instruments import refresh
    from portfolio.models import Instrument

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


def test_a_round_is_told_which_rounds_ran(monkeypatch):
    """Round 0 ran, round 1 failed, round 2 never came: the round at 09:46:30
    sees 0 and itself, so the schedule re-spreads over 1 and 2."""
    from engine.models import TradeSheet, TradeSheetState
    from orders import ladder
    from orders.models import TaskRun

    TradeSheet.objects.create(
        trading_date=DAY,
        version=1,
        trace_id="ts-2026-09-23",
        state=TradeSheetState.DRAFT,
        spendable_cash=Decimal("2600"),
    )
    TaskRun.objects.create(trading_date=DAY, task="ladder", tick_no=0, succeeded=True)
    TaskRun.objects.create(trading_date=DAY, task="ladder", tick_no=1, succeeded=False)
    seen = {}

    def run_round(**kwargs):
        seen["rounds_run"] = kwargs["rounds_run"]
        return []

    monkeypatch.setattr("orders.management.commands.ladder_tick.session.attach", lambda b: True)
    monkeypatch.setattr("orders.management.commands.ladder_tick.get_broker", lambda: object())
    monkeypatch.setattr(
        "orders.management.commands.ladder_tick.reconcile.refresh_live_orders",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(ladder, "run_round", run_round)

    moment = timezone.make_aware(datetime.combine(DAY, time(9, 46, 30)))
    with time_machine.travel(moment, tick=False):
        call_command("ladder_tick", "--date", DAY.isoformat())

    assert seen["rounds_run"] == {0, 3}
