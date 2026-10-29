"""One instrument, from the first share to the last.

Holdings answers "what do I hold". This answers "how did I come to hold it",
which is the question somebody actually has when a number looks wrong.
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
def no_broker(monkeypatch):
    from broker.exceptions import BrokerError

    def refuse(*_a, **_k):
        raise BrokerError("no session in a test")

    monkeypatch.setattr("engine.quotes.fetch", refuse)


def test_the_list_is_not_reachable_anonymously(client):
    assert client.get(reverse("portfolio:index")).status_code == 302


def test_the_list_opens_on_the_names_this_system_touches(signed_in, instrument, no_broker):
    """A name nobody holds, targets or ordered stays out of the default view,
    and is still one search away."""
    from portfolio.models import Holding, Instrument

    Holding.objects.create(instrument=instrument, quantity=3, average_cost=Decimal("1700"))
    Instrument.objects.create(tradingsymbol="ZZZUNRELATED", instrument_token=9, name="Nobody Ltd")

    body = signed_in.get(reverse("portfolio:index")).content
    assert b"INFY" in body
    assert b"ZZZUNRELATED" not in body

    found = signed_in.get(reverse("portfolio:index"), {"q": "nobody"}).content
    assert b"ZZZUNRELATED" in found


def test_the_other_views_list_shares_indices_and_retired_rows(signed_in, instrument, no_broker):
    from portfolio.models import Instrument

    Instrument.objects.create(
        tradingsymbol="NIFTY 50", instrument_token=256265, segment="INDICES", name="Nifty 50"
    )
    Instrument.objects.create(tradingsymbol="OLDCO", instrument_token=7, active=False)

    shares = signed_in.get(reverse("portfolio:index"), {"view": "stocks"}).content
    indices = signed_in.get(reverse("portfolio:index"), {"view": "indices"}).content
    retired = signed_in.get(reverse("portfolio:index"), {"view": "inactive"}).content

    assert b"INFY" in shares and b"NIFTY 50" not in shares
    assert b"NIFTY 50" in indices
    assert b"OLDCO" in retired


def test_an_unknown_symbol_is_a_404(signed_in, no_broker):
    assert signed_in.get(reverse("portfolio:detail", args=["NOPE"])).status_code == 404


def test_the_lots_are_shown_oldest_first(signed_in, instrument, no_broker):
    """The order FIFO will consume them in."""
    from portfolio.models import Lot

    for day, cost in (("2026-03-01", "1600"), ("2026-01-15", "1700")):
        Lot.objects.create(
            instrument=instrument,
            acquired_on=day,
            quantity=1,
            remaining_quantity=1,
            cost_per_unit=Decimal(cost),
        )

    body = signed_in.get(reverse("portfolio:detail", args=["INFY"])).content

    assert body.index(b"15 Jan 2026") < body.index(b"1 Mar 2026")
    assert b"never edited" in body


def test_a_superseded_lot_is_shown_as_superseded(signed_in, instrument, no_broker):
    """A split or a contract note supersedes; the original row stays."""
    from portfolio.models import Lot

    original = Lot.objects.create(
        instrument=instrument,
        acquired_on="2026-01-15",
        quantity=1,
        remaining_quantity=0,
        cost_per_unit=Decimal("1700"),
    )
    replacement = Lot.objects.create(
        instrument=instrument,
        acquired_on="2026-01-15",
        quantity=5,
        remaining_quantity=5,
        cost_per_unit=Decimal("340"),
    )
    Lot.objects.filter(pk=original.pk).update(superseded_by=replacement)

    body = signed_in.get(reverse("portfolio:detail", args=["INFY"])).content

    assert b"superseded" in body


def test_an_instrument_in_doubt_says_so(signed_in, instrument, no_broker):
    from records.models import Divergence

    Divergence.objects.create(
        instrument=instrument,
        detected_on="2026-09-22",
        expected_quantity=5,
        actual_quantity=4,
    )

    body = signed_in.get(reverse("portfolio:detail", args=["INFY"])).content

    assert b"unresolved divergence" in body


def test_it_renders_without_the_broker(signed_in, instrument, no_broker):
    from audit.models import BrokerCall

    before = BrokerCall.objects.count()
    response = signed_in.get(reverse("portfolio:detail", args=["INFY"]))

    assert response.status_code == 200
    assert BrokerCall.objects.count() == before
