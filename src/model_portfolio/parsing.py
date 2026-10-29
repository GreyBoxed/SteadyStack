"""Reading a model portfolio CSV, and refusing the ones that are wrong.

The validation list is enumerated rather than asserted, because every entry on
it is a file somebody will actually upload: a spreadsheet export with a BOM, a
symbol in lower case, a trailing blank row, the same name twice, a weight
typed as "33.33%", three thirds that sum to 99.99.

The tolerance exists for that last one. Equal thirds cannot sum to 100 in two
decimal places, and a system that refuses 99.99 refuses the most obvious
portfolio a person can describe. It is stated once, here, rather than
discovered at an upload screen.
"""

import csv
import hashlib
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from portfolio.instruments import INDEX_SEGMENT
from portfolio.models import Instrument

#: 33.33 x 3 = 99.99. Anything further out is a mistake, not a rounding.
WEIGHT_TOLERANCE = Decimal("0.05")
TARGET_TOTAL = Decimal("100")

SYMBOL_COLUMN = "symbol"
WEIGHT_COLUMN = "target_pct"

#: Accepted spellings of the two columns. A spreadsheet rarely exports the
#: exact header a developer had in mind.
SYMBOL_ALIASES = frozenset({"symbol", "tradingsymbol", "trading_symbol", "ticker"})
WEIGHT_ALIASES = frozenset({"target_pct", "target", "weight", "target_weight", "pct", "percent"})


@dataclass
class ParsedRow:
    line: int
    symbol: str
    target_pct: Decimal


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    content_hash: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def total(self) -> Decimal:
        return sum((row.target_pct for row in self.rows), Decimal("0"))


def _normalise_header(name: str) -> str:
    # A BOM arrives attached to the first header cell and turns "symbol" into
    # something that compares unequal to "symbol" while looking identical.
    return name.replace("﻿", "").strip().lower().replace(" ", "_")


def _clean_symbol(value: str) -> str:
    return value.replace("﻿", "").strip().upper()


def _clean_weight(value: str) -> str:
    return value.replace("﻿", "").strip().rstrip("%").strip()


def content_hash(rows: list[ParsedRow]) -> str:
    """A hash of what the file *means*, not of its bytes.

    Re-uploading the same portfolio with different row order, casing or line
    endings is the same portfolio, and should be recognised as one.
    """
    canonical = ";".join(
        f"{row.symbol}={row.target_pct}" for row in sorted(rows, key=lambda r: r.symbol)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse(raw: bytes | str) -> ParseResult:
    """Read the CSV and collect every problem with it.

    Every problem, deliberately: an operator fixing an upload should see all
    of what is wrong, not the first thing that was wrong.
    """
    result = ParseResult()

    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    text = text.lstrip("﻿")

    if not text.strip():
        result.errors.append("The file is empty.")
        return result

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        result.errors.append("The file has no header row.")
        return result

    headers = {_normalise_header(name): name for name in reader.fieldnames if name}
    symbol_key = next((headers[h] for h in headers if h in SYMBOL_ALIASES), None)
    weight_key = next((headers[h] for h in headers if h in WEIGHT_ALIASES), None)

    if symbol_key is None or weight_key is None:
        result.errors.append(
            f"The header must name a symbol column and a weight column; found "
            f"{', '.join(reader.fieldnames)}."
        )
        return result

    seen: dict[str, int] = {}

    for line, raw_row in enumerate(reader, start=2):  # line 1 is the header
        symbol = _clean_symbol(raw_row.get(symbol_key) or "")
        weight_text = _clean_weight(raw_row.get(weight_key) or "")

        if not symbol and not weight_text:
            continue  # a trailing blank line is not an error

        if not symbol:
            result.errors.append(f"Line {line}: no symbol.")
            continue

        if not weight_text:
            result.errors.append(f"Line {line}: {symbol} has no weight.")
            continue

        try:
            target = Decimal(weight_text)
        except InvalidOperation:
            result.errors.append(
                f"Line {line}: {symbol} has a weight that is not a number ({weight_text!r})."
            )
            continue

        if target <= 0:
            # Zero is not "hold none of it", it is a name that should not be
            # in the file at all -- and a negative weight is a short.
            result.errors.append(
                f"Line {line}: {symbol} has a weight of {target}, which must be above zero."
            )
            continue

        if target > TARGET_TOTAL:
            result.errors.append(
                f"Line {line}: {symbol} has a weight of {target}, which is above 100."
            )
            continue

        if symbol in seen:
            result.errors.append(
                f"Line {line}: {symbol} appears twice (also on line {seen[symbol]})."
            )
            continue

        seen[symbol] = line
        result.rows.append(ParsedRow(line=line, symbol=symbol, target_pct=target))

    if not result.rows and not result.errors:
        result.errors.append("The file has a header but no rows.")

    if result.rows:
        difference = abs(result.total - TARGET_TOTAL)
        if difference > WEIGHT_TOLERANCE:
            result.errors.append(
                f"The weights sum to {result.total}, not {TARGET_TOTAL} "
                f"(tolerance {WEIGHT_TOLERANCE})."
            )
        result.content_hash = content_hash(result.rows)

    return result


def resolve_instruments(result: ParseResult) -> dict[str, Instrument]:
    """Match parsed symbols to instruments, recording those that do not match.

    An unknown symbol and a delisted one are separate messages: one is a typo
    and the other is a portfolio that needs a decision.
    """
    symbols = [row.symbol for row in result.rows]
    found = {i.tradingsymbol: i for i in Instrument.objects.filter(tradingsymbol__in=symbols)}

    for row in result.rows:
        instrument = found.get(row.symbol)
        if instrument is None:
            result.errors.append(
                f"Line {row.line}: {row.symbol} is not in the instrument master. "
                "Refresh it, or check the spelling."
            )
        elif not instrument.active:
            result.errors.append(f"Line {row.line}: {row.symbol} is no longer trading.")
        elif instrument.segment == INDEX_SEGMENT:
            result.errors.append(
                f"Line {row.line}: {row.symbol} is an index. It can be the benchmark, "
                "but it cannot be bought."
            )

    return found
