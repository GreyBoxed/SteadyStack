"""The dashboards.

Every number on these pages comes from `performance`; a view that computes a
gain is in the wrong file. What the tests check is that the page renders, that
it says what it does not know, and that a withheld figure is visibly withheld
rather than quietly missing.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_holdings_is_not_reachable_anonymously(client):
    response = client.get(reverse("web:holdings"))

    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]


def test_holdings_renders_for_a_verified_operator(signed_in, no_broker):
    response = signed_in.get(reverse("web:holdings"))

    assert response.status_code == 200


def test_holdings_says_so_when_prices_could_not_be_read(signed_in, instrument, no_broker):
    """A page that silently shows zeroes when the feed is down is worse than
    one that says the feed is down."""
    a_model_portfolio(instrument)

    response = signed_in.get(reverse("web:holdings"))

    assert b"could not be read" in response.content


def a_model_portfolio(instrument, target_pct="100"):
    from model_portfolio.models import ModelPortfolio, ModelPortfolioRow

    portfolio = ModelPortfolio.objects.create(
        version=1, uploaded_by="operator", content_hash="a" * 64
    )
    ModelPortfolioRow.objects.create(
        portfolio=portfolio, instrument=instrument, target_pct=Decimal(target_pct)
    )
    return portfolio


def test_a_withheld_instrument_says_so_on_the_page(signed_in, instrument, no_broker):
    from records.models import Divergence

    a_model_portfolio(instrument)
    Divergence.objects.create(
        instrument=instrument,
        detected_on="2026-09-22",
        expected_quantity=2,
        actual_quantity=1,
    )

    response = signed_in.get(reverse("web:holdings"))

    assert b"withheld" in response.content


# -- performance (M41, M41a) ---------------------------------------------


def test_performance_renders(signed_in, no_broker):
    response = signed_in.get(reverse("web:performance"))

    assert response.status_code == 200


def test_a_short_history_prints_a_sentence_not_a_number(signed_in, no_broker):
    """M41: one contribution annualises a one-day move into nonsense. The page
    says so rather than printing it."""
    response = signed_in.get(reverse("web:performance"))

    assert b"XIRR unavailable" in response.content


def test_the_benchmark_absence_is_explained_rather_than_blank(
    signed_in, instrument, no_broker, live_mode
):
    """Money has gone in, so there is something to compare -- but no index
    level has ever been recorded. The cell says which of the two is missing
    rather than sitting empty."""
    from datetime import date

    from records.external import record_external
    from records.models import ExternalSide

    record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=1,
        occurred_on=date(2026, 9, 21),
        actor="t",
        reason="test",
        price=Decimal("1000"),
    )

    response = signed_in.get(reverse("web:performance"))

    assert b"No benchmark history" in response.content


def test_the_benchmark_explains_itself_when_nothing_has_been_invested(signed_in, no_broker):
    response = signed_in.get(reverse("web:performance"))

    assert b"No contributions to compare" in response.content


def test_the_performance_page_says_nothing_about_tax(signed_in, no_broker):
    """Decision 16, on the surface an operator actually reads."""
    body = signed_in.get(reverse("web:performance")).content.lower()

    for word in (b"short-term", b"long-term", b"financial year", b"exemption"):
        assert word not in body


# -- the ledgers (M40a, M40b) --------------------------------------------


def test_the_cash_ledger_renders(signed_in):
    response = signed_in.get(reverse("web:cash_ledger"))

    assert response.status_code == 200


def test_the_cash_ledger_shows_whether_the_identity_holds(signed_in):
    response = signed_in.get(reverse("web:cash_ledger"))

    assert b"reconcile" in response.content.lower()


def test_the_cash_ledger_accepts_a_range(signed_in):
    from engine.models import CashLedger

    CashLedger.objects.create(
        book="paper",
        trading_date="2026-09-21",
        cash_spent=Decimal("1000"),
        closing_carry=Decimal("0"),
    )

    response = signed_in.get(reverse("web:cash_ledger"), {"start": "2026-09-22"})

    assert response.status_code == 200
    assert b"2026-09-21" not in response.content


def test_a_bad_range_does_not_five_hundred(signed_in):
    """A typo in a query string should not take the page away from the person
    trying to read it."""
    response = signed_in.get(reverse("web:cash_ledger"), {"start": "not-a-date"})

    assert response.status_code == 200


def a_lumpsum_today(amount="500000", *, allocated_on=None):
    from audit.context import today_ist
    from journal import cash
    from journal.models import Book

    return cash.allocate(
        Decimal(amount),
        book=Book.PAPER,
        on=allocated_on or today_ist(),
        actor="operator",
        description="lumpsum",
        source_type=cash.LUMPSUM,
    )


@pytest.fixture
def a_trading_today(monkeypatch):
    """Today as a trading day, whatever the calendar says of the real date."""
    monkeypatch.setattr("trading_calendar.sessions.is_trading_day", lambda day: True)


def test_the_cash_ledger_shows_today_before_it_settles(signed_in, a_trading_today):
    a_lumpsum_today()

    response = signed_in.get(reverse("web:cash_ledger"))
    body = response.content.decode()

    assert "not settled" in body
    assert "500,000.00" in body or "500000.00" in body
    assert response.context["view"].totals.extra_allocation == Decimal("0")


def test_the_cash_ledger_names_money_dated_a_later_day(signed_in, a_trading_today):
    from datetime import date

    a_lumpsum_today("100000", allocated_on=date(2099, 1, 5))

    body = signed_in.get(reverse("web:cash_ledger")).content.decode()

    assert "allocated to 5 Jan" in body


def test_a_settled_today_shows_only_its_row(signed_in, a_trading_today):
    from audit.context import today_ist
    from engine.models import CashLedger

    CashLedger.objects.create(book="paper", trading_date=today_ist())

    body = signed_in.get(reverse("web:cash_ledger")).content.decode()

    assert "not settled" not in body


def test_the_csv_leaves_the_unsettled_day_out(signed_in, a_trading_today):
    a_lumpsum_today()

    body = signed_in.get(reverse("web:cash_ledger"), {"format": "csv"}).content.decode()

    assert "500000" not in body


def test_the_share_ledger_renders(signed_in):
    response = signed_in.get(reverse("web:share_ledger"))

    assert response.status_code == 200


def test_the_share_ledger_names_a_disagreement(signed_in, instrument, already_live):
    from datetime import date

    from portfolio.models import Holding, Lot, OpeningImport

    already_live()
    # The portfolio's name -- it has lots -- so the holding is compared (gap m3).
    Lot.objects.create(
        instrument=instrument,
        acquired_on=date(2026, 9, 1),
        quantity=5,
        remaining_quantity=5,
        cost_per_unit=Decimal("1000"),
        opening_import=OpeningImport.objects.create(imported_by="operator", source="notes"),
    )
    Holding.objects.create(instrument=instrument, quantity=4, average_cost=Decimal("1000"))

    response = signed_in.get(reverse("web:share_ledger"))

    assert b"disagree" in response.content
    assert b"INFY" in response.content


def test_the_share_ledger_in_paper_compares_the_paper_lots(signed_in, instrument):
    """m-3: in paper the broker's holdings are real shares no paper fill
    bought; the page says what it compares against instead."""
    from portfolio.models import Holding

    Holding.objects.create(instrument=instrument, quantity=4, average_cost=Decimal("1000"))

    body = signed_in.get(reverse("web:share_ledger")).content.decode()

    assert "paper lots hold" in body
    assert "disagree" not in body


# -- today's orders (M39) ------------------------------------------------


def a_quiet_tick(day, reason, detail=""):
    """A tick that fired and placed nothing, which is a decision the system
    made and had a reason for."""
    from engine.models import TradeSheet, TradeSheetState
    from orders.models import NoPlaceReason, Tick

    sheet, _ = TradeSheet.objects.get_or_create(
        trading_date=day,
        version=1,
        defaults={"state": TradeSheetState.RUNNING, "trace_id": f"ts-{day}"},
    )
    return Tick.objects.create(
        tradesheet=sheet,
        trading_date=day,
        tick_no=1,
        no_place_reason=reason or NoPlaceReason.values[0],
        detail=detail,
    )


def test_todays_orders_renders(signed_in):
    response = signed_in.get(reverse("web:orders"))

    assert response.status_code == 200


def test_a_tick_that_placed_nothing_is_shown_with_its_reason(signed_in):
    """Every tick writes a row, including the ones that place nothing. A page
    that showed only the orders would make the day look emptier than it was."""
    from audit.context import today_ist
    from orders.models import NoPlaceReason

    a_quiet_tick(today_ist(), NoPlaceReason.values[0], detail="nothing on offer")

    response = signed_in.get(reverse("web:orders"))

    assert b"placed nothing" in response.content
    assert b"nothing on offer" in response.content


def test_a_past_day_can_be_asked_for(signed_in):
    from orders.models import NoPlaceReason

    a_quiet_tick("2026-09-21", NoPlaceReason.values[0])

    response = signed_in.get(reverse("web:orders"), {"date": "2026-09-21"})

    assert response.status_code == 200
    assert b"2026" in response.content


def test_every_dashboard_is_reachable_from_the_nav(signed_in):
    """Covered properly in test_chrome.py: the sidebar now names five
    destinations and the rest hang off the section they belong to, so
    "is it in the sidebar" is the wrong question. What matters is that
    nothing is orphaned, which test_no_page_is_orphaned_by_the_shell asserts.
    """
    response = signed_in.get(reverse("web:home"))

    assert reverse("web:holdings").encode() in response.content
    assert reverse("web:orders").encode() in response.content


# -- reconstructable from stored data alone ------------------------------


def test_a_past_day_renders_without_calling_the_broker(signed_in):
    """A day that has already happened is history, and history is in our own
    tables. If rendering it needed the broker, a past day would stop being
    readable the moment a session expired -- and every page view would cost a
    call that can fail.
    """
    from audit.models import BrokerCall
    from orders.models import NoPlaceReason

    a_quiet_tick("2026-09-21", NoPlaceReason.values[0], detail="nothing on offer")
    before = BrokerCall.objects.count()

    response = signed_in.get(reverse("web:orders"), {"date": "2026-09-21"})

    assert response.status_code == 200
    assert b"nothing on offer" in response.content
    assert BrokerCall.objects.count() == before


def test_the_ledgers_need_no_broker_either(signed_in):
    from audit.models import BrokerCall

    before = BrokerCall.objects.count()

    assert signed_in.get(reverse("web:cash_ledger")).status_code == 200
    assert signed_in.get(reverse("web:share_ledger")).status_code == 200
    assert BrokerCall.objects.count() == before


# -- reporting survives a dead feed --------------------------------------


def a_stored_close(day, symbol, price):
    from performance.models import DailyValuation

    return DailyValuation.objects.create(
        trading_date=day,
        portfolio_value=Decimal(price),
        holdings_snapshot={symbol: {"quantity": 1, "price": str(price)}},
    )


def test_holdings_falls_back_to_the_last_stored_close(signed_in, instrument, no_broker):
    """A missing Kite session stops the system trading; it must not stop it
    reporting. The value series is written every evening for exactly this."""
    a_model_portfolio(instrument)
    a_stored_close("2026-09-21", "INFY", "1842.75")

    response = signed_in.get(reverse("web:holdings"))

    assert b"last stored close" in response.content
    assert b"1842.75" in response.content


def test_the_page_never_passes_a_stored_price_off_as_live(signed_in, instrument, no_broker):
    """Which of the two is on screen is not left to the reader to guess."""
    a_model_portfolio(instrument)
    a_stored_close("2026-09-21", "INFY", "1842.75")

    body = signed_in.get(reverse("web:holdings")).content

    assert b"21 Sep" in body
    assert b"live feed is unavailable" in body


def test_with_no_stored_close_either_it_says_so_plainly(signed_in, instrument, no_broker):
    a_model_portfolio(instrument)

    response = signed_in.get(reverse("web:holdings"))

    assert b"could not be read" in response.content


def test_performance_reports_without_a_session(signed_in, instrument, no_broker, live_mode):
    """The design says ledger, day detail and performance read from Postgres,
    not from Kite -- so a dead session leaves them working."""
    from datetime import date

    from records.external import record_external
    from records.models import ExternalSide

    record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=2,
        occurred_on=date(2026, 1, 15),
        actor="t",
        reason="test",
        price=Decimal("1700"),
    )
    a_stored_close("2026-09-21", "INFY", "1842.75")

    response = signed_in.get(reverse("web:performance"))

    assert response.status_code == 200
    assert b"last stored close" in response.content
    assert b"3685.50" in response.content  # 2 x 1842.75, valued from the close


# -- the command deck (M39, M40) -----------------------------------------


def test_the_deck_answers_the_morning_questions(signed_in, no_broker):
    body = signed_in.get(reverse("web:home")).content

    for panel in (
        b"Is anything wrong",
        b"The daily cycle",
        b"What is running",
        b"Worth knowing",
        b"What it is worth",
        b"Recent days",
        b"The machine",
    ):
        assert panel in body


def test_the_deck_leads_with_a_dead_session(signed_in, no_broker):
    """The one failure that costs a trading day goes first and largest."""
    body = signed_in.get(reverse("web:home")).content

    assert body.index(b"KITE SESSION") < body.index(b"Is anything wrong")


def test_the_machine_panel_names_what_is_unset(signed_in, no_broker):
    body = signed_in.get(reverse("web:home")).content

    assert b"not registered" in body  # static IP
    assert b"not loaded" in body  # holiday calendar


def test_drift_is_zero_with_nothing_held(db):
    from web.deck import drift

    assert drift({}) == Decimal("0")


def test_drift_sums_every_gap_unsigned(instrument, db):
    """One number for how much rebalancing is owed, which no single gap says."""
    from decimal import Decimal as D

    from portfolio.models import Instrument, Lot
    from web.deck import drift

    other = Instrument.objects.create(tradingsymbol="TCS", instrument_token=2953217, name="TCS")
    a_model_portfolio(instrument, target_pct="50")
    from model_portfolio.models import ModelPortfolio, ModelPortfolioRow

    ModelPortfolioRow.objects.create(
        portfolio=ModelPortfolio.objects.latest("version"), instrument=other, target_pct=D("50")
    )
    for each in (instrument, other):
        Lot.objects.create(
            instrument=each,
            acquired_on="2026-01-15",
            quantity=1,
            remaining_quantity=1,
            cost_per_unit=D("100"),
        )

    # Priced equally, both sit at 50%: nothing is owed.
    assert drift({"INFY": D("100"), "TCS": D("100")}) == D("0.00")
    # Priced 3:1, they are 75/25: each is 25 out, so 50 in total.
    assert drift({"INFY": D("300"), "TCS": D("100")}) == D("50.00")


# -- the panels the design puts beside the tables ------------------------


def test_holdings_leads_with_what_it_is_worth(signed_in, instrument, no_broker):
    a_model_portfolio(instrument)

    body = signed_in.get(reverse("web:holdings")).content

    for label in (
        b"MARKET VALUE",
        b"COST BASIS",
        b"UNREALISED",
        b"DRIFT",
        b"CARRY",
        b"CONTRIBUTED",
    ):
        assert label in body


def test_the_cash_ledger_shows_its_own_integrity(signed_in):
    body = signed_in.get(reverse("web:cash_ledger")).content

    assert b"ROWS EVER UPDATED" in body
    assert b"the table refuses UPDATE" in body


def test_a_skipped_day_is_raised_as_an_exception(signed_in, instrument):
    """A day that bought nothing is the row somebody stops at."""
    from engine.models import CashLedger, SkipReason, TradeSheet, TradeSheetState

    TradeSheet.objects.create(
        trading_date="2026-09-21",
        version=1,
        trace_id="ts-2026-09-21",
        state=TradeSheetState.SKIPPED,
        skip_reason=SkipReason.NO_SESSION_BY_CUTOFF,
    )
    CashLedger.objects.create(
        book="paper", trading_date="2026-09-21", closing_carry=Decimal("2600")
    )

    body = signed_in.get(reverse("web:cash_ledger")).content

    assert b"SKIPPED" in body
    assert b"carried forward whole" in body


def test_an_unresolved_divergence_is_raised_on_the_share_ledger(signed_in, instrument):
    from records.models import Divergence

    Divergence.objects.create(
        instrument=instrument,
        detected_on="2026-09-22",
        expected_quantity=10,
        actual_quantity=9,
    )

    body = signed_in.get(reverse("web:share_ledger")).content

    assert b"UNRESOLVED" in body
    assert b"withheld" in body


def test_lots_are_reported_as_never_mutated(signed_in):
    """The claim the whole gains report rests on, stated where it is checked."""
    body = signed_in.get(reverse("web:share_ledger")).content

    assert b"LOTS EVER MUTATED" in body


def test_the_subscription_cost_is_plotted_not_netted(signed_in, instrument, no_broker):
    """A cost that disappears into a return is a cost nobody questions."""
    from engine.models import CashLedger

    CashLedger.objects.create(
        book="paper",
        trading_date="2026-03-22",
        daily_allocation=Decimal("100000"),
        closing_carry=Decimal("100000"),
    )

    body = signed_in.get(reverse("web:performance")).content

    assert b"data subscription takes" in body
    assert b"share of contributions" in body


def test_the_calendar_shows_a_cell_per_day(signed_in, no_broker):
    from engine.models import TradeSheet, TradeSheetState

    for day in ("2026-09-21", "2026-09-22"):
        TradeSheet.objects.create(
            trading_date=day, version=1, trace_id=f"ts-{day}", state=TradeSheetState.FILLED
        )

    body = signed_in.get(reverse("web:performance")).content

    assert b"Did it run" in body
    assert b"21 Sep 2026" in body


def test_fill_quality_is_reported_when_anything_was_placed(signed_in, no_broker):
    body = signed_in.get(reverse("web:performance")).content

    assert b"How well it bought" in body


# -- the daily cycle reads from what proves a step happened --------------


def test_the_cycle_reports_a_tradesheet_from_the_tradesheet(signed_in, instrument, no_broker):
    """Not from TaskRun: generate_tradesheet records no run, so asking TaskRun
    would report "never ran" forever."""
    from audit.context import today_ist
    from engine.models import TradeSheet, TradeSheetState

    TradeSheet.objects.create(
        trading_date=today_ist(),
        version=1,
        trace_id="ts",
        state=TradeSheetState.FILLED,
        instrument=instrument,
        quantity=3,
    )

    body = signed_in.get(reverse("web:home")).content

    assert b"3 of INFY" in body


def test_the_cycle_reports_settlement_from_the_ledger_row(signed_in, no_broker):
    from audit.context import today_ist
    from engine.models import CashLedger

    CashLedger.objects.create(
        book="paper", trading_date=today_ist(), closing_carry=Decimal("412.50")
    )

    body = signed_in.get(reverse("web:home")).content

    assert b"closing carry" in body


def test_the_cycle_counts_ladder_ticks_by_the_name_the_command_writes(signed_in, no_broker):
    from audit.context import today_ist
    from orders import idempotency
    from orders.models import TaskRun

    TaskRun.objects.create(
        trading_date=today_ist(), task=idempotency.LADDER, tick_no=0, succeeded=True
    )

    body = signed_in.get(reverse("web:home")).content

    assert b"1 round run" in body


def test_recent_days_shows_one_row_per_day(db, instrument):
    """A rebalance batch gets its own sheet on the same date. The row somebody
    wants is the day's decision, not every sheet sharing the date."""
    from engine.models import TradeSheet, TradeSheetState
    from web.deck import recent_days

    TradeSheet.objects.create(
        trading_date="2026-09-21",
        version=1,
        trace_id="ts",
        state=TradeSheetState.FILLED,
        instrument=instrument,
        quantity=2,
    )
    TradeSheet.objects.create(
        trading_date="2026-09-21",
        version=2,
        trace_id="ts",
        state=TradeSheetState.FILLED,
    )

    rows = recent_days()

    assert len(rows) == 1
    assert rows[0]["symbol"] == "INFY"


