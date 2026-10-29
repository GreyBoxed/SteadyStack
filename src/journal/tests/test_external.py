from datetime import date
from decimal import Decimal

import pytest

from journal import accounts
from journal.balances import balance
from journal.external import open_books, record_dividend
from journal.models import Book, EntryKind, JournalEntry
from records.external import record_external
from records.models import ExternalSide, PaidFrom

DAY = date(2026, 9, 22)


def live(code):
    return balance(code, book=Book.LIVE)


@pytest.mark.django_db
def test_a_direct_buy_at_zerodha_is_paid_from_unallocated_once_settled(infy):
    record_external(
        instrument=infy,
        side=ExternalSide.BUY,
        quantity=2,
        occurred_on=DAY,
        actor="operator",
        reason="bought in Kite",
        price=Decimal("100"),
        charges=Decimal("0.5"),
    )
    assert live(accounts.INVESTMENTS) == Decimal("200.5")
    assert live(accounts.UNALLOCATED) == Decimal("-200.5")
    assert live(accounts.SETTLE_OTHER) == 0


@pytest.mark.django_db
def test_an_ipo_paid_from_the_bank_is_a_contribution_in_shares(infy):
    record_external(
        instrument=infy,
        side=ExternalSide.BUY,
        quantity=10,
        occurred_on=DAY,
        actor="operator",
        reason="IPO allotment",
        price=Decimal("50"),
        paid_from=PaidFrom.BANK,
    )
    assert live(accounts.INVESTMENTS) == Decimal("500")
    assert live(accounts.CONTRIBUTIONS) == Decimal("-500")
    assert live(accounts.UNALLOCATED) == 0


@pytest.mark.django_db
def test_a_transfer_in_is_a_contribution_at_the_transferors_basis(infy):
    record_external(
        instrument=infy,
        side=ExternalSide.TRANSFER_IN,
        quantity=1,
        occurred_on=DAY,
        actor="operator",
        reason="gift",
        price=Decimal("900"),
        acquired_on=date(2020, 1, 1),
    )
    assert live(accounts.CONTRIBUTIONS) == Decimal("-900")


@pytest.mark.django_db
def test_a_sale_at_the_broker_books_proceeds_and_gain(infy):
    record_external(
        instrument=infy,
        side=ExternalSide.BUY,
        quantity=1,
        occurred_on=DAY,
        actor="o",
        reason="r",
        price=Decimal("100"),
    )
    record_external(
        instrument=infy,
        side=ExternalSide.SELL,
        quantity=1,
        occurred_on=date(2026, 9, 23),
        actor="o",
        reason="sold in Kite",
        price=Decimal("130"),
        charges=Decimal("1"),
    )
    assert live(accounts.INVESTMENTS) == 0
    assert live(accounts.REALISED) == Decimal("-29")  # 130 - 100 - 1
    assert live(accounts.UNALLOCATED) == Decimal("29")


@pytest.mark.django_db
def test_a_transfer_out_is_a_withdrawal_in_shares_at_cost(infy):
    record_external(
        instrument=infy,
        side=ExternalSide.BUY,
        quantity=1,
        occurred_on=DAY,
        actor="o",
        reason="r",
        price=Decimal("100"),
    )
    record_external(
        instrument=infy,
        side=ExternalSide.TRANSFER_OUT,
        quantity=1,
        occurred_on=DAY,
        actor="o",
        reason="moved",
    )
    assert live(accounts.INVESTMENTS) == 0
    assert live(accounts.WITHDRAWALS) == Decimal("100")


@pytest.mark.django_db
def test_a_dividend_is_income_withdrawn_to_the_owner(infy):
    record_dividend("INFY", Decimal("42"), paid_on=DAY, actor="operator")
    assert live(accounts.DIVIDENDS) == Decimal("-42")
    assert live(accounts.WITHDRAWALS) == Decimal("42")


@pytest.mark.django_db
def test_a_replayed_dividend_posts_once(infy):
    key = "dividend:infy-2026-09-22"
    first = record_dividend(
        "INFY", Decimal("42"), paid_on=DAY, actor="operator", idempotency_key=key
    )
    second = record_dividend(
        "INFY", Decimal("42"), paid_on=DAY, actor="operator", idempotency_key=key
    )
    assert first.id == second.id
    assert live(accounts.DIVIDENDS) == Decimal("-42")


