"""Replanning the rest of a day that has already bought.

The figure that matters is that nothing is bought twice. A replanned version
ranks against everything the day already bought, spends only what the day has
left, and settles into the same single ledger row as the version before it.
Everything else here is the list of days that must not be replanned.
"""

from datetime import date
from decimal import Decimal

import pytest
import time_machine
from django.utils import timezone

from audit.models import AuditEvent
from engine import replan as replanning
from engine.models import CashLedger, SkipReason, TradeSheet, TradeSheetState
from engine.replan import ReplanRefused, replan
from engine.tests.test_tradesheet import portfolio, quotes, universe  # noqa: F401
from journal.trades import post_fill
from model_portfolio.uploads import store
from orders.models import Fill, Order, OrderSide, OrderType
from orders.state import OrderState
from portfolio import lots

TUESDAY = date(2026, 9, 22)
WEDNESDAY = date(2026, 9, 23)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def half_past_ten():
    with time_machine.travel("2026-09-23 10:30:00 +05:30", tick=False):
        yield


@pytest.fixture(autouse=True)
def prices_and_a_session(monkeypatch, settings, tmp_path):
    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
    monkeypatch.setattr(
        "engine.replan.quote_reader.fetch", lambda symbols, broker: quotes(AAA=100, BBB=100)
    )
    monkeypatch.setattr("engine.replan.session_is_valid", lambda: True)


def tuesday_left(carry="9000.00"):
    """Tuesday closes with *carry* in the engine account, and is summarised."""
    from engine.ledger import settle_day
    from journal.cash import allocate
    from journal.models import Book

    allocate(
        Decimal(carry),
        book=Book.PAPER,
        on=TUESDAY,
        actor="test",
        description="what Tuesday left",
        source_type="test",
    )
    settle_day(TUESDAY, book=Book.PAPER)


def a_sheet(version=1, *, state=TradeSheetState.RUNNING, quantity=60, reserved="0"):
    return TradeSheet.objects.create(
        trading_date=WEDNESDAY,
        version=version,
        trace_id="ts-2026-09-23",
        state=state,
        instrument_id="BBB",
        quantity=quantity,
        spendable_cash=Decimal("10000.00"),
        reference_price=Decimal("100.00"),
        carry_in=Decimal("9000.00"),
        authorized_at=timezone.now(),
        authorized_by="operator",
        ranking_snapshot={"reserved": reserved},
    )


def bought(sheet, symbol, quantity, *, tick_no=0, price="100.00", side=OrderSide.BUY):
    order = Order.objects.create(
        tag=f"tx-replan-{sheet.version}-{tick_no}-{symbol}",
        tradesheet=sheet,
        instrument_id=symbol,
        trading_date=sheet.trading_date,
        tick_no=tick_no,
        side=side,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=quantity,
        filled_quantity=quantity,
        limit_price=Decimal(price),
        state=OrderState.COMPLETE,
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-{order.tag}",
        quantity=quantity,
        price=Decimal(price),
        charges=Decimal("3.00"),
        executed_at=timezone.now(),
    )
    lots.from_fill(fill)
    post_fill(fill)
    return order


def stopped(sheet):
    TradeSheet.objects.filter(pk=sheet.pk).update(
        state=TradeSheetState.HALTED, skip_reason=SkipReason.OPERATOR
    )
    sheet.refresh_from_db()
    return sheet


@pytest.fixture
def morning(portfolio):  # noqa: F811
    """v1 bought 30 BBB for 3,000 (and 3 in charges) and was stopped."""
    tuesday_left()
    sheet = a_sheet()
    bought(sheet, "BBB", 30)
    return stopped(sheet)


def replanned(sheet, reason="the model portfolio changed"):
    return replan(sheet, actor="operator", reason=reason, broker=object())


def lines(sheet):
    return {line.instrument_id: line.quantity for line in sheet.buy_lines() if line.quantity}


# -- what a replanned version is -------------------------------------------


def test_the_rest_of_the_day_is_planned_as_a_new_version_nobody_has_authorized(morning):
    new = replanned(morning)

    assert new.version == 2
    assert not new.is_authorized
    assert TradeSheet.objects.filter(trading_date=WEDNESDAY).count() == 2


