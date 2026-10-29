"""Operator-facing pages.

The home page is where the day is read from: session status first, because a
missing Kite session is the one failure that costs a trading day and the only
one the operator can fix by hand (M4). Phase 1 shows what exists; the ladder
and settlement panels arrive with Phase 2.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods

from records.forms import LumpsumForm

logger = logging.getLogger("steadystack.web")


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    """The morning screen.

    Session status first and largest, because a missing Kite session is the
    one failure that costs a trading day and the only one the operator can fix
    by hand (M4). Everything under it answers a question somebody has before
    coffee, in the order they ask it.
    """
    from audit.context import today_ist
    from broker import session as kite_session
    from broker.mode import configured_mode
    from performance.valuation import held_quantities
    from web import deck

    day = today_ist()
    priced = _prices_now(sorted(held_quantities()))
    notices = deck.notices()

    return render(
        request,
        "web/home.html",
        {
            "session": kite_session.status(),
            "mode": configured_mode(),
            "priced": priced,
            "parked": deck.parked(day),
            **_today_live(day),
            "regeneration": _todays_regeneration(day),
            "authorization": _todays_authorization(day),
            "can_generate": _can_generate_today(day),
            "market_closed": _market_closed(day),
            "notices": notices,
            "attention": sum(1 for notice in notices if notice["level"] == "warning"),
            "snapshot": deck.snapshot(priced.by_symbol, day),
            "recent": deck.recent_days(),
            "checks": deck.checks(),
        },
    )


@require_GET
def home_live(request: HttpRequest) -> HttpResponse:
    """Today's moving parts on their own, for the page to swap in.

    Only what a round changes -- health, the cycle, what is running -- and
    only what Postgres holds: a page left open all day must not become a
    broker call every thirty seconds. The forms stay out of it, so a refresh
    can never move a button from under a click.
    """
    from audit.context import today_ist

    return _live_fragment(request, "web/_today_live.html", _today_live(today_ist()))


def _today_live(day) -> dict:
    from web import deck

    return {
        "health": deck.health(day),
        "cycle": deck.cycle(day),
        "running": deck.running(day),
        "running_totals": deck.running_totals(day),
        "live": _live(day),
    }


def _live(day) -> dict:
    """Whether a page about this day is worth refreshing, and how often.

    Only today, and only while the market is in session: before 09:15 and
    after 15:30 nothing on these pages moves, and a timer that keeps firing
    anyway is noise. The pace is the ladder's, because nothing on the page
    changes faster than a round does.
    """
    from django.utils import timezone

    from audit.context import today_ist
    from configuration.store import current as current_configuration
    from trading_calendar.sessions import session_for

    on = day == today_ist() and session_for(day).covers(timezone.localtime().time())
    return {"on": on, "every": current_configuration().placer_interval_seconds}


def _live_fragment(request: HttpRequest, template: str, context: dict) -> HttpResponse:
    """A live region's contents, marked so the page can tell it from a login
    page it was redirected to, and told whether to keep asking."""
    response = render(request, template, context)
    response["X-Live-Fragment"] = "1"
    response["X-Live-Open"] = "1" if context["live"]["on"] else "0"
    response["Cache-Control"] = "no-store"
    return response


def _todays_regeneration(day):
    """The Regenerate control for today's latest sheet, or None if there is none."""
    from engine.models import TradeSheet
    from engine.views import regeneration

    sheet = TradeSheet.objects.filter(trading_date=day).order_by("-version").first()
    return regeneration(sheet) if sheet is not None else None


def _todays_authorization(day):
    """The Authorize control for today's latest sheet, or None if there is none."""
    from engine.models import TradeSheet
    from engine.views import authorization

    return authorization(TradeSheet.objects.filter(trading_date=day).order_by("-version").first())


def _can_generate_today(day) -> bool:
    """A trading day with no tradesheet yet: the Generate button's day."""
    from engine.models import TradeSheet
    from trading_calendar.sessions import is_trading_day

    return is_trading_day(day) and not TradeSheet.objects.filter(trading_date=day).exists()


