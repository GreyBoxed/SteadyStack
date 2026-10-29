from datetime import date
from decimal import Decimal

import pytest

from journal import accounts, cash
from journal.balances import balance, engine_balance
from journal.models import Book, EntryKind, JournalEntry

MON, TUE, WED, SAT = (date(2026, 9, d) for d in (21, 22, 23, 26))


def live(code):
    return balance(code, book=Book.LIVE)


def _swept_carry():
    """Unallocated 500, engine 2500; then a sweep of all 3000."""
    cash.contribute(Decimal("3000"), on=MON, actor="operator")
    cash.allocate(
        Decimal("2500"), book=Book.LIVE, on=MON, actor="t", description="t", source_type="t"
    )
    cash.withdraw(
        Decimal("3000"), on=TUE, actor="system", reason="quarterly settlement", sweep=True
    )


@pytest.mark.django_db
def test_a_contribution_lands_in_unallocated():
    cash.contribute(Decimal("5000"), on=MON, actor="operator", reference="668124142266")
    assert live(accounts.UNALLOCATED) == Decimal("5000")
    assert live(accounts.CONTRIBUTIONS) == Decimal("-5000")


@pytest.mark.django_db
def test_the_daily_allocation_is_posted_once_per_day_and_book():
    first = cash.allocate_daily(MON, book=Book.PAPER)
    again = cash.allocate_daily(MON, book=Book.PAPER)
    assert first.pk == again.pk
    assert balance(accounts.ENGINE_CASH, book=Book.PAPER) == Decimal("1000")
    assert balance(accounts.PAPER_FUNDING, book=Book.PAPER) == Decimal("-1000")
    assert cash.daily_allocation_for(MON, book=Book.PAPER) == Decimal("1000")


@pytest.mark.django_db
def test_no_allocation_on_a_weekend():
    assert cash.allocate_daily(SAT, book=Book.PAPER) is None


@pytest.mark.django_db
def test_no_allocation_on_a_paused_day(monkeypatch):
    monkeypatch.setattr("configuration.pause.is_paused", lambda on=None: True)
    assert cash.allocate_daily(TUE, book=Book.PAPER) is None


@pytest.mark.django_db
def test_a_live_allocation_comes_out_of_unallocated():
    cash.contribute(Decimal("2000"), on=MON, actor="operator")
    cash.allocate_daily(MON, book=Book.LIVE)
    assert live(accounts.UNALLOCATED) == Decimal("1000")
    assert live(accounts.ENGINE_CASH) == Decimal("1000")


@pytest.mark.django_db
def test_a_lumpsum_in_paper_mode_is_real_money_and_paper_budget():
    result = cash.record_lumpsum(
        Decimal("25000"), received_on=SAT, actor="operator", reference="gift"
    )
    assert result.contribution.book == Book.LIVE
    assert result.allocation.book == Book.PAPER
    assert live(accounts.UNALLOCATED) == Decimal("25000")
    assert engine_balance(Book.PAPER) == Decimal("25000")


@pytest.mark.django_db
def test_a_sweep_takes_unallocated_first_then_engine_carry():
    _swept_carry()
    assert live(accounts.UNALLOCATED) == Decimal("0")
    assert live(accounts.ENGINE_CASH) == Decimal("0")
    assert cash.carry_owed() == Decimal("2500")


@pytest.mark.django_db
def test_the_next_contribution_restores_swept_carry_first():
    _swept_carry()
    entry = cash.contribute(Decimal("4000"), on=TUE, actor="operator")
    assert cash.restored_by(entry) == Decimal("2500")
    assert cash.carry_owed() == Decimal("0")
    assert live(accounts.ENGINE_CASH) == Decimal("2500")
    assert live(accounts.UNALLOCATED) == Decimal("1500")


@pytest.mark.django_db
def test_a_small_contribution_restores_carry_in_part():
    _swept_carry()
    cash.contribute(Decimal("1000"), on=TUE, actor="operator")
    assert live(accounts.ENGINE_CASH) == Decimal("1000")
    assert live(accounts.UNALLOCATED) == Decimal("0")
    assert cash.carry_owed() == Decimal("1500")


@pytest.mark.django_db
def test_a_live_lumpsum_after_a_sweep_allocates_only_what_restoring_left(live_mode):
    _swept_carry()
    result = cash.record_lumpsum(Decimal("10000"), received_on=TUE, actor="operator")
    assert result.restored == Decimal("2500")
    assert live(accounts.ENGINE_CASH) == Decimal("10000")
    assert live(accounts.UNALLOCATED) == Decimal("0")


