"""When the broker's holdings and our lots disagree.

The disagreement is the signal, not the problem. Shares leave and arrive by
routes this system does not control, so a difference means something happened
that has not been recorded yet -- and what it was is a question only the
operator can answer (M31a). It is never inferred from the shape of the
numbers, because the same difference of five shares could be a sale, a
transfer, a corporate action or a bug in this program, and treating one as
another quietly corrupts the lot history.

While a divergence is open, figures **for that instrument** are withheld
rather than computed from a holding known to be wrong. Portfolio-level figures
are still produced -- withholding everything because one name is in doubt
would be its own kind of lie -- but they are flagged with the instrument and
the quantity in question (M31b).
"""

import logging
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.utils import timezone

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from gains import fifo
from portfolio.models import Holding
from records.models import Divergence, DivergenceClassification, ExternalTransaction

logger = logging.getLogger("steadystack.records")


class DivergenceUnresolved(Exception):
    """A figure that cannot be trusted while a divergence is open."""


@dataclass
class Scan:
    opened: list[Divergence] = field(default_factory=list)
    closed: list[Divergence] = field(default_factory=list)
    #: Holdings that are not the portfolio's: listed, never raised (Nm4).
    not_ours: list[Holding] = field(default_factory=list)
    checked: int = 0

    @property
    def found_anything(self) -> bool:
        return bool(self.opened)


def _not_ours_ids(book: str | None) -> set[str]:
    """Held instruments that are not the portfolio's (Nm4).

    The account is the adult's own demat, so it can hold shares that were
    never the portfolio's. A name with no lot in the scan's book -- open or
    consumed -- that is not in the latest model portfolio is one of those:
    comparing it against zero lots would raise a divergence whose every
    classification posts it into the live book. A model name held without
    lots, or a name the portfolio has ever held, is still compared.
    """
    from model_portfolio.uploads import latest
    from portfolio.lots import in_book
    from portfolio.models import Lot

    held = set(Holding.objects.filter(quantity__gt=0).values_list("instrument_id", flat=True))
    if not held:
        return set()
    model = latest()
    modelled = set(model.rows.values_list("instrument_id", flat=True)) if model else set()
    with_lots = set(
        in_book(Lot.objects.filter(instrument_id__in=held), book).values_list(
            "instrument_id", flat=True
        )
    )
    return held - modelled - with_lots


def not_the_portfolios() -> list[Holding]:
    """Holdings on Records as "not the portfolio's", by symbol."""
    from portfolio.lots import reader_book

    ids = _not_ours_ids(reader_book())
    return list(
        Holding.objects.filter(instrument_id__in=ids)
        .select_related("instrument")
        .order_by("instrument_id")
    )