# -- the day that is waiting for a session -------------------------------


def a_parked_sheet(instrument):
    from audit.context import today_ist
    from engine.models import TradeSheet, TradeSheetState

    return TradeSheet.objects.create(
        trading_date=today_ist(),
        version=1,
        trace_id="ts",
        instrument=instrument,
        quantity=7,
        spendable_cash=Decimal("3250.90"),
        state=TradeSheetState.AWAITING_SESSION,
    )


def test_nothing_is_parked_by_default(db):
    from audit.context import today_ist
    from web.deck import parked

    assert parked(today_ist()) is None


def test_a_parked_day_leads_the_page(signed_in, instrument, no_broker):
    a_parked_sheet(instrument)

    body = signed_in.get(reverse("web:home")).content

    assert b"TODAY IS WAITING FOR YOU" in body
    assert b"3250.90" in body


def test_it_says_the_ladder_resumes_where_the_clock_is(signed_in, instrument, no_broker):
    """Tick numbering follows the clock, not the session: a day resuming at
    11:35 starts at the rung the time of day implies, not at zero."""
    a_parked_sheet(instrument)

    body = signed_in.get(reverse("web:home")).content

    assert b"the tick the clock implies" in body
    assert b"not at zero" in body


def test_it_says_nothing_is_at_risk(signed_in, instrument, no_broker):
    a_parked_sheet(instrument)

    body = signed_in.get(reverse("web:home")).content

    assert b"no money has moved" in body
    assert b"carries whole" in body


