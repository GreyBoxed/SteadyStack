"""Loading a year of sessions from a file.

The exchange publishes its holiday list once a year, and Muhurat's timing with
it. That list is the source; nothing here guesses. A calendar that invented
its own holidays would be wrong in the one way that matters -- it would say
the market is open when it is shut, and the ladder would place into it.

The file is `date,kind,open_time,close_time,settles,note`, where kind is
closed, normal or special. `settles` is optional and answers the calendar's
second question (M18b): a settlement holiday trades normally and clears
nothing, so it is `normal` with `settles` set to no. Left out, it follows the
kind -- closed days clear nothing, everything else clears -- which is right
for every ordinary row and is why the column is not required.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date, time

from trading_calendar.models import SessionKind
from trading_calendar.sessions import CalendarEditRefused, set_day

VALID_KINDS = {SessionKind.CLOSED, SessionKind.NORMAL, SessionKind.SPECIAL}


@dataclass
class LoadResult:
    loaded: int = 0
    errors: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _time(value: str) -> time | None:
    value = (value or "").strip()
    if not value:
        return None
    return time.fromisoformat(value)


TRUTHY = {"yes", "y", "true", "t", "1"}
FALSY = {"no", "n", "false", "f", "0"}


def _settles(value: str) -> bool | None:
    """Read the settlement column, or None to follow the kind.

    Anything unrecognised is a problem the caller reports rather than a guess:
    the difference between a day that settles and one that does not is a day's
    worth of shares appearing to be missing.
    """
    value = (value or "").strip().lower()
    if not value:
        return None
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    raise ValueError(value)


def load(raw: bytes | str, *, actor: str = "system") -> LoadResult:
    """Apply a session file. Every problem is collected, not just the first."""
    result = LoadResult()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw

    if not text.strip():
        result.errors.append("The file is empty.")
        return result

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        result.errors.append("The file has no header row.")
        return result

    headers = {(name or "").strip().lower(): name for name in reader.fieldnames}
    if "date" not in headers or "kind" not in headers:
        result.errors.append("The header must name a date column and a kind column.")
        return result

    for line, row in enumerate(reader, start=2):
        raw_date = (row.get(headers["date"]) or "").strip()
        kind = (row.get(headers["kind"]) or "").strip().lower()

        if not raw_date and not kind:
            continue

        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            result.errors.append(f"Line {line}: {raw_date!r} is not a date as YYYY-MM-DD.")
            continue

        if kind not in VALID_KINDS:
            result.errors.append(f"Line {line}: {kind!r} is not one of closed, normal or special.")
            continue

        try:
            opens_at = _time(row.get(headers.get("open_time", ""), ""))
            closes_at = _time(row.get(headers.get("close_time", ""), ""))
        except ValueError:
            result.errors.append(f"Line {line}: the session times are not HH:MM.")
            continue

        if kind == SessionKind.SPECIAL and not (opens_at and closes_at):
            result.errors.append(
                f"Line {line}: {day} is a special session and must state its hours."
            )
            continue

        try:
            settles = _settles(row.get(headers.get("settles", ""), ""))
        except ValueError as unreadable:
            result.errors.append(
                f"Line {line}: {str(unreadable)!r} does not say whether {day} settles; "
                "use yes or no."
            )
            continue

        try:
            set_day(
                day,
                kind=kind,
                opens_at=opens_at,
                closes_at=closes_at,
                settles=settles,
                note=(row.get(headers.get("note", ""), "") or "").strip(),
                actor=actor,
            )
        except CalendarEditRefused as refusal:
            # Not an error in the file: the date has already traded, and the
            # rest of the year should still load.
            result.refused.append(str(refusal))
            continue

        result.loaded += 1

    if not result.loaded and not result.errors:
        result.errors.append("The file has a header but no session rows.")

    return result