def _market_closed(day) -> str:
    from engine.planning import market_closed_reason

    return market_closed_reason(day) or ""


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    """Liveness, reachable without authentication.

    Deliberately says nothing a stranger could use: no holdings, no balances,
    no symbols, no token expiry -- only whether each subsystem is in a state
    somebody should look at. A monitor can tell "the process is up" from "the
    box is gone", and an operator can tell which of the two to chase (M51).

    Answers 503 when something that should have happened by now has not --
    see `web.health` -- so an outside probe polling this is the alarm for
    silence: a timer that never fired, a feed that died, a day that never
    settled.
    """
    from web import health as checks

    report = checks.report()
    return JsonResponse(
        {"status": "ok" if report.ok else "failing", "checks": report.checks, **_subsystems()},
        status=200 if report.ok else 503,
    )


def _subsystems() -> dict:
    """Booleans only: a health endpoint that leaks is a health endpoint that
    has to be authenticated, and then nothing can reach it."""
    from audit.context import today_ist
    from broker import session as kite_session
    from orders.models import TaskRun
    from records.divergence import open_divergences

    today = today_ist()

    return {
        "session": kite_session.status().valid,
        "tasks_ran_today": TaskRun.objects.filter(trading_date=today).exists(),
        "last_task_failed": TaskRun.objects.filter(trading_date=today, succeeded=False).exists(),
        "divergences_open": open_divergences().exists(),
    }


@dataclass(frozen=True)
class Prices:
    """Prices, and honesty about where they came from."""

    by_symbol: dict
    as_of: object | None = None
    unavailable: str = ""

    @property
    def is_live(self) -> bool:
        return self.as_of is None and not self.unavailable


def _prices_now(symbols: list[str]) -> Prices:
    """Live last-traded prices, or the most recent close we stored.

    A missing Kite session stops the system trading; it must not stop it
    reporting. The value series is written every evening precisely so the
    portfolio can be read from Postgres, and a page that says "as of Friday"
    is worth more than one that says nothing. Which of the two is on screen
    is never left to the reader to guess.
    """
    from broker import get_broker
    from broker.exceptions import BrokerError
    from engine import quotes as quote_reader

    if not symbols:
        return Prices(by_symbol={})

    try:
        quotes = quote_reader.fetch(sorted(symbols), broker=get_broker())
    except BrokerError as exc:
        return _last_stored_prices(str(exc))

    return Prices(by_symbol={s: q.last_price for s, q in quotes.items()})


def _last_stored_prices(reason: str) -> Prices:
    from decimal import Decimal as D

    from performance.valuation import evenings

    latest = evenings().order_by("-trading_date").first()
    if latest is None:
        return Prices(by_symbol={}, unavailable=reason)

    stored = {}
    for symbol, row in (latest.holdings_snapshot or {}).items():
        price = row.get("price") if isinstance(row, dict) else None
        if price:
            stored[symbol] = D(str(price))

    return Prices(by_symbol=stored, as_of=latest.trading_date, unavailable=reason)


def _previous_close(priced: Prices) -> dict:
    """The close before the prices on screen, for a day-change column.

    Live prices are compared with the last stored evening; prices that *are*
    the last stored evening are compared with the one before it.
    """
    from performance.valuation import evenings

    stored = evenings().order_by("-trading_date")
    if priced.as_of is not None:
        stored = stored.filter(trading_date__lt=priced.as_of)
    evening = stored.first()
    if evening is None:
        return {}
    return {
        symbol: Decimal(str(row["price"]))
        for symbol, row in (evening.holdings_snapshot or {}).items()
        if isinstance(row, dict) and row.get("price")
    }