def test_a_parked_day_shows_what_it_holds_and_how_it_got_here(signed_in, instrument, no_broker):
    from audit.context import reset_trace_id, set_trace_id
    from audit.events import record

    sheet = a_parked_sheet(instrument)
    token = set_trace_id(sheet.trace_id)
    try:
        record("tradesheet.generated", actor="system", entity=sheet)
    finally:
        reset_trace_id(token)

    response = signed_in.get(reverse("web:home"))
    parked = response.context["parked"]

    assert b"WHAT IS BEING HELD" in response.content
    assert ("shares", 7) in parked["held"]
    assert [step["action"] for step in parked["trail"]] == ["tradesheet.generated"]
    assert parked["marks"][0]["label"] == "tradesheet built"
    assert parked["marks"][-1]["pct"] == 100


# -- crediting a lumpsum (M8a) -------------------------------------------

TOKEN = "0123456789abcdef0123456789abcdef"


def test_a_double_submitted_lumpsum_posts_once(signed_in):
    """I2: one rendering of the form is one lumpsum."""
    from journal.models import EntryKind, JournalEntry

    data = {"amount": "500", "received_on": "2026-09-22", "reference": "x", "submission": TOKEN}
    signed_in.post(reverse("web:cash_ledger"), data)
    signed_in.post(reverse("web:cash_ledger"), data)

    assert JournalEntry.objects.filter(kind=EntryKind.CONTRIBUTION).count() == 1


