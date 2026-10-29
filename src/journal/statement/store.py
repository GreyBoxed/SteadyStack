"""Keeping uploads: every file verbatim, every line once (spec §7.2)."""

import hashlib
from dataclasses import dataclass

from django.db import connection, transaction

from audit.context import current_trace_id
from audit.events import record
from journal.models import JournalEntry, StatementLine, StatementUpload
from journal.statement.parse import StatementError, parse

#: Serialises store_upload() so two concurrent uploads can't both pass the
#: chain check against the same "last held" line.
_UPLOAD_LOCK = 7_310_442_901_115_020


@dataclass(frozen=True)
class Stored:
    upload: StatementUpload
    added: int
    already_held: int


def _last_held() -> StatementLine | None:
    return StatementLine.objects.order_by(
        "-posting_date", "-upload__uploaded_at", "-position"
    ).first()


def _lock_uploads() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_UPLOAD_LOCK])


@transaction.atomic
def store_upload(raw: bytes, *, filename: str, actor: str) -> Stored:
    """Store a statement, refusing one that does not continue the chain.

    Overlap is expected (months are exported with ragged edges): the rows
    already held must be a prefix of the file and end exactly where the stored
    chain ends. With no overlap, the file's opening balance must equal the
    last stored balance. Anything else is a gap or a rewrite, and is refused.

    Taken under an advisory lock: without it, two concurrent uploads could
    both read the same "last held" line and both pass the chain check.
    """
    _lock_uploads()

    digest = hashlib.sha256(raw).hexdigest()
    earlier = StatementUpload.objects.filter(sha256=digest).first()
    if earlier is not None:
        raise StatementError(f"This file was already uploaded on {earlier.uploaded_at:%d %b %Y}.")

    statement = parse(raw)
    if not statement.rows:
        raise StatementError("The file has no lines.")

    held = set(
        StatementLine.objects.filter(
            fingerprint__in=[row.fingerprint for row in statement.rows]
        ).values_list("fingerprint", flat=True)
    )
    new_rows = [row for row in statement.rows if row.fingerprint not in held]
    if not new_rows:
        raise StatementError("Every line in this file is already held.")

    overlap = statement.rows[: statement.rows.index(new_rows[0])]
    if any(row.fingerprint in held for row in statement.rows[len(overlap) :]):
        raise StatementError("The lines already held are not a clean prefix of this file.")

    last = _last_held()
    if last is not None:
        if overlap:
            continues = overlap[-1].fingerprint == last.fingerprint
        else:
            continues = statement.opening == last.net_balance
        if not continues:
            raise StatementError(
                f"This file does not continue from the last statement, which ends on "
                f"{last.posting_date} at {last.net_balance}."
            )

    upload = StatementUpload.objects.create(
        filename=filename[:255],
        sha256=digest,
        raw=raw.decode("utf-8-sig"),
        period_from=statement.rows[0].posting_date,
        period_to=statement.rows[-1].posting_date,
        opening_balance=statement.opening,
        closing_balance=statement.closing,
        uploaded_by=actor,
        trace_id=current_trace_id(),
    )
    StatementLine.objects.bulk_create(
        [
            StatementLine(
                upload=upload,
                position=row.position,
                fingerprint=row.fingerprint,
                posting_date=row.posting_date,
                particulars=row.particulars[:500],
                cost_center=row.cost_center,
                voucher_type=row.voucher_type,
                debit=row.debit,
                credit=row.credit,
                net_balance=row.net_balance,
            )
            for row in new_rows
        ]
    )
    record(
        "journal.statement_uploaded",
        actor=actor,
        entity=upload,
        after={"filename": filename, "added": len(new_rows), "already_held": len(overlap)},
    )
    return Stored(upload=upload, added=len(new_rows), already_held=len(overlap))


def open_to_matching():
    """Lines waiting for an entry: never matched, or matched to one since reversed."""
    from django.db.models import Q

    return Q(matched_entry__isnull=True) | Q(matched_entry__reversed_by__isnull=False)


def link(line: StatementLine, entry: JournalEntry) -> None:
    """Match a line to its entry: the one update the table allows.

    Idempotent when *line* is already matched to *entry* -- the filtered
    update then affects no row, and that is fine. Matched to a different
    entry that still stands is not fine: the trigger allows a line to be
    matched again only once its entry has been reversed, so this is refused
    in Python rather than left to surface as a raw DatabaseError.
    """
    updated = (
        StatementLine.objects.filter(pk=line.pk)
        .filter(open_to_matching())
        .exclude(matched_entry=entry)
        .update(matched_entry=entry)
    )
    if updated == 0:
        current = StatementLine.objects.select_related("matched_entry").get(pk=line.pk)
        if current.matched_entry_id == entry.id:
            line.matched_entry = entry
            return
        raise StatementError(
            f"That line is already matched to entry #{current.matched_entry.number}."
        )
    line.matched_entry = entry