@pytest.mark.django_db
def test_the_books_open_once_with_cash_and_the_live_lots(infy):
    record_external(
        instrument=infy,
        side=ExternalSide.TRANSFER_IN,
        quantity=1,
        occurred_on=DAY,
        actor="o",
        reason="r",
        price=Decimal("900"),
    )
    before = JournalEntry.objects.count()
    entry = open_books(cash=Decimal("-150.14"), on=date(2026, 9, 24), actor="operator")
    assert entry.kind == EntryKind.OPENING_BALANCE
    assert JournalEntry.objects.count() == before + 1
    with pytest.raises(ValueError, match="already"):
        open_books(cash=Decimal("0"), on=date(2026, 9, 24), actor="operator")


@pytest.mark.django_db
def test_opening_does_not_count_a_lot_the_journal_already_holds(infy):
    record_external(
        instrument=infy,
        side=ExternalSide.TRANSFER_IN,
        quantity=1,
        occurred_on=DAY,
        actor="o",
        reason="r",
        price=Decimal("900"),
    )
    open_books(cash=Decimal("100"), on=date(2026, 9, 24), actor="operator")
    assert live(accounts.INVESTMENTS) == Decimal("900")
    assert live(accounts.OPENING) == Decimal("-100")


@pytest.mark.django_db
def test_opening_nets_the_live_cash_the_journal_already_holds(infy):
    """C1: a paper period records real money in the live book; opening tops up to --cash."""
    from journal.balances import zerodha_cash
    from journal.cash import record_lumpsum

    record_lumpsum(Decimal("5000"), received_on=date(2026, 9, 15), actor="o", reference="upi-1")
    record_dividend("INFY", Decimal("40"), paid_on=date(2026, 9, 16), actor="o")
    record_external(
        instrument=infy,
        side=ExternalSide.BUY,
        quantity=2,
        occurred_on=DAY,
        actor="o",
        reason="bought in Kite",
        price=Decimal("100"),
        charges=Decimal("0.5"),
    )
    # The journal holds 5000 - 200.50 = 4799.50; Zerodha shows 4899.50.
    open_books(cash=Decimal("4899.50"), on=date(2026, 9, 24), actor="operator")
    assert zerodha_cash(Book.LIVE) == Decimal("4899.5")
    assert live(accounts.OPENING) == Decimal("-100")
    assert live(accounts.INVESTMENTS) == Decimal("200.5")


@pytest.mark.django_db
def test_opening_with_nothing_to_add_still_opens_the_book(infy):
    """The live checks wait for an opening entry, so it is posted even at zero."""
    from journal.cash import record_lumpsum
    from journal.external import live_book_opened
    from journal.posting import reverse

    record_lumpsum(Decimal("5000"), received_on=date(2026, 9, 15), actor="o", reference="upi-1")
    entry = open_books(cash=Decimal("5000"), on=date(2026, 9, 24), actor="operator")
    assert live_book_opened()
    assert {line.amount for line in entry.lines.all()} == {0}
    assert live(accounts.UNALLOCATED) == Decimal("5000")
    reverse(entry, reason="opened too early", actor="operator")
    assert not live_book_opened()


@pytest.mark.django_db
def test_a_reversed_opening_lets_the_books_open_again(infy):
    from journal.balances import zerodha_cash
    from journal.posting import reverse

    first = open_books(cash=Decimal("100"), on=date(2026, 9, 24), actor="operator")
    reverse(first, reason="wrong figure", actor="operator", on=date(2026, 9, 25))
    second = open_books(cash=Decimal("250"), on=date(2026, 9, 24), actor="operator")
    assert second.id != first.id
    assert zerodha_cash(Book.LIVE) == Decimal("250")
    with pytest.raises(ValueError, match="already"):
        open_books(cash=Decimal("1"), on=date(2026, 9, 24), actor="operator")


@pytest.mark.django_db
def test_the_open_books_command_posts_the_difference(infy):
    from django.core.management import CommandError, call_command

    from journal.balances import zerodha_cash
    from journal.cash import record_lumpsum

    record_lumpsum(Decimal("5000"), received_on=date(2026, 9, 15), actor="o", reference="upi-1")
    call_command("open_books", "--cash", "5200", "--on", "2026-09-24", "--actor", "operator")
    assert zerodha_cash(Book.LIVE) == Decimal("5200")
    with pytest.raises(CommandError, match="already"):
        call_command("open_books", "--cash", "5200", "--on", "2026-09-24", "--actor", "operator")


# --- Go-live round 1, wave B -------------------------------------------


def _paper_period_with_a_sweep():
    """T3: before go-live, a lumpsum of 3,000 and a confirmed sweep of 8,000.

    The paper engine received the lumpsum, so live unallocated holds only 3,000
    when the 8,000 sweep arrives: 5,000 of it spills into live engine cash.
    """
    from journal.cash import record_lumpsum, withdraw

    record_lumpsum(Decimal("3000"), received_on=date(2026, 9, 1), actor="o", reference="upi-1")
    withdraw(Decimal("8000"), on=date(2026, 9, 4), actor="o", reason="payout", sweep=True)


