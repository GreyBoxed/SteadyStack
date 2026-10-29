"""Properties that must hold across every page there is, and every page there
will be.

Enumerated from the URL conf rather than listed by hand: a screen added later
is covered the day it is added, which is the only way a rule like "nothing is
reachable unverified" stays true.
"""

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse

pytestmark = pytest.mark.django_db

#: Deliberately open, each for a stated reason.
OPEN_BY_DESIGN = {
    "health",  # a monitor cannot authenticate; it says nothing a stranger could use
    "accounts:login",
    "accounts:logout",
    "phone:login",  # the same door as accounts:login, laid out for a phone
    "phone:switch",  # sets a cookie choosing phone or desk pages, and nothing else
    "broker:callback",  # reached mid-handshake, before a session exists
    "orders:kite_postback",  # Zerodha calls it; verified by checksum, not by login
}

#: URL names whose view needs an argument we cannot invent here.
NEEDS_AN_OBJECT = {
    "engine:detail",
    "records:classify",
    "rebalance:detail",
    "rebalance:execute",
    "rebalance:cancel",
    "engine:override",
    "engine:regenerate",
    "engine:replan",
    "engine:authorize",
    "portfolio:detail",
    "web:instrument_day",
    "web:instrument_depth_live",
    "journal:ledger",
    "journal:entry",
    "journal:reverse",
    "journal:confirm",
    "journal:match",
    "journal:correct",
    "journal:explain",
}


def _named_urls() -> list[str]:
    found = []

    def walk(resolver, prefix=""):
        for entry in resolver.url_patterns:
            if isinstance(entry, URLResolver):
                namespace = entry.namespace
                walk(entry, f"{namespace}:" if namespace else prefix)
            elif isinstance(entry, URLPattern) and entry.name:
                found.append(f"{prefix}{entry.name}")

    walk(get_resolver())
    return [name for name in found if not name.startswith("admin")]


def test_there_is_no_admin_site():
    """No model of ours is registered, and the ones django-otp registers --
    the authenticator devices -- are exactly what must not have a second way
    in. A page nothing links to is surface and nothing else."""
    from django.urls import Resolver404, resolve

    with pytest.raises(Resolver404):
        resolve("/admin/")


PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile"


@pytest.mark.parametrize(("name", "agent"), [("accounts:login", ""), ("phone:login", PHONE)])
def test_the_sign_in_pages_are_never_cached(client, name, agent):
    """A shared browser's back button, or a proxy, must not replay a form that
    once held a username and a password."""
    response = client.get(reverse(name), HTTP_USER_AGENT=agent)

    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]


def test_the_security_page_is_never_cached(signed_in):
    """It shows recovery codes, once. Once means not from a cache either."""
    response = signed_in.get(reverse("accounts:security"))

    assert "no-store" in response["Cache-Control"]


def test_the_url_conf_is_actually_being_walked():
    """If this stops finding pages, every assertion below passes vacuously."""
    names = _named_urls()

    assert "web:home" in names
    assert len(names) > 15


@pytest.mark.parametrize("name", [n for n in _named_urls() if n not in NEEDS_AN_OBJECT])
def test_nothing_is_reachable_without_a_verified_session(client, name):
    """Password alone is not enough, and no session at all certainly is not.

    Nothing about the portfolio appears before sign-in -- not even whether the
    day has traded.
    """
    if name in OPEN_BY_DESIGN:
        pytest.skip("open by design, for a reason stated in OPEN_BY_DESIGN")

    response = client.get(reverse(name))

    assert response.status_code in (302, 403, 405), (
        f"{name} answered {response.status_code} to an anonymous request"
    )
    if response.status_code == 302:
        # Possibly by way of the device switch (web.device), which may send a
        # page to its phone or desk counterpart first: what matters is where
        # the chain ends, and that nothing on the way was a page.
        landed = client.get(reverse(name), follow=True).redirect_chain[-1][0]
        assert landed.startswith((reverse("accounts:login"), reverse("phone:login")))


def test_the_open_pages_say_nothing_about_the_portfolio(client):
    """The allowlist is small; what it may say is smaller."""
    import json

    body = json.loads(client.get(reverse("health")).content)

    assert all(isinstance(v, bool) for k, v in body.items() if k not in {"status", "checks"})
    assert all(isinstance(v, bool) for v in body["checks"].values())

    login = client.get(reverse("accounts:login")).content
    for leak in (b"KITE SESSION", b"trading day", b"ts-20", b"\xe2\x82\xb9"):
        assert leak not in login