@pytest.mark.django_db
def test_a_restore_on_a_locked_day_is_next_days_money(monkeypatch):
    """M4 (N3): a lumpsum on a day that has already bought is the next
    trading day's money, and the carry it restores is dated with it -- not
    on arrival, where a replan of the decided day could spend it."""
    from engine.ledger import opening_for

    _swept_carry()
    monkeypatch.setattr("engine.lock.why_locked", lambda day: "bought" if day == TUE else None)

    result = cash.record_lumpsum(Decimal("4000"), received_on=TUE, actor="operator")

    restore = JournalEntry.objects.get(idempotency_key=f"{cash.RESTORE}:{result.contribution.id}")
    assert result.restored == Decimal("2500")
    assert result.contribution.occurred_on == TUE
    assert restore.occurred_on == WED
    assert result.allocation.occurred_on == WED
    assert opening_for(TUE, book=Book.LIVE).carry == Decimal("0")
    assert cash.carry_owed(through=TUE) == Decimal("2500")
    assert cash.carry_owed(through=WED) == Decimal("0")


@pytest.mark.django_db
def test_a_withdrawal_larger_than_unallocated_spills_into_engine_cash():
    cash.contribute(Decimal("3000"), on=MON, actor="operator")
    cash.allocate(
        Decimal("2500"), book=Book.LIVE, on=MON, actor="t", description="t", source_type="t"
    )
    cash.withdraw(Decimal("1000"), on=TUE, actor="operator", reason="refund")
    assert live(accounts.UNALLOCATED) == Decimal("0")
    assert live(accounts.ENGINE_CASH) == Decimal("2000")
    assert cash.carry_owed() == Decimal("0")


@pytest.mark.django_db
def test_a_withdrawal_within_unallocated_touches_only_unallocated():
    cash.contribute(Decimal("3000"), on=MON, actor="operator")
    entry = cash.withdraw(Decimal("500"), on=TUE, actor="operator", reason="refund")
    assert live(accounts.UNALLOCATED) == Decimal("2500")
    assert {line.account_id for line in entry.lines.all()} == {
        accounts.WITHDRAWALS,
        accounts.UNALLOCATED,
    }


@pytest.mark.django_db
def test_record_lumpsum_is_idempotent():
    kwargs = {
        "amount": Decimal("5000"),
        "received_on": MON,
        "actor": "operator",
        "reference": "gift",
        "idempotency_key": "lumpsum-1",
    }
    first = cash.record_lumpsum(**kwargs)
    second = cash.record_lumpsum(**kwargs)
    assert first.contribution.pk == second.contribution.pk
    assert first.allocation.pk == second.allocation.pk
    assert JournalEntry.objects.filter(book=Book.LIVE, kind=EntryKind.CONTRIBUTION).count() == 1
    assert JournalEntry.objects.filter(book=Book.PAPER, kind=EntryKind.ALLOCATION).count() == 1
    assert live(accounts.UNALLOCATED) == Decimal("5000")
    assert engine_balance(Book.PAPER) == Decimal("5000")


@pytest.mark.django_db
def test_a_paper_lumpsum_after_a_sweep_still_reports_what_it_restored():
    _swept_carry()
    result = cash.record_lumpsum(Decimal("10000"), received_on=TUE, actor="operator")
    assert result.restored == Decimal("2500")
    assert result.allocation.book == Book.PAPER
    assert engine_balance(Book.PAPER) == Decimal("10000")
    assert cash.carry_owed() == Decimal("0")


@pytest.mark.django_db
def test_an_account_charge_is_an_expense_paid_from_unallocated():
    cash.account_charge(
        Decimal("88.5"), on=MON, component="amc", actor="operator", description="AMC"
    )
    assert live(accounts.ACCOUNT_CHARGES) == Decimal("88.5")
    assert live(accounts.UNALLOCATED) == Decimal("-88.5")


@pytest.mark.django_db
def test_an_allocation_can_name_its_source_account():
    cash.allocate(
        Decimal("5"),
        book=Book.PAPER,
        on=MON,
        actor="t",
        description="t",
        source_type="t",
        source_account=accounts.UNALLOCATED,
    )
    assert balance(accounts.UNALLOCATED, book=Book.PAPER) == Decimal("-5")


@pytest.mark.django_db
def test_a_lumpsum_on_a_day_that_has_bought_is_allocated_to_the_next_trading_day(
    paper_buy_fill,
):
    """I1 (M8a/M15i): a replan spends only what the day already had."""
    from journal.tests.conftest import DAY
    from trading_calendar.sessions import next_trading_day

    paper_buy_fill(1, "100")  # the day's daily buy has filled: DAY is locked
    before = engine_balance(Book.PAPER, through=DAY)
    result = cash.record_lumpsum(Decimal("5000"), received_on=DAY, actor="operator")
    assert result.contribution.occurred_on == DAY
    assert result.allocation.occurred_on == next_trading_day(DAY)
    assert engine_balance(Book.PAPER, through=DAY) == before


@pytest.mark.django_db
def test_a_lumpsum_on_an_open_day_is_that_days_money():
    result = cash.record_lumpsum(Decimal("5000"), received_on=TUE, actor="operator")
    assert result.allocation.occurred_on == TUE