@require_GET
def holdings(request: HttpRequest) -> HttpResponse:
    """What is held, against what was meant to be held.

    The gap column is the whole point: it is what the engine ranks on, so the
    page shows the operator the same number the machine used rather than a
    prettier one.
    """
    from engine.ledger import money
    from model_portfolio.models import ModelPortfolio
    from performance import gains as gains_reader
    from performance import valuation
    from records.divergence import portfolio_caveats

    portfolio = ModelPortfolio.objects.order_by("-version").first()
    targets = portfolio.weights() if portfolio else {}

    priced = _prices_now(sorted(targets))
    prices = priced.by_symbol
    report = gains_reader.unrealised(prices)

    # Weights are measured against the whole portfolio, including any holding
    # in doubt. Dropping it from the denominator would make every other name
    # look overweight by exactly the amount that is uncertain -- M31b withholds
    # the instrument's own figures and flags the portfolio's, not the reverse.
    whole = valuation.value_now(prices)
    total = whole.total

    # The divergence is what puts a holding in doubt, so it decides what is
    # withheld -- not whether the gains report happened to find a lot for the
    # name. An instrument can be in doubt precisely because we have no lot
    # where the broker has shares.
    from audit.context import today_ist
    from web import deck

    caveats = portfolio_caveats()
    in_doubt = {caveat["instrument"] for caveat in caveats}
    held_quantities_all = valuation.held_quantities()

    # Per instrument from the lot rows: FIFO keeps many lots per name, and the
    # page reads one line per name.
    by_symbol: dict[str, dict] = {}
    for lot in report.rows:
        agg = by_symbol.setdefault(
            lot.instrument_id, {"cost": Decimal("0"), "gain": Decimal("0"), "lots": 0}
        )
        agg["cost"] += lot.cost
        agg["gain"] += lot.gain
        agg["lots"] += 1

    rows = []
    for symbol, target_pct in sorted(targets.items()):
        value = money(whole.per_instrument.get(symbol, Decimal("0")))
        current_pct = (value / total * Decimal("100")) if total else Decimal("0")
        quantity = held_quantities_all.get(symbol, 0)
        agg = by_symbol.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "price": prices.get(symbol),
                "value": value,
                "target_pct": target_pct,
                "current_pct": current_pct,
                "gap": current_pct - target_pct,
                "average": (agg["cost"] / quantity) if agg and quantity else None,
                "gain": agg["gain"] if agg else None,
                "gain_pct": (agg["gain"] / agg["cost"] * 100) if agg and agg["cost"] else None,
                "lots": agg["lots"] if agg else 0,
                "withheld": symbol in in_doubt,
                "unpriced": symbol in report.unpriced or prices.get(symbol) is None,
            }
        )

    previous = _previous_close(priced)
    for row in rows:
        before = previous.get(row["symbol"])
        row["day_change"] = (row["price"] - before) if row["price"] and before else None
        row["day_pct"] = (
            (row["day_change"] / before * 100) if row["day_change"] is not None else None
        )

    largest_gap = max((abs(row["gap"]) for row in rows), default=Decimal("0"))
    for row in rows:
        row["gap_pct"] = float(abs(row["gap"]) / largest_gap * 50) if largest_gap else 0

    sort = request.GET.get("sort", "gap")
    orderings = {
        "gap": lambda row: row["gap"],
        "value": lambda row: -row["value"],
        "gain": lambda row: -(row["gain"] if row["gain"] is not None else Decimal("-1e18")),
        "az": lambda row: row["symbol"],
    }
    if sort not in orderings:
        sort = "gap"
    rows.sort(key=orderings[sort])
    for rank, row in enumerate(sorted(rows, key=lambda r: r["gap"]), start=1):
        row["rank"] = rank

    from datetime import timedelta

    from performance import ledgers
    from performance import series as value_series
    from web import charts

    drift = value_series.drift_history(targets, start=today_ist() - timedelta(days=90))

    return render(
        request,
        "web/holdings.html",
        {
            "stats": deck.holdings_stats(prices, today_ist()),
            "rows": rows,
            "sort": sort,
            "sorts": [("gap", "Gap"), ("value", "Value"), ("gain", "Gain"), ("az", "A to Z")],
            "report": report,
            "caveats": caveats,
            "total": money(total),
            "totals": {
                "cost": sum((a["cost"] for a in by_symbol.values()), Decimal("0")),
                "gain": sum((a["gain"] for a in by_symbol.values()), Decimal("0")),
                "lots": sum(a["lots"] for a in by_symbol.values()),
                "quantity": sum(row["quantity"] for row in rows),
                "target": sum((row["target_pct"] for row in rows), Decimal("0")),
            },
            "drift_chart": charts.value_series(
                drift, keys=["drift"], height=160, label=lambda v: f"{v:.1f}%", label_room=12
            ),
            "drift_now": drift[-1]["drift"] if drift else None,
            "recent_cash": list(reversed(ledgers.cash(today_ist() - timedelta(days=14)).rows))[:5],
            "priced": priced,
            "portfolio": portfolio,
        },
    )


