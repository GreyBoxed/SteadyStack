"""What every page wears: the sidebar, the header strip and their state.

The chrome answers three questions the operator has on every screen without
asking: can the system reach Kite, is it allowed to place an order, and which
day is this. Putting it in a context processor rather than in each view means
a page cannot accidentally render without them -- a dashboard that forgets to
say the session is dead is a dashboard that lies by omission.

It is computed only for a verified session. Anonymous requests get nothing:
the sign-in page must say nothing about the portfolio, not even whether the
day has traded.
"""

import logging
from dataclasses import dataclass

from django.urls import reverse

logger = logging.getLogger("steadystack.web")

#: Five destinations, and what lives under each.
#:
#: The sidebar carries what gets used daily or weekly; everything rarer hangs
#: off the page it belongs to. A list of sixteen is a list nobody reads, and
#: the rare entries were the ones crowding out the ones that matter.
#:
#: `extra` names views that belong to a section without appearing in its
#: sub-nav -- detail pages, mostly -- so the shell still knows where you are.
NAV = [
    {
        "url": "web:home",
        "label": "Today",
        "children": [
            ("web:home", "Overview"),
            ("web:orders", "Today's orders"),
            ("engine:index", "Tradesheets"),
        ],
        "extra": ["engine:detail", "web:instrument_day"],
    },
    {
        "url": "web:holdings",
        "label": "Holdings",
        "children": [
            ("web:holdings", "Holdings"),
            ("portfolio:index", "Instruments"),
            ("records:divergences", "Divergences"),
            ("records:basis", "Contract notes"),
            ("records:corporate_action", "Corporate actions"),
            ("rebalance:index", "Rebalance sells"),
        ],
        "extra": ["portfolio:detail", "records:classify", "rebalance:detail"],
    },
    {"url": "web:performance", "label": "Performance", "children": [], "extra": []},
    {
        "url": "web:cash_ledger",
        "label": "Ledger",
        # The design has one ledger board with a cash/shares toggle. These two
        # links are that toggle.
        "children": [("web:cash_ledger", "Cash"), ("web:share_ledger", "Shares")],
        "extra": [],
    },
    {
        "url": "journal:index",
        "label": "Books",
        "children": [
            ("journal:index", "Trial balance"),
            ("journal:entries", "Journal"),
            ("journal:statement", "Statement"),
            ("journal:dividend", "Dividend"),
        ],
        "extra": ["journal:ledger", "journal:entry", "journal:confirm", "journal:correct"],
    },
    {
        "url": "configuration:index",
        "label": "Settings",
        "children": [
            ("configuration:index", "Configuration"),
            ("model_portfolio:index", "Model portfolio"),
            ("broker:session", "Kite session"),
            ("accounts:security", "Security"),
        ],
        "extra": ["model_portfolio:upload", "model_portfolio:confirm"],
    },
]


def _section_for(view_name: str) -> dict | None:
    for section in NAV:
        names = {section["url"], *(name for name, _ in section["children"])}
        if view_name in names or view_name in section["extra"]:
            return section
    return None


@dataclass(frozen=True)
class MarketNow:
    """What the exchange is doing, in a form a template can print."""

    label: str
    is_open: bool


def _market_now(day) -> MarketNow:
    from django.utils import timezone

    from trading_calendar.sessions import session_for

    session = session_for(day)
    if not session.is_open:
        return MarketNow(label="NSE CLOSED", is_open=False)

    now = timezone.localtime().time()
    if session.covers(now):
        return MarketNow(label=f"NSE OPEN · closes {session.closes_at:%H:%M}", is_open=True)
    if session.opens_at and now < session.opens_at:
        return MarketNow(label=f"NSE PRE-OPEN · opens {session.opens_at:%H:%M}", is_open=False)
    return MarketNow(label="NSE CLOSED · session over", is_open=False)


def _trading_day_number(day) -> int | None:
    """How many days this system has planned, itself included.

    Counted from the tradesheets rather than the calendar, because the number
    an operator wants is how long *this* has been running, not how long the
    exchange has.
    """
    from engine.models import TradeSheet

    count = (
        TradeSheet.objects.filter(trading_date__lte=day).values("trading_date").distinct().count()
    )
    return count or None


def asset_version(request) -> dict:
    """A stamp for the stylesheet's URL that changes whenever the CSS is rebuilt.

    The built CSS keeps one filename, so a browser that cached it keeps the old
    styles after a rebuild -- a date picker's icon stays invisible, a new table
    renders unstyled. The file's modification time, read per request (a stat,
    nothing more), makes each build a new URL. Every page wears it, signed in
    or not.
    """
    from pathlib import Path

    from django.conf import settings

    for root in getattr(settings, "STATICFILES_DIRS", []):
        css = Path(root) / "css" / "app.css"
        if css.exists():
            return {"asset_version": int(css.stat().st_mtime)}
    return {"asset_version": ""}


def chrome(request) -> dict:
    """The sidebar and header strip, for every authenticated page."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    # OTPMiddleware puts is_verified() on the user. If it is absent the
    # middleware did not run, and an unverified page is not one to decorate.
    is_verified = getattr(user, "is_verified", None)
    if is_verified is None or not is_verified():
        return {}

    from audit.context import today_ist, trace_id_for
    from broker import session as kite_session
    from broker.mode import configured_mode

    today = today_ist()
    current = getattr(request, "resolver_match", None)
    active = current.view_name if current else ""
    current_section = _section_for(active)

    # A divergence withholds an instrument's figures, so it must be visible
    # without going looking for it.
    from records.divergence import open_divergences

    in_doubt = open_divergences().count()

    return {
        "chrome": {
            "nav": [
                {
                    "url": reverse(section["url"]),
                    "label": section["label"],
                    "active": section is current_section,
                    "badge": in_doubt if section["label"] == "Holdings" and in_doubt else 0,
                }
                for section in NAV
            ],
            "subnav": [
                {"url": reverse(name), "label": label, "active": name == active}
                for name, label in (current_section["children"] if current_section else [])
            ],
            "session": kite_session.status(),
            "mode": configured_mode(),
            "today": today,
            "trace_id": trace_id_for(today),
            "market": _market_now(today),
            "day_number": _trading_day_number(today),
        }
    }
