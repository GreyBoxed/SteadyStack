"""The settings screen.

Every number the engine reasons with, in one place, with the history of how
each came to be what it is. Changes go through `store.update`, so a save from
a browser is the same audited append as one from a shell -- including the live
gate, which refuses here for exactly the reason it refuses there.

Three changes also need a current authenticator code, because a signed-in
session is not proof that the operator is the one holding it: going live,
changing the registered static IP (which is what lets live orders out), and
raising the daily amount. Everything else, and every move in the safe
direction, stays code-free.
"""

import logging
from itertools import pairwise

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from accounts.views_security import _authenticator_accepts
from audit.events import record
from configuration.forms import ConfigurationForm
from configuration.models import ConfigurationVersion, OrderMode
from configuration.pause import active_pause
from configuration.store import MUTABLE_FIELDS, ConfigurationError
from configuration.store import current as current_configuration
from configuration.store import update as apply_change

logger = logging.getLogger("steadystack.configuration")

#: What M51 says is worth waking somebody for. There is no transport yet, so
#: the page shows what will alert rather than pretending it already does.
ALERTS = [
    "No Kite session at the 09:00 check",
    "An order rejected by the broker",
    "A day halted after three consecutive rejections",
    "A day ending unfilled",
    "The tick feed disconnecting",
    "Holdings diverging from the broker",
    "Any unhandled exception in a scheduled task",
]


@require_http_methods(["GET", "POST"])
def index(request: HttpRequest) -> HttpResponse:
    current = current_configuration()
    before = ConfigurationForm.snapshot(current)

    if request.method == "POST":
        form = ConfigurationForm(request.POST, instance=current)
        if form.is_valid():
            changed = form.changes(before)
            if not changed:
                messages.info(request, "Nothing changed.")
                return redirect("configuration:index")
            guarded = needs_a_code(changed, before)
            if guarded and not _authenticator_accepts(
                request.user, request.POST.get("otp_token", "").strip()
            ):
                record(
                    "configuration.code_refused",
                    actor=request.user.get_username(),
                    entity_type="configuration.ConfigurationVersion",
                    after={"changes": sorted(guarded)},
                )
                messages.error(
                    request,
                    f"Refused: {', '.join(guarded)} needs a current code from your "
                    "authenticator, not just a signed-in session. Nothing was saved.",
                )
                return _render(request, form, current)
            try:
                version = apply_change(
                    changed_by=request.user.get_username(),
                    reason=form.cleaned_data["reason"],
                    **changed,
                )
            except ConfigurationError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    f"Saved as version {version.version}: {', '.join(sorted(changed))} changed.",
                )
                return redirect("configuration:index")
        else:
            messages.error(request, "Nothing was saved — the form below says why.")
    else:
        form = ConfigurationForm(instance=current)

    return _render(request, form, current)


def needs_a_code(changed: dict, before: dict) -> list[str]:
    """The changes in *changed* that need the authenticator, named for the
    operator. Going back to paper and spending less never do."""
    guarded = []
    if changed.get("order_mode") == OrderMode.LIVE:
        guarded.append("going live")
    if "registered_static_ip" in changed:
        guarded.append("changing the registered static IP")
    new_amount, old_amount = changed.get("daily_amount"), before.get("daily_amount")
    if new_amount is not None and (old_amount is None or new_amount > old_amount):
        guarded.append("raising the daily amount")
    return guarded


def _render(request: HttpRequest, form, current) -> HttpResponse:
    history = _history()

    return render(
        request,
        "configuration/index.html",
        {
            "form": form,
            "current": current,
            "history": history,
            "last_change": history[0] if history else None,
            "broker": _broker_facts(request),
            "holidays": _holidays(),
            "alerts": ALERTS,
            "is_live": current.order_mode == OrderMode.LIVE,
            "pause": active_pause(),
        },
    )


@require_http_methods(["POST"])
def stop(request: HttpRequest) -> HttpResponse:
    """The emergency stop: pause, and cancel everything live (M32a, and the
    half of a pause that a pause does not do).

    A reason is required for the same reason every other irreversible action
    here requires one: in six months the question will be why, and "stopped"
    on its own does not answer it.
    """
    from broker import get_broker, session
    from orders import halt

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Say why. It is stored with the pause and cannot be edited after.")
        return redirect("configuration:index")

    broker = get_broker()
    session.attach(broker)
    result = halt.stop_trading(actor=request.user.get_username(), reason=reason, broker=broker)

    # An order that could not be cancelled is the one thing the operator has
    # to act on, so it is an error rather than a footnote on a success. A
    # missing Kite session shows up here too, as the live orders it could not
    # reach -- there is no separate warning for it, because "no session" with
    # nothing out there is not a problem.
    if result.clean:
        messages.success(request, result.summary)
    else:
        messages.error(request, result.summary)

    return redirect("configuration:index")


@require_http_methods(["POST"])
def resume(request: HttpRequest) -> HttpResponse:
    """Coming back is its own decision, which is what makes stopping safe."""
    from configuration.pause import PauseError
    from configuration.pause import resume as resume_system

    try:
        resume_system(actor=request.user.get_username(), note=request.POST.get("note", ""))
    except PauseError as refusal:
        messages.error(request, str(refusal))
    else:
        messages.success(request, "Trading resumes from the next scheduled task.")

    return redirect("configuration:index")


def _broker_facts(request) -> dict:
    """The broker settings that live outside the database, shown not edited.

    The API key is the app's public identifier, but it is still masked: the
    page proves which app is configured, and that needs four characters, not
    all of them. The secret is never read here at all. The postback URL is the
    one to paste into the Kite developer console, built from the address this
    page was served on.
    """
    from django.conf import settings
    from django.urls import reverse

    key = settings.KITE_API_KEY or ""
    return {
        "api_key": f"{key[:4]}…{key[-2:]}" if len(key) > 8 else ("set" if key else ""),
        "postback_url": request.build_absolute_uri(reverse("orders:kite_postback")),
    }


def _history(limit: int = 12) -> list[dict]:
    """What changed, when, and from what.

    Read by diffing consecutive versions rather than from the audit trail: the
    versions are the record, and a screen that agrees with them cannot drift
    from them.
    """
    versions = list(ConfigurationVersion.objects.order_by("-version")[: limit + 1])
    rows = []
    for newer, older in pairwise(versions):
        for field in sorted(MUTABLE_FIELDS):
            before, after = getattr(older, field), getattr(newer, field)
            if before != after:
                rows.append(
                    {
                        "version": newer.version,
                        "effective_from": newer.effective_from,
                        "actor": newer.changed_by,
                        "reason": newer.reason,
                        "setting": field.replace("_", " "),
                        "before": before,
                        "after": after,
                    }
                )
    return rows


def _holidays(limit: int = 8) -> list:
    """The closures ahead, so a quiet day is explained before it arrives."""
    from audit.context import today_ist
    from trading_calendar.models import SessionDay, SessionKind

    return list(
        SessionDay.objects.filter(trading_date__gte=today_ist(), kind=SessionKind.CLOSED).order_by(
            "trading_date"
        )[:limit]
    )
