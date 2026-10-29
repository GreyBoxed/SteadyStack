"""The upload screens.

The property worth holding onto: checking a file stores nothing. An operator
must be able to see what an upload would do and then walk away, leaving the
portfolio exactly as it was.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from model_portfolio.models import ModelPortfolio
from portfolio.instruments import refresh

FAKE_PASSWORD = "fake-password-for-tests"

FIRST = b"symbol,target_pct\nINFY,40\nTCS,60\n"
SECOND = b"symbol,target_pct\nINFY,30\nTCS,40\nHDFCBANK,30\n"


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


@pytest.fixture
def operator(client, db):
    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.login(username="operator", password=FAKE_PASSWORD)
    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()
    refresh([master("INFY", 408065), master("TCS", 2953217), master("HDFCBANK", 341249)])
    return user


def upload(client, content: bytes, name: str = "model.csv", **kwargs):
    return client.post(
        reverse("model_portfolio:upload"),
        {"file": SimpleUploadedFile(name, content, content_type="text/csv")},
        **kwargs,
    )


@pytest.mark.django_db
def test_the_page_needs_a_verified_session(client):
    assert client.get(reverse("model_portfolio:index")).status_code == 302


@pytest.mark.django_db
def test_the_page_renders_for_the_operator(client, operator):
    assert client.get(reverse("model_portfolio:index")).status_code == 200


@pytest.mark.django_db
def test_checking_a_file_shows_the_diff(client, operator):
    response = upload(client, FIRST)

    assert response.status_code == 200
    assert b"INFY" in response.content


@pytest.mark.django_db
def test_checking_a_file_stores_nothing(client, operator):
    """The diff is a preview. Walking away must leave the portfolio alone."""
    upload(client, FIRST)
    assert ModelPortfolio.objects.count() == 0


@pytest.mark.django_db
def test_confirming_stores_the_version(client, operator):
    upload(client, FIRST)

    response = client.post(reverse("model_portfolio:confirm"), follow=True)

    assert response.status_code == 200
    assert ModelPortfolio.objects.count() == 1
    assert ModelPortfolio.objects.get().version == 1


@pytest.mark.django_db
def test_confirming_twice_stores_one_version(client, operator):
    """A double submit must not create a second identical version."""
    upload(client, FIRST)
    client.post(reverse("model_portfolio:confirm"))
    client.post(reverse("model_portfolio:confirm"))

    assert ModelPortfolio.objects.count() == 1


@pytest.mark.django_db
def test_an_invalid_file_is_refused_with_its_reasons(client, operator):
    response = upload(client, b"symbol,target_pct\nINFY,40\nTCS,40\n")

    assert response.status_code == 400
    assert b"sum to 80" in response.content
    assert ModelPortfolio.objects.count() == 0


@pytest.mark.django_db
def test_an_unknown_symbol_is_named_in_the_refusal(client, operator):
    response = upload(client, b"symbol,target_pct\nNOSUCHCO,100\n")
    assert b"NOSUCHCO" in response.content


@pytest.mark.django_db
def test_uploading_without_a_file_says_so(client, operator):
    response = client.post(reverse("model_portfolio:upload"), {}, follow=True)
    assert b"Choose a CSV file" in response.content


@pytest.mark.django_db
def test_an_oversized_file_is_refused_without_being_parsed(client, operator):
    response = upload(client, b"symbol,target_pct\n" + b"INFY,1\n" * 60000, follow=True)

    assert b"too large" in response.content
    assert ModelPortfolio.objects.count() == 0


@pytest.mark.django_db
def test_a_second_version_records_the_change(client, operator):
    upload(client, FIRST)
    client.post(reverse("model_portfolio:confirm"))
    upload(client, SECOND)
    client.post(reverse("model_portfolio:confirm"))

    assert ModelPortfolio.objects.count() == 2
    assert ModelPortfolio.objects.order_by("-version").first().version == 2


@pytest.mark.django_db
def test_the_current_weights_are_shown(client, operator):
    upload(client, FIRST)
    client.post(reverse("model_portfolio:confirm"))

    response = client.get(reverse("model_portfolio:index"))

    assert b"INFY" in response.content
    assert b"40.00%" in response.content


@pytest.mark.django_db
def test_confirming_an_expired_upload_says_so(client, operator):
    response = client.post(reverse("model_portfolio:confirm"), follow=True)

    assert b"expired" in response.content
    assert ModelPortfolio.objects.count() == 0


# -- the target, beside what is actually held ----------------------------


@pytest.mark.django_db
def test_the_allocation_shows_actual_against_target(client, operator):
    """A target weight on its own says nothing about whether it is reached."""
    from decimal import Decimal

    from portfolio.models import Instrument, Lot

    upload(client, FIRST)
    client.post(reverse("model_portfolio:confirm"))

    for symbol in ("INFY", "TCS"):
        Lot.objects.create(
            instrument=Instrument.objects.get(tradingsymbol=symbol),
            acquired_on="2026-01-15",
            quantity=1,
            remaining_quantity=1,
            cost_per_unit=Decimal("100"),
        )

    body = client.get(reverse("model_portfolio:index")).content

    assert b"the marker is actual" in body
    assert b"Actual" in body


@pytest.mark.django_db
def test_the_page_sums_the_weights_and_says_what_each_version_changed(client, operator):
    upload(client, FIRST)
    client.post(reverse("model_portfolio:confirm"))
    upload(client, SECOND)
    client.post(reverse("model_portfolio:confirm"))

    response = client.get(reverse("model_portfolio:index"))
    summary = response.context["summary"]
    history = response.context["history"]

    assert summary["weight_sum"] == 100
    assert summary["never_bought"]  # nothing has been bought in this test
    assert history[0]["version"].version == 2
    assert history[-1]["summary"].endswith("names")
    assert history[0]["summary"] != "no change"


@pytest.mark.django_db
def test_a_refused_upload_keeps_the_summary_and_history(client, operator):
    upload(client, FIRST)
    client.post(reverse("model_portfolio:confirm"))

    response = upload(client, b"symbol,target_pct\nINFY,500\n")

    assert response.status_code == 400
    assert response.context["summary"]["universe"] == 2
    assert response.context["history"]


@pytest.mark.django_db
def test_a_name_not_held_is_priced_from_the_latest_ranking(client, operator, settings):
    from decimal import Decimal

    from configuration.store import update
    from engine.models import TradeSheet, TradeSheetState

    upload(client, FIRST)
    client.post(reverse("model_portfolio:confirm"))
    update(changed_by="test", reason="ceiling", max_price_per_share=Decimal("2000"))
    TradeSheet.objects.create(
        trading_date="2026-09-22",
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.SKIPPED,
        ranking_snapshot={
            "ranked": [
                {"symbol": "TCS", "last_price": "3500"},
                {"symbol": "INFY", "last_price": "1700"},
            ]
        },
    )

    summary = client.get(reverse("model_portfolio:index")).context["summary"]

    assert summary["out_of_reach"] == ["TCS"]


@pytest.mark.django_db
def test_an_index_cannot_be_given_a_target_weight(client, operator):
    from portfolio.models import Instrument

    Instrument.objects.create(tradingsymbol="NIFTY 50", instrument_token=256265, segment="INDICES")

    response = upload(client, b"symbol,target_pct\nINFY,50\nNIFTY 50,50\n")

    assert response.status_code == 400
    assert b"is an index" in response.content
