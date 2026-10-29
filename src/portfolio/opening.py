"""Seeding the lots that existed before this system did.

A one-time, audited act. The operator's own records are the source, because
`kite.holdings()` returns an average price and never lots -- and an average is
exactly the thing FIFO cannot work from.

One lot per original purchase (M35a). Two purchases of the same name on
different days are two lots, and collapsing them into one would silently
change which shares a future sale consumes.

Until the import is marked complete, everything that depends on a whole lot
history refuses to run. That refusal is the point of this module: FIFO against
half a lot set produces numbers that look entirely reasonable and are wrong.
"""

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from portfolio.models import BasisState, Instrument, Lot, OpeningImport

logger = logging.getLogger("steadystack.portfolio")

SYMBOL_ALIASES = frozenset({"symbol", "tradingsymbol", "trading_symbol", "ticker"})
QUANTITY_ALIASES = frozenset({"quantity", "qty", "shares"})
DATE_ALIASES = frozenset({"acquired_on", "date", "purchase_date", "trade_date"})
COST_ALIASES = frozenset({"cost_per_unit", "cost", "price", "rate", "buy_price"})


class OpeningImportIncomplete(Exception):
    """Raised by anything that cannot be correct without every lot."""


class ImportRefused(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class ParsedLot:
    line: int
    symbol: str
    quantity: int
    acquired_on: date
    cost_per_unit: Decimal


@dataclass
class ParseResult:
    lots: list[ParsedLot] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _header(name: str) -> str:
    return name.replace("﻿", "").strip().lower().replace(" ", "_")


def parse(raw: bytes | str) -> ParseResult:
    """Read an opening-positions CSV, collecting every problem with it."""
    result = ParseResult()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    text = text.lstrip("﻿")

    if not text.strip():
        result.errors.append("The file is empty.")
        return result

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        result.errors.append("The file has no header row.")
        return result

    headers = {_header(name): name for name in reader.fieldnames if name}
    keys = {
        "symbol": next((headers[h] for h in headers if h in SYMBOL_ALIASES), None),
        "quantity": next((headers[h] for h in headers if h in QUANTITY_ALIASES), None),
        "acquired_on": next((headers[h] for h in headers if h in DATE_ALIASES), None),
        "cost": next((headers[h] for h in headers if h in COST_ALIASES), None),
    }

    missing = [name for name, key in keys.items() if key is None]
    if missing:
        result.errors.append(
            f"The header must name a column for each of: {', '.join(sorted(missing))}."
        )
        return result

    for line, row in enumerate(reader, start=2):
        symbol = (row.get(keys["symbol"]) or "").strip().upper()
        quantity_text = (row.get(keys["quantity"]) or "").strip()
        date_text = (row.get(keys["acquired_on"]) or "").strip()
        cost_text = (row.get(keys["cost"]) or "").strip()

        if not any((symbol, quantity_text, date_text, cost_text)):
            continue

        if not symbol:
            result.errors.append(f"Line {line}: no symbol.")
            continue

        try:
            quantity = int(quantity_text)
        except ValueError:
            result.errors.append(
                f"Line {line}: {symbol} has a quantity that is not a whole number."
            )
            continue

        if quantity <= 0:
            result.errors.append(f"Line {line}: {symbol} has a quantity of {quantity}.")
            continue

        try:
            acquired_on = date.fromisoformat(date_text)
        except ValueError:
            result.errors.append(
                f"Line {line}: {symbol} has an acquisition date that is not YYYY-MM-DD "
                f"({date_text!r})."
            )
            continue

        if acquired_on > timezone.localdate():
            result.errors.append(f"Line {line}: {symbol} was acquired in the future.")
            continue

        try:
            cost = Decimal(cost_text)
        except InvalidOperation:
            result.errors.append(f"Line {line}: {symbol} has a cost that is not a number.")
            continue

        if cost <= 0:
            result.errors.append(f"Line {line}: {symbol} has a cost of {cost}.")
            continue

        result.lots.append(
            ParsedLot(
                line=line,
                symbol=symbol,
                quantity=quantity,
                acquired_on=acquired_on,
                cost_per_unit=cost,
            )
        )

    if not result.lots and not result.errors:
        result.errors.append(
            "The file has a header but no rows. If the account held nothing, say so with "
            "import_opening_positions --empty."
        )

    return result


def resolve(result: ParseResult) -> dict[str, Instrument]:
    known = {
        instrument.tradingsymbol: instrument
        for instrument in Instrument.objects.filter(
            tradingsymbol__in=[lot.symbol for lot in result.lots]
        )
    }
    for lot in result.lots:
        if lot.symbol not in known:
            result.errors.append(f"Line {lot.line}: {lot.symbol} is not in the instrument master.")
    return known


def latest() -> OpeningImport | None:
    return OpeningImport.objects.order_by("-started_at").first()


def is_complete() -> bool:
    """Whether the lot history can be trusted to be whole."""
    return OpeningImport.objects.filter(completed_at__isnull=False).exists()


def require_complete(what: str) -> None:
    """Refuse *what* until the opening import is done.

    Called by realised gains and by rebalance sells. The message names the act
    rather than the module, because the person reading it is trying to do
    something, not debug an import.
    """
    if is_complete():
        return
    raise OpeningImportIncomplete(
        f"{what} is refused until the opening positions have been imported: "
        "FIFO against an incomplete lot set is wrong in a way nobody would notice."
    )


@transaction.atomic
def run(
    raw: bytes | str,
    *,
    imported_by: str,
    source: str,
    note: str = "",
    complete: bool = True,
) -> OpeningImport:
    """Import opening lots, or refuse and import nothing.

    Completing is the default, because an import left half-open blocks
    everything downstream and the operator who ran it is the one who knows
    whether the file was the whole story. `complete=False` exists for an
    import that arrives in several files.
    """
    if is_complete():
        raise ImportRefused(
            ["The opening positions have already been imported. Correct a lot by superseding it."]
        )

    result = parse(raw)
    if result.lots:
        resolve(result)

    if not result.ok:
        raise ImportRefused(result.errors)

    instruments = {
        instrument.tradingsymbol: instrument
        for instrument in Instrument.objects.filter(
            tradingsymbol__in=[lot.symbol for lot in result.lots]
        )
    }

    opening = OpeningImport.objects.create(
        imported_by=imported_by,
        source=source[:255],
        note=note[:255],
        lot_count=len(result.lots),
        completed_at=timezone.now() if complete else None,
    )

    Lot.objects.bulk_create(
        [
            Lot(
                instrument=instruments[parsed.symbol],
                acquired_on=parsed.acquired_on,
                quantity=parsed.quantity,
                remaining_quantity=parsed.quantity,
                cost_per_unit=parsed.cost_per_unit,
                basis_state=BasisState.IMPORTED,
                opening_import=opening,
                trace_id=current_trace_id(),
            )
            for parsed in result.lots
        ]
    )

    record(
        "opening_import.completed" if complete else "opening_import.started",
        actor=imported_by,
        entity=opening,
        after={
            "source": source,
            "lots": len(result.lots),
            "symbols": sorted({lot.symbol for lot in result.lots}),
            "total_cost": str(
                sum((lot.cost_per_unit * lot.quantity for lot in result.lots), Decimal("0"))
            ),
        },
    )
    logger.info(
        "opening positions imported",
        extra=fields(lots=len(result.lots), source=source, complete=complete),
    )
    return opening


@transaction.atomic
def record_empty(*, imported_by: str, source: str, note: str = "") -> OpeningImport:
    """Complete the import for an account that held nothing before this system.

    A file with a header and no rows is refused, because it looks exactly like
    a file that lost its rows. An account with no prior holdings says so here
    instead, on purpose. Refused once opening lots exist -- an open import that
    already holds some did not start from nothing -- or once an import is
    complete. Lots this system bought are not prior holdings and do not count.
    """
    if is_complete():
        raise ImportRefused(
            ["The opening positions have already been imported. Correct a lot by superseding it."]
        )
    existing = Lot.objects.filter(opening_import__isnull=False).count()
    if existing:
        raise ImportRefused(
            [
                f"{existing} opening lots have already been imported, so the account did not "
                "start empty. Finish that import instead."
            ]
        )

    opening = OpeningImport.objects.create(
        imported_by=imported_by,
        source=source[:255],
        note=note[:255],
        lot_count=0,
        completed_at=timezone.now(),
    )
    record(
        "opening_import.completed",
        actor=imported_by,
        entity=opening,
        after={"source": source, "lots": 0, "symbols": [], "total_cost": "0", "empty": True},
    )
    logger.info("opening positions imported", extra=fields(lots=0, source=source, complete=True))
    return opening


def mark_complete(opening: OpeningImport, *, actor: str) -> OpeningImport:
    """Close an import that arrived in several files."""
    if opening.is_complete:
        return opening

    opening.completed_at = timezone.now()
    opening.save(update_fields=["completed_at"])

    record(
        "opening_import.completed", actor=actor, entity=opening, after={"lots": opening.lot_count}
    )
    return opening
