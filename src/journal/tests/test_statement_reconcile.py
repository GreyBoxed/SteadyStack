from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from journal import accounts, cash
from journal.balances import balance
from journal.models import Book, JournalEntry, StatementLine
from journal.statement.classify import propose
from journal.statement.reconcile import auto_match, confirm, queue
from journal.statement.store import store_upload

FIXTURES = Path(__file__).parent / "fixtures"
AUG = (FIXTURES / "statement_aug.csv").read_bytes()
SEP = (FIXTURES / "statement_sep.csv").read_bytes()


def line(text):
    return StatementLine.objects.get(particulars__startswith=text)


@pytest.mark.django_db
def test_each_known_line_gets_the_proposal_the_spec_table_gives():
    store_upload(AUG, filename="aug.csv", actor="o")
    store_upload(SEP, filename="sep.csv", actor="o")
    assert (
        propose(line("Funds added using UPI from ABC123 with reference number 111111111111")).kind
        == "contribution"
    )
    assert (
        propose(
            line("Funds added using UPI from ABC123 with reference number 111111111111")
        ).reference
        == "111111111111"
    )
    assert propose(line("Net settlement")).kind == "settlement"
    assert propose(line("AMC for Demat")).component == "amc"
    assert propose(line("Delayed payment")).component == "interest"
    assert propose(line("Charges for enabling DDPI")).component == "ddpi"
    assert propose(line("Kite Connect API")).kind == "owner_expense"
    assert propose(line("Payout done for quarterly settlement")).kind == "sweep"


@pytest.mark.django_db
def test_a_recorded_lumpsum_matches_its_upi_line():
    cash.record_lumpsum(
        Decimal("500"), received_on=date(2026, 8, 14), actor="operator", reference="111111111111"
    )
    before = JournalEntry.objects.count()

    stored = store_upload(AUG, filename="aug.csv", actor="o")
    auto_match(stored.upload)

    upi = line("Funds added using UPI")
    assert upi.matched_entry is not None
    assert upi.matched_entry.kind == "contribution"
    assert JournalEntry.objects.count() == before
    assert balance(accounts.CONTRIBUTIONS, book=Book.LIVE) == Decimal("-500")


@pytest.mark.django_db
def test_a_reference_match_still_checks_the_amount():
    cash.record_lumpsum(
        Decimal("400"), received_on=date(2026, 8, 14), actor="operator", reference="111111111111"
    )
    stored = store_upload(AUG, filename="aug.csv", actor="o")
    auto_match(stored.upload)

    upi = line("Funds added using UPI")
    assert upi.matched_entry is None
    assert upi in list(queue())


@pytest.mark.django_db
def test_a_referenceless_contribution_is_matched_nearby_by_amount():
    cash.contribute(Decimal("500"), on=date(2026, 8, 12), actor="o")
    stored = store_upload(AUG, filename="aug.csv", actor="o")
    auto_match(stored.upload)

    upi = line("Funds added using UPI")
    assert upi.matched_entry is not None
    assert upi.matched_entry.kind == "contribution"


@pytest.mark.django_db
def test_two_equal_nearby_candidates_are_not_linked():
    cash.contribute(Decimal("500"), on=date(2026, 8, 12), actor="o")
    cash.contribute(Decimal("500"), on=date(2026, 8, 13), actor="o")
    stored = store_upload(AUG, filename="aug.csv", actor="o")
    auto_match(stored.upload)

    upi = line("Funds added using UPI")
    assert upi.matched_entry is None
    assert upi in list(queue())


@pytest.mark.django_db
def test_an_unrecorded_transfer_waits_in_the_queue_until_confirmed():
    stored = store_upload(AUG, filename="aug.csv", actor="o")
    auto_match(stored.upload)
    upi = line("Funds added using UPI")
    assert upi in list(queue())

    entry = confirm(upi, kind="contribution", actor="operator")
    upi.refresh_from_db()
    assert upi.matched_entry == entry
    assert upi not in list(queue())
    assert balance(accounts.CONTRIBUTIONS, book=Book.LIVE) == Decimal("-500")


