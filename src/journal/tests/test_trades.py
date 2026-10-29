from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from journal import accounts
from journal.balances import balance
from journal.models import Book, EntryKind, JournalEntry
from journal.trades import reinvest, settle_trades
from orders.booking import book

DAY = date(2026, 9, 22)


def _sum(lines, code):
    return sum((line.amount for line in lines if line.account_id == code), Decimal("0"))


@pytest.mark.django_db
def test_a_paper_buy_posts_gross_and_every_charge_to_investments(paper_buy_fill):
    # Turnover large enough (>= Rs 5,000) that the SEBI turnover fee -- Rs 10
    # per crore -- rounds to a nonzero paisa and keeps its own line; below
    # that threshold `_checked` would drop it like any other zero line.
    fill = paper_buy_fill(quantity=10, price="1000.00")
    book(fill)
    fill.refresh_from_db()

    entry = JournalEntry.objects.get(kind=EntryKind.BUY_FILL)
    assert entry.book == Book.PAPER
    investment = {
        line.component: line.amount for line in entry.lines.filter(account_id=accounts.INVESTMENTS)
    }
    assert investment["gross"] == Decimal("10000.00")
    assert {"stt", "stamp_duty", "exchange_txn", "sebi", "gst"} <= set(investment)
    total = sum(investment.values())
    assert total == Decimal("10000.00") + fill.charges
    assert balance(accounts.SETTLE_ENGINE, book=Book.PAPER) == -total


@pytest.mark.django_db
def test_booking_a_fill_twice_posts_one_entry(paper_buy_fill):
    fill = paper_buy_fill(quantity=1, price="100")
    book(fill)
    book(fill)
    assert JournalEntry.objects.filter(kind=EntryKind.BUY_FILL).count() == 1


@pytest.mark.django_db
def test_a_sell_posts_net_proceeds_fifo_cost_gain_and_charges(paper_buy_fill, paper_sell_fill):
    book(paper_buy_fill(quantity=2, price="100"))
    sell = paper_sell_fill(quantity=2, price="120")
    book(sell)
    sell.refresh_from_db()

    lines = list(JournalEntry.objects.get(kind=EntryKind.SELL_FILL).lines.all())
    assert _sum(lines, accounts.SETTLE_OTHER) == Decimal("240") - sell.charges
    assert sum(line.amount for line in lines) == 0
    assert balance(accounts.INVESTMENTS, book=Book.PAPER) == 0


@pytest.mark.django_db
def test_a_stored_charge_that_differs_from_the_rate_card_keeps_its_total(paper_buy_fill):
    book(paper_buy_fill(quantity=1, price="100", charges=Decimal("0.5000")))
    entry = JournalEntry.objects.get(kind=EntryKind.BUY_FILL)
    charges = sum(
        line.amount
        for line in entry.lines.filter(account_id=accounts.INVESTMENTS).exclude(component="gross")
    )
    assert charges == Decimal("0.5")


