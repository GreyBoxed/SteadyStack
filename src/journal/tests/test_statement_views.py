import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from journal.models import StatementLine
from journal.tests.test_statement_reconcile import _our_settlement, _settlement_line
from journal.views import MAX_STATEMENT_BYTES

AUG = (Path(__file__).parent / "fixtures" / "statement_aug.csv").read_bytes()


def a_held_instrument(symbol="INFY", token=408065):
    """A name the account holds: a dividend can only be recorded against one."""
    from portfolio.models import Holding, Instrument

    instrument = Instrument.objects.create(
        tradingsymbol=symbol, instrument_token=token, name=symbol
    )
    Holding.objects.create(instrument=instrument, quantity=10, average_cost=Decimal("1500"))
    return instrument


@pytest.mark.django_db
def test_uploading_a_statement_stores_it_and_lists_the_queue(signed_in):
    response = signed_in.post(
        reverse("journal:statement"),
        {"file": SimpleUploadedFile("aug.csv", AUG, content_type="text/csv")},
        follow=True,
    )
    assert StatementLine.objects.count() == 4
    assert b"Funds added using UPI" in response.content


@pytest.mark.django_db
def test_a_refused_upload_says_why(signed_in):
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})
    response = signed_in.post(
        reverse("journal:statement"), {"file": SimpleUploadedFile("b.csv", AUG)}, follow=True
    )
    assert b"already uploaded" in response.content


@pytest.mark.django_db
def test_confirming_a_line_from_the_queue_posts_it(signed_in):
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})
    amc = StatementLine.objects.get(particulars__startswith="AMC")
    signed_in.post(
        reverse("journal:confirm", args=[amc.pk]), {"kind": "account_charge", "component": "amc"}
    )
    amc.refresh_from_db()
    assert amc.matched_entry is not None


@pytest.mark.django_db
def test_recording_a_dividend(signed_in):

    a_held_instrument()
    signed_in.post(
        reverse("journal:dividend"),
        {
            "instrument": "INFY",
            "amount": "42",
            "paid_on": "2026-09-22",
            "reference": "interim",
            "submission": uuid.uuid4().hex,
        },
    )
    from journal.models import EntryKind, JournalEntry

    assert JournalEntry.objects.filter(kind=EntryKind.DIVIDEND).count() == 1


@pytest.mark.django_db
def test_a_dividend_into_a_closed_year_says_why(signed_in):
    """M2: a message, not a 500."""
    from datetime import date

    from journal.models import Book, PeriodClose

    a_held_instrument()
    PeriodClose.objects.create(book=Book.LIVE, fy_end=date(2026, 3, 31), closed_by="ca")
    response = signed_in.post(
        reverse("journal:dividend"),
        {
            "instrument": "INFY",
            "amount": "42",
            "paid_on": "2026-03-20",
            "reference": "r" * 100,
            "submission": uuid.uuid4().hex,
        },
        follow=True,
    )

    assert response.status_code == 200
    assert b"closed" in response.content


@pytest.mark.django_db
def test_the_dividend_page_renders(signed_in):
    response = signed_in.get(reverse("journal:dividend"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_a_confirmed_line_shows_in_a_success_message(signed_in):
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})
    amc = StatementLine.objects.get(particulars__startswith="AMC")
    response = signed_in.post(
        reverse("journal:confirm", args=[amc.pk]),
        {"kind": "account_charge", "component": "amc"},
        follow=True,
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_a_disagreeing_settlement_can_be_corrected_from_the_page(signed_in, live_buy_fill):
    live_buy_fill(quantity=1, price="240")
    theirs = _our_settlement() - Decimal("0.21")
    settlement = _settlement_line(theirs)

    response = signed_in.get(reverse("journal:statement"))
    assert b"Correct charges" in response.content

    response = signed_in.post(reverse("journal:correct", args=[settlement.pk]), follow=True)
    assert response.status_code == 200

    from journal.statement.reconcile import settlement_difference

    assert settlement_difference(settlement) == Decimal("0")


@pytest.mark.django_db
def test_a_matched_non_settlement_line_does_not_break_the_page(signed_in):
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})
    amc = StatementLine.objects.get(particulars__startswith="AMC")
    signed_in.post(
        reverse("journal:confirm", args=[amc.pk]), {"kind": "account_charge", "component": "amc"}
    )

    response = signed_in.get(reverse("journal:statement"))
    assert response.status_code == 200