@pytest.mark.django_db
def test_a_lumpsum_that_fails_to_allocate_leaves_no_contribution(monkeypatch):
    """I2: contribution and allocation are one transaction."""

    def refuse(*args, **kwargs):
        raise cash.CashError("allocation failed")

    monkeypatch.setattr(cash, "allocate", refuse)
    with pytest.raises(cash.CashError):
        cash.record_lumpsum(Decimal("5000"), received_on=TUE, actor="operator")
    assert not JournalEntry.objects.exists()


@pytest.mark.django_db
def test_a_contribution_that_fails_to_restore_leaves_nothing(monkeypatch):
    _swept_carry()
    before = JournalEntry.objects.count()

    def refuse(*args, **kwargs):
        raise cash.CashError("restore failed")

    monkeypatch.setattr(cash, "allocate", refuse)
    with pytest.raises(cash.CashError):
        cash.contribute(Decimal("4000"), on=TUE, actor="operator")
    assert JournalEntry.objects.count() == before


@pytest.mark.django_db
def test_a_sweep_splits_on_what_unallocated_held_on_its_own_date():
    """I3a: a contribution dated after the sweep does not change the split."""
    cash.contribute(Decimal("3000"), on=MON, actor="operator")
    cash.allocate(
        Decimal("2500"), book=Book.LIVE, on=MON, actor="t", description="t", source_type="t"
    )
    # Confirmed out of order: a later contribution is recorded first.
    cash.contribute(Decimal("9000"), on=date(2026, 9, 24), actor="operator")
    cash.withdraw(Decimal("3000"), on=TUE, actor="system", reason="sweep", sweep=True)
    assert cash.carry_owed() == Decimal("2500")


@pytest.mark.django_db
def test_a_restore_reads_carry_owed_through_its_own_date():
    """I3a: a contribution dated before the sweep repays nothing of it."""
    cash.contribute(Decimal("3000"), on=MON, actor="operator")
    cash.allocate(
        Decimal("2500"), book=Book.LIVE, on=MON, actor="t", description="t", source_type="t"
    )
    cash.withdraw(Decimal("3000"), on=TUE, actor="system", reason="sweep", sweep=True)
    early = cash.contribute(Decimal("1000"), on=MON, actor="operator")
    assert cash.restored_by(early) == Decimal("0")
    assert cash.carry_owed() == Decimal("2500")


@pytest.mark.django_db
def test_a_reversed_sweep_leaves_no_carry_owed():
    """I3b: reversing a mis-confirmed sweep also cancels what it left owed."""
    from journal.posting import reverse

    cash.contribute(Decimal("3000"), on=MON, actor="operator")
    cash.allocate(
        Decimal("2500"), book=Book.LIVE, on=MON, actor="t", description="t", source_type="t"
    )
    sweep = cash.withdraw(Decimal("3000"), on=TUE, actor="system", reason="sweep", sweep=True)
    reverse(sweep, reason="was an owner withdrawal", actor="operator")
    assert cash.carry_owed() == Decimal("0")
    entry = cash.contribute(Decimal("1000"), on=date(2026, 9, 24), actor="operator")
    assert cash.restored_by(entry) == Decimal("0")


@pytest.mark.django_db
def test_a_reversed_restore_is_owed_again():
    from journal.posting import reverse

    _swept_carry()
    entry = cash.contribute(Decimal("4000"), on=TUE, actor="operator")
    restore = JournalEntry.objects.get(idempotency_key=f"{cash.RESTORE}:{entry.id}")
    reverse(restore, reason="mistake", actor="operator")
    assert cash.carry_owed() == Decimal("2500")


@pytest.mark.django_db
def test_amounts_must_be_positive():
    with pytest.raises(cash.CashError):
        cash.contribute(Decimal("0"), on=MON, actor="operator")
    with pytest.raises(cash.CashError):
        cash.withdraw(Decimal("-1"), on=MON, actor="operator", reason="x")


@pytest.mark.django_db
def test_an_owner_expense_taken_from_engine_cash_is_owed_back():
    """M10: the Kite Connect fee spills into engine carry when unallocated is empty."""
    cash.contribute(Decimal("3000"), on=MON, actor="operator")
    cash.allocate(
        Decimal("2800"), book=Book.LIVE, on=MON, actor="t", description="t", source_type="t"
    )
    cash.withdraw(
        Decimal("500"),
        on=TUE,
        actor="operator",
        reason="Kite Connect API Charges",
        source_type="owner_expense",
    )
    assert live(accounts.ENGINE_CASH) == Decimal("2500")
    assert cash.carry_owed() == Decimal("300")

    # The next contribution repays it before anything else, as after a sweep.
    later = cash.contribute(Decimal("1000"), on=date(2026, 9, 23), actor="operator")
    assert cash.restored_by(later) == Decimal("300")
    assert cash.carry_owed() == 0
