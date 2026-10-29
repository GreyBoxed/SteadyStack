"""What a fill does to the lot history.

A fill on its own is a row in a table. The record is what it did to the lots:
a buy creates one, a sell consumes them oldest first and writes the immutable
realised rows that say which (M35, M36). Until that happens the shares exist
in the order book and nowhere the portfolio is reconciled from.

This is one function because there are two paths that make fills -- the paper
feed and the broker's trade book -- and they must reach the same place. Left
to each caller, one of them gets it and the other does not, which is exactly
what happened: paper days created lots, and a live day's fills arrived through
`reconcile.record_fills` and did nothing at all.

Idempotent on both sides. `Fill.kite_trade_id` is unique, so a replayed
postback rarely gets this far, and where it does the lot's `created_from_fill`
and the realised rows' `fill` carry the second line of defence. Consuming a
lot twice is not recoverable by reconciliation -- the shares would be gone
from the history and still held at the broker -- so it is guarded here rather
than trusted upstream.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.events import record
from audit.logging import fields
from gains import fifo
from gains.models import RealisedGain
from journal import trades as journal_trades
from orders.models import Fill, OrderSide
from portfolio import lots
from portfolio.charges import cost_of
from portfolio.models import Lot

logger = logging.getLogger("steadystack.orders")

ZERO = Decimal("0")


@dataclass(frozen=True)
class Booked:
    """What the fill turned into."""

    lot: Lot | None = None
    consumption: fifo.Consumption | None = None

    @property
    def already_booked(self) -> bool:
        return self.lot is None and self.consumption is None


@transaction.atomic
def book(fill: Fill) -> Booked:
    """Turn one fill into the lots it bought or consumed.

    Charges are estimated from the rate card at fill time and stored on the
    fill, so the basis can be settled the same evening and checked against the
    contract note later (M35b, S5).
    """
    order = fill.order
    charges = _charge(fill)

    if order.side == OrderSide.BUY:
        lot = lots.from_fill(fill)
        journal_trades.post_fill(fill)
        return Booked(lot=lot)

    if RealisedGain.objects.filter(fill=fill).exists():
        # Already consumed. Never again: unlike a duplicated lot, a
        # double consumption takes shares out of the history that are still
        # held, and no later reconciliation puts them back.
        logger.info("fill already booked against lots", extra=fields(fill=str(fill.id)))
        return Booked()

    try:
        consumption = fifo.consume(
            instrument_id=order.instrument_id,
            quantity=fill.quantity,
            proceeds_per_unit=fill.price,
            # The trading day in IST, not the UTC date. They are the same
            # through an ordinary session and differ either side of it, which
            # is exactly where a test or a late paper fill lands.
            disposed_on=timezone.localdate(fill.executed_at),
            fill=fill,
            charges=charges,
        )
    except fifo.InsufficientLots as shortfall:
        # The shares are gone either way -- the exchange did it, and a fill
        # this system refuses to record is a fill nobody can reconcile. So the
        # fill stands and the shortfall goes where every other disagreement
        # between the holding and the lots goes: to the operator, through the
        # evening scan, as a divergence to classify (M31a). Raising here would
        # instead take down the tick daemon and roll back the fill on every
        # retry, for ever.
        record(
            "fill.unbooked",
            entity=fill,
            after={
                "instrument": order.instrument_id,
                "quantity": fill.quantity,
                "reason": str(shortfall),
            },
        )
        logger.error(
            "a sell filled against lots that cannot cover it",
            extra=fields(
                fill=str(fill.id),
                instrument=order.instrument_id,
                quantity=fill.quantity,
                available=fifo.available(order.instrument_id, book=order.mode),
            ),
        )
        return Booked()

    journal_trades.post_fill(fill, consumption=consumption)
    return Booked(consumption=consumption)


def _charge(fill: Fill) -> Decimal:
    """Estimate this fill's charges and keep them on the row.

    Recomputed rather than trusted when the row already carries a figure: the
    only writer is this function, and a stored zero means it has not run.
    """
    if fill.charges and fill.charges > ZERO:
        return fill.charges

    _gross, charges = cost_of(
        quantity=fill.quantity,
        price=fill.price,
        side=fill.order.side,
        exchange=fill.order.instrument.exchange,
        dp_charged=_dp_already_charged(fill),
    )
    fill.charges = charges.total
    fill.save(update_fields=["charges"])
    return charges.total


def _dp_already_charged(fill: Fill) -> bool:
    """Whether an earlier fill has already paid today's depository charge for
    this scrip.

    It is a flat fee per scrip per day, and the sell ladder fills one slice at
    a time by design -- so charging it per fill would bill a twenty-slice sale
    twenty times over, and the realised-gain statement would stop reconciling
    against the broker's own line by line.
    """
    if fill.order.side != OrderSide.SELL:
        return False

    return (
        Fill.objects.filter(
            order__instrument_id=fill.order.instrument_id,
            order__side=OrderSide.SELL,
            executed_at__date=timezone.localdate(fill.executed_at),
            charges__gt=ZERO,
        )
        .exclude(pk=fill.pk)
        .exists()
    )