def test_a_lumpsum_without_its_form_token_is_refused(signed_in):
    from journal.models import JournalEntry

    response = signed_in.post(
        reverse("web:cash_ledger"),
        {"amount": "500", "received_on": "2026-09-22", "reference": "x"},
        follow=True,
    )

    assert JournalEntry.objects.count() == 0
    assert b"Reload the form" in response.content


def test_a_lumpsum_on_a_day_that_has_bought_says_when_it_is_spendable(signed_in, monkeypatch):
    """I1: the message follows the lock, never "regenerate" on a locked day."""
    monkeypatch.setattr("engine.lock.why_locked", lambda day: "has already bought")

    response = signed_in.post(
        reverse("web:cash_ledger"),
        {"amount": "500", "received_on": "2026-09-22", "reference": "x", "submission": TOKEN},
        follow=True,
    )

    assert b"2026-09-22 has already placed its buy" in response.content
    assert b"2026-09-23" in response.content
    assert b"regenerate" not in response.content


def test_a_lumpsum_is_recorded_as_a_contribution_and_an_allocation(signed_in, instrument):
    """Never spent by a sheet already generated: its ranking was frozen
    against the cash it was told about."""
    from journal.cash import LUMPSUM
    from journal.models import EntryKind, JournalEntry

    signed_in.post(
        reverse("web:cash_ledger"),
        {
            "amount": "25000.00",
            "received_on": "2026-09-22",
            "reference": "birthday",
            "submission": TOKEN,
        },
        follow=True,
    )

    contribution = JournalEntry.objects.get(kind=EntryKind.CONTRIBUTION)
    assert contribution.source_type == LUMPSUM
    assert contribution.lines.get(account_id="1100").amount == Decimal("25000")
    allocation = JournalEntry.objects.get(kind=EntryKind.ALLOCATION, source_type=LUMPSUM)
    assert allocation.lines.get(account_id="1110").amount == Decimal("25000")