@pytest.mark.django_db
def test_open_books_zeroes_the_live_engine_accounts(infy):
    from journal.balances import engine_balance, zerodha_cash

    _paper_period_with_a_sweep()
    assert engine_balance(Book.LIVE) == Decimal("-5000")

    open_books(cash=Decimal("20000"), on=date(2026, 9, 24), actor="operator")

    assert zerodha_cash(Book.LIVE) == Decimal("20000")
    assert live(accounts.ENGINE_CASH) == 0
    assert live(accounts.SETTLE_ENGINE) == 0
    assert engine_balance(Book.LIVE) == 0
    assert live(accounts.UNALLOCATED) == Decimal("20000")


@pytest.mark.django_db
def test_a_sweep_before_the_opening_owes_no_carry(infy):
    from journal.cash import carry_owed, withdraw

    _paper_period_with_a_sweep()
    assert carry_owed() == Decimal("5000")

    open_books(cash=Decimal("20000"), on=date(2026, 9, 24), actor="operator")

    assert carry_owed() == 0
    # A sweep after the opening is owed as before.
    withdraw(Decimal("20500"), on=date(2026, 10, 2), actor="o", reason="payout", sweep=True)
    assert carry_owed() == Decimal("500")


@pytest.mark.django_db
def test_an_opening_reversal_is_dated_on_the_opening_date(infy, time_machine):
    from journal.posting import reverse

    time_machine.move_to("2026-10-05 12:00:00+05:30")
    opening = open_books(cash=Decimal("100"), on=date(2026, 9, 24), actor="operator")
    reversal = reverse(opening, reason="wrong figure", actor="operator")
    assert reversal.occurred_on == date(2026, 9, 24)


@pytest.mark.django_db
def test_an_opening_in_a_closed_year_is_reversed_today(infy, time_machine):
    from journal.models import PeriodClose
    from journal.posting import reverse

    time_machine.move_to("2026-10-05 12:00:00+05:30")
    opening = open_books(cash=Decimal("100"), on=date(2026, 3, 20), actor="operator")
    PeriodClose.objects.create(book=Book.LIVE, fy_end=date(2026, 3, 31), closed_by="ca")
    reversal = reverse(opening, reason="wrong figure", actor="operator")
    assert reversal.occurred_on == date(2026, 10, 5)


# --- Go-live round 2, wave E -------------------------------------------


@pytest.mark.django_db
def test_a_real_event_dated_inside_the_opening_is_refused(infy):
    """NI2: the opening's --cash already holds every event dated on or before it."""
    from journal.cash import allocate, record_lumpsum
    from journal.posting import JournalError, reverse

    open_books(cash=Decimal("5000"), on=date(2026, 9, 24), actor="operator")
    before = JournalEntry.objects.filter(book=Book.LIVE).count()

    with pytest.raises(JournalError, match="on or before the opening balance of 2026-09-24"):
        record_lumpsum(Decimal("1000"), received_on=date(2026, 9, 24), actor="o", reference="u")
    # m-2: a dividend never touches Zerodha's cash; the reason given is that
    # the live books begin at the opening, not that Zerodha's cash holds it.
    with pytest.raises(JournalError) as refused:
        record_dividend("INFY", Decimal("40"), paid_on=date(2026, 9, 20), actor="o")
    assert str(refused.value) == (
        "2026-09-20 is on or before the opening balance of 2026-09-24; the live books begin there."
    )
    with pytest.raises(JournalError, match="on or before the opening balance"):
        record_external(
            instrument=infy,
            side=ExternalSide.BUY,
            quantity=1,
            occurred_on=date(2026, 9, 23),
            actor="o",
            reason="bought in Kite",
            price=Decimal("100"),
        )
    assert JournalEntry.objects.filter(book=Book.LIVE).count() == before

    # The day after is the live book's own, and the paper book is not bound.
    record_dividend("INFY", Decimal("40"), paid_on=date(2026, 9, 25), actor="o")
    allocate(
        Decimal("10"),
        book=Book.PAPER,
        on=date(2026, 9, 20),
        actor="o",
        description="paper",
        source_type="daily",
    )
    # The opening's own reversal is dated on it, and is allowed.
    opening = JournalEntry.objects.get(kind=EntryKind.OPENING_BALANCE)
    assert reverse(opening, reason="wrong figure", actor="o").occurred_on == date(2026, 9, 24)
