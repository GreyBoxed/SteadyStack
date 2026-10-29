"""Generating a day.

Two behaviours carry the weight here. A tradesheet is versioned rather than
edited, so a sheet that has already explained an order keeps saying what it
said. And a day that buys nothing says why: an idle day with a blank reason
is indistinguishable from a broken one.
"""

from datetime import date
from decimal import Decimal

import pytest

from audit.models import AuditEvent
from configuration.store import update
from engine.models import SkipReason, TradeSheet, TradeSheetState
from engine.selection import Quote
from engine.tradesheet import GenerationRefused, generate
from model_portfolio.uploads import store
from portfolio.instruments import refresh
from portfolio.models import Instrument

MONDAY = date(2026, 9, 21)
TUESDAY = date(2026, 9, 22)


def master(symbol, token):
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


def quotes(**prices) -> dict[str, Quote]:
    """quotes(AAA=100) or quotes(AAA=(100, 101)) for a last price and an ask."""
    book = {}
    for symbol, price in prices.items():
        last, ask = price if isinstance(price, tuple) else (price, price)
        book[symbol] = Quote(symbol, Decimal(str(last)), Decimal(str(ask)))
    return book


@pytest.fixture
def universe(db):
    refresh([master("AAA", 1), master("BBB", 2), master("MRF", 3)])


@pytest.fixture
def portfolio(universe):
    return store("symbol,target_pct\nAAA,40\nBBB,60\n", uploaded_by="operator")


def hold(symbol, quantity):
    """Hold *quantity* on a paper day: a lot in the paper book (M19c), which
    only a paper fill makes -- a lot with no fill behind it is live (M-1)."""
    from rebalance.tests.conftest import paper_lot

    paper_lot(
        Instrument.objects.get(pk=symbol),
        acquired_on=date(2024, 1, 15),
        quantity=quantity,
        cost=Decimal("100"),
    )


# -- generating ------------------------------------------------------------


@pytest.mark.django_db
def test_a_tradesheet_is_generated(portfolio):
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert sheet.version == 1
    assert sheet.state == TradeSheetState.DRAFT
    assert sheet.quantity > 0


# -- parking on a missing session (M7) -------------------------------------


@pytest.mark.django_db
def test_a_day_without_a_kite_session_parks(portfolio):
    """It does not fail: the plan is sound, only the placing has to wait."""
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=False)

    assert sheet.state == TradeSheetState.AWAITING_SESSION


@pytest.mark.django_db
def test_a_parked_day_still_knows_what_it_would_buy(portfolio):
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=False)

    assert sheet.instrument_id is not None
    assert sheet.quantity > 0
    assert sheet.ranking_snapshot["ranked"]


@pytest.mark.django_db
def test_the_session_is_read_from_storage_when_not_stated(portfolio):
    """No session exists in these tests, so the day parks by itself."""
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))

    assert sheet.state == TradeSheetState.AWAITING_SESSION


@pytest.mark.django_db
def test_a_skipped_day_does_not_park(portfolio):
    """A day with nothing to buy is not waiting for a login."""
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=50000, BBB=50000), has_session=False)

    assert sheet.state == TradeSheetState.SKIPPED


@pytest.mark.django_db
def test_the_sheet_points_at_the_weights_that_produced_it(portfolio):
    """A foreign key, so the day is explicable without reconstruction (D14)."""
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))
    assert sheet.model_portfolio_id == portfolio.id


@pytest.mark.django_db
def test_the_full_ranking_is_stored(portfolio):
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))

    assert len(sheet.ranking_snapshot["ranked"]) == 2
    assert sheet.ranking_snapshot["ranked"][0]["symbol"] == "BBB"


@pytest.mark.django_db
def test_the_day_carries_the_trading_days_trace_id(portfolio):
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))
    assert sheet.trace_id == "ts-2026-09-22"