def test_the_lumpsum_form_explains_when_it_becomes_spendable(signed_in):
    body = signed_in.get(reverse("web:cash_ledger")).content

    assert b"Credit a lumpsum" in body
    assert b"never to one already generated" in body


def test_the_lumpsum_form_warns_between_the_opening_and_the_switch(signed_in):
    """NI2: with the live book open and the mode still paper, a lumpsum funds paper."""
    from datetime import date

    from journal.external import open_books

    assert b"switch to live first" not in signed_in.get(reverse("web:cash_ledger")).content

    open_books(cash=Decimal("0"), on=date(2026, 9, 21), actor="operator")

    body = signed_in.get(reverse("web:cash_ledger")).content.decode()
    assert "switch to live first; this would fund the paper engine" in body


def test_the_lumpsum_form_asks_for_the_upi_reference(signed_in):
    """M11: the statement matches a lumpsum by its UPI reference (spec §8)."""
    body = signed_in.get(reverse("web:cash_ledger")).content

    assert b"UPI reference" in body


def test_a_lumpsum_of_nothing_is_refused(signed_in):
    from journal.models import JournalEntry

    signed_in.post(
        reverse("web:cash_ledger"),
        {"amount": "0", "received_on": "2026-09-22", "submission": TOKEN},
        follow=True,
    )

    assert JournalEntry.objects.count() == 0