@pytest.mark.django_db
def test_confirming_the_same_line_twice_posts_once():
    store_upload(AUG, filename="aug.csv", actor="o")
    amc = line("AMC for Demat")
    first = confirm(amc, kind="account_charge", component="amc", actor="operator")
    second = confirm(amc, kind="account_charge", component="amc", actor="operator")
    assert first.pk == second.pk


@pytest.mark.django_db
def test_a_mis_confirmed_line_can_be_reversed_and_confirmed_again():
    """I4: reversing the entry puts the line back in the queue for the right kind."""
    from journal.posting import reverse

    store_upload(AUG, filename="aug.csv", actor="o")
    store_upload(SEP, filename="sep.csv", actor="o")
    fee = line("Kite Connect API")
    wrong = confirm(fee, kind="account_charge", component="other", actor="operator")
    reverse(wrong, reason="was the owner's expense", actor="operator")

    assert fee in list(queue())
    right = confirm(fee, kind="owner_expense", actor="operator")

    fee.refresh_from_db()
    assert right.pk != wrong.pk
    assert fee.matched_entry == right
    assert fee not in list(queue())
    assert balance(accounts.ACCOUNT_CHARGES, book=Book.LIVE) == 0
    assert balance(accounts.WITHDRAWALS, book=Book.LIVE) == Decimal("500.00022")
    # Confirming again returns the standing entry.
    assert confirm(fee, kind="owner_expense", actor="operator").pk == right.pk


@pytest.mark.django_db
def test_auto_match_relinks_a_line_whose_entry_was_reversed():
    from journal.posting import reverse
    from journal.statement.store import link

    stored = store_upload(AUG, filename="aug.csv", actor="o")
    upi = line("Funds added using UPI")
    wrong = cash.withdraw(Decimal("1"), on=upi.posting_date, actor="o", reason="x")
    link(upi, wrong)
    reverse(wrong, reason="wrong", actor="o")
    right = cash.contribute(upi.credit, on=upi.posting_date, actor="o")

    auto_match(stored.upload)

    upi.refresh_from_db()
    assert upi.matched_entry == right


@pytest.mark.django_db
def test_the_kite_connect_fee_is_the_owners_cost():
    store_upload(AUG, filename="aug.csv", actor="o")
    store_upload(SEP, filename="sep.csv", actor="o")
    confirm(line("Kite Connect API"), kind="owner_expense", actor="operator")
    assert balance(accounts.WITHDRAWALS, book=Book.LIVE) == Decimal("500.00022")
    assert balance(accounts.ACCOUNT_CHARGES, book=Book.LIVE) == 0


@pytest.mark.django_db
def test_confirming_a_sweep_leaves_carry_owed(live_mode):
    cash.contribute(Decimal("400"), on=date(2026, 9, 1), actor="o")
    cash.allocate(
        Decimal("300"),
        book=Book.LIVE,
        on=date(2026, 9, 1),
        actor="o",
        description="t",
        source_type="t",
    )
    store_upload(AUG, filename="aug.csv", actor="o")
    store_upload(SEP, filename="sep.csv", actor="o")
    confirm(line("Payout done for quarterly settlement"), kind="sweep", actor="operator")
    assert cash.carry_owed() > 0


@pytest.mark.django_db
def test_a_debit_cannot_be_confirmed_as_a_contribution():
    from journal.statement.reconcile import ReconcileError

    store_upload(AUG, filename="aug.csv", actor="o")
    with pytest.raises(ReconcileError):
        confirm(line("AMC for Demat"), kind="contribution", actor="operator")


SETTLEMENT_TEXT = "Net settlement for Equity with settlement number: 2026153"


def _settlement_line(amount, *, day="2026-08-14"):
    """Upload one net-settlement row of *amount* (signed as cash moved) and match it."""
    from journal.statement.reconcile import auto_match as match
    from journal.tests.conftest import csv_of

    debit, credit = (str(-amount), "0") if amount < 0 else ("0", str(amount))
    stored = store_upload(
        csv_of("0", [(SETTLEMENT_TEXT, day, debit, credit)], voucher_type="Book Voucher"),
        filename="s.csv",
        actor="o",
    )
    match(stored.upload)
    return StatementLine.objects.get(particulars=SETTLEMENT_TEXT)


def _our_settlement():
    cash_accounts = [accounts.ENGINE_CASH, accounts.UNALLOCATED]
    return balance(cash_accounts, book=Book.LIVE, kinds=["settlement"])


