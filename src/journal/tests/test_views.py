from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from journal import cash
from journal.models import EntryKind, JournalEntry


@pytest.mark.django_db
def test_the_books_page_shows_the_trial_balance(signed_in):
    cash.contribute(Decimal("5000"), on=date(2026, 9, 21), actor="o")
    response = signed_in.get(reverse("journal:index") + "?book=live")
    assert response.status_code == 200
    assert b"Contributions" in response.content


@pytest.mark.django_db
def test_the_books_open_on_the_live_book_whatever_the_order_mode(signed_in):
    """M5: nothing the CA is shown reads the paper book unless asked for."""
    from configuration.store import current

    assert current().order_mode == "paper"
    for name in ("journal:index", "journal:entries"):
        response = signed_in.get(reverse(name))
        assert response.context["book"] == "live"
        assert b"Live book" in response.content
    assert signed_in.get(reverse("journal:index") + "?book=nonsense").context["book"] == "live"
    paper = signed_in.get(reverse("journal:index") + "?book=paper")
    assert b"Paper book" in paper.content


@pytest.mark.django_db
def test_a_csv_cell_a_spreadsheet_would_run_is_exported_as_text(signed_in):
    """Statement particulars and operator reasons reach Excel as text, not formulas."""
    cash.contribute(Decimal("5000"), on=date(2026, 9, 21), actor="o")
    cash.withdraw(Decimal("10"), on=date(2026, 9, 22), actor="o", reason="=HYPERLINK(1)")
    body = signed_in.get(reverse("journal:entries") + "?book=live&format=csv").content.decode()
    assert "'=HYPERLINK(1)" in body
    assert ",=HYPERLINK" not in body


def test_the_web_csv_helper_quotes_formula_cells_but_not_numbers(rf):
    from web.views import _csv

    body = _csv("x", ["a", "b", "c"], [["+1 call", Decimal("-5"), "@SUM"]]).content.decode()
    assert "'+1 call,-5,'@SUM" in body


@pytest.mark.django_db
def test_the_journal_exports_csv(signed_in):
    cash.contribute(Decimal("5000"), on=date(2026, 9, 21), actor="o")
    response = signed_in.get(reverse("journal:entries") + "?book=live&format=csv")
    assert response["Content-Type"].startswith("text/csv")
    header = response.content.decode().splitlines()[0]
    expected = (
        "number,date,kind,description,account,account_name,"
        "instrument,component,debit,credit,trace_id"
    )
    assert header == expected


@pytest.mark.django_db
def test_reversing_an_entry_needs_a_reason(signed_in):
    entry = cash.contribute(Decimal("5000"), on=date(2026, 9, 21), actor="o")
    url = reverse("journal:reverse", args=[entry.pk])
    signed_in.post(url, {"reason": ""})
    assert not JournalEntry.objects.filter(kind=EntryKind.REVERSAL).exists()
    signed_in.post(url, {"reason": "recorded twice"})
    assert JournalEntry.objects.filter(kind=EntryKind.REVERSAL, reverses=entry).exists()


@pytest.mark.django_db
def test_the_books_need_a_signed_in_operator(client):
    response = client.get(reverse("journal:index"))
    assert response.status_code in (302, 403)


@pytest.mark.django_db
def test_only_entries_a_person_recorded_can_be_reversed_from_the_books(signed_in):
    """An engine entry -- an allocation, a fill, a settlement -- has lots and a
    cash ledger resting on it; reversing it here would split the two."""
    from journal.models import Book

    allocation = cash.allocate(
        Decimal("1000"),
        book=Book.LIVE,
        on=date(2026, 9, 21),
        actor="o",
        description="daily",
        source_type="daily",
    )
    page = signed_in.get(reverse("journal:entry", args=[allocation.pk]))
    assert b"Reverse this entry" not in page.content

    signed_in.post(reverse("journal:reverse", args=[allocation.pk]), {"reason": "try"})
    assert not JournalEntry.objects.filter(reverses=allocation).exists()

    contribution = cash.contribute(Decimal("5000"), on=date(2026, 9, 21), actor="o")
    assert (
        b"Reverse this entry"
        in signed_in.get(reverse("journal:entry", args=[contribution.pk])).content
    )


def test_the_reversible_kinds_are_the_ones_a_person_records():
    from journal.views import REVERSIBLE_KINDS

    assert set(REVERSIBLE_KINDS) == {
        EntryKind.CONTRIBUTION,
        EntryKind.WITHDRAWAL,
        EntryKind.ACCOUNT_CHARGE,
        EntryKind.DIVIDEND,
        EntryKind.OPENING_BALANCE,
        EntryKind.CHARGE_CORRECTION,
    }
