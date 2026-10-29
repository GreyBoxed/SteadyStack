"""The model portfolio screens: upload, review the diff, confirm.

Two steps rather than one, because the upload is the instruction every future
purchase follows. The first step validates and shows what would change; the
second writes the version. Nothing is stored until the operator has seen the
diff (D14).

The file waits in the session between the steps. It is a few kilobytes of
symbols and percentages, it belongs to the operator who just uploaded it, and
the alternative -- writing a provisional version and deleting it on
abandonment -- would put rows in an immutable table that were never meant to
be there.
"""

import logging

from django.contrib import messages
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from audit.logging import fields
from model_portfolio.models import ModelPortfolio
from model_portfolio.uploads import (
    UploadRefused,
    diff_against_current,
    is_identical_to_current,
    store,
    validate,
)

logger = logging.getLogger("steadystack.model_portfolio")

PENDING_KEY = "model_portfolio_pending"

#: A model portfolio is a few dozen lines. Anything larger is not one, and
#: reading it into memory to say so would be the wrong way to find out.
MAX_UPLOAD_BYTES = 256 * 1024


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    return render(request, "model_portfolio/index.html", _page())


def _page() -> dict:
    """Everything the page shows about the version in force.

    Shared by the page and by a refused upload, which re-renders it with the
    errors on top -- and should not lose the summary and history to do so.
    """
    from performance.valuation import evenings

    versions = list(ModelPortfolio.objects.all()[:20])
    current = versions[0] if versions else None
    rows = _with_actual(current)
    latest = evenings().order_by("-trading_date").values_list("trading_date", flat=True)
    return {
        "versions": versions,
        "history": _history(versions),
        "current": current,
        "rows": rows,
        "summary": _summary(rows, latest.first()),
    }


@require_POST
def upload(request: HttpRequest) -> HttpResponse:
    uploaded = request.FILES.get("file")
    if uploaded is None:
        messages.error(request, "Choose a CSV file to upload.")
        return redirect("model_portfolio:index")

    if uploaded.size > MAX_UPLOAD_BYTES:
        messages.error(request, "That file is too large to be a model portfolio.")
        return redirect("model_portfolio:index")

    raw = uploaded.read().decode("utf-8-sig", errors="replace")
    result = validate(raw)

    if not result.ok:
        return render(
            request,
            "model_portfolio/index.html",
            {**_page(), "errors": result.errors, "filename": uploaded.name},
            status=400,
        )

    request.session[PENDING_KEY] = {"raw": raw, "filename": uploaded.name}

    return render(
        request,
        "model_portfolio/preview.html",
        {
            "diff": diff_against_current(result),
            "result": result,
            "filename": uploaded.name,
            "identical": is_identical_to_current(result),
        },
    )


@require_POST
def confirm(request: HttpRequest) -> HttpResponse:
    pending = request.session.get(PENDING_KEY)
    if not pending:
        messages.error(request, "That upload has expired. Upload the file again.")
        return redirect("model_portfolio:index")

    try:
        portfolio = store(
            pending["raw"],
            uploaded_by=request.user.get_username(),
            source_filename=pending.get("filename", ""),
            allow_identical=request.POST.get("allow_identical") == "yes",
        )
    except UploadRefused as refused:
        # Reachable when the instrument master changed between the two steps.
        for error in refused.errors:
            messages.error(request, error)
        return redirect("model_portfolio:index")
    finally:
        request.session.pop(PENDING_KEY, None)

    logger.info(
        "model portfolio stored",
        extra=fields(version=portfolio.version, rows=portfolio.rows.count()),
    )
    messages.success(request, f"Model portfolio v{portfolio.version} is now in force.")
    return redirect("model_portfolio:index")


