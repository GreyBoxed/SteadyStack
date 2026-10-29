"""Settling an estimated basis against what was actually charged (M35b).

A lot is written the evening it is bought, from a rate card, so that carry can
settle the same day. The contract note arrives afterwards and says what the
charges really were. Three things then happen, and none of them edits
anything:

  * the difference between estimated and actual charges is posted to the
    journal as a charge correction dated the day it is known -- the fill's
    entry is never touched, and the next day's cash summary counts it;
  * the lot is **superseded** by a new one carrying the true basis, with the
    original acquisition date and quantity intact;
  * the correction is recorded with an actor and a reason.

Superseding rather than updating costs a row and buys the ability to answer
"what did we think it cost, and when did we find out otherwise?" -- which is
the question somebody asks precisely when a number looks wrong.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction

from audit.context import current_trace_id, today_ist
from audit.events import record
from audit.logging import fields
from portfolio.lots import basis_per_unit
from portfolio.models import BasisState, Lot
from records.models import Correction

logger = logging.getLogger("steadystack.portfolio")

ZERO = Decimal("0")


class FinalisationRefused(Exception):
    """A basis that cannot be settled as described."""


@dataclass(frozen=True)
class Finalisation:
    original: Lot
    replacement: Lot
    charge_difference: Decimal

    @property
    def changed(self) -> bool:
        return self.charge_difference != ZERO


@transaction.atomic
def finalise(
    lot: Lot,
    *,
    actual_charges: Decimal,
    actor: str,
    note_reference: str = "",
    correction_key: str | None = None,
) -> Finalisation:
    """Settle one lot's basis against the contract note.

    *correction_key* keys the journal's charge correction when the caller
    needs to find it again -- the statement's settlement check does (§7.3).
    """
    if lot.superseded_by_id is not None:
        raise FinalisationRefused("That lot has already been superseded.")
    if lot.basis_state == BasisState.FINAL:
        raise FinalisationRefused("That lot's basis is already final.")
    if lot.basis_state != BasisState.ESTIMATED:
        # An imported lot is the operator's own record of what was paid, and
        # has no rate-card estimate for a contract note to settle.
        raise FinalisationRefused("Only a lot with an estimated basis can be finalised.")

    gross = _gross_of(lot)
    estimated_charges = _estimated_charges(lot, gross)
    difference = Decimal(actual_charges) - estimated_charges

    replacement = Lot.objects.create(
        instrument=lot.instrument,
        acquired_on=lot.acquired_on,
        quantity=lot.quantity,
        # The replacement inherits what is left, not the original quantity: a
        # lot half consumed before the note arrives stays half consumed.
        remaining_quantity=lot.remaining_quantity,
        cost_per_unit=basis_per_unit(
            gross=gross, charges=Decimal(actual_charges), quantity=lot.quantity
        ),
        basis_state=BasisState.FINAL,
        opening_import=lot.opening_import,
        # `external_transaction` is a plain `ForeignKey` now (not OneToOne), so
        # both the original and the replacement may carry it -- they describe
        # the same purchase, and the original keeps its provenance readable.
        external_transaction=lot.external_transaction,
        trace_id=current_trace_id(),
    )

    # Posted while `lot.remaining_quantity` is still what the lot holds: the
    # correction splits the difference between shares held and shares sold.
    # The money has already moved; the journal records it as a new entry
    # dated today, never as a change to the fill's (M35b).
    from journal.trades import post_charge_correction

    post_charge_correction(
        lot,
        difference,
        actor=actor,
        reference=note_reference or "the contract note",
        on=today_ist(),
        key=correction_key,
    )

    Lot.objects.filter(pk=lot.pk).update(superseded_by=replacement, remaining_quantity=0)

    Correction.objects.create(
        target_table="portfolio.Lot",
        target_id=str(lot.id),
        reason=f"contract note {note_reference}".strip() or "charges finalised",
        actor=actor,
        trace_id=current_trace_id(),
        before={
            "cost_per_unit": str(lot.cost_per_unit),
            "basis_state": lot.basis_state,
            "estimated_charges": str(estimated_charges),
        },
        after={
            "cost_per_unit": str(replacement.cost_per_unit),
            "basis_state": BasisState.FINAL,
            "actual_charges": str(actual_charges),
            "superseded_by": str(replacement.id),
        },
    )

    record(
        "lot.basis_finalised",
        actor=actor,
        entity=replacement,
        before={"lot": str(lot.id), "cost_per_unit": str(lot.cost_per_unit)},
        after={
            "lot": str(replacement.id),
            "cost_per_unit": str(replacement.cost_per_unit),
            "charge_difference": str(difference),
            "note_reference": note_reference,
        },
    )
    logger.info(
        "lot basis finalised against the contract note",
        extra=fields(
            instrument=lot.instrument_id,
            charge_difference=str(difference),
            note_reference=note_reference,
        ),
    )

    return Finalisation(original=lot, replacement=replacement, charge_difference=difference)


def estimated_charges(lot: Lot) -> Decimal:
    """The charges this lot's basis carries now: what `finalise` measures against."""
    return _estimated_charges(lot, _gross_of(lot))


def _estimated_charges(lot: Lot, gross: Decimal) -> Decimal:
    """The charges the journal already holds for this lot's shares.

    For a lot bought by a fill, the fill's entry carries them exactly, line by
    line; measuring against them means the correction moves precisely what
    the journal is wrong by. The lot's own `cost_per_unit` is rounded to four
    places, so reading the charges back from it would post sub-paisa noise.
    A lot with no posted fill falls back to its basis.
    """
    from django.db.models import Sum

    from journal.accounts import INVESTMENTS
    from journal.models import Component, JournalEntry, JournalLine

    fill = getattr(lot, "created_from_fill", None)
    if fill is not None and JournalEntry.objects.filter(idempotency_key=f"fill:{fill.id}").exists():
        posted = JournalLine.objects.filter(
            entry__idempotency_key=f"fill:{fill.id}", account_id=INVESTMENTS
        ).exclude(component=Component.GROSS)
        return posted.aggregate(total=Sum("amount"))["total"] or ZERO
    return (lot.cost_per_unit * lot.quantity) - gross


def _gross_of(lot: Lot) -> Decimal:
    """What the shares cost before charges.

    Recovered from the fill when there is one, because that is the price the
    exchange actually traded at. An imported lot has no fill, and its basis is
    already what the operator said it was.
    """
    fill = getattr(lot, "created_from_fill", None)
    if fill is not None:
        return fill.price * lot.quantity
    return lot.cost_per_unit * lot.quantity


def estimated_lots(*, on: date | None = None):
    """Lots still waiting for their contract note."""
    lots = Lot.objects.filter(basis_state=BasisState.ESTIMATED, superseded_by__isnull=True)
    if on:
        lots = lots.filter(acquired_on=on)
    return lots.order_by("acquired_on")


def has_estimated_basis(instrument_id: str) -> bool:
    return Lot.objects.filter(
        instrument_id=instrument_id,
        basis_state=BasisState.ESTIMATED,
        superseded_by__isnull=True,
        remaining_quantity__gt=0,
    ).exists()