MUTATING = [
    "records:classify",
    "rebalance:execute",
    "rebalance:cancel",
    "engine:override",
    "engine:regenerate",
    "engine:replan",
    "engine:generate",
    "engine:authorize",
    "configuration:index",
    "model_portfolio:upload",
    "model_portfolio:confirm",
    "journal:reverse",
    "journal:close_year",
    "journal:statement",
    "journal:confirm",
    "journal:match",
    "journal:correct",
    "journal:explain",
    "journal:dividend",
]


def test_every_mutating_endpoint_refuses_a_get(signed_in):
    """A state change reachable by GET is a state change a link can cause."""
    from engine.models import TradeSheet, TradeSheetState
    from rebalance.models import RebalanceSell
    from records.models import Divergence

    args = {
        "records:classify": lambda: [
            Divergence.objects.create(
                instrument_id=_an_instrument(),
                detected_on="2026-09-22",
                expected_quantity=2,
                actual_quantity=1,
            ).pk
        ],
        "rebalance:execute": lambda: [
            RebalanceSell.objects.create(uploaded_by="o", content_hash="a" * 64).pk
        ],
        "rebalance:cancel": lambda: [
            RebalanceSell.objects.create(uploaded_by="o", content_hash="b" * 64).pk
        ],
        "engine:override": lambda: [
            *_a_sheet(TradeSheet, TradeSheetState),
        ],
        "engine:regenerate": lambda: [
            *_a_sheet(TradeSheet, TradeSheetState),
        ],
        "engine:replan": lambda: [
            *_a_sheet(TradeSheet, TradeSheetState),
        ],
        "engine:authorize": lambda: [
            *_a_sheet(TradeSheet, TradeSheetState),
        ],
        "journal:reverse": lambda: [_a_journal_entry()],
        "journal:confirm": lambda: [_a_statement_line()],
        "journal:match": lambda: [_a_statement_line()],
        "journal:correct": lambda: [_a_statement_line()],
        "journal:explain": lambda: [_a_statement_line()],
    }

    for name in MUTATING:
        if name in (
            "configuration:index",
            "records:classify",
            "journal:statement",
            "journal:dividend",
        ):
            continue  # these render a form on GET, which is their job
        url = reverse(name, args=args[name]() if name in args else None)
        response = signed_in.get(url)

        assert response.status_code == 405, f"{name} accepted a GET"


def _an_instrument() -> str:
    from portfolio.models import Instrument

    instrument, _ = Instrument.objects.get_or_create(
        tradingsymbol="INFY", defaults={"instrument_token": 408065, "name": "Infosys"}
    )
    return instrument.pk


def _a_journal_entry():
    from journal import cash

    return cash.contribute(1000, on="2026-09-21", actor="o").pk


def _a_statement_line():
    import uuid

    from journal.models import StatementLine, StatementUpload

    unique = uuid.uuid4().hex
    upload = StatementUpload.objects.create(
        filename="a.csv",
        sha256=unique,
        raw="x",
        opening_balance=0,
        closing_balance=0,
        uploaded_by="o",
    )
    return StatementLine.objects.create(
        upload=upload,
        position=1,
        fingerprint=unique[::-1],
        posting_date="2026-09-21",
        particulars="a line",
        voucher_type="Journal Entry",
        debit=0,
        credit=0,
        net_balance=0,
    ).pk


def _a_sheet(TradeSheet, TradeSheetState):
    sheet, _ = TradeSheet.objects.get_or_create(
        trading_date="2026-09-22",
        version=1,
        defaults={"trace_id": "ts", "state": TradeSheetState.RUNNING},
    )
    return [sheet.trading_date, sheet.version]


def test_every_template_is_one_page_of_source():
    """A bulk edit once wrote a template out as thousands of repeated fragments.
    Every test that read only the view's context still passed. The size and
    the single `extends` are what a mangled template gives away first."""
    from pathlib import Path

    from django.conf import settings

    roots = [Path(d) for engine in settings.TEMPLATES for d in engine.get("DIRS", [])]
    templates = [path for root in roots for path in root.rglob("*.html")]

    assert templates, "no templates found; the check would pass vacuously"
    for path in templates:
        text = path.read_text()
        assert len(text) < 60_000, f"{path} is {len(text)} bytes"
        assert text.count("{% extends") <= 1, f"{path} extends more than once"


def test_no_template_comment_spans_lines():
    """`{# ... #}` is single-line only. Spread across lines, Django prints it
    as page text -- the settings page once showed its own design note."""
    from pathlib import Path

    from django.conf import settings

    roots = [Path(d) for engine in settings.TEMPLATES for d in engine.get("DIRS", [])]
    for path in (p for root in roots for p in root.rglob("*.html")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "{#" in line:
                assert "#}" in line, f"{path}:{number} opens a comment it does not close"