# --- Fix round 1 -------------------------------------------------------


@pytest.mark.django_db
def test_a_non_utf8_upload_says_why_and_stores_nothing(signed_in):
    bad = b"\xff\xfe" + AUG
    response = signed_in.post(
        reverse("journal:statement"),
        {"file": SimpleUploadedFile("bad.csv", bad)},
        follow=True,
    )
    assert b"not a UTF-8 CSV" in response.content
    assert StatementLine.objects.count() == 0


@pytest.mark.django_db
def test_an_oversized_upload_is_refused(signed_in):
    huge = b"0" * (MAX_STATEMENT_BYTES + 1)
    response = signed_in.post(
        reverse("journal:statement"),
        {"file": SimpleUploadedFile("huge.csv", huge)},
        follow=True,
    )
    assert b"far larger" in response.content
    assert StatementLine.objects.count() == 0


@pytest.mark.django_db
def test_a_post_without_a_file_says_so(signed_in):
    response = signed_in.post(reverse("journal:statement"), {}, follow=True)
    assert b"Choose a file to upload" in response.content


@pytest.mark.django_db
def test_an_unmatched_settlement_line_offers_no_confirm_form(signed_in):
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})

    response = signed_in.get(reverse("journal:statement"))

    assert b"Waits for its trade to be recorded; it matches automatically." in response.content


@pytest.mark.django_db
def test_an_unknown_line_starts_with_the_empty_option_selected(signed_in):
    from journal.tests.conftest import csv_of

    raw = csv_of(
        "0", [("Some unexplained line", "2026-08-14", "10", "0")], voucher_type="Journal Entry"
    )
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("u.csv", raw)})

    response = signed_in.get(reverse("journal:statement"))

    assert b'<option value="" selected>' in response.content


@pytest.mark.django_db
def test_confirming_with_an_empty_kind_is_refused(signed_in):
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})
    amc = StatementLine.objects.get(particulars__startswith="AMC")

    response = signed_in.post(
        reverse("journal:confirm", args=[amc.pk]), {"kind": "", "component": ""}, follow=True
    )

    assert b"Choose what this line is" in response.content
    amc.refresh_from_db()
    assert amc.matched_entry is None


@pytest.mark.django_db
def test_the_dividend_form_carries_a_fresh_submission_token(signed_in):
    response = signed_in.get(reverse("journal:dividend"))
    assert b'name="submission"' in response.content


@pytest.mark.django_db
def test_a_double_submit_of_the_same_rendered_dividend_form_posts_once(signed_in):
    from journal.models import EntryKind, JournalEntry

    a_held_instrument()
    payload = {
        "instrument": "INFY",
        "amount": "42",
        "paid_on": "2026-09-22",
        "reference": "interim",
        "submission": uuid.uuid4().hex,
    }
    signed_in.post(reverse("journal:dividend"), payload)
    signed_in.post(reverse("journal:dividend"), payload)

    assert JournalEntry.objects.filter(kind=EntryKind.DIVIDEND).count() == 1


@pytest.mark.django_db
def test_two_separately_rendered_dividend_forms_both_post(signed_in):
    from journal.models import EntryKind, JournalEntry

    a_held_instrument()
    base = {
        "instrument": "INFY",
        "amount": "42",
        "paid_on": "2026-09-22",
        "reference": "interim",
    }
    signed_in.post(reverse("journal:dividend"), {**base, "submission": uuid.uuid4().hex})
    signed_in.post(reverse("journal:dividend"), {**base, "submission": uuid.uuid4().hex})

    assert JournalEntry.objects.filter(kind=EntryKind.DIVIDEND).count() == 2


# --- Fix round 2 -------------------------------------------------------


