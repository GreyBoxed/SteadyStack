"""One instrument, from the first share to the last.

The holdings page answers "what do I hold"; this answers "how did I come to
hold it". Every lot with the day it was bought and what it cost, every order
placed for it, every share that left, and the gains those produced -- under
one symbol, so a question about ITC does not require reading the whole ledger.
"""

import logging
from decimal import Decimal

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

logger = logging.getLogger("steadystack.portfolio")

ZERO = Decimal("0")


#: The list's views: the key in the URL and its label. "Relevant" is the
#: default because the master is thousands of rows and a handful matter.
INSTRUMENT_VIEWS = [
    ("relevant", "Relevant"),
    ("stocks", "All shares"),
    ("indices", "Indices"),
    ("inactive", "Retired"),
]

PAGE_SIZE = 100


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    """The instrument master, starting from the names this system touches.

    Relevant means: carries a target weight, is held (by our lots or at the
    broker), has ever had an order, or is the benchmark. Everything else is
    searchable rather than listed.
    """
    from django.core.paginator import Paginator
    from django.db.models import Q

    from configuration.store import current as current_configuration
    from model_portfolio.models import ModelPortfolio
    from orders.models import Order
    from performance.valuation import held_quantities
    from portfolio.instruments import INDEX_SEGMENT
    from portfolio.models import Holding, Instrument

    held = held_quantities()
    portfolio = ModelPortfolio.objects.order_by("-version").first()
    targets = portfolio.weights() if portfolio else {}
    benchmark = current_configuration().benchmark_symbol

    relevant = (
        set(targets)
        | {symbol for symbol, quantity in held.items() if quantity}
        | set(Holding.objects.filter(quantity__gt=0).values_list("instrument_id", flat=True))
        | set(Order.objects.values_list("instrument_id", flat=True).distinct())
        | {benchmark}
    )

    view = request.GET.get("view", "relevant")
    if view not in dict(INSTRUMENT_VIEWS):
        view = "relevant"
    query = (request.GET.get("q") or "").strip()

    views = {
        "relevant": Instrument.objects.filter(tradingsymbol__in=relevant),
        "stocks": Instrument.objects.filter(active=True).exclude(segment=INDEX_SEGMENT),
        "indices": Instrument.objects.filter(segment=INDEX_SEGMENT),
        "inactive": Instrument.objects.filter(active=False),
    }
    instruments = views[view]
    if query:
        # A search looks across the whole master, whichever view is open.
        instruments = Instrument.objects.filter(
            Q(tradingsymbol__icontains=query) | Q(name__icontains=query)
        )

    page = Paginator(instruments.order_by("tradingsymbol"), PAGE_SIZE).get_page(
        request.GET.get("page")
    )

    rows = [
        {
            "symbol": instrument.tradingsymbol,
            "name": instrument.name,
            "exchange": instrument.exchange,
            "token": instrument.instrument_token,
            "active": instrument.active,
            "is_index": instrument.segment == INDEX_SEGMENT,
            "is_benchmark": instrument.tradingsymbol == benchmark,
            "held": held.get(instrument.tradingsymbol, 0),
            "target": targets.get(instrument.tradingsymbol),
        }
        for instrument in page
    ]

    return render(
        request,
        "portfolio/index.html",
        {
            "rows": rows,
            "page": page,
            "view": view,
            "views": [(key, label, views[key].count()) for key, label in INSTRUMENT_VIEWS],
            "query": query,
            "in_universe": len(targets),
            "benchmark": benchmark,
        },
    )


@require_GET
def detail(request: HttpRequest, symbol: str) -> HttpResponse:
    from gains.models import RealisedGain
    from orders.models import Fill, Order
    from portfolio.models import Instrument, Lot
    from records.divergence import is_in_doubt
    from web.views import _prices_now

    try:
        instrument = Instrument.objects.get(tradingsymbol=symbol)
    except Instrument.DoesNotExist as exc:
        raise Http404("No such instrument.") from exc

    priced = _prices_now([symbol])
    price = priced.by_symbol.get(symbol)

    lots = list(
        Lot.objects.filter(instrument=instrument)
        .select_related("superseded_by")
        .order_by("acquired_on", "created_at")
    )
    # Every lot is listed, but only the reader's book's count as held (I1):
    # live, a paper lot is shares that were never bought.
    from portfolio.lots import in_book, reader_book

    counted = set(
        in_book(Lot.objects.filter(instrument=instrument), reader_book()).values_list(
            "pk", flat=True
        )
    )
    open_lots = [
        lot
        for lot in lots
        if lot.remaining_quantity > 0 and not lot.superseded_by_id and lot.pk in counted
    ]

    held = sum(lot.remaining_quantity for lot in open_lots)
    cost = sum((lot.cost_per_unit * lot.remaining_quantity for lot in open_lots), ZERO)
    value = price * held if price else None

    realised = RealisedGain.objects.filter(instrument=instrument).order_by("-disposed_on")

    return render(
        request,
        "portfolio/detail.html",
        {
            "instrument": instrument,
            "priced": priced,
            "price": price,
            "held": held,
            "cost": cost,
            "value": value,
            "gain": (value - cost) if value is not None else None,
            "average": (cost / held) if held else None,
            "lots": lots,
            "open_lots": open_lots,
            "orders": Order.objects.filter(instrument=instrument)
            .select_related("tradesheet")
            .order_by("-trading_date", "tick_no")[:40],
            "fills": Fill.objects.filter(order__instrument=instrument)
            .select_related("order")
            .order_by("-executed_at")[:40],
            "realised": realised,
            "realised_total": sum((row.gain for row in realised), ZERO),
            "in_doubt": is_in_doubt(symbol),
        },
    )