#: The value chart's ranges: the key in the URL, its label, and days back.
PERFORMANCE_RANGES = [("6m", "6M", 182), ("1y", "1Y", 365), ("all", "All", None)]


@require_GET
def performance(request: HttpRequest) -> HttpResponse:
    """Invested, worth, and whether either was worth doing.

    XIRR is the headline because daily irregular contributions make a simple
    percentage meaningless, and the benchmark sits beside it because a return
    is only good relative to the index it could have been (M41, M41a).
    """
    from datetime import timedelta

    from audit.context import today_ist
    from performance import gains as gains_reader
    from performance import returns as returns_reader
    from performance import series
    from performance.valuation import held_quantities
    from web import charts, deck

    today = today_ist()
    priced = _prices_now(sorted(held_quantities()))
    result = returns_reader.measure(priced.by_symbol, as_of=today)

    chosen = request.GET.get("range", "1y")
    days = next((d for key, _, d in PERFORMANCE_RANGES if key == chosen), 365)
    if chosen not in {key for key, _, _ in PERFORMANCE_RANGES}:
        chosen = "1y"
    history = series.value_history(start=today - timedelta(days=days) if days else None)

    net_rate, net_unavailable = series.xirr_net_of_subscription(
        returns_reader.contribution_flows(today, result.current_value), as_of=today
    )

    largest = max((abs(row.gain) for row in result.per_instrument), default=Decimal("0"))

    return render(
        request,
        "web/performance.html",
        {
            "result": result,
            "net_xirr_pct": (net_rate * 100).quantize(Decimal("0.01"))
            if net_rate is not None
            else None,
            "net_unavailable": net_unavailable,
            "totals": series.totals(gain=result.absolute_return, as_of=today),
            "chart": charts.value_series(
                [
                    {
                        "date": point.on,
                        "value": point.value,
                        "invested": point.invested,
                        "with_subscription": point.with_subscription,
                    }
                    for point in history
                ],
                keys=["value", "invested", "with_subscription"],
                fill_key="value",
                label=charts.rupees_short,
            ),
            "history_end": history[-1] if history else None,
            "ranges": PERFORMANCE_RANGES,
            "chosen": chosen,
            "contribution": [
                {
                    "row": row,
                    "pct": float(abs(row.gain) / largest * 50) if largest else 0,
                }
                for row in result.per_instrument
            ],
            "realised": gains_reader.realised(),
            "priced": priced,
            "drag": deck.cost_drag(priced.by_symbol, today),
            "quality": deck.fill_quality(),
            "calendar": deck.did_it_run(),
        },
    )


def _date_arg(request: HttpRequest, name: str):
    """A range bound the URL asked for, or None.

    A malformed date is treated as absent rather than as an error: this is a
    reading surface, and a typo in a query string should not take the page
    away from the person trying to use it.
    """
    from datetime import date

    raw = request.GET.get(name, "")
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


#: How far back a ledger looks when nobody said. Inception-to-date is the
#: honest default for a reconciliation and a terrible one for a screen.
DEFAULT_LEDGER_DAYS = 30

RANGES = [("1m", "1M", 30), ("6m", "6M", 182), ("1y", "1Y", 365), ("all", "All", None)]


def _window(request: HttpRequest):
    """The range asked for: explicit dates, a preset, or the recent default."""
    from datetime import timedelta

    from audit.context import today_ist

    start, end = _date_arg(request, "start"), _date_arg(request, "end")
    if start or end:
        return start, end, ""

    chosen = request.GET.get("range", "1m")
    for key, _, days in RANGES:
        if key == chosen:
            if days is None:
                return None, None, key
            return today_ist() - timedelta(days=days), None, key

    return today_ist() - timedelta(days=DEFAULT_LEDGER_DAYS), None, "1m"


