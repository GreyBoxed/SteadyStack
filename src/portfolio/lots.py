"""Turning fills into lots.

Every buy fill creates exactly one lot, in the same transaction as the fill
(D11). That timing is the whole design:

  * per fill rather than per order, so an order that fills in three parts at
    three prices produces three lots with three bases, which is what actually
    happened;
  * inside the fill's transaction, so the `kite_trade_id` unique constraint is
    sufficient on its own to stop a replayed postback creating a second lot;
  * never on placement, so a cancelled order needs no un-consumption at all --
    there is nothing to undo, because nothing was ever written.

A lot's basis is everything the shares cost, charges included, divided by the
quantity. It is written `estimated` and settles to `final` when the contract
note arrives (M35b).
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from orders.models import Fill, OrderSide
from portfolio.charges import cost_of
from portfolio.models import BasisState, Lot

logger = logging.getLogger("steadystack.portfolio")

FOUR_PLACES = Decimal("0.0001")


class LotError(Exception):
    """A lot that cannot be created from what is being described."""


def basis_per_unit(*, gross: Decimal, charges: Decimal, quantity: int) -> Decimal:
    """What one share cost, charges included.

    Four decimal places, because a charge of a few rupees spread over a single
    share still belongs in the basis, and rounding it to paise per share would
    quietly lose money that the ledger is counting.
    """
    if quantity <= 0:
        raise LotError("A lot of no shares has no basis.")
    return ((gross + charges) / quantity).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


@transaction.atomic
def from_fill(fill: Fill) -> Lot | None:
    """Create the lot this fill bought.

    Returns None for a sell fill: those consume lots rather than creating
    them, which is the FIFO path rather than this one.
    """
    order = fill.order

    if order.side != OrderSide.BUY:
        return None

    if Lot.objects.filter(created_from_fill=fill).exists():
        # A replayed postback. The unique trade id normally stops this
        # earlier; this is the second line of defence, and it is cheap.
        return Lot.objects.get(created_from_fill=fill)

    gross, charges = cost_of(
        quantity=fill.quantity,
        price=fill.price,
        side=OrderSide.BUY,
        exchange=order.instrument.exchange,
    )
    per_unit = basis_per_unit(gross=gross, charges=charges.total, quantity=fill.quantity)

    lot = Lot.objects.create(
        instrument=order.instrument,
        acquired_on=fill.executed_at.date(),
        quantity=fill.quantity,
        remaining_quantity=fill.quantity,
        cost_per_unit=per_unit,
        basis_state=BasisState.ESTIMATED,
        created_from_fill=fill,
        trace_id=fill.trace_id or current_trace_id(),
    )

    record(
        "lot.created",
        entity=lot,
        after={
            "instrument": order.instrument_id,
            "quantity": fill.quantity,
            "price": str(fill.price),
            "gross": str(gross),
            "charges": charges.as_dict(),
            "cost_per_unit": str(per_unit),
            "mode": order.mode,
        },
    )
    logger.info(
        "lot created",
        extra=fields(
            instrument=order.instrument_id,
            quantity=fill.quantity,
            cost_per_unit=str(per_unit),
            mode=order.mode,
        ),
    )
    return lot


def paper_lot_ids(instrument_id: str | None = None) -> set:
    """Every lot bought by a paper fill, and every lot that replaced one.

    A lot's book is its origin fill's order mode. A replacement -- written by
    `contract_note.finalise` or a corporate action -- has no fill of its own,
    so the paper set is grown along `superseded_by` from the paper fills'
    lots. Everything else (live fills, external transactions, the opening
    import) is live.
    """
    from orders.models import OrderMode

    lots = Lot.objects.all()
    if instrument_id is not None:
        lots = lots.filter(instrument_id=instrument_id)
    ids = set(
        lots.filter(created_from_fill__order__mode=OrderMode.PAPER).values_list("id", flat=True)
    )
    frontier = ids
    while frontier:
        frontier = (
            set(
                Lot.objects.filter(pk__in=frontier, superseded_by__isnull=False).values_list(
                    "superseded_by_id", flat=True
                )
            )
            - ids
        )
        ids |= frontier
    return ids


def in_book(lots, book: str | None, *, instrument_id: str | None = None):
    """*lots* narrowed to one book's; all of them when *book* is None."""
    from orders.models import OrderMode

    if book is None:
        return lots
    paper = paper_lot_ids(instrument_id)
    return lots.filter(pk__in=paper) if book == OrderMode.PAPER else lots.exclude(pk__in=paper)


def reader_book() -> str | None:
    """The book a reader of lots sees when it is not told one (I1).

    Live, the live lots: a paper lot is shares that were never bought, and a
    live reader that counted them would value, rank and reconcile against a
    portfolio that does not exist. Paper, every lot, which is what the paper
    screens have always shown. The one place this is decided, so no reader
    decides it differently.
    """
    from configuration.store import current
    from orders.models import OrderMode

    return OrderMode.LIVE if current().is_live else None


def open_lots(instrument_id: str, *, book: str | None = None):
    """Every lot with shares left, oldest first -- the FIFO order; only
    *book*'s when given."""
    lots = Lot.objects.filter(
        instrument_id=instrument_id,
        remaining_quantity__gt=0,
        superseded_by__isnull=True,
    )
    return in_book(lots, book, instrument_id=instrument_id).order_by("acquired_on", "created_at")


def held_quantity(instrument_id: str, *, book: str | None = None) -> int:
    """What the lots say is held, which is checked against the broker (M42)."""
    return sum(lot.remaining_quantity for lot in open_lots(instrument_id, book=book))


def total_basis(instrument_id: str, *, book: str | None = None) -> Decimal:
    return sum(
        (lot.cost_per_unit * lot.remaining_quantity for lot in open_lots(instrument_id, book=book)),
        Decimal("0"),
    )
