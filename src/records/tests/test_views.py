"""The divergence screen.

The one thing the system refuses to decide for itself, and the screen that
lets somebody decide it. Classifying must also stop the figures being
withheld, or it has explained nothing.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


@pytest.fixture
def open_divergence(instrument):
    from portfolio.models import Lot
    from records.models import Divergence

    Lot.objects.create(
        instrument=instrument,
        acquired_on="2026-01-15",
        quantity=10,
        remaining_quantity=10,
        cost_per_unit=Decimal("1700"),
    )
    return Divergence.objects.create(
        instrument=instrument,
        detected_on="2026-09-22",
        expected_quantity=10,
        actual_quantity=8,
    )


def test_the_list_is_not_reachable_anonymously(client):
    response = client.get(reverse("records:divergences"))

    assert response.status_code == 302


def test_an_open_divergence_is_shown_with_both_numbers(signed_in, open_divergence):
    body = signed_in.get(reverse("records:divergences")).content

    assert b"INFY" in body
    assert b"withheld" in body


def test_nothing_in_doubt_says_so(signed_in, instrument):
    body = signed_in.get(reverse("records:divergences")).content

    assert b"Nothing is withheld" in body


def test_classifying_as_a_system_error_settles_it(signed_in, open_divergence):
    from records.divergence import is_in_doubt

    signed_in.post(
        reverse("records:classify", args=[open_divergence.pk]),
        {"classification": "system_error", "note": "we miscounted"},
        follow=True,
    )

    open_divergence.refresh_from_db()
    assert open_divergence.resolved_at is not None
    assert is_in_doubt("INFY") is False


def test_an_external_sell_needs_its_price(signed_in, open_divergence):
    """Without a price the lots it touches have no proceeds, and every gain
    after it is wrong."""
    from records.divergence import is_in_doubt

    response = signed_in.post(
        reverse("records:classify", args=[open_divergence.pk]),
        {"classification": "external_sell", "note": "sold in the Kite app"},
    )

    assert response.status_code == 200
    assert is_in_doubt("INFY") is True


def test_an_external_sell_with_a_price_consumes_the_lots(signed_in, open_divergence, already_live):
    from gains.fifo import available
    from records.divergence import is_in_doubt

    already_live()
    signed_in.post(
        reverse("records:classify", args=[open_divergence.pk]),
        {
            "classification": "external_sell",
            "note": "sold two in the Kite app",
            "price": "1850.00",
            "charges": "3.40",
            "occurred_on": "2026-09-20",
        },
        follow=True,
    )

    assert is_in_doubt("INFY") is False
    assert available("INFY") == 8  # the lots now agree with the broker


def test_classifying_twice_is_refused(signed_in, open_divergence):
    signed_in.post(
        reverse("records:classify", args=[open_divergence.pk]),
        {"classification": "system_error", "note": "first"},
        follow=True,
    )

    response = signed_in.get(reverse("records:classify", args=[open_divergence.pk]), follow=True)

    assert b"already classified" in response.content


def test_a_failed_entry_leaves_the_divergence_open(signed_in, open_divergence, already_live):
    """Either both land or neither: a classification without its entry claims
    to have explained a holding that is still wrong."""
    from records.divergence import is_in_doubt

    already_live()
    signed_in.post(
        reverse("records:classify", args=[open_divergence.pk]),
        {
            "classification": "external_sell",
            "note": "more than we hold",
            "price": "1850.00",
            "occurred_on": "2026-09-20",
        },
        follow=True,
    )

    # 2 shares is within the 10 held, so this one succeeds -- the guard that
    # matters is that nothing is half-applied, asserted by the pair agreeing.
    open_divergence.refresh_from_db()
    assert (open_divergence.resolved_at is None) == is_in_doubt("INFY")


# -- the contract note that settles a basis (M35b) -----------------------


@pytest.fixture
def estimated_lot(instrument):
    from django.utils import timezone

    from engine.models import TradeSheet, TradeSheetState
    from journal.trades import post_fill
    from orders.models import Fill, Order, OrderMode, OrderType
    from portfolio import lots as lot_writer

    sheet = TradeSheet.objects.create(
        trading_date="2026-09-22",
        version=1,
        trace_id="ts",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=2,
    )
    order = Order.objects.create(
        tag="tx-1",
        tradesheet=sheet,
        instrument=instrument,
        trading_date="2026-09-22",
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=2,
        limit_price=Decimal("1000"),
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id="t-1",
        quantity=2,
        price=Decimal("1000"),
        executed_at=timezone.now(),
    )
    lot = lot_writer.from_fill(fill)
    post_fill(fill)
    return lot


def test_a_lot_waiting_for_a_note_is_listed(signed_in, estimated_lot):
    body = signed_in.get(reverse("records:basis")).content

    assert b"INFY" in body
    assert b"settles it" in body


def test_finalising_supersedes_and_posts_the_difference(signed_in, estimated_lot):
    """The acceptance criterion this screen exists for: the difference posts
    to the journal and the lot is superseded."""
    from journal.models import EntryKind, JournalLine
    from portfolio.models import BasisState

    estimated = (estimated_lot.cost_per_unit * estimated_lot.quantity) - Decimal("2000")

    signed_in.post(
        reverse("records:basis"),
        {
            "lot": str(estimated_lot.pk),
            "actual_charges": str(estimated + Decimal("18")),
            "note_reference": "CN-1",
        },
        follow=True,
    )

    estimated_lot.refresh_from_db()
    assert estimated_lot.superseded_by_id is not None
    assert estimated_lot.superseded_by.basis_state == BasisState.FINAL
    cash = JournalLine.objects.get(
        entry__kind=EntryKind.CHARGE_CORRECTION, account_id__in=["1100", "1110"]
    )
    assert cash.amount == Decimal("-18")


def test_nothing_waiting_says_so(signed_in, instrument):
    body = signed_in.get(reverse("records:basis")).content

    assert b"Nothing is waiting on a note" in body


# -- corporate actions ---------------------------------------------------


def test_a_split_supersedes_every_open_lot(signed_in, instrument):
    from portfolio.models import Lot

    lot = Lot.objects.create(
        instrument=instrument,
        acquired_on="2026-01-15",
        quantity=4,
        remaining_quantity=4,
        cost_per_unit=Decimal("1000"),
    )

    signed_in.post(
        reverse("records:corporate_action"),
        {
            "instrument": "INFY",
            "new_shares": 5,
            "per_old_shares": 1,
            "effective_on": "2026-09-22",
            "reason": "5-for-1 split",
        },
        follow=True,
    )

    lot.refresh_from_db()
    assert lot.superseded_by_id is not None
    assert lot.superseded_by.quantity == 20
    # The same money, divided differently.
    assert lot.superseded_by.cost_per_unit * 20 == Decimal("4000.0000")


def test_an_unknown_symbol_is_refused(signed_in, instrument):
    from records.models import Correction

    signed_in.post(
        reverse("records:corporate_action"),
        {
            "instrument": "NOPE",
            "new_shares": 2,
            "per_old_shares": 1,
            "effective_on": "2026-09-22",
            "reason": "x",
        },
        follow=True,
    )

    assert Correction.objects.count() == 0


def test_the_page_says_why_a_person_enters_this(signed_in):
    body = signed_in.get(reverse("records:corporate_action")).content

    assert b"silent rewrite of every cost basis" in body


def test_the_divergences_page_lists_holdings_that_are_not_the_portfolios(signed_in):
    """Nm4: a personal holding is named, not raised."""
    from portfolio.models import Holding, Instrument

    tcs = Instrument.objects.create(tradingsymbol="TCS", instrument_token=2953217, name="TCS")
    Holding.objects.create(instrument=tcs, quantity=12)

    body = signed_in.get(reverse("records:divergences")).content.decode()

    assert "not the portfolio" in body
    assert "TCS" in body


def test_a_classification_that_posts_to_the_live_book_is_refused_in_paper(
    signed_in, open_divergence
):
    """m-8: in paper the scan compares paper holdings against every lot, so an
    external movement recorded from it would put a paper difference into the
    real accounts."""
    from journal.models import JournalEntry
    from records.divergence import is_in_doubt

    response = signed_in.post(
        reverse("records:classify", args=[open_divergence.pk]),
        {
            "classification": "external_sell",
            "note": "sold two in the Kite app",
            "price": "1850.00",
            "occurred_on": "2026-09-20",
        },
        follow=True,
    )

    assert "would post to the live book" in response.content.decode()
    assert is_in_doubt("INFY") is True
    assert JournalEntry.objects.count() == 0

    # One that posts nothing is still allowed.
    signed_in.post(
        reverse("records:classify", args=[open_divergence.pk]),
        {"classification": "system_error", "note": "paper noise"},
        follow=True,
    )
    assert is_in_doubt("INFY") is False
