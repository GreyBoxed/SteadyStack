"""Realised and unrealised gains, reported as economics and nothing else.

The broker's tax P&L does classification, rates and financial years. This
report exists so those numbers can be checked against records the system can
prove -- which means proceeds, cost basis and gain, and stopping there (M37).
"""

from datetime import date
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

JAN = date(2026, 1, 15)
SEP = date(2026, 9, 22)


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


def a_realisation(
    instrument, *, quantity=1, cost="1000", proceeds="1200", disposed_on=SEP, estimated=False
):
    from gains.models import RealisedGain
    from portfolio.models import Lot

    lot = Lot.objects.create(
        instrument=instrument,
        acquired_on=JAN,
        quantity=quantity,
        remaining_quantity=0,
        cost_per_unit=Decimal(cost),
    )
    return RealisedGain.objects.create(
        lot=lot,
        instrument=instrument,
        quantity=quantity,
        acquired_on=JAN,
        disposed_on=disposed_on,
        cost=Decimal(cost) * quantity,
        net_proceeds=Decimal(proceeds) * quantity,
        gain=(Decimal(proceeds) - Decimal(cost)) * quantity,
        basis_was_estimated=estimated,
    )


def test_a_sale_is_reported_as_proceeds_cost_and_gain(instrument):
    from performance import gains

    a_realisation(instrument, quantity=2, cost="1000", proceeds="1200")

    report = gains.realised()

    assert report.total_proceeds == Decimal("2400.00")
    assert report.total_cost == Decimal("2000.00")
    assert report.total_gain == Decimal("400.00")
    assert report.rows[0].instrument_id == "INFY"


def test_a_loss_is_reported_as_a_negative_gain(instrument):
    from performance import gains

    a_realisation(instrument, quantity=1, cost="1200", proceeds="1000")

    assert gains.realised().total_gain == Decimal("-200.00")


def test_the_range_is_inclusive_at_both_ends(instrument):
    from performance import gains

    a_realisation(instrument, disposed_on=date(2026, 9, 1))
    a_realisation(instrument, disposed_on=date(2026, 9, 30))

    assert len(gains.realised(start=date(2026, 9, 1), end=date(2026, 9, 30)).rows) == 2
    assert len(gains.realised(start=date(2026, 9, 2), end=date(2026, 9, 29)).rows) == 0


def test_a_reversal_cancels_the_row_it_reverses(instrument):
    """A correction reverses a reported gain rather than deleting it, so the
    report must net the pair to zero instead of showing the sale twice."""
    from gains.models import RealisedGain
    from performance import gains

    original = a_realisation(instrument, quantity=1, cost="1000", proceeds="1200")
    RealisedGain.objects.create(
        lot=original.lot,
        instrument=instrument,
        quantity=original.quantity,
        acquired_on=original.acquired_on,
        disposed_on=original.disposed_on,
        cost=-original.cost,
        net_proceeds=-original.net_proceeds,
        gain=-original.gain,
        reverses=original,
    )

    assert gains.realised().total_gain == Decimal("0.00")


def test_an_estimated_basis_is_flagged(instrument):
    """A lot whose contract note has not landed reports a gain that may move."""
    from performance import gains

    a_realisation(instrument, estimated=True)

    report = gains.realised()

    assert report.any_estimated is True
    assert report.rows[0].estimated is True


def test_the_report_says_nothing_about_tax(instrument):
    """Decision 16: no classification, no rates, no financial year."""
    from performance import gains

    a_realisation(instrument)
    row = gains.realised().rows[0]

    forbidden = {"short_term", "long_term", "tax", "rate", "financial_year", "exemption"}
    assert forbidden.isdisjoint(vars(row))


# -- unrealised (M38) ----------------------------------------------------


def an_open_lot(instrument, *, quantity=2, cost="1000", acquired_on=JAN, estimated=False):
    from portfolio.models import BasisState, Lot

    return Lot.objects.create(
        instrument=instrument,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal(cost),
        basis_state=BasisState.ESTIMATED if estimated else BasisState.FINAL,
    )