def test_holdings_can_be_sorted_and_shows_what_each_name_cost(signed_in, instrument, no_broker):
    a_model_portfolio(instrument)

    for key in ("gap", "value", "gain", "az", "nonsense"):
        response = signed_in.get(reverse("web:holdings"), {"sort": key})
        assert response.status_code == 200
        assert response.context["sort"] == (key if key != "nonsense" else "gap")

    body = signed_in.get(reverse("web:holdings")).content
    assert b"Avg cost" in body
    assert b"DRIFT" in body


# -- ledger exports and filters -------------------------------------------


def a_settled_day(instrument, day="2026-09-18"):
    from django.utils import timezone

    from engine.models import CashLedger, TradeSheet, TradeSheetState
    from orders.models import Fill, Order, OrderMode, OrderState, OrderType

    sheet = TradeSheet.objects.create(
        trading_date=day,
        version=1,
        trace_id=f"ts-{day}",
        state=TradeSheetState.FILLED,
        instrument=instrument,
        quantity=1,
        spendable_cash=Decimal("2600"),
    )
    order = Order.objects.create(
        tag="t0",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=day,
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=1,
        filled_quantity=1,
        limit_price=Decimal("1700"),
        state=OrderState.COMPLETE,
    )
    Fill.objects.create(
        order=order,
        kite_trade_id="p-1",
        quantity=1,
        price=Decimal("1700"),
        executed_at=timezone.now(),
    )
    CashLedger.objects.create(
        book="paper",
        trading_date=day,
        daily_allocation=Decimal("2600"),
        cash_spent=Decimal("1700"),
        closing_carry=Decimal("900"),
    )