def _with_actual(portfolio):
    """Each target beside what the portfolio actually holds.

    A target weight on its own says nothing about whether it is being reached.
    The marker is the actual, which is the number the engine ranks on, so the
    page shows the same gap the machine acts upon.
    """
    if portfolio is None:
        return []

    from decimal import Decimal

    from performance.valuation import evenings, value_now

    latest = evenings().order_by("-trading_date").first()
    prices = {
        symbol: Decimal(str(row["price"]))
        for symbol, row in ((latest.holdings_snapshot or {}) if latest else {}).items()
        if isinstance(row, dict) and row.get("price")
    }
    whole = value_now(prices)

    # A name that is not held has no price in the evening's holdings -- and it
    # is exactly the name most likely to be out of reach. The latest ranking
    # priced every name in the universe, so it fills the gaps, marked as such.
    from engine.models import TradeSheet

    ranked_on = None
    sheet = (
        TradeSheet.objects.exclude(ranking_snapshot={})
        .filter(ranking_snapshot__has_key="ranked")
        .order_by("-trading_date", "-version")
        .first()
    )
    ranked_prices = {}
    if sheet is not None:
        ranked_on = sheet.trading_date
        for entry in sheet.ranking_snapshot.get("ranked", []):
            symbol = entry.get("symbol") or entry.get("tradingsymbol")
            if symbol and entry.get("last_price"):
                ranked_prices[symbol] = Decimal(str(entry["last_price"]))

    from django.db.models import Max

    from audit.context import today_ist
    from orders.models import OrderSide

    last_bought = dict(
        portfolio.rows.values("instrument_id")
        .annotate(
            last=Max(
                "instrument__orders__trading_date",
                filter=Q(
                    instrument__orders__side=OrderSide.BUY,
                    instrument__orders__filled_quantity__gt=0,
                ),
            )
        )
        .values_list("instrument_id", "last")
    )
    today = today_ist()

    rows = []
    for row in portfolio.rows.select_related("instrument").order_by("-target_pct"):
        value = whole.per_instrument.get(row.tradingsymbol, Decimal("0"))
        actual = (value / whole.total * Decimal("100")) if whole.total else Decimal("0")
        bought = last_bought.get(row.tradingsymbol)
        rows.append(
            {
                "row": row,
                "symbol": row.tradingsymbol,
                "exchange": row.instrument.exchange,
                "token": row.instrument.instrument_token,
                "tick_size": row.instrument.tick_size,
                "target": row.target_pct,
                "actual": actual,
                "gap": actual - row.target_pct,
                "value": value,
                "price": prices.get(row.tradingsymbol) or ranked_prices.get(row.tradingsymbol),
                "price_from_ranking": row.tradingsymbol not in prices
                and row.tradingsymbol in ranked_prices,
                "ranked_on": ranked_on,
                "last_bought": bought,
                "days_since": (today - bought).days if bought else None,
            }
        )
    return rows


def _summary(rows: list[dict], prices_as_of) -> dict:
    """The strip above the table: size of the universe and what is out of reach."""
    from decimal import Decimal

    from configuration.store import current as current_configuration

    ceiling = current_configuration().max_price_per_share
    out_of_reach = [
        row["symbol"] for row in rows if ceiling and row["price"] and row["price"] > ceiling
    ]
    waits = [row for row in rows if row["days_since"] is not None]
    longest = max(waits, key=lambda row: row["days_since"], default=None)
    return {
        "universe": len(rows),
        "weight_sum": sum((row["target"] for row in rows), Decimal("0")),
        "drift": sum((abs(row["gap"]) for row in rows), Decimal("0")),
        "ceiling": ceiling,
        "out_of_reach": out_of_reach,
        "never_bought": [row["symbol"] for row in rows if row["last_bought"] is None],
        "longest": longest,
        "prices_as_of": prices_as_of,
    }


def _history(versions) -> list[dict]:
    """Each version with what it changed against the one before it."""
    ordered = sorted(versions, key=lambda v: v.version)
    history, before = [], {}
    for version in ordered:
        weights = version.weights()
        added = sorted(set(weights) - set(before))
        removed = sorted(set(before) - set(weights))
        moved = sorted(s for s in set(weights) & set(before) if weights[s] != before[s])
        parts = []
        if not before:
            history.append({"version": version, "summary": f"first version, {len(weights)} names"})
            before = weights
            continue
        if added:
            parts.append("added " + ", ".join(added))
        if removed:
            parts.append("removed " + ", ".join(removed))
        if moved:
            parts.append(f"reweighted {len(moved)}")
        history.append({"version": version, "summary": "; ".join(parts) or "no change"})
        before = weights
    return list(reversed(history))