@pytest.mark.django_db
def test_settlement_clears_the_day_into_engine_cash(paper_buy_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    entry = settle_trades(DAY, book=Book.PAPER)
    assert entry.kind == EntryKind.SETTLEMENT
    assert balance(accounts.SETTLE_ENGINE, book=Book.PAPER) == 0
    assert balance(accounts.ENGINE_CASH, book=Book.PAPER) < 0


@pytest.mark.django_db
def test_settling_twice_posts_nothing_the_second_time(paper_buy_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    settle_trades(DAY, book=Book.PAPER)
    assert settle_trades(DAY, book=Book.PAPER) is None


@pytest.mark.django_db
def test_a_late_fill_is_settled_by_a_second_entry(paper_buy_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    settle_trades(DAY, book=Book.PAPER)
    book(paper_buy_fill(quantity=1, price="101"))
    assert settle_trades(DAY, book=Book.PAPER) is not None
    assert balance(accounts.SETTLE_ENGINE, book=Book.PAPER) == 0


@pytest.mark.django_db
def test_sale_proceeds_settle_to_unallocated(paper_buy_fill, paper_sell_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    book(paper_sell_fill(quantity=1, price="120"))
    settle_trades(DAY, book=Book.PAPER)
    assert balance(accounts.SETTLE_OTHER, book=Book.PAPER) == 0
    assert balance(accounts.UNALLOCATED, book=Book.PAPER) > 0


@pytest.mark.django_db
def test_a_reinvesting_batch_allocates_its_net_proceeds(paper_buy_fill, paper_sell_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    sell = paper_sell_fill(quantity=1, price="120", stream="rebalance:b1")
    book(sell)
    sell.refresh_from_db()

    batch = SimpleNamespace(id="b1", stream="rebalance:b1", reinvest=True)
    entries = reinvest(batch, book=Book.PAPER)

    assert len(entries) == 1
    assert balance(accounts.ENGINE_CASH, book=Book.PAPER, source_type="reinvest") == (
        Decimal("120") - sell.charges
    )
    assert balance(accounts.UNALLOCATED, book=Book.PAPER) == 0
    assert reinvest(batch, book=Book.PAPER)[0].pk == entries[0].pk


@pytest.mark.django_db
def test_a_batch_that_does_not_reinvest_allocates_nothing(paper_buy_fill, paper_sell_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    book(paper_sell_fill(quantity=1, price="120", stream="rebalance:b2"))
    batch = SimpleNamespace(id="b2", stream="rebalance:b2", reinvest=False)
    assert reinvest(batch, book=Book.PAPER) == []


def _lot(fill=None, *, quantity=4, remaining=4, pk="lot-1"):
    return SimpleNamespace(
        id=pk,
        created_from_fill=fill,
        quantity=quantity,
        remaining_quantity=remaining,
        instrument_id="INFY",
        acquired_on=DAY,
    )


@pytest.mark.django_db
def test_a_charge_correction_splits_between_shares_held_and_shares_sold(paper_buy_fill):
    from journal.trades import post_charge_correction

    fill = paper_buy_fill(quantity=4, price="1000.00")
    entry = post_charge_correction(
        _lot(fill, remaining=1), Decimal("8"), actor="operator", reference="CN-1", on=DAY
    )

    lines = list(entry.lines.all())
    assert entry.book == Book.PAPER
    assert entry.kind == EntryKind.CHARGE_CORRECTION
    assert _sum(lines, accounts.INVESTMENTS) == Decimal("2")
    assert _sum(lines, accounts.REALISED) == Decimal("6")
    assert _sum(lines, accounts.ENGINE_CASH) == Decimal("-8")


@pytest.mark.django_db
def test_a_charge_correction_is_posted_once_per_lot(paper_buy_fill):
    from journal.trades import post_charge_correction

    lot = _lot(paper_buy_fill(quantity=4, price="1000.00"))
    first = post_charge_correction(lot, Decimal("3"), actor="op", reference="CN", on=DAY)
    second = post_charge_correction(lot, Decimal("3"), actor="op", reference="CN", on=DAY)

    assert first.pk == second.pk


@pytest.mark.django_db
def test_a_charge_correction_on_a_lot_with_no_fill_is_live_unallocated_cash():
    from journal.trades import post_charge_correction
    from portfolio.models import Instrument

    Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")
    entry = post_charge_correction(_lot(), Decimal("-5"), actor="op", reference="CN", on=DAY)

    assert entry.book == Book.LIVE
    assert _sum(entry.lines.all(), accounts.UNALLOCATED) == Decimal("5")


def _live_lot_imported(quantity=10, price="900"):
    """A real lot from before the system -- a transfer in, dated well before DAY."""
    from portfolio.models import Instrument
    from records.external import record_external
    from records.models import ExternalSide

    instrument, _ = Instrument.objects.get_or_create(
        tradingsymbol="INFY", defaults={"instrument_token": 408065, "name": "Infosys"}
    )
    record_external(
        instrument=instrument,
        side=ExternalSide.TRANSFER_IN,
        quantity=quantity,
        occurred_on=date(2026, 1, 5),
        actor="o",
        reason="moved in",
        price=Decimal(price),
    )
    from portfolio.models import Lot

    return Lot.objects.get(external_transaction__isnull=False, superseded_by__isnull=True)


@pytest.mark.django_db
def test_a_paper_sell_consumes_only_paper_lots(paper_buy_fill, paper_sell_fill):
    """I6: the older, real lot is not taken by a paper sale."""
    from journal.external import live_open_lots

    real = _live_lot_imported()
    book(paper_buy_fill(quantity=2, price="100"))
    book(paper_sell_fill(quantity=1, price="120"))

    real.refresh_from_db()
    assert real.remaining_quantity == 10
    assert sum(lot.remaining_quantity for lot in live_open_lots()) == 10
    assert balance(accounts.INVESTMENTS, book=Book.PAPER, instrument_id="INFY") > 0


@pytest.mark.django_db
def test_a_paper_sell_beyond_the_paper_lots_books_nothing(paper_buy_fill, paper_sell_fill):
    """I6 + T6: live lots do not cover a paper shortfall, and no entry posts."""
    real = _live_lot_imported()
    book(paper_buy_fill(quantity=2, price="100"))
    booked = book(paper_sell_fill(quantity=5, price="120"))

    real.refresh_from_db()
    assert booked.already_booked
    assert real.remaining_quantity == 10
    assert not JournalEntry.objects.filter(kind=EntryKind.SELL_FILL).exists()


@pytest.mark.django_db
def test_a_live_sale_consumes_only_live_lots(paper_buy_fill):
    from portfolio.models import Lot
    from records.external import record_external
    from records.models import ExternalSide

    paper = paper_buy_fill(quantity=3, price="100")
    book(paper)
    real = _live_lot_imported()
    record_external(
        instrument=real.instrument,
        side=ExternalSide.SELL,
        quantity=4,
        occurred_on=DAY,
        actor="o",
        reason="sold in Kite",
        price=Decimal("950"),
    )

    real.refresh_from_db()
    assert real.remaining_quantity == 6
    assert Lot.objects.get(created_from_fill=paper).remaining_quantity == 3


@pytest.mark.django_db
def test_a_finalised_paper_lot_stays_paper(paper_buy_fill):
    """I6: a replacement lot's book follows superseded_by back to its fill."""
    from journal.external import live_open_lots
    from portfolio.contract_note import finalise
    from portfolio.models import Lot

    fill = paper_buy_fill(quantity=2, price="100")
    book(fill)
    result = finalise(
        Lot.objects.get(created_from_fill=fill), actual_charges=Decimal("0.5"), actor="op"
    )

    assert result.replacement not in list(live_open_lots())


@pytest.mark.django_db
def test_only_an_estimated_lot_can_be_finalised():
    """T9: an imported lot is the operator's own final record."""
    from portfolio.contract_note import FinalisationRefused, finalise

    real = _live_lot_imported()
    with pytest.raises(FinalisationRefused):
        finalise(real, actual_charges=Decimal("1"), actor="op")


@pytest.mark.django_db
def test_a_replayed_sell_posts_no_second_entry(paper_buy_fill, paper_sell_fill):
    """T6: a sell booked twice consumes once and posts once."""
    book(paper_buy_fill(quantity=2, price="100"))
    sell = paper_sell_fill(quantity=1, price="120")
    book(sell)
    before = JournalEntry.objects.count()

    again = book(sell)

    assert again.already_booked
    assert JournalEntry.objects.count() == before


@pytest.mark.django_db
def test_settling_takes_the_books_own_lock_before_reading(paper_buy_fill):
    """M3: one lock -- the book's -- so settle and post cannot wait on each other
    in opposite orders."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from journal.posting import _NUMBER_LOCKS

    book(paper_buy_fill(quantity=1, price="100"))
    with CaptureQueriesContext(connection) as queries:
        settle_trades(DAY, book=Book.PAPER)

    sqls = [q["sql"] for q in queries]
    locks = [sql for sql in sqls if "pg_advisory_xact_lock" in sql]
    first_read = next(i for i, sql in enumerate(sqls) if "journal_journalline" in sql)
    assert locks and all(str(_NUMBER_LOCKS[Book.PAPER]) in sql for sql in locks)
    assert sqls.index(locks[0]) < first_read


@pytest.mark.django_db
def test_no_difference_posts_no_correction():
    from journal.trades import post_charge_correction

    assert post_charge_correction(_lot(), Decimal("0"), actor="op", reference="CN", on=DAY) is None