@require_http_methods(["GET", "POST"])
def cash_ledger(request: HttpRequest) -> HttpResponse:
    """Every trading day in the window, and the identity it has to satisfy."""
    from performance import ledgers
    from web import deck

    if request.method == "POST":
        return _record_lumpsum(request)

    start, end, chosen = _window(request)
    view = ledgers.cash(start, end)
    days = _what_each_day_did([row.trading_date for row in view.rows])

    if request.GET.get("format") == "csv":
        return _csv(
            "cash-ledger",
            [
                "date", "opening_carry", "daily_allocation", "extra_allocation",
                "cash_spent", "charges", "proceeds", "other", "closing_carry", "symbol",
                "quantity",
                "outcome", "trace_id",
            ],
            [
                [
                    row.trading_date, row.opening_carry, row.daily_allocation,
                    row.extra_allocation, row.cash_spent, row.charges, row.proceeds,
                    row.other,
                    row.closing_carry,
                    days.get(row.trading_date, {}).get("symbol", ""),
                    days.get(row.trading_date, {}).get("quantity", ""),
                    days.get(row.trading_date, {}).get("outcome", ""),
                    days.get(row.trading_date, {}).get("trace_id", ""),
                ]
                for row in view.rows
            ],
        )  # fmt: skip

    return render(
        request,
        "web/cash_ledger.html",
        {
            "view": view,
            "rows": [(row, days.get(row.trading_date)) for row in view.rows],
            "exceptions": deck.cash_exceptions(),
            "integrity": deck.cash_integrity(),
            "start": request.GET.get("start", ""),
            "end": request.GET.get("end", ""),
            "ranges": RANGES,
            "chosen": chosen,
            "query": request.GET.urlencode(),
            "lumpsum_form": LumpsumForm(),
            "lumpsum_funds_paper": _lumpsum_funds_paper(),
        },
    )


def _lumpsum_funds_paper() -> bool:
    """The go-live window (NI2): the live book is open, the mode is still paper.

    A lumpsum is allocated to the engine of the mode in force, so recorded
    now it would fund the paper engine while the money is live.
    """
    from configuration.store import current
    from journal.external import live_book_opened
    from journal.models import Book

    return current().order_mode == Book.PAPER and live_book_opened()


def _what_each_day_did(days: list) -> dict:
    """For each ledger day: what it bought, how many filled, how it ended.

    Read from the sheet that placed the day's buy -- the trading board's
    choice -- and only that sheet's fills. Taking the symbol from one version
    and the fills from all of them once put another version's name beside
    this one's count.
    """
    from orders.models import DAILY_BUY_STREAM, Fill
    from web.trading import buy_sheet

    found: dict = {}
    for day in days:
        sheet = buy_sheet(day)
        if sheet is None:
            continue
        filled = sum(
            Fill.objects.filter(
                order__tradesheet=sheet, order__stream=DAILY_BUY_STREAM
            ).values_list("quantity", flat=True)
        )
        found[day] = {
            "symbol": sheet.names_label,
            "quantity": filled,
            "outcome": sheet.get_state_display(),
            "trace_id": sheet.trace_id,
            "version": sheet.version,
        }
    return found


def _csv(name: str, header: list[str], rows: list[list]) -> HttpResponse:
    """A download of exactly what the page shows, for a spreadsheet or an auditor."""
    import csv

    from audit.context import today_ist

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}-{today_ist():%Y-%m-%d}.csv"'
    writer = csv.writer(response)
    writer.writerow(header)
    # A text cell starting = + - @ is a formula to a spreadsheet; the quote
    # makes it text. Descriptions carry statement particulars and typed
    # reasons. Numbers are not text, so a negative amount stays a number.
    writer.writerows(
        [
            [
                f"'{cell}" if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@") else cell
                for cell in row
            ]
            for row in rows
        ]
    )
    return response


