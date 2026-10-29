"""Consuming lots, oldest first.

Called once per sell fill, inside that fill's transaction (D11). Three
consequences, and each one removes a whole class of bug:

  * the `kite_trade_id` unique constraint on `Fill` is sufficient on its own to
    stop a replayed postback consuming lots twice -- no second guard needed;
  * a cancelled order needs no un-consumption, because nothing was consumed at
    placement;
  * an order that sells in three parts consumes against three sets of lots at
    three prices, which is what actually happened.

`remaining_quantity` on a lot is a projection maintained only here. The
authority is the realised-gain rows: `quantity - sum(realisations)`. A
scheduled check asserts the two agree, because a denormalised number that
nobody checks is a number that eventually lies.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from gains.models import RealisedGain
from portfolio.lots import in_book
from portfolio.models import BasisState, Lot

logger = logging.getLogger("steadystack.gains")

FOUR_PLACES = Decimal("0.0001")
ZERO = Decimal("0")


class InsufficientLots(Exception):
    """A sale of more shares than the lots account for.

    Never resolved by inventing a lot. It means the holdings and the lot
    history disagree, which is a divergence for the operator to classify
    (M31a) -- not something this module may decide.
    """


@dataclass
class Consumption:
    realisations: list[RealisedGain] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return sum(row.quantity for row in self.realisations)

    @property
    def cost(self) -> Decimal:
        return sum((row.cost for row in self.realisations), ZERO)

    @property
    def gain(self) -> Decimal:
        return sum((row.gain for row in self.realisations), ZERO)


def open_lots(instrument_id: str, *, book: str | None = None, lock: bool = True):
    """Lots with shares left, in FIFO order; only *book*'s when given.

    Ordered by acquisition date and then by creation, so two lots bought on
    the same day are consumed in the order they were recorded rather than in
    whatever order the database returns them.

    Locked for update by default, for a caller about to consume them. A
    screen that only shows them passes `lock=False`: it has no transaction to
    lock inside, and a lock taken for a page view holds up a sale.
    """
    lots = Lot.objects.select_for_update() if lock else Lot.objects.all()
    lots = lots.filter(
        instrument_id=instrument_id,
        remaining_quantity__gt=0,
        superseded_by__isnull=True,
    )
    return in_book(lots, book, instrument_id=instrument_id).order_by("acquired_on", "created_at")


def available(instrument_id: str, *, book: str | None = None) -> int:
    lots = Lot.objects.filter(
        instrument_id=instrument_id,
        remaining_quantity__gt=0,
        superseded_by__isnull=True,
    )
    lots = in_book(lots, book, instrument_id=instrument_id)
    return lots.aggregate(total=Sum("remaining_quantity"))["total"] or 0


def book_of_disposal(*, fill=None) -> str:
    """The book whose lots a disposal consumes (I6).

    A fill's is its order's mode; an external transaction is always real, so
    live. A paper sale must never take a real lot: it would under-count the
    shares actually held, and cost the paper sale at a real lot's price.
    """
    from orders.models import OrderMode

    return fill.order.mode if fill is not None else OrderMode.LIVE


def settlement_window(instrument_id: str, *, on: date, book: str | None = None) -> tuple[int, int]:
    """The range of holdings that settlement alone can explain on *on* (M31c).

    Returns `(delivered, held)`: the quantity whose settlement has landed by
    the end of *on*, and everything the lots hold regardless. A CNC buy
    credits after settlement, so between those two numbers there is no fact
    to be had -- the broker may reasonably report either end.

    A range rather than a single figure because the two sides of the
    comparison lag differently. A live holding comes from the broker and sits
    at `delivered`; a paper one is grown from the lots and sits at `held`.
    Deferring the lots alone would silence the live false alarm and invent the
    mirror image of it in paper, which is how this was found.

    Anything outside the range is a real difference: shares that arrived or
    left by a route this system does not know about, and a question only the
    operator can answer (M31a).

    Only *book*'s lots; with none given, the reader's book (`reader_book`):
    live lots when live, since the broker holds no paper shares (I1).
    """
    from portfolio.lots import reader_book
    from trading_calendar.sessions import settlement_due

    delivered = 0
    held = 0
    lots = Lot.objects.filter(
        instrument_id=instrument_id,
        remaining_quantity__gt=0,
        superseded_by__isnull=True,
    )
    book = book if book is not None else reader_book()
    lots = in_book(lots, book, instrument_id=instrument_id).values_list(
        "acquired_on", "remaining_quantity"
    )

    due_by: dict[date, bool] = {}
    for acquired_on, remaining in lots:
        held += remaining
        if acquired_on not in due_by:
            due_by[acquired_on] = settlement_due(acquired_on) <= on
        if due_by[acquired_on]:
            delivered += remaining

    return delivered, held


def money4(value: Decimal) -> Decimal:
    return Decimal(value).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


@transaction.atomic
def consume(
    *,
    instrument_id: str,
    quantity: int,
    proceeds_per_unit: Decimal,
    disposed_on: date,
    fill=None,
    external_transaction=None,
    charges: Decimal = ZERO,
) -> Consumption:
    """Consume *quantity* shares, oldest lots first, and record what it realised.

    `proceeds_per_unit` is the sale price; the charges on the sale are
    subtracted from the proceeds across the consumed quantity, because a
    charge is part of what the sale returned, exactly as it is part of what a
    purchase cost (D6).

    Only lots of the disposal's own book are consumed (`book_of_disposal`).
    """
    if quantity <= 0:
        raise ValueError("A sale of no shares is not a sale.")

    book = book_of_disposal(fill=fill)
    on_hand = available(instrument_id, book=book)
    if on_hand < quantity:
        raise InsufficientLots(
            f"{quantity} shares of {instrument_id} are being disposed of but the {book} "
            f"lots account for {on_hand}. Classify the difference rather than assuming one."
        )

    # Charges spread over the quantity sold, so each consumed slice carries its
    # share of them.
    charge_per_unit = money4(Decimal(charges) / quantity) if charges else ZERO
    net_per_unit = money4(Decimal(proceeds_per_unit) - charge_per_unit)

    consumption = Consumption()
    outstanding = quantity

    for lot in open_lots(instrument_id, book=book):
        if outstanding <= 0:
            break

        taken = min(lot.remaining_quantity, outstanding)
        cost = money4(lot.cost_per_unit * taken)
        net_proceeds = money4(net_per_unit * taken)

        realisation = RealisedGain.objects.create(
            lot=lot,
            fill=fill,
            external_transaction=external_transaction,
            instrument_id=instrument_id,
            quantity=taken,
            acquired_on=lot.acquired_on,
            disposed_on=disposed_on,
            cost=cost,
            net_proceeds=net_proceeds,
            gain=money4(net_proceeds - cost),
            basis_was_estimated=lot.basis_state == BasisState.ESTIMATED,
            trace_id=current_trace_id(),
        )

        # The projection. The realisation rows above are the authority.
        lot.remaining_quantity -= taken
        lot.save(update_fields=["remaining_quantity"])

        consumption.realisations.append(realisation)
        outstanding -= taken

    if outstanding > 0:
        # The check at the top said there were enough, and the walk found
        # otherwise. That can only be a concurrent consumption of the same
        # lots, and finishing quietly would write a realised statement for
        # fewer shares than were sold -- wrong in a way nothing downstream
        # would notice. The transaction goes back instead.
        raise InsufficientLots(
            f"{quantity} shares of {instrument_id} were being disposed of and the "
            f"lots ran out {outstanding} short part way through. Nothing was recorded."
        )

    record(
        "gain.realised",
        entity_type="gains.RealisedGain",
        entity_id=str(fill.id) if fill else str(external_transaction.id),
        after={
            "instrument": instrument_id,
            "quantity": quantity,
            "lots_consumed": len(consumption.realisations),
            "cost": str(consumption.cost),
            "gain": str(consumption.gain),
            "disposed_on": str(disposed_on),
        },
    )
    logger.info(
        "lots consumed",
        extra=fields(
            instrument=instrument_id,
            quantity=quantity,
            lots=len(consumption.realisations),
            gain=str(consumption.gain),
        ),
    )
    return consumption


def realised_quantity(lot: Lot) -> int:
    """What the realisation rows say has been taken from this lot."""
    return (
        RealisedGain.objects.filter(lot=lot, reverses__isnull=True).aggregate(
            total=Sum("quantity")
        )["total"]
        or 0
    )


def check_projection(instrument_id: str | None = None) -> list[dict]:
    """Assert `remaining == quantity - sum(realisations)` for every lot (D11).

    Scheduled, because a denormalised number nobody checks is one that
    eventually lies -- and this particular number decides which shares a
    future sale consumes.
    """
    lots = Lot.objects.filter(superseded_by__isnull=True)
    if instrument_id:
        lots = lots.filter(instrument_id=instrument_id)

    problems = []
    for lot in lots:
        expected = lot.quantity - realised_quantity(lot)
        if expected != lot.remaining_quantity:
            problems.append(
                {
                    "lot": str(lot.id),
                    "instrument": lot.instrument_id,
                    "remaining": lot.remaining_quantity,
                    "expected": expected,
                }
            )

    if problems:
        record(
            "lots.projection_drift",
            entity_type="portfolio.Lot",
            after={"problems": problems},
        )
        logger.error(
            "lot remaining quantities disagree with the realised rows",
            extra=fields(problems=len(problems)),
        )

    return problems