@pytest.mark.django_db
def test_a_settlement_that_agrees_posts_nothing(live_buy_fill):
    from journal.statement.reconcile import correct_settlement, settlement_difference

    live_buy_fill(quantity=1, price="240")
    line = _settlement_line(_our_settlement())

    assert line.matched_entry.kind == "settlement"
    assert settlement_difference(line) == 0
    assert correct_settlement(line, actor="o") == []


@pytest.mark.django_db
def test_a_settlement_that_took_more_raises_the_lot_cost_and_finalises_it(live_buy_fill):
    from journal.statement.reconcile import correct_settlement, settlement_difference
    from portfolio.models import BasisState, Lot

    live_buy_fill(quantity=1, price="240")
    ours = _our_settlement()
    theirs = ours - Decimal("0.21")  # Zerodha took 21 paise more
    line = _settlement_line(theirs)
    before = Lot.objects.get().cost_per_unit

    assert settlement_difference(line) == Decimal("-0.21")
    posted = correct_settlement(line, actor="o")

    assert [entry.kind for entry in posted] == ["charge_correction"]
    final = Lot.objects.get(superseded_by__isnull=True)
    assert final.basis_state == BasisState.FINAL
    assert final.cost_per_unit == before + Decimal("0.21")
    assert balance(accounts.ENGINE_CASH, book=Book.LIVE) == theirs
    assert settlement_difference(line) == 0
    assert correct_settlement(line, actor="o") == []  # idempotent


@pytest.mark.django_db
def test_a_settlement_correction_is_dated_by_the_journals_own_today(live_buy_fill, monkeypatch):
    """M10: the one helper the rest of the journal dates by, not timezone.localdate."""
    from journal.statement import reconcile
    from portfolio import contract_note

    known = date(2026, 9, 23)
    monkeypatch.setattr(reconcile, "today_ist", lambda: known)
    monkeypatch.setattr(contract_note, "today_ist", lambda: known)
    live_buy_fill(quantity=1, price="240")
    line = _settlement_line(_our_settlement() - Decimal("0.21"))

    posted = reconcile.correct_settlement(line, actor="o")

    assert [entry.occurred_on for entry in posted] == [known]


@pytest.mark.django_db
def test_a_difference_is_split_by_turnover_and_the_last_fill_takes_the_remainder(live_buy_fill):
    from journal.statement.reconcile import correct_settlement
    from portfolio.models import Lot

    small = live_buy_fill(quantity=1, price="100")
    large = live_buy_fill(quantity=2, price="100")
    line = _settlement_line(_our_settlement() - Decimal("0.10"))

    correct_settlement(line, actor="o")

    corrections = JournalEntry.objects.filter(kind="charge_correction").order_by("number")
    amounts = [
        sum(ln.amount for ln in entry.lines.filter(account_id=accounts.INVESTMENTS))
        for entry in corrections
    ]
    # 100 of 300 turnover takes a third, rounded; the last fill takes the rest.
    assert [entry.source_id for entry in corrections] == [str(small.lot.id), str(large.lot.id)]
    assert amounts == [Decimal("0.033333"), Decimal("0.066667")]
    assert Lot.objects.filter(superseded_by__isnull=True, basis_state="final").count() == 2


@pytest.mark.django_db
def test_a_lot_already_final_takes_its_share_as_a_charge_correction(live_buy_fill):
    from journal.statement.reconcile import correct_settlement
    from portfolio.contract_note import estimated_charges, finalise
    from portfolio.models import Lot

    fill = live_buy_fill(quantity=1, price="240")
    finalise(fill.lot, actual_charges=estimated_charges(fill.lot), actor="o")
    line = _settlement_line(_our_settlement() - Decimal("0.05"))

    posted = correct_settlement(line, actor="o")

    final = Lot.objects.get(superseded_by__isnull=True)
    assert len(posted) == 1
    assert posted[0].idempotency_key == f"settlement_correction:{line.fingerprint}:{final.id}"
    # Engine money paid for the buy, so engine money pays the difference.
    assert balance(accounts.ENGINE_CASH, book=Book.LIVE) == _our_settlement() - Decimal("0.05")
    assert correct_settlement(line, actor="o") == []


