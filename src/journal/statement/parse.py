"""Zerodha Console's funds statement, as the CSV it exports (spec §7.2).

The rows carry no identifier, and monthly exports overlap, so each row is
fingerprinted by what it says plus how many identical rows came before it on
the same day. The running balance is checked row by row: a file whose
arithmetic does not hold is not a statement this system can reconcile against.
"""

import csv
import hashlib
import io
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from journal.posting import q6

COLUMNS = [
    "particulars",
    "posting_date",
    "cost_center",
    "voucher_type",
    "debit",
    "credit",
    "net_balance",
]


class StatementError(Exception):
    pass


@dataclass(frozen=True)
class Row:
    position: int
    posting_date: date
    particulars: str
    cost_center: str
    voucher_type: str
    debit: Decimal
    credit: Decimal
    net_balance: Decimal
    fingerprint: str


@dataclass(frozen=True)
class Statement:
    opening: Decimal
    closing: Decimal
    rows: list[Row]


def _amount(value: str, where: str) -> Decimal:
    try:
        return q6(Decimal(value.strip() or "0"))
    except InvalidOperation as exc:
        raise StatementError(f"{where}: {value!r} is not an amount.") from exc


def parse(raw: bytes | str) -> Statement:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise StatementError("The file is not a UTF-8 CSV export from Console.") from exc
    else:
        text = raw
    reader = csv.DictReader(io.StringIO(text))
    if [c.strip() for c in (reader.fieldnames or [])] != COLUMNS:
        raise StatementError(f"Expected the columns {', '.join(COLUMNS)}.")
    reader.fieldnames = [c.strip() for c in reader.fieldnames]

    opening = closing = None
    rows: list[Row] = []
    seen: Counter = Counter()

    for record in reader:
        particulars = record["particulars"].strip()
        if particulars == "Opening Balance":
            if opening is not None:
                raise StatementError("The file has more than one Opening Balance row.")
            opening = _amount(record["net_balance"], "Opening Balance")
            continue
        if particulars == "Closing Balance":
            if closing is not None:
                raise StatementError("The file has more than one Closing Balance row.")
            closing = _amount(record["net_balance"], "Closing Balance")
            continue

        position = len(rows) + 1
        where = f"line {position}"
        try:
            posting_date = date.fromisoformat(record["posting_date"].strip())
        except ValueError as exc:
            raise StatementError(f"{where}: {record['posting_date']!r} is not a date.") from exc
        debit = _amount(record["debit"], where)
        credit = _amount(record["credit"], where)

        identity = (
            posting_date.isoformat(),
            particulars,
            str(debit),
            str(credit),
        )
        occurrence = seen[identity]
        seen[identity] += 1
        fingerprint = hashlib.sha256("|".join([*identity, str(occurrence)]).encode()).hexdigest()

        rows.append(
            Row(
                position=position,
                posting_date=posting_date,
                particulars=particulars,
                cost_center=record["cost_center"].strip(),
                voucher_type=record["voucher_type"].strip(),
                debit=debit,
                credit=credit,
                net_balance=_amount(record["net_balance"], where),
                fingerprint=fingerprint,
            )
        )

    if opening is None or closing is None:
        raise StatementError("The file has no Opening Balance or Closing Balance row.")

    running = opening
    for row in rows:
        running = q6(running - row.debit + row.credit)
        if running != row.net_balance:
            raise StatementError(
                f"line {row.position}: the balance should be "
                f"{running}, the file says {row.net_balance}."
            )
    if running != closing:
        raise StatementError(f"The closing balance should be {running}, the file says {closing}.")

    return Statement(opening=opening, closing=closing, rows=rows)