@pytest.mark.django_db
def test_a_regenerate_after_a_batch_closed_mid_day_leaves_its_proceeds(portfolio):
    """I-1: proceeds dated today are tomorrow's money, so a Regenerate of the
    same day sizes against the day's own budget alone."""
    from journal.accounts import UNALLOCATED
    from journal.cash import REINVEST, allocate

    generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))
    allocate(
        Decimal("5000"),
        book="paper",
        on=TUESDAY,
        actor="system",
        description="reinvested proceeds of a batch cancelled mid-day",
        source_type=REINVEST,
        source_account=UNALLOCATED,
    )

    again = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))

    assert again.spendable_cash == Decimal("1000.00")


def test_spendable_cash_is_the_daily_amount_plus_carry(portfolio):
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), carry_in=Decimal("250"))
    assert sheet.spendable_cash == Decimal("1250.00")


@pytest.mark.django_db
def test_the_configured_daily_amount_is_used(portfolio):
    update(changed_by="operator", daily_amount=Decimal("5000"))

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))

    assert sheet.spendable_cash == Decimal("5000.00")


@pytest.mark.django_db
def test_generation_is_audited(portfolio):
    generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))

    event = AuditEvent.objects.get(action="tradesheet.generated")
    assert event.after["trading_date"] == "2026-09-22"


@pytest.mark.django_db
def test_generation_without_a_model_portfolio_is_refused(universe):
    with pytest.raises(GenerationRefused, match="model portfolio"):
        generate(trading_date=TUESDAY, quotes=quotes(AAA=100))

    assert TradeSheet.objects.count() == 0


# -- versioning (D13) ------------------------------------------------------


@pytest.mark.django_db
def test_regenerating_produces_a_new_version(portfolio):
    generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))
    second = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))

    assert second.version == 2
    assert TradeSheet.objects.filter(trading_date=TUESDAY).count() == 2


@pytest.mark.django_db
def test_an_earlier_version_is_left_exactly_as_it_was(portfolio):
    """It may already be the explanation for an order."""
    first = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))
    original_snapshot = first.ranking_snapshot

    generate(trading_date=TUESDAY, quotes=quotes(AAA=500, BBB=500))

    first.refresh_from_db()
    assert first.ranking_snapshot == original_snapshot


@pytest.mark.django_db
def test_versions_are_per_trading_date(portfolio):
    generate(trading_date=MONDAY, quotes=quotes(AAA=100, BBB=100))
    tuesday = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))

    assert tuesday.version == 1


# -- days that buy nothing -------------------------------------------------


@pytest.mark.django_db
def test_a_day_with_nothing_affordable_is_skipped_with_a_reason(portfolio):
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=50000, BBB=50000))

    assert sheet.state == TradeSheetState.SKIPPED
    assert sheet.skip_reason == SkipReason.NO_AFFORDABLE_INSTRUMENT


@pytest.mark.django_db
def test_a_day_with_no_prices_at_all_is_skipped_with_a_reason(portfolio):
    sheet = generate(trading_date=TUESDAY, quotes={})

    assert sheet.state == TradeSheetState.SKIPPED
    assert sheet.skip_reason == SkipReason.EMPTY_UNIVERSE


@pytest.mark.django_db
def test_a_skipped_day_never_has_a_blank_reason(portfolio):
    """An idle day that cannot explain itself reads as a broken one."""
    sheet = generate(trading_date=TUESDAY, quotes={})
    assert sheet.skip_reason != ""


@pytest.mark.django_db
def test_a_skipped_day_commits_no_quantity(portfolio):
    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=50000, BBB=50000))

    assert sheet.quantity == 0
    assert sheet.instrument_id is None


@pytest.mark.django_db
def test_an_excluded_name_appears_in_the_snapshot_as_excluded(portfolio):
    update(changed_by="operator", max_price_per_share=Decimal("1000"))

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=5000))

    excluded = {row["symbol"]: row["reason"] for row in sheet.ranking_snapshot["excluded"]}
    assert "BBB" in excluded


# -- holdings drive the weights -------------------------------------------


@pytest.mark.django_db
def test_holdings_move_the_ranking(portfolio):
    """BBB leads on day one; once it is held, AAA takes over."""
    hold("BBB", 100)

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))

    assert sheet.instrument_id == "AAA"