@pytest.mark.django_db
def test_a_sells_share_goes_to_realised_and_the_dp_line_is_added_back(
    live_buy_fill, live_sell_fill
):
    from journal.statement.reconcile import correct_settlement, settlement_difference

    live_buy_fill(quantity=2, price="100")
    live_sell_fill(quantity=1, price="100")
    # Zerodha debits DP on its own line, so its settlement is ours without DP.
    theirs = _our_settlement() + Decimal("15.34")
    line = _settlement_line(theirs + Decimal("0.02"))  # and gave 2 paise back

    assert settlement_difference(line) == Decimal("0.02")
    correct_settlement(line, actor="o")

    # 100 of 300 turnover is the sale's, and it is the last fill: the remainder.
    on_the_sale = balance(
        accounts.REALISED, book=Book.LIVE, kinds=["charge_correction"], source_type="fill"
    )
    assert on_the_sale == Decimal("-0.006667")
    cash_back = balance(
        [accounts.ENGINE_CASH, accounts.UNALLOCATED], book=Book.LIVE, kinds=["charge_correction"]
    )
    assert cash_back == Decimal("0.02")
    assert settlement_difference(line) == 0


@pytest.mark.django_db
def test_a_dp_line_matches_the_live_sell_that_carries_it(live_buy_fill, live_sell_fill):
    from journal.tests.conftest import csv_of

    live_buy_fill(quantity=2, price="100")
    sell = live_sell_fill(quantity=2, price="100")
    raw = csv_of("0", [("DP charges for sale of INFY on 14/08/2026", "2026-08-14", "15.34", "0")])
    stored = store_upload(raw, filename="dp.csv", actor="o")
    auto_match(stored.upload)

    dp = line("DP charges")
    assert dp.matched_entry is not None
    assert dp.matched_entry.kind == "sell_fill"
    assert dp.matched_entry.source_id == str(sell.id)


@pytest.mark.django_db
def test_only_a_matched_settlement_line_has_a_difference():
    from journal.statement.reconcile import ReconcileError, settlement_difference

    store_upload(AUG, filename="aug.csv", actor="o")
    with pytest.raises(ReconcileError):
        settlement_difference(line("AMC for Demat"))


@pytest.mark.django_db
def test_a_contract_note_already_finalised_is_not_corrected_twice(live_buy_fill):
    from journal.statement.reconcile import correct_settlement, settlement_difference
    from portfolio.contract_note import estimated_charges, finalise

    fill = live_buy_fill(quantity=1, price="240")
    finalise(fill.lot, actual_charges=estimated_charges(fill.lot) + Decimal("0.30"), actor="o")
    # Zerodha's settlement already carries the true charges the note gave.
    line = _settlement_line(_our_settlement() - Decimal("0.30"))

    assert settlement_difference(line) == 0
    assert correct_settlement(line, actor="o") == []


@pytest.mark.django_db
def test_a_difference_on_a_date_with_no_fills_is_refused():
    from journal.posting import Line, post
    from journal.statement.reconcile import ReconcileError, correct_settlement

    post(
        book=Book.LIVE,
        kind="settlement",
        occurred_on=date(2026, 8, 14),
        description="t",
        lines=[Line(accounts.SETTLE_OTHER, Decimal("100")), Line(accounts.UNALLOCATED, -100)],
    )
    line = _settlement_line(Decimal("-99"))
    with pytest.raises(ReconcileError):
        correct_settlement(line, actor="o")


@pytest.mark.django_db
def test_a_dp_line_matches_a_sell_on_the_previous_trading_day(live_buy_fill, live_sell_fill):
    from journal.tests.conftest import csv_of

    live_buy_fill(quantity=2, price="100")
    sell = live_sell_fill(quantity=2, price="100")
    # 15 August 2026 is a Saturday: the Monday's previous session is the 14th.
    raw = csv_of("0", [("DP charges for sale of INFY", "2026-08-17", "15.34", "0")])
    auto_match(store_upload(raw, filename="dp.csv", actor="o").upload)

    assert line("DP charges").matched_entry.source_id == str(sell.id)


