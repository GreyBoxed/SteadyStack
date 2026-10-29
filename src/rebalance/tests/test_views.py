"""The rebalance screen.

Uploading a file must never place an order. That is the whole shape of M32,
and it is the property worth asserting hardest.
"""

from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


@pytest.fixture
def holding(instrument):
    """Ten shares, and a completed opening import so sells are permitted."""
    from django.utils import timezone

    from portfolio.models import OpeningImport
    from rebalance.tests.conftest import paper_lot

    OpeningImport.objects.create(
        imported_by="operator", completed_at=timezone.now(), source="csv", lot_count=1
    )
    paper_lot(instrument, acquired_on="2026-01-15", quantity=10, cost=Decimal("1700"))


def _csv(text: str) -> SimpleUploadedFile:
    return SimpleUploadedFile("sells.csv", text.encode(), content_type="text/csv")


def test_the_page_is_not_reachable_anonymously(client):
    assert client.get(reverse("rebalance:index")).status_code == 302


def test_it_explains_that_uploading_sells_nothing(signed_in):
    body = signed_in.get(reverse("rebalance:index")).content

    assert b"never places an order" in body


def test_a_valid_file_becomes_a_draft_and_places_nothing(signed_in, holding):
    from orders.models import Order
    from rebalance.models import BatchState, RebalanceSell

    before = Order.objects.count()  # the paper buy the held lot came from
    signed_in.post(
        reverse("rebalance:index"), {"file": _csv("symbol,quantity\nINFY,4\n")}, follow=True
    )

    batch = RebalanceSell.objects.get()
    assert batch.state == BatchState.DRAFT
    assert Order.objects.count() == before


def test_asking_for_more_than_is_held_is_refused(signed_in, holding):
    from rebalance.models import RebalanceSell

    response = signed_in.post(
        reverse("rebalance:index"), {"file": _csv("symbol,quantity\nINFY,999\n")}, follow=True
    )

    assert RebalanceSell.objects.count() == 0
    assert response.status_code == 200


def test_the_preview_shows_what_the_holding_drops_to(signed_in, holding):
    from rebalance.models import RebalanceSell

    signed_in.post(
        reverse("rebalance:index"), {"file": _csv("symbol,quantity\nINFY,4\n")}, follow=True
    )
    batch = RebalanceSell.objects.get()

    body = signed_in.get(reverse("rebalance:detail", args=[batch.pk])).content

    assert b"INFY" in body
    assert b"oldest first" in body


def test_executing_without_the_token_is_refused(signed_in, holding):
    from rebalance.models import BatchState, RebalanceSell

    signed_in.post(
        reverse("rebalance:index"), {"file": _csv("symbol,quantity\nINFY,4\n")}, follow=True
    )
    batch = RebalanceSell.objects.get()

    signed_in.post(reverse("rebalance:execute", args=[batch.pk]), {"token": "wrong"}, follow=True)

    batch.refresh_from_db()
    assert batch.state == BatchState.DRAFT


def test_an_empty_upload_says_so(signed_in):
    response = signed_in.post(reverse("rebalance:index"), {}, follow=True)

    assert b"Choose a CSV" in response.content


def test_the_preview_names_the_lots_fifo_would_take(signed_in, holding, instrument, no_broker):
    from rebalance.models import RebalanceSell
    from rebalance.tests.conftest import paper_lot

    paper_lot(instrument, acquired_on="2026-03-02", quantity=5, cost=Decimal("1800"))
    signed_in.post(
        reverse("rebalance:index"), {"file": _csv("symbol,quantity\nINFY,12\n")}, follow=True
    )
    batch = RebalanceSell.objects.get()

    row = signed_in.get(reverse("rebalance:detail", args=[batch.pk])).context["preview"][0]

    assert [(lot["take"], lot["of"]) for lot in row["lots"]] == [(10, 10), (2, 5)]
    assert row["cost"] == Decimal("1700") * 10 + Decimal("1800") * 2
    assert row["sold"] == 0
    assert row["sold_pct"] == 0


def test_the_template_is_a_header_and_nothing_to_sell(signed_in):
    response = signed_in.get(reverse("rebalance:template"))

    assert response["Content-Type"].startswith("text/csv")
    assert response.content.decode() == "symbol,quantity\n"


def test_a_draft_page_is_whole_and_can_be_executed(signed_in, holding, no_broker):
    """The page is the whole shell, once: a template mangled into repeats still
    passed every test that only read the context."""
    from rebalance.models import RebalanceSell

    signed_in.post(
        reverse("rebalance:index"), {"file": _csv("symbol,quantity\nINFY,4\n")}, follow=True
    )
    batch = RebalanceSell.objects.get()

    response = signed_in.get(reverse("rebalance:detail", args=[batch.pk]))
    body = response.content

    assert body.count(b"<html") == 1
    assert body.count(b"<table") == 1
    assert b"Execute this batch" in body
    assert len(body) < 100_000
    assert response.context["totals"]["shares"] == 4
    assert response.context["preview"][0]["after"] == 6


@pytest.mark.parametrize(
    ("reinvest", "says"),
    [
        (True, b"Proceeds reinvested"),
        (False, b"Proceeds kept out of the engine"),
    ],
)
def test_the_batch_page_says_where_the_proceeds_go(signed_in, holding, no_broker, reinvest, says):
    """M-6: whether the sale money goes back to the engine is decided at
    upload, and the operator must see it before Execute."""
    from rebalance.models import RebalanceSell

    form = {"file": _csv("symbol,quantity\nINFY,4\n")}
    if reinvest:
        form["reinvest"] = "on"
    signed_in.post(reverse("rebalance:index"), form, follow=True)
    batch = RebalanceSell.objects.get()
    assert batch.reinvest is reinvest

    body = signed_in.get(reverse("rebalance:detail", args=[batch.pk])).content

    assert says in body


@pytest.mark.django_db(transaction=True)
def test_the_batch_page_opens_outside_a_transaction(signed_in, holding, no_broker):
    """A GET is not wrapped in a transaction on the server, as it is in every
    other test here. The preview only reads the lots; locking them there
    raised on every real page load."""
    from rebalance.models import RebalanceSell

    signed_in.post(reverse("rebalance:index"), {"file": _csv("symbol,quantity\nINFY,4\n")})
    batch = RebalanceSell.objects.get()

    response = signed_in.get(reverse("rebalance:detail", args=[batch.pk]))

    assert response.status_code == 200
    assert b"oldest first" in response.content