def test_the_cash_ledger_says_what_each_day_bought(signed_in, instrument):
    a_settled_day(instrument)

    body = signed_in.get(reverse("web:cash_ledger"), {"range": "all"}).content

    assert "1 \u00d7 INFY".encode() in body
    assert b"ts-2026-09-18" in body


def test_the_cash_ledger_exports_what_it_shows(signed_in, instrument):
    a_settled_day(instrument)

    response = signed_in.get(reverse("web:cash_ledger"), {"range": "all", "format": "csv"})
    lines = response.content.decode().splitlines()

    assert response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"]
    assert lines[0].startswith("date,opening_carry")
    assert lines[1].split(",")[-4:] == ["INFY", "1", "Filled", "ts-2026-09-18"]


def test_the_share_ledger_keeps_a_running_balance_and_filters(signed_in, instrument):
    from portfolio.models import Instrument

    a_settled_day(instrument, "2026-09-17")
    Instrument.objects.create(tradingsymbol="ITC", instrument_token=2, name="ITC")

    response = signed_in.get(reverse("web:share_ledger"), {"range": "all", "instrument": "INFY"})
    rows = response.context["rows"]

    assert [r["balance"] for r in rows] == [1]
    assert b'<option value="ITC"' in response.content

    csv_lines = (
        signed_in.get(reverse("web:share_ledger"), {"range": "all", "format": "csv"})
        .content.decode()
        .splitlines()
    )
    assert csv_lines[0] == "date,symbol,quantity,price,source,reference,balance_after"
    assert csv_lines[1].startswith("2026-09-17,INFY,1,1700")