def test_an_open_lot_is_reported_per_lot(instrument):
    from performance import gains

    an_open_lot(instrument, quantity=2, cost="1000", acquired_on=JAN)

    report = gains.unrealised({"INFY": Decimal("1100")})

    row = report.rows[0]
    assert row.acquired_on == JAN
    assert row.cost == Decimal("2000.00")
    assert row.value == Decimal("2200.00")
    assert row.gain == Decimal("200.00")


def test_a_partly_consumed_lot_reports_only_what_remains(instrument):
    from performance import gains

    lot = an_open_lot(instrument, quantity=4, cost="1000")
    lot.remaining_quantity = 1
    lot.save(update_fields=["remaining_quantity"])

    assert gains.unrealised({"INFY": Decimal("1100")}).total_value == Decimal("1100.00")


def test_an_unpriced_instrument_is_named_not_valued(instrument):
    from performance import gains

    an_open_lot(instrument)

    report = gains.unrealised({})

    assert report.unpriced == ["INFY"]
    assert report.rows == []


def test_lots_are_reported_oldest_first(instrument):
    """The same order FIFO will consume them in, so the report reads as the
    queue it actually is."""
    from performance import gains

    an_open_lot(instrument, acquired_on=SEP)
    an_open_lot(instrument, acquired_on=JAN)

    report = gains.unrealised({"INFY": Decimal("1100")})

    assert [row.acquired_on for row in report.rows] == [JAN, SEP]


def test_an_estimated_basis_is_flagged_here_too(instrument):
    from performance import gains

    an_open_lot(instrument, estimated=True)

    assert gains.unrealised({"INFY": Decimal("1100")}).any_estimated is True


# -- withheld under divergence (M31b) ------------------------------------


@pytest.fixture
def in_doubt(instrument):
    """An unresolved divergence on INFY: the lots say one quantity, the broker
    another, and nobody has said which is right yet."""
    from records.models import Divergence

    return Divergence.objects.create(
        instrument=instrument,
        detected_on=SEP,
        expected_quantity=10,
        actual_quantity=8,
    )


def test_realised_gains_are_withheld_for_an_instrument_in_doubt(instrument, in_doubt):
    from performance import gains

    a_realisation(instrument)

    report = gains.realised()

    assert report.rows == []
    assert report.withheld == ["INFY"]
    assert report.total_gain == Decimal("0.00")


def test_unrealised_gains_are_withheld_too(instrument, in_doubt):
    from performance import gains

    an_open_lot(instrument)

    report = gains.unrealised({"INFY": Decimal("1100")})

    assert report.rows == []
    assert report.withheld == ["INFY"]


def test_a_resolved_divergence_stops_withholding(instrument, in_doubt):
    """Withholding is a state, not a verdict: once the operator says what the
    difference was, the figures come back."""
    from performance import gains
    from records import divergence as divergence_reader
    from records.models import DivergenceClassification

    divergence_reader.classify(
        in_doubt,
        classification=DivergenceClassification.SYSTEM_ERROR,
        actor="operator",
        note="miscount",
    )

    an_open_lot(instrument)

    assert gains.unrealised({"INFY": Decimal("1100")}).withheld == []


def test_a_divergence_on_one_name_does_not_withhold_another(instrument, in_doubt):
    """Withholding everything because of one name would be its own kind of lie."""
    from performance import gains
    from portfolio.models import Instrument

    other = Instrument.objects.create(tradingsymbol="TCS", instrument_token=2953217, name="TCS")
    an_open_lot(other, quantity=1, cost="3000")
    an_open_lot(instrument)

    report = gains.unrealised({"INFY": Decimal("1100"), "TCS": Decimal("3200")})

    assert [row.instrument_id for row in report.rows] == ["TCS"]
    assert report.withheld == ["INFY"]
    assert report.total_value == Decimal("3200.00")
