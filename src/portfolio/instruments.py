"""Refreshing the instrument master from Kite.

Kite returns the whole universe -- every exchange, every derivative, tens of
thousands of rows. This system buys NSE equity for delivery, so the filter is
applied here and the table stays small enough to be read by a human.

The refresh is idempotent by construction: it is an upsert keyed on the
trading symbol, and running it twice in a morning changes nothing the second
time. Instruments that stop appearing are marked inactive rather than deleted,
because a lot acquired in 2029 still needs its instrument row in 2046.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone

from audit.events import record
from audit.logging import fields
from portfolio.models import Instrument

logger = logging.getLogger("steadystack.portfolio")

DEFAULT_EXCHANGE = "NSE"
EQUITY_TYPE = "EQ"

#: Kite files indices as type EQ under their own segment. They are kept --
#: the benchmark's evening level is quoted through its row -- but never as
#: something to buy.
INDEX_SEGMENT = "INDICES"

#: NSE series that are shares this system could buy one of. Kite writes the
#: ordinary EQ series with no suffix; every other series is a suffix on the
#: symbol. BE is trade-for-trade equity, still bought for delivery. The rest --
#: state loans (SG), NCDs and bonds (N0..NZ, Y*, Z*), government securities
#: (GS), treasury bills (TB), gold bonds (GB), SME boards (SM, ST) and the
#: like -- are not, and at ten thousand rows they buried the few that are.
EQUITY_SERIES_SUFFIXES = frozenset({"BE"})


@dataclass(frozen=True)
class RefreshResult:
    created: int
    updated: int
    deactivated: int
    reactivated: int
    considered: int

    @property
    def total(self) -> int:
        return self.created + self.updated


def _decimal(value: Any, default: Decimal) -> Decimal:
    """Kite sends numbers as floats. Decimal(str(float)) keeps the paise."""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def is_tradable_equity(row: dict[str, Any], exchange: str) -> bool:
    """Whether a master row is a share this system could buy one of.

    Rejects derivatives, indices, other exchanges, debt and SME series, and
    anything that trades in lots larger than one share. An index has a token
    and a symbol and can never be bought, and would otherwise rank in a
    universe it cannot be filled from.
    """
    if row.get("exchange") != exchange:
        return False
    if row.get("instrument_type") != EQUITY_TYPE:
        return False
    if (row.get("segment") or exchange) != exchange:
        return False
    symbol = row.get("tradingsymbol") or ""
    if not symbol or not row.get("instrument_token"):
        return False
    if "-" in symbol and symbol.rsplit("-", 1)[1] not in EQUITY_SERIES_SUFFIXES:
        return False
    return int(row.get("lot_size") or 1) == 1


def is_index(row: dict[str, Any], exchange: str) -> bool:
    """An index row, kept so the benchmark can be quoted, never ranked."""
    return (
        row.get("exchange") == exchange
        and row.get("segment") == INDEX_SEGMENT
        and bool(row.get("tradingsymbol"))
        and bool(row.get("instrument_token"))
    )


@transaction.atomic
def refresh(
    rows: list[dict[str, Any]], *, exchange: str = DEFAULT_EXCHANGE, actor: str = "system"
) -> RefreshResult:
    """Apply an instrument master to the table.

    Takes the rows rather than fetching them, so the filtering and the upsert
    are testable without a broker and the caller owns the call that costs a
    round trip.
    """
    tradable = [row for row in rows if is_tradable_equity(row, exchange) or is_index(row, exchange)]

    existing = {
        instrument.tradingsymbol: instrument
        for instrument in Instrument.objects.filter(exchange=exchange)
    }

    created = updated = reactivated = 0
    seen: set[str] = set()

    for row in tradable:
        symbol = row["tradingsymbol"]
        seen.add(symbol)

        attributes = {
            "exchange": exchange,
            "instrument_token": int(row["instrument_token"]),
            "name": (row.get("name") or "")[:255],
            "tick_size": _decimal(row.get("tick_size"), Decimal("0.0500")),
            "lot_size": int(row.get("lot_size") or 1),
            "instrument_type": row.get("instrument_type") or EQUITY_TYPE,
            "segment": (row.get("segment") or "")[:32],
            "active": True,
        }

        instrument = existing.get(symbol)
        if instrument is None:
            Instrument.objects.create(tradingsymbol=symbol, **attributes)
            created += 1
            continue

        was_inactive = not instrument.active
        for field, value in attributes.items():
            setattr(instrument, field, value)
        instrument.save(update_fields=[*attributes.keys(), "refreshed_at"])
        updated += 1
        reactivated += int(was_inactive)

    # Anything that stopped appearing: delisted, renamed, or suspended. Kept,
    # because history references it.
    stale = [symbol for symbol, row in existing.items() if symbol not in seen and row.active]
    deactivated = Instrument.objects.filter(tradingsymbol__in=stale).update(
        active=False, refreshed_at=timezone.now()
    )

    result = RefreshResult(
        created=created,
        updated=updated,
        deactivated=deactivated,
        reactivated=reactivated,
        considered=len(rows),
    )

    record(
        "instruments.refreshed",
        actor=actor,
        entity_type="portfolio.Instrument",
        after={
            "exchange": exchange,
            "created": created,
            "updated": updated,
            "deactivated": deactivated,
            "reactivated": reactivated,
            "considered": len(rows),
        },
    )
    logger.info(
        "instrument master refreshed",
        extra=fields(
            exchange=exchange,
            created=created,
            updated=updated,
            deactivated=deactivated,
        ),
    )
    return result


def ever_held():
    """Every instrument the account has held: the broker's holdings, and any
    name a lot was opened for in either book, sold or not.

    What a dividend can be recorded against. It is paid on shares held at the
    record date, which may be after they were sold, so a closed lot counts.
    """
    from django.db.models import Q

    return (
        Instrument.objects.filter(Q(holding__isnull=False) | Q(lots__isnull=False))
        .distinct()
        .order_by("tradingsymbol")
    )
