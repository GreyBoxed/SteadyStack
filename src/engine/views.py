"""The day, as the operator reads it.

The ranking is shown in full rather than summarised, because the question a
person actually asks is never "what did it buy" on its own -- it is "why that
one", and the answer is the row above the one that was chosen.
"""

from decimal import Decimal

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from engine.models import TradeSheet


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    sheets = TradeSheet.objects.select_related("instrument").all()[:30]
    return render(request, "engine/index.html", {"sheets": sheets})


@require_GET
def detail(request: HttpRequest, trading_date, version: int) -> HttpResponse:
    try:
        sheet = TradeSheet.objects.select_related("instrument", "model_portfolio").get(
            trading_date=trading_date, version=version
        )
    except TradeSheet.DoesNotExist as exc:
        raise Http404("No such tradesheet.") from exc

    from audit.context import today_ist
    from audit.models import AuditEvent, BrokerCall
    from engine import day as day_view
    from engine.models import CashLedger
    from orders import overrides
    from orders.models import DAILY_BUY_STREAM, Fill, Order, Tick
    from portfolio.models import Lot

    snapshot = sheet.ranking_snapshot or {}
    orders = (
        Order.objects.filter(tradesheet=sheet)
        .select_related("instrument")
        .prefetch_related("fills")
        .order_by("tick_no")
    )
    ticks = list(Tick.objects.filter(tradesheet=sheet).select_related("order").order_by("tick_no"))
    # The day's buy only: a rebalance batch's sells hang off the day's sheet
    # too, and are not what the day bought.
    fills = list(
        Fill.objects.filter(order__tradesheet=sheet, order__stream=DAILY_BUY_STREAM).select_related(
            "order", "order__instrument"
        )
    )
    charges = sum((fill.charges for fill in fills), Decimal("0"))
    gross = sum((fill.price * fill.quantity for fill in fills), Decimal("0"))
    filled = sum(fill.quantity for fill in fills)
    cash_row = CashLedger.objects.filter(trading_date=sheet.trading_date, book=sheet.book).first()
    today = today_ist()
    today_sheet = TradeSheet.objects.filter(trading_date=today).order_by("-version").first()

    return render(
        request,
        "engine/detail.html",
        {
            "sheet": sheet,
            "ranked": day_view.ranking_rows(sheet),
            "lines": day_view.line_rows(sheet),
            "can_adjust": sheet.state in {"DRAFT", "RUNNING", "AWAITING_SESSION"},
            "excluded": snapshot.get("excluded", []),
            "portfolio_value": snapshot.get("portfolio_value"),
            "orders": orders,
            "order_rows": day_view.order_rows(orders, ticks),
            "ticks": ticks,
            "ticks_placed": sum(1 for tick in ticks if tick.order_id),
            "excluded_ticks": sorted(overrides.excluded_ticks(sheet)),
            "history": sheet.overrides.order_by("-decided_at"),
            "fills": fills,
            "filled": filled,
            "average": (gross / filled) if filled else None,
            "gross": gross,
            "charges": charges,
            "charge_lines": day_view.charge_lines(fills),
            "net": gross + charges,
            "projected_carry": sheet.spendable_cash - gross - charges,
            "lots_created": Lot.objects.filter(created_from_fill__in=fills).count(),
            "fallback_used": next((o for o in orders if o.is_fallback), None),
            "broker_calls": BrokerCall.objects.filter(trace_id=sheet.trace_id).count(),
            "cash_row": cash_row,
            "lumpsums": day_view.lumpsums(sheet.trading_date),
            "neighbours": day_view.neighbours(sheet.trading_date),
            "today_sheet": today_sheet if today_sheet and today_sheet.pk != sheet.pk else None,
            "regeneration": regeneration(sheet),
            "authorization": authorization(sheet),
            "other_versions": TradeSheet.objects.filter(trading_date=sheet.trading_date)
            .exclude(pk=sheet.pk)
            .order_by("version"),
            # Everything that happened that day, under the one trace id the
            # day stamps on every line it writes.
            "audit": AuditEvent.objects.filter(trace_id=sheet.trace_id).order_by("occurred_at")[
                :200
            ],
        },
    )


