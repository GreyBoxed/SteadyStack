"""Splits and consolidations, which change lots without changing money.

A five-for-one split turns 10 shares at ₹1,000 into 50 at ₹200. Nothing was
bought and nothing was sold; the same money is divided differently. So the
lots are **superseded** rather than edited (M36b): the originals stay
readable, the replacements carry the original acquisition dates forward, and
the correction that caused them says so.

Carrying the dates is the part that matters. Dating the new lots from the
split would say the position was acquired on the day the company restructured
its share capital, which is not true of a single share.

On the arithmetic: the replacement carries the same *total* cost as the
original, divided over the new quantity. That total cannot always be preserved
to the last paise, because a per-share cost is stored to four places and a
ratio like 7 shares into 21 gives 333.3333... recurring. The error is bounded
by half of the last stored place per share -- well under a paise on any
realistic holding -- and it is a property of representing a basis per share at
all, not of this operation. It is asserted as a bound rather than described as
exactness, because a comment claiming exactness would be wrong.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from django.db import transaction

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from portfolio.models import Instrument, Lot
from records.models import Correction

logger = logging.getLogger("steadystack.records")

FOUR_PLACES = Decimal("0.0001")


class CorporateActionRefused(Exception):
    """A ratio or a holding the lot history cannot absorb."""


@dataclass
class Restructure:
    superseded: list[Lot] = field(default_factory=list)
    created: list[Lot] = field(default_factory=list)

    @property
    def quantity_before(self) -> int:
        return sum(lot.remaining_quantity for lot in self.superseded)

    @property
    def quantity_after(self) -> int:
        return sum(lot.quantity for lot in self.created)

    @property
    def cost_before(self) -> Decimal:
        return sum(
            (lot.cost_per_unit * lot.remaining_quantity for lot in self.superseded),
            Decimal("0"),
        )

    @property
    def cost_after(self) -> Decimal:
        return sum((lot.cost_per_unit * lot.quantity for lot in self.created), Decimal("0"))


@transaction.atomic
def apply_ratio(
    *,
    instrument: Instrument,
    new_shares: int,
    per_old_shares: int,
    effective_on: date,
    actor: str,
    reason: str = "",
) -> Restructure:
    """Apply `new_shares : per_old_shares` to every open lot.

    5-for-1 split is `new_shares=5, per_old_shares=1`. A 1-for-10
    consolidation is `new_shares=1, per_old_shares=10`.
    """
    if new_shares <= 0 or per_old_shares <= 0:
        raise CorporateActionRefused("A ratio needs two positive numbers.")

    ratio = Fraction(new_shares, per_old_shares)

    lots = list(
        Lot.objects.select_for_update()
        .filter(instrument=instrument, remaining_quantity__gt=0, superseded_by__isnull=True)
        .order_by("acquired_on", "created_at")
    )
    if not lots:
        raise CorporateActionRefused(
            f"There are no open lots of {instrument.tradingsymbol} to restructure."
        )

    result = Restructure()

    for lot in lots:
        new_quantity = int(Fraction(lot.remaining_quantity) * ratio)
        if new_quantity <= 0:
            raise CorporateActionRefused(
                f"{ratio} would reduce a lot of {lot.remaining_quantity} shares to nothing. "
                "A fractional-share outcome is a decision for a person, not this program."
            )

        # The same money, divided differently: total cost is preserved exactly.
        total_cost = lot.cost_per_unit * lot.remaining_quantity
        replacement = Lot.objects.create(
            instrument=instrument,
            # The original clock, carried forward (M36b).
            acquired_on=lot.acquired_on,
            quantity=new_quantity,
            remaining_quantity=new_quantity,
            cost_per_unit=(total_cost / new_quantity).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP),
            basis_state=lot.basis_state,
            opening_import=lot.opening_import,
            trace_id=current_trace_id(),
        )

        Lot.objects.filter(pk=lot.pk).update(superseded_by=replacement, remaining_quantity=0)

        Correction.objects.create(
            target_table="portfolio.Lot",
            target_id=str(lot.id),
            reason=reason or f"{new_shares}-for-{per_old_shares} on {effective_on}",
            actor=actor,
            trace_id=current_trace_id(),
            before={
                "quantity": lot.remaining_quantity,
                "cost_per_unit": str(lot.cost_per_unit),
                "acquired_on": str(lot.acquired_on),
            },
            after={
                "quantity": new_quantity,
                "cost_per_unit": str(replacement.cost_per_unit),
                "acquired_on": str(replacement.acquired_on),
                "superseded_by": str(replacement.id),
            },
        )

        result.superseded.append(lot)
        result.created.append(replacement)

    record(
        "corporate_action.applied",
        actor=actor,
        entity_type="portfolio.Instrument",
        entity_id=instrument.tradingsymbol,
        after={
            "instrument": instrument.tradingsymbol,
            "ratio": f"{new_shares}:{per_old_shares}",
            "effective_on": str(effective_on),
            "lots_superseded": len(result.superseded),
            "quantity_before": result.quantity_before,
            "quantity_after": result.quantity_after,
            "cost_before": str(result.cost_before),
            "cost_after": str(result.cost_after),
        },
    )
    logger.warning(
        "corporate action applied to lots",
        extra=fields(
            instrument=instrument.tradingsymbol,
            ratio=f"{new_shares}:{per_old_shares}",
            lots=len(result.superseded),
        ),
    )
    return result


def split(*, instrument: Instrument, factor: int, effective_on: date, actor: str) -> Restructure:
    """A `factor`-for-one split."""
    return apply_ratio(
        instrument=instrument,
        new_shares=factor,
        per_old_shares=1,
        effective_on=effective_on,
        actor=actor,
        reason=f"{factor}-for-1 split on {effective_on}",
    )


def consolidate(
    *, instrument: Instrument, factor: int, effective_on: date, actor: str
) -> Restructure:
    """A one-for-`factor` consolidation."""
    return apply_ratio(
        instrument=instrument,
        new_shares=1,
        per_old_shares=factor,
        effective_on=effective_on,
        actor=actor,
        reason=f"1-for-{factor} consolidation on {effective_on}",
    )
