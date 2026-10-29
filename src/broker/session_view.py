"""The Kite session screen.

One boundary is worth a whole page: the Zerodha password and TOTP are typed on
Zerodha's page, never on ours, and the request_token that comes back is
worthless without the API secret. Somebody who understands that will not be
phished by a page that looks like this one.
"""

import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

logger = logging.getLogger("steadystack.broker")

FLOW = [
    {
        "where": "THIS APP",
        "title": "You press Connect",
        "text": "The app builds the Kite login URL from the API key and sends your browser there.",
        "tag": "no credentials involved",
        "at_zerodha": False,
        "good": False,
    },
    {
        "where": "ZERODHA",
        "title": "You log in to Kite",
        "text": (
            "Zerodha's own page takes your client ID, password and TOTP. "
            "This app never sees any of the three."
        ),
        "tag": "the only place secrets are typed",
        "at_zerodha": True,
        "good": True,
    },
    {
        "where": "THIS APP",
        "title": "request_token returns",
        "text": (
            "Zerodha redirects back with a short-lived request_token in the URL. "
            "It is worthless on its own."
        ),
        "tag": "single use, expires in minutes",
        "at_zerodha": False,
        "good": False,
    },
    {
        "where": "THIS APP",
        "title": "Exchanged for an access token",
        "text": "Combined with the API secret server side, then encrypted and stored.",
        "tag": "never logged, never rendered",
        "at_zerodha": False,
        "good": True,
    },
]

NEEDS = [
    ("Placing and cancelling orders", "the whole ladder, and the 15:00 fallback", True),
    ("Tradesheet generation", "needs a live price to size against the ask", True),
    ("Tick feed and depth", "the websocket authenticates with the token", True),
    ("Holdings reconciliation", "compares the broker against the ledger", True),
    ("Ledger, day detail, performance", "read from Postgres, not from Kite", False),
    ("Overrides and configuration", "edits stored state, takes effect on resume", False),
]


def _morning_check() -> dict:
    """Whether a usable token existed at 09:00 today, read from the sessions table.

    The 09:00 check is what decides whether the day runs or parks. It writes
    no row of its own, so the answer is read from the sessions it would have
    found: one issued before nine, unexpired at nine, and not revoked by then.
    """
    from datetime import datetime, time

    from django.db.models import Q
    from django.utils import timezone

    from audit.context import today_ist
    from broker.models import KiteSession

    nine = timezone.make_aware(datetime.combine(today_ist(), time(9, 0)))
    if timezone.now() < nine:
        return {"state": "later", "at": nine}

    found = (
        KiteSession.objects.filter(created_at__lte=nine, expires_at__gt=nine)
        .filter(Q(revoked_at__isnull=True) | Q(revoked_at__gt=nine))
        .exists()
    )
    return {"state": "passed" if found else "failed", "at": nine}


@require_GET
def session(request: HttpRequest) -> HttpResponse:
    from broker import session as kite_session
    from broker.mode import configured_mode
    from broker.models import KiteSession
    from configuration.store import current as current_configuration

    status = kite_session.status()
    needs = [
        {
            "label": label,
            "sub": sub,
            "status": ("RUNNING" if status.valid else "BLOCKED") if needs_session else "UNAFFECTED",
        }
        for label, sub, needs_session in NEEDS
    ]

    return render(
        request,
        "broker/session.html",
        {
            "status": status,
            "mode": configured_mode(),
            "static_ip": current_configuration().registered_static_ip,
            "flow": FLOW,
            "needs": needs,
            "blocked": sum(1 for need in needs if need["status"] == "BLOCKED"),
            "morning": _morning_check(),
            "history": [
                {
                    "issued_at": row.created_at,
                    "expires_at": row.expires_at,
                    "actor": row.kite_user_id,
                    # In use only if it is the session in force *and* it works.
                    "live": status.valid and row.pk == status.session.pk,
                    "revoked": row.revoked_reason if row.revoked_at else "",
                }
                for row in KiteSession.objects.order_by("-created_at")[:10]
            ],
        },
    )