@pytest.mark.django_db
def test_a_dp_line_skips_a_sell_another_line_has_claimed(live_buy_fill, live_sell_fill):
    from journal.tests.conftest import csv_of

    live_buy_fill(quantity=2, price="100")
    live_sell_fill(quantity=2, price="100")
    row = ("DP charges for sale of INFY", "2026-08-14", "15.34", "0")
    auto_match(store_upload(csv_of("0", [row, row]), filename="dp.csv", actor="o").upload)

    first, second = StatementLine.objects.order_by("position")
    assert first.matched_entry is not None
    assert second.matched_entry is None
    assert second in list(queue())


@pytest.mark.django_db
def test_a_partly_sold_lot_splits_its_share_between_cost_and_realised(
    live_buy_fill, live_sell_fill
):
    from journal.statement.reconcile import correct_settlement
    from portfolio.models import BasisState, Lot

    buy = live_buy_fill(quantity=2, price="100")
    live_sell_fill(quantity=1, price="100")
    line = _settlement_line(_our_settlement() + Decimal("15.34") - Decimal("0.03"))

    correct_settlement(line, actor="o")

    # The buy is 200 of 300 turnover: 0.02, half on the share still held.
    on_the_lot = JournalEntry.objects.get(source_type="lot", source_id=str(buy.lot.id))
    by_account = {ln.account_id: ln.amount for ln in on_the_lot.lines.all()}
    assert by_account[accounts.INVESTMENTS] == Decimal("0.01")
    assert by_account[accounts.REALISED] == Decimal("0.01")
    final = Lot.objects.get(superseded_by__isnull=True)
    assert final.basis_state == BasisState.FINAL
    assert final.remaining_quantity == 1


# --- Go-live round 1, wave B -------------------------------------------


@pytest.mark.django_db
def test_correct_charges_refuses_a_difference_the_size_of_a_trade(live_buy_fill):
    """I5 (T6): a whole missing fill is not a charge, and never lands in lot cost."""
    from journal.statement.reconcile import ReconcileError, correct_settlement
    from portfolio.models import Lot

    live_buy_fill(quantity=1, price="240")
    line = _settlement_line(_our_settlement() - Decimal("2400"))
    before = Lot.objects.get().cost_per_unit
    entries = JournalEntry.objects.count()

    with pytest.raises(ReconcileError, match="missing fill"):
        correct_settlement(line, actor="o")

    assert Lot.objects.get(superseded_by__isnull=True).cost_per_unit == before
    assert JournalEntry.objects.count() == entries


@pytest.mark.django_db
def test_correct_charges_still_corrects_paise(live_buy_fill):
    from journal.statement.reconcile import correct_settlement, correction_bound

    live_buy_fill(quantity=1, price="240")
    line = _settlement_line(_our_settlement() - Decimal("0.07"))

    assert correction_bound(line) == Decimal("5")
    assert [entry.kind for entry in correct_settlement(line, actor="o")] == ["charge_correction"]


@pytest.mark.django_db
def test_the_bound_grows_with_the_days_turnover(live_buy_fill):
    """A tenth of a per cent of turnover, once that is more than five rupees."""
    from journal.statement.reconcile import correct_settlement, correction_bound

    live_buy_fill(quantity=100, price="1000")
    line = _settlement_line(_our_settlement() - Decimal("60"))

    assert correction_bound(line) == Decimal("100")
    assert len(correct_settlement(line, actor="o")) == 1


@pytest.mark.django_db
def test_a_line_before_the_opening_is_linked_to_the_opening_not_posted():
    """I6: the opening's cash already holds every movement on or before its date."""
    from journal.external import open_books

    opening = open_books(cash=Decimal("11.568916"), on=date(2026, 8, 31), actor="operator")
    entries = JournalEntry.objects.count()

    stored = store_upload(AUG, filename="aug.csv", actor="o")
    linked = auto_match(stored.upload)

    assert linked == 4
    assert set(StatementLine.objects.values_list("matched_entry", flat=True)) == {opening.id}
    assert JournalEntry.objects.count() == entries
    assert list(queue()) == []


