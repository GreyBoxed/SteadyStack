"""Turning a validated CSV into a version, and saying what changed.

The diff is the point of this module. A model portfolio is the instruction
that every future purchase follows, so the operator sees what an upload will
do -- which names arrive, which leave, which weights move and by how much --
before it takes effect, and the same summary is kept in the audit trail
afterwards.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from audit.events import record
from model_portfolio.models import ModelPortfolio, ModelPortfolioRow
from model_portfolio.parsing import ParseResult, parse, resolve_instruments


class UploadRefused(Exception):
    """The file did not survive validation. `errors` lists every reason."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class WeightChange:
    symbol: str
    before: Decimal | None
    after: Decimal | None

    @property
    def delta(self) -> Decimal:
        return (self.after or Decimal("0")) - (self.before or Decimal("0"))

    @property
    def kind(self) -> str:
        if self.before is None:
            return "added"
        if self.after is None:
            return "removed"
        return "changed"


@dataclass
class Diff:
    previous_version: int | None
    changes: list[WeightChange] = field(default_factory=list)

    @property
    def is_first(self) -> bool:
        return self.previous_version is None

    @property
    def added(self) -> list[WeightChange]:
        return [c for c in self.changes if c.kind == "added"]

    @property
    def removed(self) -> list[WeightChange]:
        return [c for c in self.changes if c.kind == "removed"]

    @property
    def changed(self) -> list[WeightChange]:
        return [c for c in self.changes if c.kind == "changed"]

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def as_json(self) -> list[dict[str, str]]:
        return [
            {
                "symbol": change.symbol,
                "kind": change.kind,
                "before": str(change.before) if change.before is not None else None,
                "after": str(change.after) if change.after is not None else None,
            }
            for change in self.changes
        ]


def latest() -> ModelPortfolio | None:
    return ModelPortfolio.objects.order_by("-version").first()


def validate(raw: bytes | str) -> ParseResult:
    """Parse and check a file without storing anything.

    Instrument resolution is part of validation rather than of saving: a
    portfolio naming something the system cannot buy is not a portfolio.
    """
    result = parse(raw)
    if result.rows:
        resolve_instruments(result)
    return result


def diff_against_current(result: ParseResult) -> Diff:
    """What this file would change, shown before it takes effect (D14)."""
    previous = latest()
    proposed = {row.symbol: row.target_pct for row in result.rows}

    if previous is None:
        return Diff(
            previous_version=None,
            changes=[
                WeightChange(symbol, None, weight) for symbol, weight in sorted(proposed.items())
            ],
        )

    existing = previous.weights()
    changes = []
    for symbol in sorted(set(existing) | set(proposed)):
        before = existing.get(symbol)
        after = proposed.get(symbol)
        if before != after:
            changes.append(WeightChange(symbol, before, after))

    return Diff(previous_version=previous.version, changes=changes)


def is_identical_to_current(result: ParseResult) -> bool:
    """Whether this file says exactly what the current version already says.

    Re-uploading an unchanged portfolio is a common accident. It is not an
    error -- but it should not silently create a version that changes nothing.
    """
    previous = latest()
    return previous is not None and previous.content_hash == result.content_hash


def store(
    raw: bytes | str,
    *,
    uploaded_by: str,
    source_filename: str = "",
    note: str = "",
    allow_identical: bool = False,
) -> ModelPortfolio:
    """Validate and store a new version, or refuse and store nothing.

    Validation happens outside the transaction that writes the version, so
    that the record of a rejected upload is not rolled back along with the
    rejection. A file somebody tried to upload is a thing that happened.
    """
    result = validate(raw)
    if not result.ok:
        record(
            "model_portfolio.rejected",
            actor=uploaded_by,
            entity_type="model_portfolio.ModelPortfolio",
            after={"filename": source_filename, "errors": result.errors},
        )
        raise UploadRefused(result.errors)

    if is_identical_to_current(result) and not allow_identical:
        raise UploadRefused(
            ["This file is identical to the current version; nothing would change."]
        )

    return _persist(
        result,
        uploaded_by=uploaded_by,
        source_filename=source_filename,
        note=note,
    )


@transaction.atomic
def _persist(
    result: ParseResult,
    *,
    uploaded_by: str,
    source_filename: str,
    note: str,
) -> ModelPortfolio:
    diff = diff_against_current(result)
    instruments = {instrument.tradingsymbol: instrument for instrument in _instruments_for(result)}

    previous = ModelPortfolio.objects.select_for_update().order_by("-version").first()
    portfolio = ModelPortfolio.objects.create(
        version=(previous.version + 1) if previous else 1,
        uploaded_by=uploaded_by,
        source_filename=source_filename[:255],
        content_hash=result.content_hash,
        note=note[:255],
    )

    ModelPortfolioRow.objects.bulk_create(
        [
            ModelPortfolioRow(
                portfolio=portfolio,
                instrument=instruments[row.symbol],
                target_pct=row.target_pct,
            )
            for row in result.rows
        ]
    )

    record(
        "model_portfolio.uploaded",
        actor=uploaded_by,
        entity=portfolio,
        before={"version": diff.previous_version},
        after={
            "version": portfolio.version,
            "filename": source_filename,
            "total_weight": str(result.total),
            "rows": len(result.rows),
            "diff": diff.as_json(),
        },
    )
    return portfolio


def _instruments_for(result: ParseResult):
    from portfolio.models import Instrument

    return Instrument.objects.filter(tradingsymbol__in=[row.symbol for row in result.rows])