@require_POST
def override(request: HttpRequest, trading_date, version: int) -> HttpResponse:
    """Change what the day does, through the rules that already exist.

    Nothing is validated twice: `orders.overrides` refuses a skip after a fill
    and cancels the newest orders on a reduction, and a second opinion here
    could only disagree with the first.
    """
    from django.contrib import messages
    from django.shortcuts import redirect

    from broker import get_broker
    from engine.forms import AdjustLineForm, ExcludeTickForm, ReduceForm, SkipForm, SkipLineForm
    from orders import overrides

    try:
        sheet = TradeSheet.objects.get(trading_date=trading_date, version=version)
    except TradeSheet.DoesNotExist as exc:
        raise Http404("No such tradesheet.") from exc

    action = request.POST.get("action", "")
    actor = request.user.get_username()
    forms = {
        "reduce": ReduceForm,
        "exclude": ExcludeTickForm,
        "skip": SkipForm,
        "stop": SkipForm,
        "adjust_line": AdjustLineForm,
        "skip_line": SkipLineForm,
    }

    back = redirect(_safe_next(request, _detail_url(trading_date, version)))
    if action not in forms:
        messages.error(request, "That is not something an override can do.")
        return back

    form = forms[action](request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
        return back

    try:
        if action in ("adjust_line", "skip_line"):
            symbol = form.cleaned_data["symbol"]
            outcome = overrides.adjust_line(
                sheet,
                symbol=symbol,
                quantity=form.cleaned_data.get("quantity", 0),
                actor=actor,
                reason=form.cleaned_data["reason"],
                broker=get_broker(),
                skip=action == "skip_line",
            )
            cancelled = len(outcome.cancelled or [])
            messages.success(
                request,
                f"{symbol} skipped for the day; {cancelled} order(s) cancelled."
                if action == "skip_line"
                else f"{symbol} is now {form.cleaned_data['quantity']}; "
                f"{cancelled} order(s) cancelled.",
            )
        elif action == "reduce":
            outcome = overrides.reduce_quantity(
                sheet,
                quantity=form.cleaned_data["quantity"],
                actor=actor,
                reason=form.cleaned_data["reason"],
                broker=get_broker(),
            )
            messages.success(
                request,
                f"Quantity is now {form.cleaned_data['quantity']}; "
                f"{len(getattr(outcome, 'cancelled', []) or [])} order(s) cancelled.",
            )
        elif action == "exclude":
            overrides.exclude_tick(
                sheet,
                tick_no=form.cleaned_data["tick_no"],
                actor=actor,
                reason=form.cleaned_data["reason"],
            )
            messages.success(request, f"Tick {form.cleaned_data['tick_no']} will place nothing.")
        elif action == "stop":
            outcome = overrides.stop_day(
                sheet, actor=actor, reason=form.cleaned_data["reason"], broker=get_broker()
            )
            messages.success(
                request,
                "The day is stopped: "
                f"{len(getattr(outcome, 'cancelled', []) or [])} order(s) cancelled, "
                "nothing more is placed, and what was not spent carries.",
            )
            unconfirmed = getattr(outcome, "unconfirmed", []) or []
            if unconfirmed:
                messages.error(
                    request,
                    f"{len(unconfirmed)} order(s) could NOT be confirmed cancelled and may "
                    f"still be live at the broker: {', '.join(unconfirmed)}. "
                    "Check the broker's own order book.",
                )
        else:
            overrides.skip_day(
                sheet, actor=actor, reason=form.cleaned_data["reason"], broker=get_broker()
            )
            messages.success(request, "The day is skipped; the full amount carries.")
    except overrides.OverrideRefused as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, str(exc))

    return back


