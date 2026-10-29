"""Keeping the holdings table level with the broker.

`kite.holdings()` is a read, so this works in every environment (M19c) and is
how Phase 1 sees the real portfolio from a laptop.

Quantities here are the broker's and are treated as authoritative for *how
much* is held. What it cost is a separate question answered by lots, and the
disagreement between the two is a divergence to be classified rather than
silently resolved (M42) -- which is why this function never touches a lot.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction

from audit.events import record
from audit.logging import fields
from portfolio.models import Holding, Instrument

logger = logging.getLogger("steadystack.portfolio")


@dataclass(frozen=True)
class SyncResult:
    reconciled: int
    unknown_symbols: list[str]
    zeroed: int


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")


@transaction.atomic
def sync(rows: list[dict[str, Any]], *, actor: str = "system") -> SyncResult:
    """Apply a broker holdings response to the table.

    A symbol the instrument master has never heard of is reported rather than
    created: an instrument that appears here and not there means the master is
    stale, and inventing a row would hide that.
    """
    known = {
        instrument.tradingsymbol: instrument
        for instrument in Instrument.objects.filter(
            tradingsymbol__in=[row.get("tradingsymbol") for row in rows]
        )
    }

    unknown: list[str] = []
    seen: set[str] = set()
    reconciled = 0

    for row in rows:
        symbol = row.get("tradingsymbol")
        instrument = known.get(symbol)
        if instrument is None:
            unknown.append(str(symbol))
            continue

        # Kite splits settled stock across these two: t1 is bought but not yet
        # delivered. Both are owned, and a tradesheet that ignored t1 would
        # buy a name it already bought this week.
        quantity = int(row.get("quantity") or 0) + int(row.get("t1_quantity") or 0)

        Holding.objects.update_or_create(
            instrument=instrument,
            defaults={
                "quantity": quantity,
                "average_cost": _decimal(row.get("average_price")),
            },
        )
        seen.add(symbol)
        reconciled += 1

    # A name sold out at the broker keeps its row at zero rather than
    # vanishing, so that "we hold none" is a fact with a timestamp.
    zeroed = Holding.objects.exclude(instrument_id__in=seen).exclude(quantity=0).update(quantity=0)

    result = SyncResult(reconciled=reconciled, unknown_symbols=unknown, zeroed=zeroed)

    record(
        "holdings.reconciled",
        actor=actor,
        entity_type="portfolio.Holding",
        after={
            "reconciled": reconciled,
            "zeroed": zeroed,
            "unknown_symbols": unknown,
        },
    )
    if unknown:
        logger.warning(
            "holdings name instruments the master does not have",
            extra=fields(symbols=unknown),
        )
    logger.info("holdings reconciled", extra=fields(reconciled=reconciled, zeroed=zeroed))
    return result
