"""Recording what happened outside this system.

The account belongs to a person. They can sell at the broker's own app,
transfer shares in from another demat, or receive a bonus issue, and none of
that is prevented -- it is recorded, in a form the lot history can absorb
(M31a, M36d).

Each side means something different to the lots:

  * **buy** -- creates a lot with the operator's stated date and basis, so it
    sits in the right place in the FIFO order;
  * **sell** -- consumes lots oldest first, exactly as a rebalance sell does;
  * **transfer in** -- creates a lot carrying the transferor's acquisition
    date and basis, because the shares did not start existing on arrival;
  * **transfer out** -- a disposal at nil proceeds. The lots are consumed and
    the row is flagged for manual treatment: what a transfer out was *for* is
    a decision this system does not make.

One refusal matters more than the rest. A back-dated buy earlier than the last
matched sale is **refused** (M36a): accepting it would change which lots that
sale consumed, and those realised-gain rows are immutable by construction. It
is routed to a correction instead.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from gains import fifo
from portfolio.models import BasisState, Instrument, Lot
from records.models import ExternalSide, ExternalTransaction, PaidFrom

logger = logging.getLogger("steadystack.records")

ZERO = Decimal("0")


class ExternalRefused(Exception):
    """A record the lot history cannot absorb as described."""


@dataclass(frozen=True)
class Recorded:
    transaction: ExternalTransaction
    lot: Lot | None = None
    consumed: fifo.Consumption | None = None


def last_disposal_on(instrument_id: str) -> date | None:
    """When shares of this instrument were last matched against lots."""
    from gains.models import RealisedGain

    return (
        RealisedGain.objects.filter(instrument_id=instrument_id)
        .order_by("-disposed_on")
        .values_list("disposed_on", flat=True)
        .first()
    )


@transaction.atomic
def record_external(
    *,
    instrument: Instrument,
    side: str,
    quantity: int,
    occurred_on: date,
    actor: str,
    reason: str,
    price: Decimal = ZERO,
    charges: Decimal = ZERO,
    acquired_on: date | None = None,
    paid_from: str = PaidFrom.ZERODHA,
) -> Recorded:
    """Record a movement this system did not cause."""
    if quantity <= 0:
        raise ExternalRefused("A movement of no shares is not a movement.")

    if side in (ExternalSide.BUY, ExternalSide.TRANSFER_IN):
        acquired_on = acquired_on or occurred_on
        _refuse_if_it_would_rewrite_a_matched_sale(instrument.tradingsymbol, acquired_on)

    entry = ExternalTransaction.objects.create(
        instrument=instrument,
        side=side,
        quantity=quantity,
        price=Decimal(price),
        charges=Decimal(charges),
        paid_from=paid_from,
        occurred_on=occurred_on,
        acquired_on=acquired_on,
        reason=reason[:255],
        actor=actor,
        trace_id=current_trace_id(),
        needs_manual_treatment=side == ExternalSide.TRANSFER_OUT,
    )

    lot = None
    consumed = None

    if side in (ExternalSide.BUY, ExternalSide.TRANSFER_IN):
        lot = _create_lot(entry)
    else:
        consumed = _consume(entry)

    from journal.external import post_external

    post_external(entry, consumption=consumed)

    record(
        "external.recorded",
        actor=actor,
        entity=entry,
        after={
            "instrument": instrument.tradingsymbol,
            "side": side,
            "quantity": quantity,
            "price": str(price),
            "occurred_on": str(occurred_on),
            "acquired_on": str(acquired_on) if acquired_on else None,
            "reason": reason,
            "needs_manual_treatment": entry.needs_manual_treatment,
        },
    )
    logger.info(
        "external transaction recorded",
        extra=fields(
            instrument=instrument.tradingsymbol,
            side=side,
            quantity=quantity,
            occurred_on=str(occurred_on),
        ),
    )
    return Recorded(transaction=entry, lot=lot, consumed=consumed)


def _refuse_if_it_would_rewrite_a_matched_sale(instrument_id: str, acquired_on: date) -> None:
    """M36a: a back-dated buy earlier than the last matched sale is refused.

    Accepting it would put a lot *before* shares that have already been
    matched against other lots, changing which ones that sale consumed -- and
    those realised-gain rows are immutable by construction. The fix is a
    correction that supersedes the rows it names, not a quiet insertion.
    """
    last = last_disposal_on(instrument_id)
    if last is None or acquired_on > last:
        return

    raise ExternalRefused(
        f"A lot acquired on {acquired_on} would sit before a sale already matched on "
        f"{last}, which would change which lots that sale consumed. Record a "
        "correction instead."
    )


def _create_lot(entry: ExternalTransaction) -> Lot:
    """A buy or a transfer in becomes a lot with its true clock and basis."""
    quantity = entry.quantity
    gross = entry.price * quantity
    cost_per_unit = ((gross + entry.charges) / quantity).quantize(Decimal("0.0001"))

    return Lot.objects.create(
        instrument=entry.instrument,
        acquired_on=entry.acquired_on or entry.occurred_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=cost_per_unit,
        # The operator's own record, like an opening import: not an estimate
        # from a rate card, and not waiting for a contract note this system
        # will never see.
        basis_state=BasisState.IMPORTED,
        external_transaction=entry,
        trace_id=current_trace_id(),
    )


def _consume(entry: ExternalTransaction) -> fifo.Consumption:
    """A sale or a transfer out consumes lots oldest first."""
    return fifo.consume(
        instrument_id=entry.instrument_id,
        quantity=entry.quantity,
        proceeds_per_unit=entry.price,
        disposed_on=entry.occurred_on,
        charges=entry.charges,
        external_transaction=entry,
    )


def bonus_issue(
    *,
    instrument: Instrument,
    quantity: int,
    occurred_on: date,
    actor: str,
    reason: str = "bonus issue",
) -> list[Lot]:
    """New shares at nil cost, inheriting the dates that earned them (S8).

    The acquisition dates matter: bonus shares are not new money, and dating
    them from the issue would misstate how long the position has been held.
    One lot per existing lot, in proportion, so the clock survives.
    """
    existing = list(
        Lot.objects.filter(
            instrument=instrument, remaining_quantity__gt=0, superseded_by__isnull=True
        ).order_by("acquired_on", "created_at")
    )
    held = sum(lot.remaining_quantity for lot in existing)

    if held <= 0:
        raise ExternalRefused("A bonus cannot be issued against a holding of nothing.")

    created: list[Lot] = []
    allocated = 0

    for index, lot in enumerate(existing):
        if index == len(existing) - 1:
            share = quantity - allocated  # the last lot takes the remainder
        else:
            share = (quantity * lot.remaining_quantity) // held
        if share <= 0:
            continue

        created.append(
            Lot.objects.create(
                instrument=instrument,
                acquired_on=lot.acquired_on,
                quantity=share,
                remaining_quantity=share,
                cost_per_unit=ZERO,
                basis_state=BasisState.IMPORTED,
                trace_id=current_trace_id(),
            )
        )
        allocated += share

    record(
        "corporate_action.bonus",
        actor=actor,
        entity_type="portfolio.Lot",
        entity_id=instrument.tradingsymbol,
        after={
            "instrument": instrument.tradingsymbol,
            "quantity": quantity,
            "lots_created": len(created),
            "occurred_on": str(occurred_on),
            "reason": reason,
        },
    )
    logger.info(
        "bonus shares recorded",
        extra=fields(instrument=instrument.tradingsymbol, quantity=quantity),
    )
    return created
