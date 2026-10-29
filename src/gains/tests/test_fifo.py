"""FIFO consumption.

FIFO is kept because the broker matches that way, and two records that agree
are worth more than two that do not. Nothing here is tax-aware: there are no
rates, no holding-period thresholds and no reporting, and the holding period
is recorded only because it is a fact about the trade.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.db import DatabaseError

from audit.models import AuditEvent
from gains import fifo
from gains.models import RealisedGain
from portfolio.instruments import refresh
from portfolio.models import BasisState, Lot

pytestmark = pytest.mark.django_db

JAN = date(2024, 1, 15)
JUN = date(2024, 6, 20)
SEP = date(2026, 9, 22)


@pytest.fixture
def instrument(db):
    refresh(
        [
            {
                "tradingsymbol": "INFY",
                "instrument_token": 408065,
                "name": "INFY",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "segment": "NSE",
                "tick_size": 0.05,
                "lot_size": 1,
            }
        ]
    )
    from portfolio.models import Instrument

    return Instrument.objects.get(pk="INFY")


def a_lot(instrument, *, acquired_on, quantity, cost, state=BasisState.FINAL):
    return Lot.objects.create(
        instrument=instrument,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal(cost),
        basis_state=state,
    )


# -- oldest first ----------------------------------------------------------


def test_the_oldest_lot_is_consumed_first(instrument):
    old = a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    new = a_lot(instrument, acquired_on=JUN, quantity=5, cost="1500")

    fifo.consume(
        instrument_id="INFY",
        quantity=3,
        proceeds_per_unit=Decimal("2000"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    old.refresh_from_db()
    new.refresh_from_db()
    assert old.remaining_quantity == 2
    assert new.remaining_quantity == 5


def test_a_sale_spanning_two_lots_consumes_both(instrument):
    a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    a_lot(instrument, acquired_on=JUN, quantity=5, cost="1500")

    consumption = fifo.consume(
        instrument_id="INFY",
        quantity=7,
        proceeds_per_unit=Decimal("2000"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    assert len(consumption.realisations) == 2
    assert [r.quantity for r in consumption.realisations] == [5, 2]


def test_each_realisation_carries_its_own_lots_cost(instrument):
    """Which is the whole point: a share bought at 1000 and one bought at 1500
    realise different gains at the same sale price."""
    a_lot(instrument, acquired_on=JAN, quantity=1, cost="1000")
    a_lot(instrument, acquired_on=JUN, quantity=1, cost="1500")

    consumption = fifo.consume(
        instrument_id="INFY",
        quantity=2,
        proceeds_per_unit=Decimal("2000"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    gains = sorted(r.gain for r in consumption.realisations)
    assert gains == [Decimal("500.0000"), Decimal("1000.0000")]


def test_two_lots_from_the_same_day_consume_in_the_order_recorded(instrument):
    first = a_lot(instrument, acquired_on=JAN, quantity=2, cost="1000")
    a_lot(instrument, acquired_on=JAN, quantity=2, cost="1100")

    fifo.consume(
        instrument_id="INFY",
        quantity=2,
        proceeds_per_unit=Decimal("2000"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    first.refresh_from_db()
    assert first.remaining_quantity == 0


# -- what a realisation records -------------------------------------------


def test_a_realisation_states_the_gain(instrument):
    a_lot(instrument, acquired_on=JAN, quantity=2, cost="1000")

    fifo.consume(
        instrument_id="INFY",
        quantity=2,
        proceeds_per_unit=Decimal("1500"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    row = RealisedGain.objects.get()
    assert row.cost == Decimal("2000.0000")
    assert row.net_proceeds == Decimal("3000.0000")
    assert row.gain == Decimal("1000.0000")


def test_charges_on_the_sale_reduce_the_proceeds(instrument):
    """A charge is part of what the sale returned, exactly as it is part of
    what a purchase cost (D6)."""
    a_lot(instrument, acquired_on=JAN, quantity=2, cost="1000")

    fifo.consume(
        instrument_id="INFY",
        quantity=2,
        proceeds_per_unit=Decimal("1500"),
        disposed_on=SEP,
        charges=Decimal("20"),
        external_transaction=_an_external(instrument),
    )

    assert RealisedGain.objects.get().net_proceeds == Decimal("2980.0000")


def test_a_realisation_stands_alone_without_its_lot(instrument):
    """The acquisition date is copied, so the row explains itself."""
    a_lot(instrument, acquired_on=JAN, quantity=1, cost="1000")

    fifo.consume(
        instrument_id="INFY",
        quantity=1,
        proceeds_per_unit=Decimal("1500"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    row = RealisedGain.objects.get()
    assert row.acquired_on == JAN
    assert row.disposed_on == SEP
    assert row.holding_days == (SEP - JAN).days


def test_a_realisation_flags_an_unsettled_basis(instrument):
    """Gains computed from an estimate are marked as such, so nobody reads
    them as final (M35b)."""
    a_lot(instrument, acquired_on=JAN, quantity=1, cost="1000", state=BasisState.ESTIMATED)

    fifo.consume(
        instrument_id="INFY",
        quantity=1,
        proceeds_per_unit=Decimal("1500"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    assert RealisedGain.objects.get().basis_was_estimated is True


def test_consuming_is_audited(instrument):
    a_lot(instrument, acquired_on=JAN, quantity=1, cost="1000")

    fifo.consume(
        instrument_id="INFY",
        quantity=1,
        proceeds_per_unit=Decimal("1500"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    assert AuditEvent.objects.filter(action="gain.realised").exists()


# -- what it refuses -------------------------------------------------------


def test_selling_more_than_the_lots_hold_is_refused(instrument):
    """Never resolved by inventing a lot: it means the holdings and the lot
    history disagree, which is the operator's to classify."""
    a_lot(instrument, acquired_on=JAN, quantity=2, cost="1000")

    with pytest.raises(fifo.InsufficientLots, match="account for 2"):
        fifo.consume(
            instrument_id="INFY",
            quantity=5,
            proceeds_per_unit=Decimal("1500"),
            disposed_on=SEP,
            external_transaction=_an_external(instrument),
        )