@pytest.mark.django_db
def test_a_dividend_post_without_a_submission_posts_nothing(signed_in):
    from journal.models import EntryKind, JournalEntry

    a_held_instrument()
    response = signed_in.post(
        reverse("journal:dividend"),
        {"instrument": "INFY", "amount": "42", "paid_on": "2026-09-22", "reference": "interim"},
        follow=True,
    )

    assert b"Reload the form and try again." in response.content
    assert JournalEntry.objects.filter(kind=EntryKind.DIVIDEND).count() == 0


@pytest.mark.django_db
def test_a_dividend_post_with_a_junk_submission_posts_nothing(signed_in):
    from journal.models import EntryKind, JournalEntry

    a_held_instrument()
    response = signed_in.post(
        reverse("journal:dividend"),
        {
            "instrument": "INFY",
            "amount": "42",
            "paid_on": "2026-09-22",
            "reference": "interim",
            "submission": "not-a-uuid-at-all",
        },
        follow=True,
    )

    assert b"Reload the form and try again." in response.content
    assert JournalEntry.objects.filter(kind=EntryKind.DIVIDEND).count() == 0


@pytest.mark.django_db
def test_the_correction_bound_is_shown_beside_the_button(signed_in, live_buy_fill):
    live_buy_fill(quantity=1, price="240")
    _settlement_line(_our_settlement() - Decimal("2400"))

    response = signed_in.get(reverse("journal:statement"))

    assert b"up to \xe2\x82\xb95.00" in response.content


@pytest.mark.django_db
def test_the_statement_page_warns_when_the_chain_disagrees_with_the_opening(signed_in):
    from journal.external import open_books

    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})
    open_books(cash=Decimal("300"), on=date(2026, 8, 31), actor="operator")

    response = signed_in.get(reverse("journal:statement"))

    assert b"does not agree with the opening" in response.content


@pytest.mark.django_db
def test_a_queued_line_can_be_matched_by_hand_from_the_page(signed_in):
    from journal import cash

    cash.record_lumpsum(
        Decimal("500"), received_on=date(2026, 8, 13), actor="o", reference="bank-rrn-1"
    )
    second = cash.record_lumpsum(
        Decimal("500"), received_on=date(2026, 8, 14), actor="o", reference="bank-rrn-2"
    ).contribution
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})
    upi = StatementLine.objects.get(particulars__startswith="Funds added")

    page = signed_in.get(reverse("journal:statement"))
    assert b"Match to" in page.content

    signed_in.post(reverse("journal:match", args=[upi.pk]), {"entry": str(second.pk)})
    upi.refresh_from_db()
    assert upi.matched_entry == second


@pytest.mark.django_db
def test_each_upload_shows_its_reconciliation_and_exports_it(signed_in):
    from journal.external import open_books

    open_books(cash=Decimal("11.568916"), on=date(2026, 8, 31), actor="operator")
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})

    page = signed_in.get(reverse("journal:statement"))
    assert b"Ours" in page.content
    assert b"By the opening" in page.content

    response = signed_in.get(reverse("journal:statement"), {"format": "csv"})
    assert response["Content-Type"].startswith("text/csv")
    header, row = response.content.decode().splitlines()[:2]
    assert header.startswith("uploaded_at,filename,period_from,period_to,closing_balance,ours")
    assert "a.csv" in row


@pytest.mark.django_db
def test_the_dividend_form_refuses_a_future_date(time_machine):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from journal.forms import DividendForm

    time_machine.move_to(datetime(2026, 9, 25, 23, 30, tzinfo=ZoneInfo("Asia/Kolkata")))
    a_held_instrument()

    def form(paid_on):
        return DividendForm(
            {
                "instrument": "INFY",
                "amount": "42",
                "paid_on": paid_on,
                "reference": "",
                "submission": uuid.uuid4().hex,
            }
        )

    later = form("2026-09-26")
    assert not later.is_valid()
    assert "paid_on" in later.errors
    assert form("2026-09-25").is_valid()