def test_it_spends_only_what_the_day_has_left(morning):
    new = replanned(morning)

    assert new.spendable_cash == Decimal("6997.00")  # 10,000 - 3,000 - 3
    assert new.planned_cost <= new.spendable_cash


def test_it_ranks_against_what_the_day_already_bought(morning):
    """30 BBB at 100 is held. With 6,997 more the whole is ~9,997: BBB's 60%
    is ~5,998, so at most ~29 more BBB -- never another full day's worth."""
    new = replanned(morning)

    assert (30 + lines(new).get("BBB", 0)) * 100 <= Decimal("0.60") * Decimal("9997") + 100
    assert lines(new).get("AAA", 0) > 0


def test_it_plans_against_the_model_portfolio_uploaded_since_the_morning(morning):
    store("symbol,target_pct\nAAA,100\n", uploaded_by="operator")

    new = replanned(morning)

    assert set(lines(new)) == {"AAA"}


def test_it_records_what_it_netted_and_why(morning):
    new = replanned(morning, reason="moving to v3 of the model")

    block = new.ranking_snapshot["replan"]
    assert block["supersedes_version"] == 1
    assert block["reason"] == "moving to v3 of the model"
    assert block["fills_before"] == {"BBB": 30}
    assert Decimal(block["spent_before"]) == Decimal("3000.00")
    assert Decimal(block["charges_before"]) == Decimal("3.00")
    event = AuditEvent.objects.get(action="tradesheet.replanned")
    assert event.trace_id == "ts-2026-09-23"
    assert event.actor == "operator"


def test_the_stopped_version_is_left_exactly_as_it_was(morning):
    replanned(morning)

    morning.refresh_from_db()
    assert morning.state == TradeSheetState.HALTED
    assert morning.skip_reason == SkipReason.OPERATOR


def test_a_name_sold_today_is_not_bought_back(morning):
    """CNC nets a same-day buy against a same-day sell. A rebalance sell's
    orders hang off the day's sheet in a stream of their own."""
    Order.objects.create(
        tag="tx-sell-aaa",
        tradesheet=morning,
        instrument_id="AAA",
        trading_date=WEDNESDAY,
        tick_no=5,
        stream="rebalance-1",
        side=OrderSide.SELL,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=1,
        filled_quantity=1,
        limit_price=Decimal("100"),
        state=OrderState.COMPLETE,
    )

    new = replanned(morning)

    assert "AAA" not in lines(new)


def test_the_reserve_is_carried_over_rather_than_recounted(portfolio):  # noqa: F811
    tuesday_left()
    sheet = a_sheet(reserved="0")
    TradeSheet.objects.filter(pk=sheet.pk).update(
        reserve_symbol="BBB", reserve_days=4, reserve_engaged=False
    )
    bought(sheet, "BBB", 30)
    stopped(sheet)

    new = replanned(sheet)

    assert (new.reserve_symbol, new.reserve_days, new.reserve_engaged) == ("BBB", 4, False)


def test_replanning_twice_nets_both_earlier_versions(morning):
    second = replanned(morning)
    TradeSheet.objects.filter(pk=second.pk).update(
        authorized_at=timezone.now(), authorized_by="operator", state=TradeSheetState.RUNNING
    )
    second.refresh_from_db()
    bought(second, "AAA", 20, tick_no=40)
    stopped(second)

    third = replanned(second)

    assert third.version == 3
    assert third.spendable_cash == Decimal("4994.00")  # 10,000 - 3,000 - 2,000 - 6
    assert third.ranking_snapshot["replan"]["fills_before"] == {"AAA": 20, "BBB": 30}


def test_a_replanned_version_stopped_before_it_bought_can_be_replanned_again(morning):
    """A version with no fills of its own, on a day that bought, is halted by
    Stop today (never skipped), and the day can be replanned again."""
    from orders import overrides

    second = replanned(morning)
    TradeSheet.objects.filter(pk=second.pk).update(
        authorized_at=timezone.now(), authorized_by="operator", state=TradeSheetState.RUNNING
    )
    second.refresh_from_db()
    Order.objects.create(
        tag="tx-v2-resting",
        tradesheet=second,
        instrument_id="AAA",
        trading_date=WEDNESDAY,
        tick_no=41,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=Decimal("99"),
        state=OrderState.OPEN,
    )

    class Broker:
        def cancel_order(self, order_id):
            return order_id

    overrides.stop_day(second, actor="operator", reason="changed again", broker=Broker())
    second.refresh_from_db()
    assert second.state == TradeSheetState.HALTED

    third = replanned(second)

    assert third.version == 3