@transaction.atomic
def scan(*, detected_on: date | None = None) -> Scan:
    """Compare every holding against its lots (M42).

    Run after settlement, when both sides should agree -- and measured only
    against the shares that are actually due (M31c). A difference that
    appears and then disappears -- settlement lagging a postback, say -- is
    closed automatically; one that persists waits for an operator.
    """
    from portfolio.lots import reader_book

    detected_on = detected_on or timezone.localdate()
    result = Scan()
    # The broker holds real shares only, so a live scan compares against the
    # live lots; a paper lot there is a divergence nobody could classify (I1).
    book = reader_book()
    not_ours = _not_ours_ids(book)

    for holding in Holding.objects.select_related("instrument").order_by("instrument_id"):
        if holding.instrument_id in not_ours and not is_in_doubt(holding.instrument_id):
            result.not_ours.append(holding)
            continue
        result.checked += 1
        # Measured against the range settlement can explain (M31c), not a
        # single number. Shares bought today have not been delivered, and a
        # settlement holiday delays them a further day, so the broker showing
        # fewer than the lots hold is correct on those evenings -- and a
        # recurring false alarm is worse than no alarm at all.
        delivered, expected = fifo.settlement_window(
            holding.instrument_id, on=detected_on, book=book
        )
        actual = holding.quantity
        explained = delivered <= actual <= expected

        open_divergence = (
            Divergence.objects.filter(instrument=holding.instrument, resolved_at__isnull=True)
            .order_by("-detected_on")
            .first()
        )

        if explained:
            if open_divergence is not None:
                # It resolved itself: whatever was lagging has caught up.
                open_divergence.resolved_at = timezone.now()
                open_divergence.resolved_by = "system"
                open_divergence.note = "the holding and the lots agree again"
                open_divergence.save(update_fields=["resolved_at", "resolved_by", "note"])
                result.closed.append(open_divergence)
            continue

        if open_divergence is not None:
            continue  # already raised; do not raise it again every evening

        divergence = Divergence.objects.create(
            instrument=holding.instrument,
            expected_quantity=expected,
            actual_quantity=actual,
            detected_on=detected_on,
            trace_id=current_trace_id(),
        )
        result.opened.append(divergence)

        record(
            "divergence.detected",
            entity=divergence,
            after={
                "instrument": holding.instrument_id,
                "expected": expected,
                "actual": actual,
                "difference": divergence.difference,
            },
        )
        logger.error(
            "holdings and lots disagree",
            extra=fields(
                instrument=holding.instrument_id,
                lots_say=expected,
                broker_says=actual,
            ),
        )

    return result


@transaction.atomic
def classify(
    divergence: Divergence,
    *,
    classification: str,
    actor: str,
    note: str = "",
    explained_by: ExternalTransaction | None = None,
) -> Divergence:
    """Record what the operator says the difference was.

    A classification on its own explains nothing: an external sale still has
    to be *recorded* so the lots are consumed. This links the two, so the
    divergence and the entry that explains it stay together.
    """
    if classification == DivergenceClassification.UNCLASSIFIED:
        raise ValueError("Classifying a divergence as unclassified explains nothing.")

    divergence.classification = classification
    divergence.explained_by = explained_by
    divergence.note = note[:255]
    divergence.resolved_at = timezone.now()
    divergence.resolved_by = actor
    divergence.save(
        update_fields=["classification", "explained_by", "note", "resolved_at", "resolved_by"]
    )

    record(
        "divergence.classified",
        actor=actor,
        entity=divergence,
        after={
            "instrument": divergence.instrument_id,
            "classification": classification,
            "explained_by": str(explained_by.id) if explained_by else None,
            "note": note,
        },
    )
    logger.info(
        "divergence classified",
        extra=fields(
            instrument=divergence.instrument_id,
            classification=classification,
            actor=actor,
        ),
    )
    return divergence


def open_divergences(instrument_id: str | None = None):
    rows = Divergence.objects.filter(resolved_at__isnull=True)
    if instrument_id:
        rows = rows.filter(instrument_id=instrument_id)
    return rows


def is_in_doubt(instrument_id: str) -> bool:
    return open_divergences(instrument_id).exists()


def require_settled(instrument_id: str, *, what: str) -> None:
    """Withhold a per-instrument figure while its holding is in doubt (M31b)."""
    divergence = open_divergences(instrument_id).first()
    if divergence is None:
        return

    raise DivergenceUnresolved(
        f"{what} for {instrument_id} is withheld: the lots say "
        f"{divergence.expected_quantity} and the broker says "
        f"{divergence.actual_quantity}. Classify the difference first."
    )


def portfolio_caveats() -> list[dict]:
    """What a portfolio-level figure should be shown alongside.

    Portfolio figures are still produced while one name is in doubt --
    withholding everything because of one instrument would be its own kind of
    lie -- but never without saying which name and how many shares (M31b).
    """
    return [
        {
            "instrument": divergence.instrument_id,
            "lots_say": divergence.expected_quantity,
            "broker_says": divergence.actual_quantity,
            "difference": divergence.difference,
            "detected_on": str(divergence.detected_on),
        }
        for divergence in open_divergences().select_related("instrument")
    ]
