"""Divergences, and the operator's answer to them.

A divergence is the one thing the system refuses to decide for itself: the
broker says one quantity and the lots say another, and which is right is a
question about the world, not about arithmetic. Until somebody answers it the
instrument's figures are withheld (M31b), so this screen is what un-withholds
them.
"""

import logging

from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from records import divergence as divergence_reader
from records.forms import ClassifyForm
from records.models import Divergence

logger = logging.getLogger("steadystack.records")


@require_http_methods(["GET"])
def divergences(request: HttpRequest) -> HttpResponse:
    open_rows = list(
        divergence_reader.open_divergences().select_related("instrument").order_by("detected_on")
    )
    settled = list(
        Divergence.objects.filter(resolved_at__isnull=False)
        .select_related("instrument", "explained_by")
        .order_by("-resolved_at")[:20]
    )

    return render(
        request,
        "records/divergences.html",
        {
            "open_rows": open_rows,
            "settled": settled,
            "not_ours": divergence_reader.not_the_portfolios(),
        },
    )


@require_http_methods(["GET", "POST"])
def classify(request: HttpRequest, pk) -> HttpResponse:
    try:
        row = Divergence.objects.select_related("instrument").get(pk=pk)
    except Divergence.DoesNotExist as exc:
        raise Http404("No such divergence.") from exc

    if row.resolved_at is not None:
        messages.info(request, "That divergence was already classified.")
        return redirect("records:divergences")

    form = ClassifyForm(request.POST or None)

    if request.method == "POST" and form.is_valid() and _posts_in_paper(form):
        messages.error(
            request,
            "The order mode is paper: this classification would post to the live book, "
            "and a paper evening's difference is not the real account's. Leave it open "
            "until live resumes, or classify it as one that records no movement.",
        )
    elif request.method == "POST" and form.is_valid():
        try:
            _apply(row, form, actor=request.user.get_username())
        except Exception as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"{row.instrument_id} classified. Its figures are no longer withheld.",
            )
            return redirect("records:divergences")

    return render(request, "records/classify.html", {"row": row, "form": form})


def _posts_in_paper(form: ClassifyForm) -> bool:
    """A classification that records an external movement posts to the live
    book; in paper mode the difference it explains is a paper one (m-8)."""
    from configuration.store import current

    return form.side is not None and not current().is_live


@transaction.atomic
def _apply(row: Divergence, form: ClassifyForm, *, actor: str) -> None:
    """Classify, and record the movement that explains it, as one act.

    Either both land or neither does: a classification without its entry
    leaves the holding still wrong while claiming to have explained it.
    """
    from records.external import record_external
    from records.models import PaidFrom

    entry = None
    side = form.side
    if side is not None:
        entry = record_external(
            instrument=row.instrument,
            side=side,
            quantity=abs(row.difference),
            occurred_on=form.cleaned_data["occurred_on"],
            actor=actor,
            reason=form.cleaned_data["note"],
            price=form.cleaned_data["price"],
            charges=form.cleaned_data.get("charges") or 0,
            paid_from=form.cleaned_data.get("paid_from") or PaidFrom.ZERODHA,
        ).transaction

    divergence_reader.classify(
        row,
        classification=form.cleaned_data["classification"],
        actor=actor,
        note=form.cleaned_data["note"],
        explained_by=entry,
    )


@require_http_methods(["GET", "POST"])
def basis(request: HttpRequest) -> HttpResponse:
    """Lots still carrying an estimated basis, and the note that settles one.

    A lot written from the rate card is an estimate. The contract note is what
    settles it (M35b): the difference posts to the ledger and the lot is
    superseded by one carrying the true cost.
    """
    from portfolio import contract_note
    from records.forms import ContractNoteForm

    form = ContractNoteForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        from portfolio.models import Lot

        try:
            lot = Lot.objects.get(pk=request.POST.get("lot"))
            result = contract_note.finalise(
                lot,
                actual_charges=form.cleaned_data["actual_charges"],
                actor=request.user.get_username(),
                note_reference=form.cleaned_data["note_reference"],
            )
        except Lot.DoesNotExist:
            messages.error(request, "That lot no longer exists.")
        except Exception as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Basis finalised. The ₹{result.charge_difference} difference is owed "
                f"to the next day that settles.",
            )
            return redirect("records:basis")

    return render(
        request,
        "records/basis.html",
        {"lots": contract_note.estimated_lots().select_related("instrument"), "form": form},
    )


@require_http_methods(["GET", "POST"])
def corporate_action(request: HttpRequest) -> HttpResponse:
    """Apply a ratio to every open lot of one instrument.

    A person enters this on purpose. A split applied automatically, without
    anybody looking, is a silent rewrite of every cost basis in the name.
    """
    from records import corporate_actions
    from records.forms import CorporateActionForm
    from records.models import Correction

    form = CorporateActionForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        from portfolio.models import Instrument

        try:
            instrument = Instrument.objects.get(tradingsymbol=form.cleaned_data["instrument"])
            result = corporate_actions.apply_ratio(
                instrument=instrument,
                new_shares=form.cleaned_data["new_shares"],
                per_old_shares=form.cleaned_data["per_old_shares"],
                effective_on=form.cleaned_data["effective_on"],
                actor=request.user.get_username(),
                reason=form.cleaned_data["reason"],
            )
        except Instrument.DoesNotExist:
            messages.error(request, "No instrument with that symbol.")
        except corporate_actions.CorporateActionRefused as exc:
            messages.error(request, str(exc))
        except Exception as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"{len(result.created)} lot(s) superseded. The originals stand "
                f"as they were written.",
            )
            return redirect("records:corporate_action")

    return render(
        request,
        "records/corporate_action.html",
        {
            "form": form,
            "corrections": Correction.objects.order_by("-created_at")[:15],
        },
    )