def test_a_refused_consumption_touches_nothing(instrument):
    lot = a_lot(instrument, acquired_on=JAN, quantity=2, cost="1000")

    with pytest.raises(fifo.InsufficientLots):
        fifo.consume(
            instrument_id="INFY",
            quantity=5,
            proceeds_per_unit=Decimal("1500"),
            disposed_on=SEP,
            external_transaction=_an_external(instrument),
        )

    lot.refresh_from_db()
    assert lot.remaining_quantity == 2
    assert RealisedGain.objects.count() == 0


def test_a_superseded_lot_is_not_consumed(instrument):
    """It has been replaced; consuming it would spend shares twice."""
    original = a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    replacement = a_lot(instrument, acquired_on=JAN, quantity=5, cost="1100")
    Lot.objects.filter(pk=original.pk).update(superseded_by=replacement)

    assert fifo.available("INFY") == 5


def test_selling_nothing_is_not_a_sale(instrument):
    with pytest.raises(ValueError, match="not a sale"):
        fifo.consume(
            instrument_id="INFY",
            quantity=0,
            proceeds_per_unit=Decimal("1500"),
            disposed_on=SEP,
            external_transaction=_an_external(instrument),
        )


# -- the projection (D11) --------------------------------------------------


def test_the_projection_agrees_after_a_consumption(instrument):
    a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")

    fifo.consume(
        instrument_id="INFY",
        quantity=3,
        proceeds_per_unit=Decimal("1500"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    assert fifo.check_projection("INFY") == []


def test_drift_between_the_projection_and_the_rows_is_reported(instrument):
    """The remaining quantity decides which shares a future sale consumes, so
    a number nobody checks is one that eventually lies."""
    lot = a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    Lot.objects.filter(pk=lot.pk).update(remaining_quantity=2)

    problems = fifo.check_projection("INFY")

    assert len(problems) == 1
    assert problems[0]["expected"] == 5
    assert AuditEvent.objects.filter(action="lots.projection_drift").exists()


@pytest.mark.django_db(transaction=True)
def test_a_realised_gain_cannot_be_rewritten(instrument):
    """A number reported and then quietly changed is worse than one that was
    wrong and corrected in public."""
    a_lot(instrument, acquired_on=JAN, quantity=1, cost="1000")
    fifo.consume(
        instrument_id="INFY",
        quantity=1,
        proceeds_per_unit=Decimal("1500"),
        disposed_on=SEP,
        external_transaction=_an_external(instrument),
    )

    with pytest.raises(DatabaseError, match="append-only"):
        RealisedGain.objects.update(gain=Decimal("0"))


def _an_external(instrument):
    from records.models import ExternalSide, ExternalTransaction

    return ExternalTransaction.objects.create(
        instrument=instrument,
        side=ExternalSide.SELL,
        quantity=1,
        price=Decimal("1500"),
        occurred_on=SEP,
        reason="test",
        actor="operator",
    )
