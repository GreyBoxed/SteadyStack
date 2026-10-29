"""The phone: what an ordinary day asks of the operator, and the way out.

Five screens -- sign in, Today, authorise, the Kite session and stop -- and
nothing else, because an ordinary day asks two things of the operator (the
morning login and authorising the tradesheet) and both have to be doable on
a train. Every decision goes through the same code the desk pages use: the
phone posts to the engine's own views and names itself as where to come back
to, so there is no second path to a rule and no second rule.

Like every page here, these compute nothing: a number shown is read from the
engine, the ledger or the deck.
"""

from datetime import datetime

from django.contrib import messages
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from accounts.views import OperatorLoginView
from web.device import DESKTOP, PHONE, SITE_COOKIE

#: Rows under the hero before it says "and more": the phone shows the day's
#: shape, the tradesheet shows all of it.
RANKED_ON_TODAY = 3
RANKED_ON_TRADE = 6


class PhoneLoginView(OperatorLoginView):
    """The same door as the desk's -- same form, same throttle, same audit --
    laid out for a phone whose authenticator is on the same phone."""

    template_name = "phone/login.html"

    def get_form(self, form_class=None):
        # The phone's keyboard: digits for the code, and the one-time-code
        # hint so the OS can offer a code it has just seen.
        form = super().get_form(form_class)
        form.fields["otp_token"].widget.attrs.update(
            {"inputmode": "numeric", "autocomplete": "one-time-code", "autocapitalize": "off"}
        )
        form.fields["username"].widget.attrs.update({"autocapitalize": "off"})
        return form


@require_GET
def switch(request: HttpRequest) -> HttpResponse:
    """Overrule the device guess, and remember it (`web.device`)."""
    to = request.GET.get("to")
    response = redirect("/")
    if to in (PHONE, DESKTOP):
        response.set_cookie(
            SITE_COOKIE, to, max_age=60 * 60 * 24 * 365, samesite="Lax", httponly=True
        )
    return response


def _latest_today(day):
    from engine.models import TradeSheet

    return (
        TradeSheet.objects.select_related("instrument")
        .filter(trading_date=day)
        .order_by("-version")
        .first()
    )


