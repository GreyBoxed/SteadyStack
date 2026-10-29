"""Planning from the broker, and the command that runs it on the timer.

The regression pinned here: the command once defaulted `--carry-in` to zero
and passed it through, so every day it planned ignored its carry and its
lumpsums. Left out, carry now comes from the ledger.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command

from engine.tests.test_tradesheet import TUESDAY, portfolio, quotes, universe  # noqa: F401

MONDAY = date(2026, 9, 21)


@pytest.fixture(autouse=True)
def market_hours():
    """Planning today is refused outside market hours, so these tests run at
    10:30 on an ordinary Wednesday, whatever the wall clock says."""
    import time_machine

    with time_machine.travel("2026-09-23 10:30:00 +05:30", tick=True):
        yield


@pytest.fixture
def broker(monkeypatch):
    class Stub:
        def holdings(self):
            return []

    stub = Stub()
    monkeypatch.setattr("engine.planning.get_broker", lambda: stub)
    monkeypatch.setattr(
        "engine.planning.quote_reader.fetch", lambda symbols, broker: quotes(AAA=100, BBB=100)
    )
    return stub


def monday_left_carry(amount="750.00"):
    """Monday closes with *amount* left in the engine account."""
    from journal.cash import allocate
    from journal.models import Book

    allocate(
        Decimal(amount),
        book=Book.PAPER,
        on=MONDAY,
        actor="test",
        description="what Monday left",
        source_type="test",
    )


@pytest.mark.django_db
def test_the_command_reads_carry_from_the_ledger_when_not_told(portfolio, broker):  # noqa: F811
    from engine.models import TradeSheet

    monday_left_carry()

    call_command("generate_tradesheet", "--date", TUESDAY.isoformat())

    sheet = TradeSheet.objects.get(trading_date=TUESDAY)
    assert sheet.carry_in == Decimal("750.00")


@pytest.mark.django_db
def test_an_explicit_carry_is_still_an_override(portfolio, broker):  # noqa: F811
    from engine.models import TradeSheet

    monday_left_carry()

    call_command("generate_tradesheet", "--date", TUESDAY.isoformat(), "--carry-in", "10")

    assert TradeSheet.objects.get(trading_date=TUESDAY).carry_in == Decimal("10")


def paper_lot(symbol, quantity):
    """A lot the paper book holds: what a paper fill creates. A lot with no
    fill behind it is an opening or external one, which is live (M-1)."""
    import uuid

    from django.utils import timezone

    from engine.models import TradeSheet
    from orders.models import Fill, Order, OrderMode, OrderType
    from orders.tests.test_booking import JANUARY
    from portfolio.models import Lot

    key = uuid.uuid4().hex[:8]
    sheet = TradeSheet.objects.create(
        trading_date=JANUARY, version=TradeSheet.objects.count() + 1, trace_id="ts-paper"
    )
    order = Order.objects.create(
        tag=f"tx-paper-{key}",
        tradesheet=sheet,
        instrument_id=symbol,
        trading_date=JANUARY,
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=Decimal("100"),
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-{key}",
        quantity=quantity,
        price=Decimal("100"),
        executed_at=timezone.now(),
    )
    return Lot.objects.create(
        instrument_id=symbol,
        acquired_on=JANUARY,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal("100"),
        basis_state="final",
        created_from_fill=fill,
    )


def bought(sheet) -> set[str]:
    return {line.instrument_id for line in sheet.buy_lines() if line.quantity}


@pytest.mark.django_db
def test_a_paper_day_ranks_against_the_paper_book_not_the_real_account(portfolio, broker):  # noqa: F811
    """M19c. The real account holds none of this, and says so every morning;
    the paper book holds BBB well past its 60%. A plan that believed the
    broker would rank BBB first and buy it again."""
    from engine.planning import plan_from_broker

    paper_lot("BBB", 100)

    sheet = plan_from_broker(trading_date=TUESDAY, actor="system", carry_in=Decimal("0"))

    assert bought(sheet) == {"AAA"}


@pytest.mark.django_db
def test_the_paper_book_is_what_the_lots_say_is_held(portfolio):  # noqa: F811
    """The same open lots FIFO uses: a superseded lot holds nothing."""
    from engine.book import book_for_ranking
    from portfolio import lots

    first = paper_lot("BBB", 100)
    paper_lot("AAA", 7)
    replacement = paper_lot("BBB", 40)
    first.superseded_by = replacement
    first.save(update_fields=["superseded_by"])

    book = book_for_ranking(["AAA", "BBB"])

    assert book == {"AAA": lots.held_quantity("AAA"), "BBB": lots.held_quantity("BBB")}
    assert book == {"AAA": 7, "BBB": 40}


@pytest.mark.django_db
def test_a_paper_plan_leaves_the_brokers_view_of_holdings_alone(portfolio, broker):  # noqa: F811
    """Holding stays the broker's record, so the divergence check (M42) still
    compares two things rather than one thing with itself."""
    from engine.planning import plan_from_broker
    from portfolio.models import Holding

    paper_lot("BBB", 100)

    plan_from_broker(trading_date=TUESDAY, actor="system", carry_in=Decimal("0"))

    assert not Holding.objects.filter(quantity__gt=0).exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("live_book_open")
def test_a_live_day_ranks_against_what_the_broker_holds(portfolio, broker, monkeypatch):  # noqa: F811
    from configuration.models import OrderMode
    from configuration.store import update
    from engine.planning import plan_from_broker

    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: "203.0.113.7")
    update(changed_by="operator", order_mode=OrderMode.LIVE, registered_static_ip="203.0.113.7")
    monkeypatch.setattr(
        broker,
        "holdings",
        lambda: [{"tradingsymbol": "BBB", "quantity": 100, "t1_quantity": 0, "average_price": 100}],
        raising=False,
    )

    sheet = plan_from_broker(trading_date=TUESDAY, actor="system", carry_in=Decimal("0"))

    assert bought(sheet) == {"AAA"}


@pytest.mark.django_db
def test_planning_needs_a_model_portfolio(broker):
    from engine.planning import plan_from_broker
    from engine.tradesheet import GenerationRefused

    with pytest.raises(GenerationRefused, match="model portfolio"):
        plan_from_broker(trading_date=TUESDAY)


# -- the Regenerate button -------------------------------------------------


@pytest.fixture
def today_sheet(portfolio, broker):  # noqa: F811
    from audit.context import today_ist
    from engine.planning import plan_from_broker

    return plan_from_broker(trading_date=today_ist())


def regenerate(client, sheet, reason="prices moved before the open"):
    from django.urls import reverse

    return client.post(
        reverse("engine:regenerate", args=[sheet.trading_date.isoformat(), sheet.version]),
        {"reason": reason},
    )


@pytest.mark.django_db
def test_regenerating_writes_a_new_version_and_audits_why(signed_in, today_sheet):
    from audit.models import AuditEvent
    from engine.models import TradeSheet

    response = regenerate(signed_in, today_sheet)

    assert response.status_code == 302
    versions = list(
        TradeSheet.objects.filter(trading_date=today_sheet.trading_date).values_list(
            "version", flat=True
        )
    )
    assert sorted(versions) == [1, 2]
    event = AuditEvent.objects.get(action="tradesheet.regenerated")
    assert event.after["reason"] == "prices moved before the open"
    assert event.before == {"version": 1}


@pytest.mark.django_db
def test_regenerating_needs_a_reason(signed_in, today_sheet):
    from engine.models import TradeSheet

    regenerate(signed_in, today_sheet, reason="  ")

    assert TradeSheet.objects.filter(trading_date=today_sheet.trading_date).count() == 1


@pytest.mark.django_db
def test_a_day_that_has_bought_cannot_be_regenerated(signed_in, today_sheet):
    """The double buy, from the browser."""
    from engine.models import TradeSheet
    from orders.models import Order, OrderMode, OrderState, OrderType

    Order.objects.create(
        tag="t0",
        tradesheet=today_sheet,
        instrument_id="AAA",  # not the sheet's: a weekend sheet chooses nothing
        trading_date=today_sheet.trading_date,
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=1,
        filled_quantity=1,
        limit_price=Decimal("100"),
        state=OrderState.COMPLETE,
    )

    regenerate(signed_in, today_sheet)

    assert TradeSheet.objects.filter(trading_date=today_sheet.trading_date).count() == 1


@pytest.mark.django_db
def test_an_older_version_offers_no_regeneration(signed_in, today_sheet):
    from django.urls import reverse

    from engine.planning import plan_from_broker

    plan_from_broker(trading_date=today_sheet.trading_date)

    body = signed_in.get(
        reverse("engine:detail", args=[today_sheet.trading_date.isoformat(), 1])
    ).content

    assert b"A later version exists" in body


@pytest.mark.django_db
def test_money_added_after_the_sheet_is_built_is_spent_by_regenerating(signed_in, today_sheet):
    """Add new money, see the prompt, regenerate: the new version has it."""
    from django.urls import reverse

    from engine.models import TradeSheet
    from journal.models import EntryKind, JournalEntry
    from trading_calendar.sessions import is_trading_day

    if not is_trading_day(today_sheet.trading_date):
        pytest.skip("money received on a closed day waits for the next session")

    before = today_sheet.spendable_cash
    response = signed_in.post(
        reverse("web:cash_ledger"),
        {
            "amount": "5000",
            "received_on": today_sheet.trading_date.isoformat(),
            "reference": "x",
            "submission": "0123456789abcdef0123456789abcdef",
        },
        follow=True,
    )
    contribution = JournalEntry.objects.get(kind=EntryKind.CONTRIBUTION)
    assert contribution.occurred_on == today_sheet.trading_date
    assert b"regenerate it" in response.content

    home = signed_in.get(reverse("web:home"))
    assert home.context["regeneration"]["unplanned_money"] == Decimal("5000.00")
    assert b"REGENERATE TODAY" in home.content

    regenerate(signed_in, today_sheet, reason="lumpsum arrived")

    newest = TradeSheet.objects.filter(trading_date=today_sheet.trading_date).latest("version")
    assert newest.version == today_sheet.version + 1
    assert newest.spendable_cash == before + Decimal("5000.00")


# -- the Generate button ---------------------------------------------------


@pytest.mark.django_db
def test_today_offers_generate_until_a_sheet_exists_and_then_regenerate(
    signed_in,
    portfolio,  # noqa: F811
    broker,
):
    from django.urls import reverse

    from audit.context import today_ist
    from engine.models import TradeSheet
    from trading_calendar.sessions import is_trading_day

    if not is_trading_day(today_ist()):
        pytest.skip("a closed day has nothing to generate")

    home = signed_in.get(reverse("web:home"))
    assert home.context["can_generate"] is True
    assert b"Generate today" in home.content

    response = signed_in.post(reverse("engine:generate"))

    sheet = TradeSheet.objects.get(trading_date=today_ist())
    assert response["Location"] == reverse(
        "engine:detail", args=[today_ist().isoformat(), sheet.version]
    )
    assert sheet.generated_by == "operator"

    again = signed_in.get(reverse("web:home"))
    assert again.context["can_generate"] is False
    assert b"REGENERATE TODAY" in again.content


@pytest.mark.django_db
def test_generate_refuses_a_day_that_already_has_a_sheet(signed_in, today_sheet):
    from django.urls import reverse

    from engine.models import TradeSheet

    signed_in.post(reverse("engine:generate"))

    assert TradeSheet.objects.filter(trading_date=today_sheet.trading_date).count() == 1


# -- the market-closed guard -----------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("clock", "says"),
    [("2026-09-23 01:42:00 +05:30", "opens at 09:15"), ("2026-09-23 16:10:00 +05:30", "closed at")],
)
def test_today_is_not_planned_outside_market_hours(portfolio, broker, clock, says):  # noqa: F811
    import time_machine

    from audit.context import today_ist
    from engine.models import TradeSheet
    from engine.planning import MarketClosed, plan_from_broker

    with time_machine.travel(clock, tick=False):
        with pytest.raises(MarketClosed, match=says):
            plan_from_broker(trading_date=today_ist())

    assert TradeSheet.objects.count() == 0


@pytest.mark.django_db
def test_the_guard_can_be_overridden_and_leaves_other_dates_alone(portfolio, broker):  # noqa: F811
    import time_machine

    from audit.context import today_ist
    from engine.planning import plan_from_broker

    with time_machine.travel("2026-09-23 01:42:00 +05:30", tick=False):
        assert plan_from_broker(trading_date=today_ist(), allow_closed_market=True)
        assert plan_from_broker(trading_date=TUESDAY)  # a replay of another day


@pytest.mark.django_db
def test_today_shows_why_generate_is_not_available_before_the_open(signed_in, portfolio, broker):  # noqa: F811
    import time_machine
    from django.urls import reverse

    with time_machine.travel("2026-09-23 01:42:00 +05:30", tick=True):
        home = signed_in.get(reverse("web:home"))
        refused = signed_in.post(reverse("engine:generate"), follow=True)

    assert b"Not now: the market opens at 09:15" in home.content
    assert b"opens at 09:15" in refused.content


# -- a live plan spends only what the account holds (I4) --------------------


def margins_saying(live_balance=None, *, fails=False):
    from broker.exceptions import BrokerError

    def margins():
        if fails:
            raise BrokerError("margins unavailable")
        available = {"cash": 99999, "opening_balance": 99999}
        if live_balance is not None:
            available["live_balance"] = live_balance
        return {"equity": {"available": available}}

    return margins


def opened_with(cash):
    from journal.external import open_books

    open_books(cash=Decimal(cash), on=MONDAY, actor="operator")


@pytest.mark.django_db
def test_a_live_plan_is_capped_at_zerodhas_available_cash(portfolio, broker, already_live):  # noqa: F811
    from engine.planning import plan_from_broker

    opened_with("5000")
    already_live()
    broker.margins = margins_saying(400.5)

    sheet = plan_from_broker(trading_date=TUESDAY, actor="system")

    assert sheet.spendable_cash == Decimal("400.50")
    assert sheet.ranking_snapshot["cash_cap"] == {
        "source": "kite_live_balance",
        "available": "400.5",
        "budget": "1000.00",
        "capped": True,
    }


@pytest.mark.django_db
def test_a_capped_day_is_not_offered_money_that_never_arrived(portfolio, broker, already_live):  # noqa: F811
    """m-1: the cap is not a lumpsum. Money that does arrive later still shows."""
    from engine.planning import plan_from_broker
    from engine.views import _unplanned_money
    from journal.cash import record_lumpsum

    opened_with("5000")
    already_live()
    broker.margins = margins_saying(400.5)
    sheet = plan_from_broker(trading_date=TUESDAY, actor="system")

    assert _unplanned_money(sheet) == Decimal("0")

    record_lumpsum(Decimal("2500"), received_on=TUESDAY, actor="o", reference="upi-9")

    assert _unplanned_money(sheet) == Decimal("2500")


@pytest.mark.django_db
def test_cash_to_deploy_notes_a_capped_day(portfolio, broker, already_live):  # noqa: F811
    from engine.planning import plan_from_broker
    from web.deck import snapshot

    opened_with("5000")
    already_live()
    broker.margins = margins_saying(400.5)
    plan_from_broker(trading_date=TUESDAY, actor="system")

    stat = next(stat for stat in snapshot({}, TUESDAY) if stat.label == "CASH TO DEPLOY")

    assert "capped at ₹400.50" in stat.sub


@pytest.mark.django_db
def test_the_cap_falls_back_to_the_journal_when_margins_cannot_be_read(
    portfolio,  # noqa: F811
    broker,
    already_live,
    caplog,
):
    from engine.planning import plan_from_broker

    opened_with("300")
    already_live()
    broker.margins = margins_saying(fails=True)

    sheet = plan_from_broker(trading_date=TUESDAY, actor="system")

    assert sheet.spendable_cash == Decimal("300.00")
    assert sheet.ranking_snapshot["cash_cap"]["source"] == "journal"
    assert "capped by the journal" in caplog.text


@pytest.mark.django_db
def test_the_cap_falls_back_to_the_journal_when_live_balance_is_absent(
    portfolio,  # noqa: F811
    broker,
    already_live,
):
    """The field is unverified: a response without it must not be read as
    nought, nor as the whole account."""
    from engine.planning import plan_from_broker

    opened_with("650")
    already_live()
    broker.margins = margins_saying(None)

    sheet = plan_from_broker(trading_date=TUESDAY, actor="system")

    assert sheet.spendable_cash == Decimal("650.00")
    assert sheet.ranking_snapshot["cash_cap"]["source"] == "journal"


@pytest.mark.django_db
def test_a_live_plan_the_account_can_cover_is_not_capped(portfolio, broker, already_live):  # noqa: F811
    from engine.planning import plan_from_broker

    opened_with("5000")
    already_live()
    broker.margins = margins_saying(5000)

    sheet = plan_from_broker(trading_date=TUESDAY, actor="system")

    assert sheet.spendable_cash == Decimal("1000.00")
    assert sheet.ranking_snapshot["cash_cap"]["capped"] is False


@pytest.mark.django_db
def test_a_paper_plan_reads_no_margins(portfolio, broker):  # noqa: F811
    from engine.planning import plan_from_broker

    broker.margins = margins_saying(fails=True)

    sheet = plan_from_broker(trading_date=TUESDAY, actor="system")

    assert sheet.spendable_cash == Decimal("1000.00")
    assert "cash_cap" not in sheet.ranking_snapshot
