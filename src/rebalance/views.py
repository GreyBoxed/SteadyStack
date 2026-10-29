"""Rebalance sells: the deliberate way shares leave the portfolio.

Uploading a file never places an order. It parses, validates against what is
actually held, and stores a draft with a one-shot token. Executing is a second,
explicit act against that token, so a stale tab cannot sell a portfolio
somebody was looking at an hour ago (M32, M32a).
"""

import logging

from django.contrib import messages
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from rebalance import batches
from rebalance.models import BatchState, RebalanceSell

logger = logging.getLogger("steadystack.rebalance")

MAX_UPLOAD_BYTES = 256 * 1024


@require_http_methods(["GET", "POST"])
def index(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        return _upload(request)

    return render(
        request,
        "rebalance/index.html",
        {
            "batches": RebalanceSell.objects.annotate(row_count=Count("rows")).order_by(
                "-uploaded_at"
            )[:20],
            "live": RebalanceSell.objects.filter(state=BatchState.RUNNING).first(),
        },
    )


def _upload(request: HttpRequest) -> HttpResponse:
    uploaded = request.FILES.get("file")
    if uploaded is None:
        messages.error(request, "Choose a CSV of symbol and quantity.")
        return redirect("rebalance:index")

    if uploaded.size > MAX_UPLOAD_BYTES:
        messages.error(request, "That file is far larger than a list of holdings should be.")
        return redirect("rebalance:index")

    try:
        batch = batches.upload(
            uploaded.read(),
            uploaded_by=request.user.get_username(),
            source_filename=uploaded.name,
            reinvest=request.POST.get("reinvest") == "on",
        )
    except batches.UploadRefused as exc:
        for problem in _problems(exc):
            messages.error(request, problem)
        return redirect("rebalance:index")
    except Exception as exc:
        messages.error(request, str(exc))
        return redirect("rebalance:index")

    messages.success(request, "Validated. Nothing has been sold — review it below.")
    return redirect("rebalance:detail", pk=batch.pk)


def _problems(exc: Exception) -> list[str]:
    """UploadRefused carries a list of line errors, or a single message."""
    payload = exc.args[0] if exc.args else str(exc)
    return [str(p) for p in payload] if isinstance(payload, list | tuple) else [str(payload)]


@require_http_methods(["GET"])
def detail(request: HttpRequest, pk) -> HttpResponse:
    from decimal import Decimal

    batch = _batch(pk)
    preview = _with_remainder(batches.preview(batch))

    return render(
        request,
        "rebalance/detail.html",
        {
            "batch": batch,
            "preview": preview,
            "totals": {
                "shares": sum(row["quantity"] for row in preview),
                "sold": sum(row["sold"] for row in preview),
                "proceeds": sum(
                    (row["proceeds"] for row in preview if row["proceeds"] is not None),
                    Decimal("0"),
                ),
            },
            "rows": batch.rows.select_related("instrument").order_by("line"),
            "can_execute": batch.state == BatchState.DRAFT and batch.token_is_live,
        },
    )


@require_POST
def execute(request: HttpRequest, pk) -> HttpResponse:
    batch = _batch(pk)

    try:
        batches.start(
            batch,
            token=request.POST.get("token", ""),
            actor=request.user.get_username(),
        )
    except batches.ExecutionRefused as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _executing_message())

    return redirect("rebalance:detail", pk=pk)


def _executing_message() -> str:
    """What happens next, which depends on when the batch was executed."""
    from django.utils import timezone

    from configuration.store import current as current_configuration

    config = current_configuration()
    if timezone.localtime().time() >= config.placement_window_close:
        return (
            f"Executing. The placement window has closed, so the close sells it at market "
            f"from {config.market_fallback_time:%H:%M}."
        )
    return (
        "Executing. The sells are placed across the rest of the placement window, one "
        f"name at a time, and whatever is unsold goes at market from "
        f"{config.market_fallback_time:%H:%M}."
    )


@require_POST
def cancel(request: HttpRequest, pk) -> HttpResponse:
    from broker import get_broker

    batch = _batch(pk)
    reason = request.POST.get("reason", "").strip() or "cancelled by the operator"

    try:
        batches.cancel(batch, actor=request.user.get_username(), reason=reason, broker=get_broker())
    except Exception as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, "Cancelled. Fills already taken stand; nothing further is placed."
        )

    return redirect("rebalance:detail", pk=pk)


def _with_remainder(rows: list[dict]) -> list[dict]:
    """What each holding drops to, and what the sale would roughly raise.

    The arithmetic belongs here, not in a template contorting filters to
    subtract two numbers. Proceeds are at the last price we have -- live, or
    the last stored close -- and say so; the fills decide the real figure.
    """
    from web.views import _prices_now

    priced = _prices_now(sorted({row["symbol"] for row in rows}))
    for row in rows:
        # `held` is what the lots hold now, which already excludes whatever
        # this batch has sold. Only the part still to sell comes off it.
        row["after"] = row["held"] - (row["quantity"] - row["sold"])
        price = priced.by_symbol.get(row["symbol"])
        row["price"] = price
        row["proceeds"] = price * row["quantity"] if price is not None else None
        row["sold_pct"] = (
            min(100, round(row["sold"] / row["quantity"] * 100)) if row["quantity"] else 0
        )
    return rows


@require_GET
def template(request: HttpRequest) -> HttpResponse:
    """The file format, as a file: a header and nothing else.

    Deliberately empty. A template pre-filled with what is held would sell
    everything if uploaded unchanged, and a sell file should only ever say
    what somebody decided to sell.
    """
    response = HttpResponse("symbol,quantity\n", content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="rebalance-sell-template.csv"'
    return response


def _batch(pk) -> RebalanceSell:
    try:
        return RebalanceSell.objects.get(pk=pk)
    except RebalanceSell.DoesNotExist as exc:
        raise Http404("No such batch.") from exc