@pytest.mark.django_db
def test_a_holding_outside_the_universe_does_not_confuse_the_day(portfolio):
    """MRF is held but not targeted. It is not bought and does not break the
    ranking."""
    hold("MRF", 1)

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100, MRF=142000))

    assert sheet.instrument_id in {"AAA", "BBB"}


# -- the reserve (D5, M15d) ------------------------------------------------


@pytest.mark.django_db
def test_an_unaffordable_leader_starts_the_counter(universe):
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")

    sheet = generate(trading_date=TUESDAY, quotes=quotes(MRF=142000, AAA=100))

    assert sheet.reserve_symbol == "MRF"
    assert sheet.reserve_days == 1
    assert sheet.reserve_engaged is False
    assert sheet.instrument_id == "AAA"  # still buys the next affordable name


@pytest.mark.django_db
def test_the_counter_accumulates_across_days(universe):
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    book = quotes(MRF=142000, AAA=100)

    days = [date(2026, 9, d) for d in (14, 15, 16, 17, 18)]
    sheets = [generate(trading_date=day, quotes=book) for day in days]

    assert [sheet.reserve_days for sheet in sheets] == [1, 2, 3, 4, 5]


@pytest.mark.django_db
def test_the_reserve_engages_at_the_threshold_and_the_day_places_nothing(universe):
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    book = quotes(MRF=142000, AAA=100)

    for day in (14, 15, 16, 17):
        generate(trading_date=date(2026, 9, day), quotes=book)

    # The four days before each allocated a daily amount that nothing spent,
    # so the journal would carry 4,000 in. Stated as zero, the day holds one
    # daily amount, and the reserve sets all of it aside.
    engaged = generate(trading_date=date(2026, 9, 18), quotes=book, carry_in=Decimal("0"))

    assert engaged.reserve_engaged is True
    assert engaged.state == TradeSheetState.SKIPPED
    assert engaged.skip_reason == SkipReason.RESERVE_ENGAGED
    assert engaged.quantity == 0


@pytest.mark.django_db
def test_an_engaged_reserve_saves_one_daily_amount_and_spends_the_rest(universe):
    """M15d: waiting for MRF no longer idles the portfolio. The first engaged
    day sets aside one daily amount toward it; the rest goes to the others."""
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    book = quotes(MRF=142000, AAA=100)

    for day in (14, 15, 16, 17):
        generate(trading_date=date(2026, 9, day), quotes=book)
    engaged = generate(trading_date=date(2026, 9, 18), quotes=book, carry_in=Decimal("4000"))

    assert engaged.reserve_engaged is True
    assert engaged.ranking_snapshot["reserved"] == "1000.00"
    # 5,000 reachable, AAA's 40% is 2,000: 20 shares, from the 4,000 not reserved.
    assert [(line.instrument_id, line.quantity) for line in engaged.buy_lines()] == [("AAA", 20)]
    assert engaged.state != TradeSheetState.SKIPPED


@pytest.mark.django_db
def test_engaging_the_reserve_raises_a_notice(universe):
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    book = quotes(MRF=142000, AAA=100)

    for day in (14, 15, 16, 17, 18):
        generate(trading_date=date(2026, 9, day), quotes=book)

    assert AuditEvent.objects.filter(action="reserve.engaged").count() == 1


@pytest.mark.django_db
def test_the_notice_is_raised_once_not_every_morning(universe):
    """An alert repeated daily stops being read, which is not raising it."""
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    book = quotes(MRF=142000, AAA=100)

    for day in (14, 15, 16, 17, 18, 21, 22):
        generate(trading_date=date(2026, 9, day), quotes=book)

    assert AuditEvent.objects.filter(action="reserve.engaged").count() == 1


@pytest.mark.django_db
def test_the_counter_resets_when_the_leader_becomes_affordable(universe):
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")

    generate(trading_date=MONDAY, quotes=quotes(MRF=142000, AAA=100))
    affordable = generate(trading_date=TUESDAY, quotes=quotes(MRF=500, AAA=100))

    assert affordable.reserve_days == 0
    assert affordable.reserve_symbol == ""
    assert affordable.instrument_id == "MRF"