@pytest.mark.django_db
def test_a_line_on_the_opening_date_is_inside_it_and_one_after_is_not():
    from journal.external import open_books
    from journal.tests.conftest import csv_of

    opening = open_books(cash=Decimal("0"), on=date(2026, 8, 14), actor="operator")
    raw = csv_of(
        "0",
        [
            ("AMC for Demat Account", "2026-08-14", "88.5", "0"),
            ("Charges for enabling DDPI", "2026-08-17", "118", "0"),
        ],
    )
    auto_match(store_upload(raw, filename="s.csv", actor="o").upload)

    assert line("AMC for Demat").matched_entry == opening
    assert line("Charges for enabling DDPI").matched_entry is None


@pytest.mark.django_db
def test_confirm_refuses_a_pre_opening_line():
    from journal.external import open_books
    from journal.statement.reconcile import ReconcileError

    open_books(cash=Decimal("0"), on=date(2026, 8, 31), actor="operator")
    store_upload(AUG, filename="aug.csv", actor="o")  # not auto-matched

    with pytest.raises(ReconcileError, match="opening"):
        confirm(line("AMC for Demat"), kind="account_charge", actor="operator")
    assert balance(accounts.ACCOUNT_CHARGES, book=Book.LIVE) == 0


@pytest.mark.django_db
def test_opening_the_books_links_lines_already_waiting():
    from journal.external import open_books

    auto_match(store_upload(AUG, filename="aug.csv", actor="o").upload)
    assert len(list(queue())) == 4

    opening = open_books(cash=Decimal("11.568916"), on=date(2026, 8, 31), actor="operator")

    assert list(queue()) == []
    assert StatementLine.objects.filter(matched_entry=opening).count() == 4


@pytest.mark.django_db
def test_the_chain_is_compared_with_the_opening_cash():
    from journal.external import open_books
    from journal.statement.reconcile import opening_disagreement

    store_upload(AUG, filename="aug.csv", actor="o")
    open_books(cash=Decimal("11.568916"), on=date(2026, 8, 31), actor="operator")
    assert opening_disagreement() is None


@pytest.mark.django_db
def test_a_chain_that_disagrees_with_the_opening_is_reported():
    from journal.external import open_books
    from journal.statement.reconcile import opening_disagreement

    store_upload(SEP, filename="sep.csv", actor="o")  # opens at 259.79 on 14 Aug
    open_books(cash=Decimal("300"), on=date(2026, 8, 31), actor="operator")

    found = opening_disagreement()
    assert found["on"] == date(2026, 8, 31)
    assert found["theirs"] == Decimal("11.568916")
    assert found["ours"] == Decimal("300")


@pytest.mark.django_db
def test_a_chain_starting_after_the_opening_is_compared_at_its_opening_balance():
    from journal.external import open_books
    from journal.statement.reconcile import opening_disagreement
    from journal.tests.conftest import csv_of

    open_books(cash=Decimal("250"), on=date(2026, 8, 31), actor="operator")
    raw = csv_of("200", [("Charges for enabling DDPI", "2026-09-22", "118", "0")])
    store_upload(raw, filename="s.csv", actor="o")

    found = opening_disagreement()
    assert (found["theirs"], found["ours"]) == (Decimal("200"), Decimal("250"))


def _two_lumpsums_a_day_apart():
    """Two ₹500 lumpsums whose typed references are not Zerodha's: auto-matching
    finds two candidates and links neither."""
    first = cash.record_lumpsum(
        Decimal("500"), received_on=date(2026, 8, 13), actor="o", reference="bank-rrn-1"
    ).contribution
    second = cash.record_lumpsum(
        Decimal("500"), received_on=date(2026, 8, 14), actor="o", reference="bank-rrn-2"
    ).contribution
    auto_match(store_upload(AUG, filename="aug.csv", actor="o").upload)
    return first, second


@pytest.mark.django_db
def test_a_queued_upi_line_can_be_matched_to_a_recorded_lumpsum_by_hand():
    from audit.models import AuditEvent
    from journal.statement.reconcile import candidates_for, link_to

    first, second = _two_lumpsums_a_day_apart()
    upi = line("Funds added using UPI")
    assert upi.matched_entry is None
    assert set(candidates_for(upi)) == {first, second}
    entries = JournalEntry.objects.count()

    link_to(upi, second, actor="operator")

    upi.refresh_from_db()
    assert upi.matched_entry == second
    assert upi not in list(queue())
    assert JournalEntry.objects.count() == entries
    assert balance(accounts.CONTRIBUTIONS, book=Book.LIVE) == Decimal("-1000")
    event = AuditEvent.objects.get(action="journal.statement_matched")
    assert event.actor == "operator"
    assert event.after["entry_number"] == second.number