def test_a_copy_read_before_the_stop_is_judged_as_the_day_is_now(portfolio):  # noqa: F811
    tuesday_left()
    sheet = a_sheet()
    bought(sheet, "BBB", 30)
    stopped(TradeSheet.objects.get(pk=sheet.pk))  # `sheet` still says RUNNING

    assert replanned(sheet).version == 2


# -- days that must not be replanned ---------------------------------------


def refused(sheet, match):
    with pytest.raises(ReplanRefused, match=match):
        replanned(sheet)
    assert TradeSheet.objects.filter(trading_date=sheet.trading_date).count() == 1


def test_a_day_still_running_is_stopped_first(portfolio):  # noqa: F811
    tuesday_left()
    sheet = a_sheet()
    bought(sheet, "BBB", 30)

    refused(sheet, "Stop today first")


def test_a_day_with_an_order_still_out_is_refused(morning):
    Order.objects.create(
        tag="tx-still-out",
        tradesheet=morning,
        instrument_id="BBB",
        trading_date=WEDNESDAY,
        tick_no=9,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=Decimal("100"),
        state=OrderState.OPEN,
    )

    refused(morning, "still out")


def test_a_day_that_has_not_bought_is_regenerated_instead(portfolio):  # noqa: F811
    tuesday_left()
    sheet = stopped(a_sheet())

    refused(sheet, "Regenerate")


def test_an_older_version_is_refused(morning):
    replanned(morning)

    with pytest.raises(ReplanRefused, match="later version"):
        replanned(morning)


def test_a_day_that_has_settled_is_refused(morning):
    CashLedger.objects.create(book="paper", trading_date=WEDNESDAY)

    refused(morning, "settled")


def test_a_paused_system_is_refused(morning):
    from configuration.pause import pause

    pause(actor="operator", reason="away")

    refused(morning, "stopped")


def test_live_mode_is_refused(already_live, morning):
    already_live()

    refused(morning, "paper")


def test_too_close_to_the_close_is_refused(morning):
    with time_machine.travel("2026-09-23 14:59:00 +05:30", tick=False):
        refused(morning, "close")


def test_another_day_is_refused(portfolio):  # noqa: F811
    sheet = a_sheet()
    TradeSheet.objects.filter(pk=sheet.pk).update(trading_date=TUESDAY)
    sheet.refresh_from_db()

    refused(sheet, "today")


def test_nothing_left_to_spend_writes_no_version(portfolio):  # noqa: F811
    tuesday_left(carry="2006.00")  # 3,006 opening, all but 3 spent below
    sheet = a_sheet()
    bought(sheet, "BBB", 30)
    stopped(sheet)

    refused(sheet, "Nothing is left to buy")


def test_a_round_holding_the_lock_makes_it_wait_its_turn(morning):
    from orders import idempotency

    with idempotency.order_lock(), pytest.raises(idempotency.Busy):
        replanned(morning)

    assert TradeSheet.objects.filter(trading_date=WEDNESDAY).count() == 1


def test_regenerate_still_refuses_a_day_that_has_bought(morning):
    """Replan is the way through; Regenerate is unchanged (R1)."""
    from engine.lock import why_locked

    assert "already bought" in why_locked(WEDNESDAY)


def test_why_not_says_nothing_when_a_day_can_be_replanned(morning):
    assert replanning.why_not(morning) is None


# -- from the browser and the terminal -------------------------------------


def post_replan(client, sheet, reason="the model portfolio changed"):
    from django.urls import reverse

    return client.post(
        reverse("engine:replan", args=[sheet.trading_date.isoformat(), sheet.version]),
        {"reason": reason},
    )