def regeneration(sheet: TradeSheet) -> dict:
    """Whether this sheet can be replanned from the browser, and if not, why.

    Only today's latest version, and only while the lock is open: before the
    day has bought anything and before it has settled. A past day is a record.
    """
    from audit.context import today_ist
    from engine.lock import why_locked

    latest = TradeSheet.objects.filter(trading_date=sheet.trading_date).order_by("-version")
    if sheet.trading_date != today_ist():
        return {
            "allowed": False,
            "why": "Only today's tradesheet can be regenerated.",
            "sheet": sheet,
        }
    if latest.values_list("pk", flat=True).first() != sheet.pk:
        return {
            "allowed": False,
            "why": "A later version exists; regenerate from that one.",
            "sheet": sheet,
        }
    from engine.planning import market_closed_reason
    from engine.replan import why_not as why_not_replannable

    reason = why_locked(sheet.trading_date)
    closed = market_closed_reason(sheet.trading_date) if reason is None else None
    # Once the day has bought, Regenerate is refused for good; the way
    # through is to stop the day and replan what is left of it.
    replan_why = why_not_replannable(sheet) if reason is not None else None
    return {
        "allowed": reason is None and closed is None,
        "why": reason or (f"{closed}." if closed else ""),
        "sheet": sheet,
        "unplanned_money": _unplanned_money(sheet) if reason is None else Decimal("0"),
        "market_closed": closed or "",
        "replan_allowed": reason is not None and replan_why is None,
        "replan_why": replan_why or "",
        "placement_close": _placement_close(),
    }


def _placement_close():
    from configuration.store import current as current_configuration

    return current_configuration().placement_window_close


def _unplanned_money(sheet: TradeSheet) -> Decimal:
    """Cash the day now has that this version was not sized against.

    Usually a lumpsum credited after the sheet was built. The sheet is never
    changed to include it; a regenerated version is how the day spends it.

    A live sheet capped at what Zerodha held (I4) was sized below the budget
    on purpose, so it is measured against the budget it was capped from: the
    cap is not money that arrived (m-1). Read in the sheet's own book.
    """
    from engine.ledger import opening_for

    cap = (sheet.ranking_snapshot or {}).get("cash_cap") or {}
    planned = Decimal(cap["budget"]) if cap.get("capped") else sheet.spendable_cash
    spendable = opening_for(sheet.trading_date, book=sheet.book).spendable
    return max(Decimal("0"), spendable - planned)


@require_POST
def regenerate(request: HttpRequest, trading_date, version: int) -> HttpResponse:
    """Replan today from fresh holdings and quotes, as a new version (D13).

    The old version is kept, as every version is. The reason is audited with
    the new version, because "why is there a v2" is exactly the question the
    day will be asked later.
    """
    from django.contrib import messages
    from django.shortcuts import redirect

    from audit.events import record
    from broker.exceptions import BrokerError
    from engine.lock import TradesheetLocked
    from engine.planning import PlanningFailed, plan_from_broker
    from engine.tradesheet import GenerationRefused

    try:
        sheet = TradeSheet.objects.get(trading_date=trading_date, version=version)
    except TradeSheet.DoesNotExist as exc:
        raise Http404("No such tradesheet.") from exc

    back = redirect(_safe_next(request, _detail_url(trading_date, version)))
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Say why the day is being replanned.")
        return back

    allowed = regeneration(sheet)
    if not allowed["allowed"]:
        messages.error(request, allowed["why"])
        return back

    actor = request.user.get_username()
    try:
        new = plan_from_broker(trading_date=sheet.trading_date, actor=actor)
    except (TradesheetLocked, GenerationRefused, PlanningFailed, BrokerError) as exc:
        messages.error(request, f"Not regenerated: {exc}")
        return back

    record(
        "tradesheet.regenerated",
        actor=actor,
        entity=new,
        before={"version": sheet.version},
        after={"version": new.version, "reason": reason[:255]},
    )
    messages.success(request, f"Regenerated as v{new.version}. v{sheet.version} is kept as it was.")
    return redirect(_safe_next(request, _detail_url(trading_date, new.version)))