@pytest.mark.django_db
def test_manual_match_refuses_a_different_amount_or_a_claimed_entry():
    from journal.statement.reconcile import ReconcileError, candidates_for, link_to
    from journal.tests.conftest import csv_of

    first, second = _two_lumpsums_a_day_apart()
    other = cash.contribute(Decimal("400"), on=date(2026, 8, 14), actor="o")
    far = cash.contribute(Decimal("500"), on=date(2026, 8, 30), actor="o")
    upi = line("Funds added using UPI")

    assert other not in candidates_for(upi)
    with pytest.raises(ReconcileError):
        link_to(upi, other, actor="operator")
    with pytest.raises(ReconcileError):
        link_to(upi, far, actor="operator")  # more than seven days away

    link_to(upi, first, actor="operator")
    raw = csv_of(
        "11.568916",
        [("Funds added using UPI with reference number 999", "2026-08-15", "0", "500")],
        voucher_type="Bank Receipts",
    )
    store_upload(raw, filename="more.csv", actor="o")
    again = line("Funds added using UPI with reference number 999")
    assert first not in candidates_for(again)
    with pytest.raises(ReconcileError):
        link_to(again, first, actor="operator")
    link_to(again, second, actor="operator")
    with pytest.raises(ReconcileError):
        link_to(again, second, actor="operator")  # the line is matched already


def _a_month_after_the_opening():
    """Opened at nought on 1 September; a ₹1,000 lumpsum recorded in the app;
    the statement has it, an AMC nobody has confirmed, and a ₹200 lumpsum whose
    reference differs from the one typed."""
    from journal.external import open_books
    from journal.tests.conftest import csv_of

    open_books(cash=Decimal("0"), on=date(2026, 9, 1), actor="operator")
    cash.record_lumpsum(
        Decimal("1000"), received_on=date(2026, 9, 18), actor="o", reference="222222222222"
    )
    typed = cash.contribute(Decimal("200"), on=date(2026, 9, 21), actor="o", reference="rrn-x")
    cash.contribute(Decimal("200"), on=date(2026, 9, 22), actor="o", reference="rrn-y")
    raw = csv_of(
        "0",
        [
            ("Funds added using UPI with reference number 222222222222", "2026-09-18", "0", "1000"),
            ("AMC for Demat Account", "2026-09-20", "88.5", "0"),
            ("Funds added using UPI with reference number 333333333333", "2026-09-21", "0", "200"),
        ],
        voucher_type="Bank Receipts",
    )
    stored = store_upload(raw, filename="sep.csv", actor="o")
    auto_match(stored.upload)
    return stored.upload, typed


@pytest.mark.django_db
def test_summary_ties_statement_closing_to_zerodha_cash():
    from journal.statement.reconcile import link_to, summary

    upload, typed = _a_month_after_the_opening()
    before = summary(upload)
    assert before.closing_balance == Decimal("1111.5")
    # Through the statement's last date: the ₹1,000 and the ₹200 of the 21st,
    # not the AMC nobody has confirmed, nor the ₹200 of the 22nd.
    assert before.ours == Decimal("1200")
    assert before.difference == Decimal("-88.5")
    assert (before.matched, before.by_hand, before.confirmed, before.queued) == (1, 0, 0, 2)

    confirm(line("AMC for Demat"), kind="account_charge", actor="operator")
    link_to(line("Funds added using UPI with reference number 3333"), typed, actor="operator")

    after = summary(upload)
    assert after.ours == Decimal("1111.5")
    assert after.difference == 0
    assert (after.matched, after.by_hand, after.confirmed, after.queued) == (1, 1, 1, 0)


@pytest.mark.django_db
def test_lines_the_opening_explains_are_counted_apart():
    from journal.external import open_books
    from journal.statement.reconcile import summary

    open_books(cash=Decimal("11.568916"), on=date(2026, 8, 31), actor="operator")
    stored = store_upload(AUG, filename="aug.csv", actor="o")
    auto_match(stored.upload)

    result = summary(stored.upload)
    assert result.opening == 4
    # It ends before the opening, where the live book holds only a fragment of
    # Zerodha's cash: the balance is compared at the opening date instead.
    assert result.ours is None
    assert result.difference is None