@pytest.mark.django_db
def test_the_counter_resets_when_the_leader_stops_leading(universe):
    """Waiting for a name that is no longer the most underweight is just not
    buying."""
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    generate(trading_date=MONDAY, quotes=quotes(MRF=142000, AAA=100))

    hold("MRF", 1)  # now heavily overweight at 142,000
    later = generate(trading_date=TUESDAY, quotes=quotes(MRF=142000, AAA=100))

    assert later.reserve_symbol == ""


@pytest.mark.django_db
def test_a_name_leaving_the_universe_clears_the_wait(universe):
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    generate(trading_date=MONDAY, quotes=quotes(MRF=142000, AAA=100))

    store("symbol,target_pct\nAAA,100\n", uploaded_by="operator")
    later = generate(trading_date=TUESDAY, quotes=quotes(MRF=142000, AAA=100))

    assert later.reserve_symbol == ""
    assert later.reserve_days == 0


@pytest.mark.django_db
def test_a_ceiling_exclusion_also_clears_the_wait(universe):
    """A name excluded by M11 is not one the portfolio should idle for."""
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    generate(trading_date=MONDAY, quotes=quotes(MRF=142000, AAA=100))

    update(changed_by="operator", max_price_per_share=Decimal("5000"))
    later = generate(trading_date=TUESDAY, quotes=quotes(MRF=142000, AAA=100))

    assert later.reserve_symbol == ""


@pytest.mark.django_db
def test_the_threshold_is_configurable(universe):
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    update(changed_by="operator", reserve_threshold_days=2)
    book = quotes(MRF=142000, AAA=100)

    generate(trading_date=MONDAY, quotes=book)
    second = generate(trading_date=TUESDAY, quotes=book)

    assert second.reserve_engaged is True


@pytest.mark.django_db
def test_a_runaway_price_is_alerted_rather_than_idling_for_ever(universe):
    store("symbol,target_pct\nMRF,60\nAAA,40\n", uploaded_by="operator")
    update(changed_by="operator", reserve_threshold_days=2)
    book = quotes(MRF=142000, AAA=100)

    for day in (14, 15, 16, 17, 18):
        generate(trading_date=date(2026, 9, day), quotes=book)

    assert AuditEvent.objects.filter(action="reserve.runaway").exists()


@pytest.mark.django_db
def test_a_leader_held_back_by_the_cap_does_not_start_the_reserve(universe):
    """The reserve is for names the day cannot afford, not for names it chose
    not to overshoot on. Counting a capped name would idle the portfolio for
    something that was never out of reach."""
    store("symbol,target_pct\nAAA,35\nBBB,25\nMRF,40\n", uploaded_by="operator")
    hold("MRF", 3)

    sheet = generate(
        trading_date=TUESDAY,
        quotes=quotes(AAA=(3400, 3401), BBB=(1700, 1700.5), MRF=(1650, 1650.5)),
        carry_in=Decimal("2500"),
        has_session=True,
    )

    assert sheet.instrument_id == "BBB"
    assert sheet.reserve_symbol == ""
    assert sheet.reserve_days == 0


# -- the calendar and the ledger (2.1) -------------------------------------


@pytest.mark.django_db
def test_a_weekend_produces_a_skipped_sheet_rather_than_nothing(portfolio):
    """An absent day and a day that bought nothing look identical in a list of
    tradesheets, and only one of them is a problem."""
    saturday = date(2026, 9, 26)

    sheet = generate(trading_date=saturday, quotes=quotes(AAA=100, BBB=100))

    assert sheet.state == TradeSheetState.SKIPPED
    assert sheet.skip_reason == SkipReason.WEEKEND
    assert sheet.quantity == 0


@pytest.mark.django_db
def test_a_holiday_produces_a_skipped_sheet(portfolio):
    from trading_calendar.models import SessionKind
    from trading_calendar.sessions import set_day

    set_day(TUESDAY, kind=SessionKind.CLOSED, note="Exchange holiday")

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100))

    assert sheet.skip_reason == SkipReason.HOLIDAY