def _clock(day) -> dict:
    """Now, and how long until the fallback cutoff ends today's chances."""
    from configuration.store import current as current_configuration

    config = current_configuration()
    now = timezone.localtime()
    cutoff = timezone.make_aware(datetime.combine(day, config.market_fallback_time))
    minutes = max(0, int((cutoff - now).total_seconds() // 60))
    return {
        "now": now,
        "cutoff": config.market_fallback_time,
        "left": f"{minutes // 60}h {minutes % 60:02d}m" if minutes else "",
        "config": config,
    }


def _chrome() -> dict:
    """What every phone header shows: the session, and paper or live."""
    from broker import session as kite_session
    from broker.mode import configured_mode

    return {"session": kite_session.status(), "mode": configured_mode()}


@require_GET
def today(request: HttpRequest) -> HttpResponse:
    from audit.context import today_ist
    from web import deck
    from web.views import _live

    day = today_ist()
    notices = deck.notices()
    return render(
        request,
        "phone/today.html",
        {
            **_chrome(),
            **_today_state(day),
            "live": _live(day),
            "notices": notices,
            "attention": sum(1 for notice in notices if notice["level"] == "warning"),
        },
    )


@require_GET
def today_live(request: HttpRequest) -> HttpResponse:
    """The hero and what is running, on their own, for the page to swap in."""
    from audit.context import today_ist
    from web.views import _live, _live_fragment

    day = today_ist()
    return _live_fragment(
        request, "phone/_today_live.html", {**_chrome(), **_today_state(day), "live": _live(day)}
    )


def _today_state(day) -> dict:
    """Which of the day's situations this is, and the one thing to do about it."""
    from configuration.pause import active_pause
    from engine import day as day_view
    from engine.settlement import day_spend
    from engine.views import authorization
    from orders.overrides import ENDED
    from web import deck
    from web.views import _can_generate_today, _market_closed

    sheet = _latest_today(day)
    clock = _clock(day)
    lines = day_view.line_rows(sheet) if sheet else []
    # The day's totals, across every version: a day stopped and replanned
    # bought under both, and the rows below are the running version's alone.
    spend = day_spend(day)
    filled = sum(spend.fills.values())
    spent = spend.spent

    state = {
        "sheet": sheet,
        "clock": clock,
        "filled": filled,
        "spent": spent,
        "rows": _rows(sheet, lines),
        "can_stop": sheet is not None and sheet.state not in ENDED,
    }
    session = _chrome()["session"]
    auth = authorization(sheet)
    pause = active_pause()

    if pause is not None:
        hero = {"kind": "paused", "pause": pause}
    elif sheet is None:
        closed = _market_closed(day)
        if closed and not _can_generate_today(day):
            hero = {"kind": "quiet", "why": closed}
        elif not session.valid:
            hero = {"kind": "connect"}
        elif _can_generate_today(day) and not closed:
            hero = {"kind": "generate"}
        else:
            hero = {"kind": "quiet", "why": closed or "No tradesheet today."}
    elif sheet.state == "AWAITING_SESSION":
        hero = {"kind": "parked"}
    elif sheet.state in ENDED:
        hero = {"kind": "ended"}
    elif not sheet.is_authorized:
        hero = {"kind": "authorize", "why_not": auth["why_not"] if auth else ""}
    else:
        hero = {"kind": "running", "position": deck._ladder_position(day, clock["config"])}

    return {**state, "hero": hero}


def _rows(sheet, lines) -> list[dict]:
    """What the day wants, or what it has done: one row a name."""
    if sheet is None:
        return []
    if not sheet.is_authorized and sheet.state not in {"FILLED", "PARTIAL", "UNFILLED"}:
        from engine import day as day_view

        rows = [
            {
                "symbol": row["line"].instrument_id,
                "qty": str(row["line"].quantity),
                "tag": "PROPOSED",
                "tone": "accent",
            }
            for row in lines
        ]
        chosen = {row["symbol"] for row in rows}
        rest = [r for r in day_view.ranking_rows(sheet) if r["symbol"] not in chosen]
        rows += [
            {"symbol": r["symbol"], "qty": "—", "tag": f"RANKED {r.get('rank', '')}", "tone": ""}
            for r in rest[: max(0, RANKED_ON_TODAY - len(rows))]
        ]
        return rows
    return [
        {
            "symbol": row["line"].instrument_id,
            "qty": f"{row['filled']} / {row['line'].quantity}",
            "tag": "COMPLETE" if row["filled"] >= row["line"].quantity else "WORKING",
            "tone": "good" if row["filled"] >= row["line"].quantity else "accent",
        }
        for row in lines
    ]


@require_http_methods(["GET", "POST"])
def trade(request: HttpRequest) -> HttpResponse:
    """Review today's plan, change a quantity, and authorise it (M15f).

    A changed quantity is an override like any other -- `adjust_line`, with
    the reason given -- and it is taken in the same step as the authorisation,
    so what is authorised is what the screen showed. Either both happen or
    neither does.
    """
    from audit.context import today_ist
    from engine import day as day_view
    from engine.views import authorization, regeneration
    from orders.overrides import ENDED

    day = today_ist()
    sheet = _latest_today(day)

    if request.method == "POST":
        return _decide(request, sheet)

    context = {**_chrome(), "sheet": sheet, "clock": _clock(day)}
    if sheet is not None:
        ranked = day_view.ranking_rows(sheet)
        context.update(
            {
                "lines": day_view.line_rows(sheet),
                "ranked": ranked[:RANKED_ON_TRADE],
                "ranked_more": max(0, len(ranked) - RANKED_ON_TRADE),
                "authorization": authorization(sheet),
                "regeneration": regeneration(sheet),
                "can_adjust": sheet.state not in ENDED,
                "age": _age(sheet.generated_at),
                "left_over": sheet.spendable_cash - sheet.planned_cost,
            }
        )
    return render(request, "phone/trade.html", context)


def _age(moment) -> str:
    minutes = int((timezone.now() - moment).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    return f"{minutes // 60}h {minutes % 60:02d}m ago" if minutes >= 60 else f"{minutes}m ago"


def _decide(request: HttpRequest, sheet) -> HttpResponse:
    from engine.authorization import AuthorizationRefused
    from engine.authorization import authorize as authorize_sheet
    from orders import overrides

    back = redirect("phone:trade")
    if sheet is None or str(sheet.version) != request.POST.get("version"):
        messages.error(request, "The plan changed while you were looking. Here is the latest.")
        return back

    changed = {}
    for line in sheet.buy_lines():
        raw = request.POST.get(f"qty_{line.instrument_id}")
        if raw is None:
            continue
        try:
            quantity = int(raw)
        except ValueError:
            messages.error(request, f"{line.instrument_id}: that is not a number of shares.")
            return back
        if quantity != line.quantity:
            changed[line.instrument_id] = quantity

    reason = (request.POST.get("reason") or "").strip()
    if changed and not reason:
        messages.error(request, "Say why the quantity changed. It is kept with the override.")
        return back

    actor = request.user.get_username()
    try:
        with transaction.atomic():
            if changed:
                from broker import get_broker

                broker = get_broker()
                for symbol, quantity in changed.items():
                    overrides.adjust_line(
                        sheet,
                        symbol=symbol,
                        quantity=quantity,
                        actor=actor,
                        reason=reason,
                        broker=broker,
                        skip=quantity == 0,
                    )
                sheet.refresh_from_db()
            if request.POST.get("action") == "authorize" and not sheet.is_authorized:
                sheet = authorize_sheet(sheet, actor=actor)
    except (overrides.OverrideRefused, AuthorizationRefused) as refused:
        messages.error(request, str(refused))
        return back

    if sheet.is_authorized and request.POST.get("action") == "authorize":
        messages.success(
            request,
            f"Authorised v{sheet.version}: {sheet.names_label}, ₹{sheet.planned_cost:.2f}. "
            "The ladder places from its next round.",
        )
        return redirect("phone:today")
    messages.success(request, "Saved. The override is recorded with your name against it.")
    return back


@require_GET
def kite(request: HttpRequest) -> HttpResponse:
    from broker.session_view import NEEDS

    chrome = _chrome()
    valid = chrome["session"].valid
    return render(
        request,
        "phone/kite.html",
        {
            **chrome,
            "left": _until(chrome["session"].expires_at) if valid else "",
            "needs": [
                {
                    "label": label,
                    "status": ("RUNNING" if valid else "BLOCKED") if needed else "UNAFFECTED",
                    "blocked": needed and not valid,
                }
                for label, _sub, needed in NEEDS
            ],
        },
    )


def _until(moment) -> str:
    if moment is None:
        return ""
    minutes = max(0, int((moment - timezone.now()).total_seconds() // 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


@require_GET
def stop(request: HttpRequest) -> HttpResponse:
    """What stopping today would do, and -- once done -- what it did.

    The press itself posts to the engine's override view (`action=stop`), so
    the rule lives in `orders.overrides.stop_day` and nowhere else.
    """
    from audit.context import today_ist
    from engine.models import OverrideKind
    from engine.settlement import day_spend, planned_budget
    from orders import placement
    from orders.overrides import ENDED

    day = today_ist()
    sheet = _latest_today(day)
    context = {**_chrome(), "sheet": sheet, "clock": _clock(day)}
    if sheet is not None:
        # What the day's plans had, less what every version's fills cost,
        # charges included: the same figure as the desk's cash line.
        spend = day_spend(day)
        context.update(
            {
                "ended": sheet.state in ENDED,
                "resting": placement.open_orders(sheet).count(),
                "filled": sum(spend.fills.values()),
                "unspent": planned_budget(sheet) - spend.spent - spend.charges,
                "stopped": sheet.overrides.filter(
                    kind__in=[OverrideKind.STOP_DAY, OverrideKind.SKIP_DAY]
                )
                .order_by("-decided_at")
                .first(),
                "back": reverse("phone:stop"),
            }
        )
    return render(request, "phone/stop.html", context)