def test_replanning_from_the_browser_writes_the_new_version_and_goes_to_it(signed_in, morning):
    response = post_replan(signed_in, morning)

    assert response.status_code == 302
    assert response["Location"].endswith(f"/{WEDNESDAY.isoformat()}/v2/")
    assert TradeSheet.objects.get(trading_date=WEDNESDAY, version=2).generated_by == "operator"


def test_replanning_from_the_browser_needs_a_reason(signed_in, morning):
    post_replan(signed_in, morning, reason="  ")

    assert not TradeSheet.objects.filter(version=2).exists()


def test_a_refused_replan_says_why_and_writes_nothing(signed_in, portfolio):  # noqa: F811
    tuesday_left()
    sheet = a_sheet()
    bought(sheet, "BBB", 30)

    response = post_replan(signed_in, sheet)

    assert response.status_code == 302
    assert not TradeSheet.objects.filter(version=2).exists()
    followed = signed_in.get(response["Location"])
    assert "Stop today first" in followed.content.decode()


def test_the_tradesheet_offers_replan_on_a_stopped_day_that_bought(signed_in, morning):
    from django.urls import reverse

    page = signed_in.get(reverse("engine:detail", args=[WEDNESDAY.isoformat(), 1]))

    assert "Replan the rest" in page.content.decode()


def test_the_tradesheet_does_not_offer_replan_while_the_day_runs(signed_in, portfolio):  # noqa: F811
    from django.urls import reverse

    tuesday_left()
    sheet = a_sheet()
    bought(sheet, "BBB", 30)

    page = signed_in.get(reverse("engine:detail", args=[WEDNESDAY.isoformat(), 1]))

    assert "Replan the rest" not in page.content.decode()


def test_the_command_replans_and_says_what_it_wrote(morning, capsys):
    from django.core.management import call_command

    call_command("replan_tradesheet", "--reason", "new model", "--actor", "operator")

    assert TradeSheet.objects.filter(trading_date=WEDNESDAY, version=2).exists()
    assert "v2" in capsys.readouterr().out


def test_the_command_fails_loudly_when_refused(portfolio):  # noqa: F811
    from django.core.management import CommandError, call_command

    tuesday_left()
    sheet = a_sheet()
    bought(sheet, "BBB", 30)

    with pytest.raises(CommandError, match="Stop today first"):
        call_command("replan_tradesheet", "--reason", "new model")


def test_the_command_fails_loudly_when_a_round_holds_the_lock(morning):
    from django.core.management import CommandError, call_command

    from orders import idempotency

    with idempotency.order_lock(), pytest.raises(CommandError, match="order-touching"):
        call_command("replan_tradesheet", "--reason", "new model")


def test_a_replan_from_the_browser_is_stamped_with_the_days_trace(signed_in, morning):
    post_replan(signed_in, morning)

    new = TradeSheet.objects.get(trading_date=WEDNESDAY, version=2)
    assert new.trace_id == "ts-2026-09-23"
    assert AuditEvent.objects.get(action="tradesheet.replanned").trace_id == "ts-2026-09-23"


# -- sells elsewhere in the day --------------------------------------------


def a_sell(on, symbol, *, state, filled=0):
    return Order.objects.create(
        tag=f"tx-sell-{symbol}-{state}",
        tradesheet=on,
        instrument_id=symbol,
        trading_date=WEDNESDAY,
        tick_no=7,
        stream="rebalance-1",
        side=OrderSide.SELL,
        mode="paper",
        order_type=OrderType.LIMIT,
        quantity=1,
        filled_quantity=filled,
        limit_price=Decimal("100"),
        state=state,
    )


def test_a_resting_rebalance_sell_does_not_block_a_replan_and_its_name_is_left_out(morning):
    """Stop today cancels the daily buy, not a sale; a sale resting is not a
    reason to refuse, only a name not to buy back."""
    a_sell(morning, "AAA", state=OrderState.OPEN)

    new = replanned(morning)

    assert "AAA" not in lines(new)


def test_a_sell_that_never_filled_does_not_keep_its_name_out(morning):
    a_sell(morning, "AAA", state=OrderState.REJECTED)

    new = replanned(morning)

    assert "AAA" in lines(new)