@pytest.mark.django_db
def test_a_difference_too_large_to_correct_can_be_explained_from_the_page(signed_in, live_buy_fill):
    """m-4: the page offers an explanation where it cannot offer a correction."""
    from journal.statement.reconcile import disagreeing_settlements

    live_buy_fill(quantity=1, price="240")
    settlement = _settlement_line(_our_settlement() - Decimal("2400"))

    assert b"Explain it" in signed_in.get(reverse("journal:statement")).content

    refused = signed_in.post(
        reverse("journal:explain", args=[settlement.pk]), {"reason": ""}, follow=True
    )
    assert b"reason" in refused.content
    assert disagreeing_settlements() != []

    response = signed_in.post(
        reverse("journal:explain", args=[settlement.pk]),
        {"reason": "Zerodha's auction debit, reconciled by hand"},
        follow=True,
    )

    assert disagreeing_settlements() == []
    body = response.content.decode()
    assert "Explained settlements" in body
    assert "auction debit" in body


@pytest.mark.django_db
def test_the_reconciliation_csv_counts_disagreeing_and_explained_settlements(
    signed_in, live_buy_fill
):
    """Nm6: an explained settlement leaves a permanent line-level difference;
    the CA's export says how many each upload carries, explained or not."""
    import csv
    import io

    from journal.statement.reconcile import explain_settlement

    live_buy_fill(quantity=1, price="240")
    settlement = _settlement_line(_our_settlement() - Decimal("2400"))

    def rows():
        body = signed_in.get(reverse("journal:statement"), {"format": "csv"}).content.decode()
        return list(csv.DictReader(io.StringIO(body)))

    [before] = rows()
    assert (before["disagreeing"], before["explained"]) == ("1", "0")

    explain_settlement(settlement, reason="auction debit, reconciled by hand", actor="ca")

    [after] = rows()
    assert (after["disagreeing"], after["explained"]) == ("0", "1")


@pytest.mark.django_db
def test_the_settlements_csv_carries_each_difference_and_its_explanation(signed_in, live_buy_fill):
    import csv
    import io

    from journal.statement.reconcile import correction_bound, explain_settlement

    live_buy_fill(quantity=1, price="240")
    settlement = _settlement_line(_our_settlement() - Decimal("2400"))
    explain_settlement(settlement, reason="auction debit, reconciled by hand", actor="ca")

    response = signed_in.get(reverse("journal:statement"), {"format": "settlements-csv"})

    assert response["Content-Type"].startswith("text/csv")
    [row] = list(csv.DictReader(io.StringIO(response.content.decode())))
    assert row["date"] == "2026-08-14"
    assert Decimal(row["difference"]) == Decimal("-2400")
    assert Decimal(row["bound"]) == correction_bound(settlement)
    assert row["state"] == "explained"
    assert row["explained_by"] == "ca"
    assert row["reason"] == "auction debit, reconciled by hand"
    assert row["explained_at"]
    assert b"settlements-csv" in signed_in.get(reverse("journal:statement")).content


@pytest.mark.django_db
def test_the_dividend_list_is_only_what_has_been_held():
    """A dividend is paid on shares held at the record date: the broker's
    holdings, and every name a lot was ever opened for, sold or not."""
    from journal.forms import DividendForm
    from portfolio.models import Instrument, Lot

    a_held_instrument("INFY", 408065)
    sold = Instrument.objects.create(tradingsymbol="TCS", instrument_token=2953217, name="TCS")
    Lot.objects.create(
        instrument=sold,
        acquired_on=date(2026, 9, 1),
        quantity=1,
        remaining_quantity=0,
        cost_per_unit=Decimal("3000"),
    )
    Instrument.objects.create(tradingsymbol="ITC", instrument_token=424961, name="ITC")

    choices = list(DividendForm().fields["instrument"].queryset.values_list("pk", flat=True))

    assert choices == ["INFY", "TCS"]


@pytest.mark.django_db
def test_a_dividend_on_a_name_never_held_is_refused(signed_in):
    from journal.models import EntryKind, JournalEntry
    from portfolio.models import Instrument

    Instrument.objects.create(tradingsymbol="ITC", instrument_token=424961, name="ITC")

    signed_in.post(
        reverse("journal:dividend"),
        {
            "instrument": "ITC",
            "amount": "42",
            "paid_on": "2026-09-22",
            "submission": uuid.uuid4().hex,
        },
    )

    assert not JournalEntry.objects.filter(kind=EntryKind.DIVIDEND).exists()