def test_the_lumpsum_date_is_a_picker_that_starts_at_today(signed_in):
    """A date field only accepts an ISO value; anything in the locale's own
    format is dropped by the browser and the field comes up blank."""
    from audit.context import today_ist

    body = signed_in.get(reverse("web:cash_ledger")).content.decode()

    assert 'type="date"' in body
    assert f'name="received_on" value="{today_ist():%Y-%m-%d}"' in body


def test_a_later_version_does_not_lend_its_name_to_the_days_fills(signed_in, instrument):
    """The day bought INFY on v1; a v2 naming another instrument that placed
    nothing must not turn the row into '1 x ITC'."""
    from engine.models import TradeSheet, TradeSheetState
    from portfolio.models import Instrument

    a_settled_day(instrument)
    itc = Instrument.objects.create(tradingsymbol="ITC", instrument_token=2, name="ITC")
    TradeSheet.objects.create(
        trading_date="2026-09-18",
        version=2,
        trace_id="ts-2026-09-18",
        state=TradeSheetState.AWAITING_SESSION,
        instrument=itc,
        quantity=3,
    )

    rows = signed_in.get(reverse("web:cash_ledger"), {"range": "all"}).context["rows"]
    day = next(day for row, day in rows if str(row.trading_date) == "2026-09-18")

    assert (day["symbol"], day["quantity"], day["version"]) == ("INFY", 1, 1)


# -- one book per figure (M9) ------------------------------------------------


def test_holdings_figures_read_only_the_engines_book(instrument):
    """Contributed is the engine book's, so value and cost must be too."""
    from datetime import date

    from portfolio.models import Lot
    from rebalance.tests.conftest import paper_lot
    from web import deck

    Lot.objects.create(  # a real, imported lot: the live book's
        instrument=instrument,
        acquired_on=date(2024, 1, 15),
        quantity=10,
        remaining_quantity=10,
        cost_per_unit=Decimal("1000"),
    )
    paper_lot(instrument, acquired_on=date(2026, 9, 1), quantity=2, cost=Decimal("100"))
    prices = {instrument.pk: Decimal("150")}

    stats = {stat.label: stat for stat in deck.holdings_stats(prices, date(2026, 9, 22))}
    assert stats["MARKET VALUE"].value == "₹300.00"
    assert stats["COST BASIS"].value == "₹200.00"
    assert "1 lots" in stats["MARKET VALUE"].sub
