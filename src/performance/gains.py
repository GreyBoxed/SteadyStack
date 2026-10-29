"""Realised and unrealised gains (M37, M38).

Economics only. The system knows what it paid, what it received and the
difference; it does not know, and deliberately does not guess, how any of that
is taxed. That is the broker's report to make -- this one exists so it can be
checked against records we can prove.

A reversal is a row whose `reverses` points at the row it undoes. Both stay in
the table, because a reported gain is corrected by reversing it rather than by
deleting it, and both are summed here so the pair nets to zero.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from engine.ledger import money
from gains.models import RealisedGain
from records.divergence import is_in_doubt

logger = logging.getLogger("steadystack.performance")

ZERO = Decimal("0")


@dataclass(frozen=True)
class RealisedRow:
    instrument_id: str
    quantity: int
    acquired_on: date
    disposed_on: date
    cost: Decimal
    proceeds: Decimal
    gain: Decimal
    estimated: bool


@dataclass(frozen=True)
class RealisedReport:
    rows: list[RealisedRow]
    total_proceeds: Decimal
    total_cost: Decimal
    total_gain: Decimal
    withheld: list[str]
    any_estimated: bool


def realised(
    start: date | None = None,
    end: date | None = None,
    instrument_id: str | None = None,
) -> RealisedReport:
    """Every disposal in the range, per sale and per lot consumed.

    Only disposals of the reader's book's lots (I1): live, a paper sale
    realised nothing.
    """
    from portfolio.lots import in_book, reader_book
    from portfolio.models import Lot

    query = RealisedGain.objects.select_related("instrument").order_by("disposed_on", "acquired_on")
    book = reader_book()
    if book is not None:
        query = query.filter(lot__in=in_book(Lot.objects.all(), book))
    if start is not None:
        query = query.filter(disposed_on__gte=start)
    if end is not None:
        query = query.filter(disposed_on__lte=end)
    if instrument_id is not None:
        query = query.filter(instrument_id=instrument_id)

    rows: list[RealisedRow] = []
    withheld: set[str] = set()
    proceeds = cost = gain = ZERO

    for record in query:
        if is_in_doubt(record.instrument_id):
            withheld.add(record.instrument_id)
            continue
        rows.append(
            RealisedRow(
                instrument_id=record.instrument_id,
                quantity=record.quantity,
                acquired_on=record.acquired_on,
                disposed_on=record.disposed_on,
                cost=money(record.cost),
                proceeds=money(record.net_proceeds),
                gain=money(record.gain),
                estimated=record.basis_was_estimated,
            )
        )
        proceeds += record.net_proceeds
        cost += record.cost
        gain += record.gain

    return RealisedReport(
        rows=rows,
        total_proceeds=money(proceeds),
        total_cost=money(cost),
        total_gain=money(gain),
        withheld=sorted(withheld),
        any_estimated=any(row.estimated for row in rows),
    )


@dataclass(frozen=True)
class UnrealisedRow:
    instrument_id: str
    acquired_on: date
    quantity: int
    cost_per_unit: Decimal
    cost: Decimal
    price: Decimal
    value: Decimal
    gain: Decimal
    estimated: bool


@dataclass(frozen=True)
class UnrealisedReport:
    rows: list[UnrealisedRow]
    total_cost: Decimal
    total_value: Decimal
    total_gain: Decimal
    withheld: list[str]
    unpriced: list[str]
    any_estimated: bool


def unrealised(prices: dict[str, Decimal]) -> UnrealisedReport:
    """Every open lot at the prices given: acquisition date, basis, value, gain.

    Oldest first, which is the order FIFO will consume them in, so the report
    reads as the queue it actually is.
    """
    from portfolio.lots import in_book, reader_book
    from portfolio.models import BasisState, Lot

    open_lots = (
        in_book(
            Lot.objects.filter(remaining_quantity__gt=0, superseded_by__isnull=True),
            reader_book(),
        )
        .select_related("instrument")
        .order_by("acquired_on", "created_at")
    )

    rows: list[UnrealisedRow] = []
    withheld: set[str] = set()
    unpriced: set[str] = set()
    cost = value = ZERO

    for lot in open_lots:
        if is_in_doubt(lot.instrument_id):
            withheld.add(lot.instrument_id)
            continue

        price = prices.get(lot.instrument_id)
        if price is None or price <= ZERO:
            unpriced.add(lot.instrument_id)
            continue

        lot_cost = lot.cost_per_unit * lot.remaining_quantity
        lot_value = price * lot.remaining_quantity

        rows.append(
            UnrealisedRow(
                instrument_id=lot.instrument_id,
                acquired_on=lot.acquired_on,
                quantity=lot.remaining_quantity,
                cost_per_unit=lot.cost_per_unit,
                cost=money(lot_cost),
                price=price,
                value=money(lot_value),
                gain=money(lot_value - lot_cost),
                estimated=lot.basis_state == BasisState.ESTIMATED,
            )
        )
        cost += lot_cost
        value += lot_value

    return UnrealisedReport(
        rows=rows,
        total_cost=money(cost),
        total_value=money(value),
        total_gain=money(value - cost),
        withheld=sorted(withheld),
        unpriced=sorted(unpriced),
        any_estimated=any(row.estimated for row in rows),
    )