@pytest.mark.django_db
def test_a_settlement_line_does_not_claim_an_entry_another_line_holds(live_buy_fill):
    """NI1: a day whose settlement entry was never posted must not borrow the
    day before's, which its own line already holds: the second line waits in
    the queue rather than reading as a whole-day disagreement."""
    from journal.statement.reconcile import auto_match as match
    from journal.tests.conftest import LIVE_DAY, csv_of

    live_buy_fill(1, "100.00")
    ours = _our_settlement()
    stored = store_upload(
        csv_of(
            "0",
            [
                (f"{SETTLEMENT_TEXT}1", LIVE_DAY.isoformat(), str(-ours), "0"),
                (f"{SETTLEMENT_TEXT}2", "2026-08-17", "300.00", "0"),
            ],
            voucher_type="Book Voucher",
        ),
        filename="s.csv",
        actor="o",
    )

    match(stored.upload)

    first = StatementLine.objects.get(particulars=f"{SETTLEMENT_TEXT}1")
    second = StatementLine.objects.get(particulars=f"{SETTLEMENT_TEXT}2")
    assert first.matched_entry is not None and first.matched_entry.kind == "settlement"
    assert second.matched_entry is None


# -- go-live round 2, wave E: an explained settlement (m-4) ------------------


@pytest.mark.django_db
def test_an_explained_difference_no_longer_disagrees(live_buy_fill):
    """m-4: a difference no correction can take is explained by a person,
    appended, and honoured -- for that difference only."""
    from audit.models import AuditEvent
    from journal.models import SettlementExplanation
    from journal.statement.reconcile import (
        disagreeing_settlements,
        explain_settlement,
        explained_settlements,
    )

    live_buy_fill(quantity=1, price="240")
    line = _settlement_line(_our_settlement() - Decimal("2400"))
    assert [row["line"] for row in disagreeing_settlements()] == [line]

    explain_settlement(line, reason="Zerodha's auction debit, see ticket 123", actor="ca")

    assert disagreeing_settlements() == []
    [row] = explained_settlements()
    assert row["line"] == line
    assert row["explanation"].explained_by == "ca"
    assert row["explanation"].difference == Decimal("-2400")
    assert SettlementExplanation.objects.get().reason.startswith("Zerodha's auction")
    assert AuditEvent.objects.filter(action="journal.settlement_explained").count() == 1


@pytest.mark.django_db
def test_an_explanation_needs_a_reason_and_a_difference(live_buy_fill):
    from journal.statement.reconcile import ReconcileError, explain_settlement

    live_buy_fill(quantity=1, price="240")
    agrees = _settlement_line(_our_settlement())

    with pytest.raises(ReconcileError, match="agrees"):
        explain_settlement(agrees, reason="nothing to explain", actor="ca")
    with pytest.raises(ReconcileError, match="reason"):
        explain_settlement(agrees, reason="  ", actor="ca")


@pytest.mark.django_db
def test_a_difference_that_changes_after_it_was_explained_disagrees_again(live_buy_fill):
    """The explanation is of the difference it saw; a later movement reopens it."""
    from journal.statement.reconcile import disagreeing_settlements, explain_settlement

    live_buy_fill(quantity=1, price="240")
    line = _settlement_line(_our_settlement() - Decimal("2400"))
    explain_settlement(line, reason="auction", actor="ca")

    live_buy_fill(quantity=1, price="100")

    assert [row["line"] for row in disagreeing_settlements()] == [line]


@pytest.mark.django_db
def test_an_explanation_is_append_only(live_buy_fill):
    from django.db import DatabaseError, connection, transaction

    from journal.statement.reconcile import explain_settlement

    live_buy_fill(quantity=1, price="240")
    line = _settlement_line(_our_settlement() - Decimal("2400"))
    explanation = explain_settlement(line, reason="auction", actor="ca")

    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE journal_settlementexplanation SET reason = 'x' WHERE id = %s",
                [explanation.pk],
            )
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM journal_settlementexplanation WHERE id = %s", [explanation.pk]
            )
