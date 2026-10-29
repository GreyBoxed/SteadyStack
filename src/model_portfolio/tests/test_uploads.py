"""Storing a model portfolio, and the diff that precedes it.

The behaviour being protected is immutability: an upload creates a version and
never edits one, so a tradesheet from any past day can still be read against
the weights that produced it.
"""

from decimal import Decimal

import pytest

from audit.models import AuditEvent
from model_portfolio.models import ModelPortfolio, ModelPortfolioRow
from model_portfolio.uploads import (
    UploadRefused,
    diff_against_current,
    is_identical_to_current,
    store,
    validate,
)
from portfolio.instruments import refresh


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
def universe(db):
    refresh([master("INFY", 408065), master("TCS", 2953217), master("HDFCBANK", 341249)])


FIRST = "symbol,target_pct\nINFY,40\nTCS,60\n"
SECOND = "symbol,target_pct\nINFY,30\nTCS,40\nHDFCBANK,30\n"


@pytest.mark.django_db
def test_an_upload_creates_a_version(universe):
    portfolio = store(FIRST, uploaded_by="operator", source_filename="model.csv")

    assert portfolio.version == 1
    assert portfolio.rows.count() == 2


@pytest.mark.django_db
def test_versions_increment(universe):
    store(FIRST, uploaded_by="operator")
    second = store(SECOND, uploaded_by="operator")

    assert second.version == 2
    assert ModelPortfolio.objects.count() == 2


@pytest.mark.django_db
def test_an_earlier_version_keeps_its_rows(universe):
    """The whole reason for versioning: an old tradesheet reads against the
    weights that were live when it ran."""
    first = store(FIRST, uploaded_by="operator")
    store(SECOND, uploaded_by="operator")

    assert first.weights() == {"INFY": Decimal("40.0000"), "TCS": Decimal("60.0000")}


@pytest.mark.django_db
def test_weights_are_readable_as_a_mapping(universe):
    portfolio = store(FIRST, uploaded_by="operator")
    assert portfolio.weights()["INFY"] == Decimal("40.0000")


@pytest.mark.django_db
def test_a_refused_file_stores_nothing(universe):
    with pytest.raises(UploadRefused):
        store("symbol,target_pct\nINFY,40\nTCS,40\n", uploaded_by="operator")

    assert ModelPortfolio.objects.count() == 0
    assert ModelPortfolioRow.objects.count() == 0


@pytest.mark.django_db
def test_a_refusal_carries_every_reason(universe):
    with pytest.raises(UploadRefused) as refused:
        store("symbol,target_pct\nINFY,0\nNOSUCHCO,100\n", uploaded_by="operator")

    assert len(refused.value.errors) >= 2


@pytest.mark.django_db
def test_a_refusal_is_audited(universe):
    """An upload that was refused is still something that happened."""
    with pytest.raises(UploadRefused):
        store("symbol,target_pct\nINFY,40\n", uploaded_by="operator", source_filename="bad.csv")

    event = AuditEvent.objects.get(action="model_portfolio.rejected")
    assert event.after["filename"] == "bad.csv"


@pytest.mark.django_db
def test_an_upload_is_audited_with_its_diff(universe):
    store(FIRST, uploaded_by="operator")
    store(SECOND, uploaded_by="operator")

    event = AuditEvent.objects.filter(action="model_portfolio.uploaded").latest("occurred_at")
    assert event.after["version"] == 2
    assert any(change["symbol"] == "HDFCBANK" for change in event.after["diff"])


@pytest.mark.django_db
def test_re_uploading_the_current_version_is_refused(universe):
    """A common accident. Not an error, but it must not create a version that
    changes nothing."""
    store(FIRST, uploaded_by="operator")

    with pytest.raises(UploadRefused, match="identical"):
        store(FIRST, uploaded_by="operator")


@pytest.mark.django_db
def test_an_identical_re_upload_can_be_forced(universe):
    store(FIRST, uploaded_by="operator")
    portfolio = store(FIRST, uploaded_by="operator", allow_identical=True, note="re-issued")

    assert portfolio.version == 2


@pytest.mark.django_db
def test_an_identical_file_is_recognised_across_ordering(universe):
    store(FIRST, uploaded_by="operator")
    reordered = validate("symbol,target_pct\nTCS,60\nINFY,40\n")

    assert is_identical_to_current(reordered) is True


# -- the diff --------------------------------------------------------------


@pytest.mark.django_db
def test_the_first_upload_diffs_as_all_additions(universe):
    diff = diff_against_current(validate(FIRST))

    assert diff.is_first is True
    assert {change.symbol for change in diff.added} == {"INFY", "TCS"}


@pytest.mark.django_db
def test_a_diff_names_what_arrives_leaves_and_moves(universe):
    store(FIRST, uploaded_by="operator")

    diff = diff_against_current(validate(SECOND))

    assert [c.symbol for c in diff.added] == ["HDFCBANK"]
    assert {c.symbol for c in diff.changed} == {"INFY", "TCS"}
    assert diff.removed == []


@pytest.mark.django_db
def test_a_diff_reports_a_removal(universe):
    store(SECOND, uploaded_by="operator")

    diff = diff_against_current(validate(FIRST))

    assert [c.symbol for c in diff.removed] == ["HDFCBANK"]


@pytest.mark.django_db
def test_a_diff_carries_the_size_of_each_move(universe):
    store(FIRST, uploaded_by="operator")

    diff = diff_against_current(validate(SECOND))
    infy = next(c for c in diff.changed if c.symbol == "INFY")

    assert infy.before == Decimal("40.0000")
    assert infy.after == Decimal("30")
    assert infy.delta == Decimal("-10")


@pytest.mark.django_db
def test_an_unchanged_portfolio_diffs_empty(universe):
    store(FIRST, uploaded_by="operator")
    assert diff_against_current(validate(FIRST)).is_empty is True


@pytest.mark.django_db
def test_validation_does_not_store_anything(universe):
    """The diff is shown before the upload takes effect, so validating must
    leave no trace (D14)."""
    validate(FIRST)
    assert ModelPortfolio.objects.count() == 0