@pytest.mark.django_db
def test_a_closed_day_is_not_regenerated_into_a_second_version(portfolio):
    saturday = date(2026, 9, 26)

    first = generate(trading_date=saturday, quotes=quotes(AAA=100, BBB=100))
    second = generate(trading_date=saturday, quotes=quotes(AAA=100, BBB=100))

    assert first.pk == second.pk


@pytest.mark.django_db
def test_a_muhurat_session_is_a_trading_day(portfolio):
    """The case a holiday deny-list gets wrong: a real session on a day most
    calendars call a holiday."""
    from datetime import time

    from trading_calendar.models import SessionKind
    from trading_calendar.sessions import set_day

    set_day(
        TUESDAY,
        kind=SessionKind.SPECIAL,
        opens_at=time(18, 15),
        closes_at=time(19, 15),
        note="Muhurat trading",
    )

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert sheet.state == TradeSheetState.DRAFT
    assert sheet.quantity > 0


def _spend(day, gross):
    """A buy the engine paid for, as the journal records one."""
    from journal import accounts
    from journal.models import Book, Component, EntryKind
    from journal.posting import Line, post

    post(
        book=Book.PAPER,
        kind=EntryKind.BUY_FILL,
        occurred_on=day,
        description="buy",
        lines=[
            Line(accounts.INVESTMENTS, Decimal(gross), component=Component.GROSS),
            Line(accounts.SETTLE_ENGINE, -Decimal(gross)),
        ],
    )


@pytest.mark.django_db
def test_regenerating_a_day_does_not_allocate_twice(portfolio):
    from journal.cash import daily_allocation_for
    from journal.models import Book

    generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)
    generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert daily_allocation_for(TUESDAY, book=Book.PAPER) == Decimal("1000")


@pytest.mark.django_db
def test_carry_is_taken_from_the_journal_when_not_stated(portfolio):
    from engine.ledger import settle_day
    from journal.cash import allocate_daily
    from journal.models import Book

    allocate_daily(MONDAY, book=Book.PAPER)
    _spend(MONDAY, "400")
    settle_day(MONDAY, book=Book.PAPER)

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert sheet.carry_in == Decimal("600")
    assert sheet.spendable_cash == Decimal("1600.00")


@pytest.mark.django_db
def test_an_overspend_reduces_what_the_next_day_may_spend(portfolio):
    """D7 reaching the tradesheet: the day is sized after the debt."""
    from journal.cash import allocate_daily
    from journal.models import Book

    allocate_daily(MONDAY, book=Book.PAPER)
    _spend(MONDAY, "1200")

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert sheet.spendable_cash == Decimal("800.00")


@pytest.mark.django_db
def test_a_lumpsum_is_spendable_from_the_next_sheet(portfolio):
    from journal.cash import record_lumpsum

    record_lumpsum(Decimal("5000"), received_on=TUESDAY, actor="operator")

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert sheet.spendable_cash == Decimal("6000.00")


# -- many names a day (M15) and the close as a price (M15e) ----------------


@pytest.mark.django_db
def test_a_day_writes_a_line_for_every_name_it_buys(portfolio):
    sheet = generate(
        trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), carry_in=Decimal("9000")
    )

    lines = sheet.buy_lines()
    assert [(line.instrument_id, line.quantity) for line in lines] == [("BBB", 60), ("AAA", 40)]
    assert sheet.quantity == 100
    assert sheet.instrument_id == "BBB"  # the lead
    assert sheet.planned_cost == Decimal("10000.00")


@pytest.mark.django_db
def test_a_line_sized_at_the_previous_close_says_so(portfolio):
    from engine.selection import Quote

    book = {
        "AAA": Quote("AAA", Decimal("100"), Decimal("100"), source="close"),
        "BBB": Quote("BBB", Decimal("100"), Decimal("100")),
    }

    sheet = generate(trading_date=TUESDAY, quotes=book, carry_in=Decimal("9000"))

    sources = {line.instrument_id: line.price_source for line in sheet.buy_lines()}
    assert sources == {"AAA": "close", "BBB": "ask"}