def _record_lumpsum(request: HttpRequest) -> HttpResponse:
    """Money arriving outside the daily amount (M8a).

    Recorded as a contribution and allocated to the engine in one step. A
    sheet already planned is not changed -- its ranking was frozen against the
    cash it was told about -- so the money is spent by a regenerated version
    or by the next tradesheet.
    """
    from django.contrib import messages
    from django.shortcuts import redirect

    from engine.ledger import money
    from journal.cash import CashError, record_lumpsum
    from journal.posting import JournalError

    form = LumpsumForm(request.POST)
    if not form.is_valid():
        if list(form.errors) == ["submission"]:
            # A missing or tampered hidden token, not a person's mistake.
            messages.error(request, "Reload the form and try again.")
        else:
            messages.error(request, "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
        return redirect("web:cash_ledger")

    try:
        result = record_lumpsum(
            form.cleaned_data["amount"],
            received_on=form.cleaned_data["received_on"],
            actor=request.user.get_username(),
            reference=form.cleaned_data["reference"],
            idempotency_key=f"lumpsum:{form.cleaned_data['submission']}",
        )
    except (CashError, JournalError) as exc:
        messages.error(request, str(exc))
    else:
        from audit.context import today_ist
        from engine.models import TradeSheet
        from journal.accounts import UNALLOCATED

        received = form.cleaned_data["received_on"]
        amount = result.contribution.lines.get(account_id=UNALLOCATED).amount
        text = f"₹{money(amount)} recorded."
        if result.restored:
            text += f" ₹{money(result.restored)} restored carry the quarterly settlement took."
        allocated = result.allocation.occurred_on if result.allocation else None
        if allocated is not None and allocated != received:
            text += (
                f" {received} has already placed its buy, so it is spendable from "
                f"{allocated}'s tradesheet."
            )
        elif received == today_ist() and TradeSheet.objects.filter(trading_date=received).exists():
            text += (
                " Today's tradesheet was built before it arrived — regenerate it to spend it today."
            )
        else:
            text += " It is spendable from the next tradesheet."
        messages.success(request, text)

    return redirect("web:cash_ledger")


@require_GET
def share_ledger(request: HttpRequest) -> HttpResponse:
    """Every quantity in and out, and where it disagrees with the broker."""
    from performance import ledgers
    from portfolio.models import Instrument
    from web import deck

    start, end, chosen = _window(request)
    instrument = request.GET.get("instrument") or None
    view = ledgers.shares(instrument_id=instrument, start=start, end=end)

    # A running total needs everything before the window too, or it would
    # restart at zero on the first row shown. The window is a date range over
    # movements already sorted by date, so walking the whole history and
    # keeping the ones inside it gives each row its balance.
    running: dict[str, int] = {}
    rows = []
    for movement in ledgers.shares(instrument_id=instrument).movements:
        running[movement.instrument_id] = running.get(movement.instrument_id, 0) + movement.quantity
        if (start is None or movement.on >= start) and (end is None or movement.on <= end):
            rows.append({"movement": movement, "balance": running[movement.instrument_id]})

    by_source: dict[str, dict] = {}
    for movement in view.movements:
        entry = by_source.setdefault(movement.label, {"in": 0, "out": 0})
        entry["in" if movement.quantity > 0 else "out"] += abs(movement.quantity)

    if request.GET.get("format") == "csv":
        return _csv(
            "share-ledger",
            ["date", "symbol", "quantity", "price", "source", "reference", "balance_after"],
            [
                [r["movement"].on, r["movement"].instrument_id, r["movement"].quantity,
                 r["movement"].price if r["movement"].price is not None else "",
                 r["movement"].label, r["movement"].reference, r["balance"]]
                for r in rows
            ],
        )  # fmt: skip

    return render(
        request,
        "web/share_ledger.html",
        {
            "view": view,
            "rows": rows,
            "by_source": sorted(by_source.items()),
            "instrument": instrument or "",
            "instruments": Instrument.objects.order_by("tradingsymbol").values_list(
                "tradingsymbol", flat=True
            ),
            "exceptions": deck.share_exceptions(),
            "integrity": deck.share_integrity(),
            "ranges": RANGES,
            "chosen": chosen,
            "query": request.GET.urlencode(),
        },
    )


@require_GET
def orders(request: HttpRequest) -> HttpResponse:
    """The trading board: every instrument with orders, and every order.

    Ticks that placed nothing are listed too. A tick that placed nothing is a
    decision the system made and had a reason for; showing only the orders
    would make the day look emptier than it was and hide the reason (M39).
    """
    from audit.context import today_ist
    from web import trading

    day = _date_arg(request, "date") or today_ist()
    # A past day is priced by what filled, not by today's market.
    symbols = trading.symbols_on(day) if day == today_ist() else []
    priced = _prices_now(symbols)
    return render(
        request,
        "web/orders.html",
        _board(request, day, priced, price_source="quote"),
    )


@require_GET
def orders_live(request: HttpRequest) -> HttpResponse:
    """The trading board's contents, for the page to swap in.

    Priced from the book the latest round stored rather than a fresh quote,
    so a board left open all afternoon costs queries, not broker calls; the
    page says which of the two it is showing.
    """
    from audit.context import today_ist
    from web import trading

    day = _date_arg(request, "date") or today_ist()
    priced = Prices(by_symbol=trading.stored_mids(day))
    return _live_fragment(
        request,
        "web/_orders_live.html",
        _board(request, day, priced, price_source="round"),
    )


def _board(request: HttpRequest, day, priced: "Prices", *, price_source: str) -> dict:
    from web import trading

    board = trading.board(
        day=day,
        prices=priced.by_symbol if priced.is_live else {},
        show=request.GET.get("show", "all"),
    )
    return {
        "day": day,
        "priced": priced,
        "price_source": price_source,
        "live": _live(day),
        **board,
    }


#: The instrument board's tabs, in the order the design gives them.
INSTRUMENT_TABS = ["orders", "ticks", "fills", "calls"]


@require_GET
def instrument_day(request: HttpRequest, day: str, symbol: str) -> HttpResponse:
    """One instrument on one day: every order, every tick, the book it read.

    The depth ladder is the one a tick stored, not a live one. That is the
    book the order was priced against, and the only one that still means
    anything once the day is over.
    """
    from web import trading

    trading_date, instrument = _instrument_day_args(day, symbol)

    raw_tick = request.GET.get("tick", "")
    tick_no = int(raw_tick) if raw_tick.isdigit() else None
    tab = request.GET.get("tab", "orders")
    if tab not in INSTRUMENT_TABS:
        tab = "orders"

    from audit.context import today_ist

    # Today's market says nothing about a past day's orders; that day is read
    # against the mid its last tick stored instead.
    priced = _prices_now([symbol]) if trading_date == today_ist() else Prices(by_symbol={})

    board = trading.instrument_day(
        day=trading_date,
        instrument=instrument,
        tick_no=tick_no,
        price=priced.by_symbol.get(symbol) if priced.is_live else None,
    )

    # A pinned round is history being read; only the latest follows the market.
    live = _live(trading_date)
    if tick_no is not None:
        live = {**live, "on": False}

    return render(
        request,
        "web/instrument_day.html",
        {**board, "tab": tab, "priced": priced, "pinned": tick_no, "live": live},
    )


@require_GET
def instrument_depth_live(request: HttpRequest, day: str, symbol: str) -> HttpResponse:
    """The depth panel on its own: the book the latest round stored.

    The ladder reads depth every round and keeps it, so following the book
    through the session is a query, not a broker call -- and it is the book
    the next order will be priced against, not a streaming one.
    """
    from web import trading

    trading_date, instrument = _instrument_day_args(day, symbol)
    tab = request.GET.get("tab", "orders")
    return _live_fragment(
        request,
        "web/_depth_live.html",
        {
            **trading.latest_depth(day=trading_date, instrument=instrument),
            "tab": tab if tab in INSTRUMENT_TABS else "orders",
            "live": _live(trading_date),
        },
    )


def _instrument_day_args(day: str, symbol: str):
    from datetime import date

    from django.http import Http404

    from portfolio.models import Instrument

    try:
        trading_date = date.fromisoformat(day)
    except ValueError as exc:
        raise Http404("Not a date.") from exc

    try:
        instrument = Instrument.objects.get(tradingsymbol=symbol)
    except Instrument.DoesNotExist as exc:
        raise Http404("No such instrument.") from exc

    return trading_date, instrument