@require_POST
def replan(request: HttpRequest, trading_date, version: int) -> HttpResponse:
    """Plan what is left of a stopped day as a new version (engine.replan).

    The counterpart of Regenerate for a day that has already bought: the new
    version is net of everything bought and spent so far, and waits to be
    authorised like any other.
    """
    from django.contrib import messages
    from django.shortcuts import redirect

    from broker.exceptions import BrokerError
    from engine.replan import ReplanRefused, replan_from_broker
    from orders.idempotency import Busy

    try:
        sheet = TradeSheet.objects.get(trading_date=trading_date, version=version)
    except TradeSheet.DoesNotExist as exc:
        raise Http404("No such tradesheet.") from exc

    back = redirect(_safe_next(request, _detail_url(trading_date, version)))
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Say why the rest of the day is being replanned.")
        return back

    try:
        new = replan_from_broker(sheet, actor=request.user.get_username(), reason=reason)
    except (ReplanRefused, Busy, BrokerError) as exc:
        messages.error(request, f"Not replanned: {exc}")
        return back

    messages.success(
        request,
        f"The rest of the day is planned as v{new.version}, with "
        f"₹{new.spendable_cash:,.2f} left to spend. Authorize it to start placing.",
    )
    return redirect(_safe_next(request, _detail_url(trading_date, new.version)))


@require_POST
def generate_today(request: HttpRequest) -> HttpResponse:
    """Build today's first tradesheet from the browser.

    The same path the 09:45 timer takes -- fresh holdings, fresh quotes, a
    name nobody is offering sized at its previous close -- so a sheet made by
    hand is made exactly as the scheduled one would be. Refused once today
    has a sheet: from then on the day is regenerated, with a reason, not
    generated again.
    """
    from django.contrib import messages
    from django.shortcuts import redirect

    from audit.context import today_ist
    from broker.exceptions import BrokerError
    from engine.lock import TradesheetLocked
    from engine.planning import PlanningFailed, plan_from_broker
    from engine.tradesheet import GenerationRefused
    from trading_calendar.sessions import is_trading_day

    today = today_ist()
    if not is_trading_day(today):
        messages.error(request, f"{today:%d %b} is not a trading day; there is nothing to plan.")
        return redirect(_safe_next(request))
    if TradeSheet.objects.filter(trading_date=today).exists():
        messages.error(request, "Today already has a tradesheet. Regenerate it instead.")
        return redirect(_safe_next(request))

    try:
        sheet = plan_from_broker(trading_date=today, actor=request.user.get_username())
    except (TradesheetLocked, GenerationRefused, PlanningFailed, BrokerError) as exc:
        messages.error(request, f"Not generated: {exc}")
        return redirect(_safe_next(request))

    messages.success(
        request,
        f"Today's tradesheet is v{sheet.version}: {sheet.names_label or 'nothing to buy'}.",
    )
    return redirect(_safe_next(request, _detail_url(today, sheet.version)))


@require_POST
def authorize(request: HttpRequest, trading_date, version: int) -> HttpResponse:
    """Let today's plan place orders (M15f)."""
    from django.contrib import messages
    from django.shortcuts import redirect

    from engine.authorization import AuthorizationRefused
    from engine.authorization import authorize as authorize_sheet

    try:
        sheet = TradeSheet.objects.get(trading_date=trading_date, version=version)
    except TradeSheet.DoesNotExist as exc:
        raise Http404("No such tradesheet.") from exc

    try:
        sheet = authorize_sheet(sheet, actor=request.user.get_username())
    except AuthorizationRefused as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Authorized v{sheet.version}: the ladder places from its next tick "
            f"({sheet.names_label}, ₹{sheet.planned_cost:.2f}).",
        )
    return redirect(_safe_next(request))


def _safe_next(request: HttpRequest, default: str | None = None) -> str:
    """Where to go back to after the form: this site only, else `default`
    (Today when not given).

    The page posts its own path; anything pointing off the site is ignored,
    so a crafted form cannot use the authorize step to send anybody elsewhere.
    The phone posts to these same views and names itself here, so a decision
    taken on the phone lands back on the phone.
    """
    from django.urls import reverse
    from django.utils.http import url_has_allowed_host_and_scheme

    target = request.POST.get("next") or ""
    if url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return default or reverse("web:home")


def _detail_url(trading_date, version: int) -> str:
    from django.urls import reverse

    return reverse("engine:detail", kwargs={"trading_date": trading_date, "version": version})


def authorization(sheet: TradeSheet | None) -> dict | None:
    """The Authorize control's state for a sheet, or None if there is no sheet."""
    if sheet is None:
        return None
    from engine.authorization import why_not

    return {"sheet": sheet, "why_not": why_not(sheet) or ""}
