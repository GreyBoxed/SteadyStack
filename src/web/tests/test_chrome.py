"""The shell every authenticated page wears.

Three questions the operator should never have to ask: can the system reach
Kite, is it allowed to place an order, and which day is this. They are in the
chrome so a page cannot render without them -- a dashboard that forgets to say
the session is dead is a dashboard that lies by omission.
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_the_sidebar_names_the_five_destinations(signed_in):
    """What gets used daily or weekly. Everything rarer hangs off the page it
    belongs to -- a list of sixteen is a list nobody reads."""
    body = signed_in.get(reverse("web:home")).content

    for name in (
        "web:home",
        "web:holdings",
        "web:performance",
        "web:cash_ledger",
        "configuration:index",
    ):
        assert reverse(name).encode() in body


def test_no_page_is_orphaned_by_the_shell(signed_in):
    """Every screen is reachable from the sidebar, or from the sub-nav of the
    section it belongs to. A page nobody can navigate to is a page that gets
    forgotten and then rots.
    """
    from web.chrome import NAV

    named = {section["url"] for section in NAV}
    for section in NAV:
        named |= {name for name, _ in section["children"]}
        named |= set(section["extra"])

    # Everything with its own screen must be claimed by exactly one section.
    for page in (
        "web:home",
        "web:orders",
        "web:instrument_day",
        "engine:index",
        "engine:detail",
        "web:holdings",
        "portfolio:index",
        "portfolio:detail",
        "records:divergences",
        "records:basis",
        "records:corporate_action",
        "rebalance:index",
        "web:performance",
        "web:cash_ledger",
        "web:share_ledger",
        "configuration:index",
        "model_portfolio:index",
        "broker:session",
        "accounts:security",
    ):
        assert page in named, f"{page} belongs to no section and cannot be reached"


def test_a_section_shows_its_own_sub_nav(signed_in):
    """Standing on Holdings, the rarer records are one click away."""
    body = signed_in.get(reverse("web:holdings")).content

    for name in (
        "portfolio:index",
        "records:divergences",
        "records:basis",
        "records:corporate_action",
        "rebalance:index",
    ):
        assert reverse(name).encode() in body


def test_a_page_does_not_show_another_section_s_sub_nav(signed_in):
    """The sub-nav is the section you are in, not every link there is."""
    body = signed_in.get(reverse("web:performance")).content

    assert reverse("records:corporate_action").encode() not in body


def test_an_open_divergence_is_badged_where_it_cannot_be_missed(signed_in, instrument):
    """It withholds an instrument's figures, so it has to be visible without
    anybody going looking for it."""
    from records.models import Divergence

    Divergence.objects.create(
        instrument=instrument,
        detected_on="2026-09-22",
        expected_quantity=5,
        actual_quantity=4,
    )

    body = signed_in.get(reverse("web:performance")).content

    assert b"instrument(s) in doubt" in body


def test_the_current_page_is_marked_in_the_sidebar(signed_in):
    response = signed_in.get(reverse("web:holdings"))

    assert b'aria-current="page"' in response.content


def test_a_dead_session_is_said_so_on_every_page(signed_in):
    """There is no session in a test, and that is the state that costs a
    trading day -- so it has to be the one that shows."""
    for name in ("web:home", "web:holdings", "web:cash_ledger"):
        response = signed_in.get(reverse(name))

        assert b"NO KITE SESSION" in response.content


def test_a_valid_session_shows_as_valid(signed_in, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from broker.session import SessionStatus

    class Fake:
        expires_at = timezone.now() + timedelta(hours=6)
        kite_user_id = "AB1234"

    monkeypatch.setattr(
        "broker.session.status", lambda **_: SessionStatus(session=Fake(), valid=True)
    )

    response = signed_in.get(reverse("web:home"))

    assert b"KITE SESSION OK" in response.content


def test_the_mode_is_on_every_page(signed_in):
    """Whether an order can actually be sent is not a detail to go looking for."""
    response = signed_in.get(reverse("web:home"))

    assert b"PAPER" in response.content


def test_the_day_carries_its_trace_id(signed_in):
    from audit.context import today_ist, trace_id_for

    response = signed_in.get(reverse("web:home"))

    assert trace_id_for(today_ist()).encode() in response.content


def test_the_sign_in_page_says_nothing_about_the_portfolio(client):
    """Not even whether the day has traded. The chrome is computed only for a
    verified session, so an anonymous page cannot leak it by accident."""
    body = client.get(reverse("accounts:login")).content

    for leak in (b"KITE SESSION", b"PAPER", b"trading day", b"ts-20"):
        assert leak not in body


def test_an_unverified_session_gets_no_chrome_either(client, django_user_model):
    """Password alone is not enough to see the shell."""
    django_user_model.objects.create_user(username="operator", password="fake-password")
    client.login(username="operator", password="fake-password")

    body = client.get(reverse("web:home"), follow=True).content

    assert b"KITE SESSION" not in body


def test_every_page_loads_the_scroll_keeper(signed_in):
    body = signed_in.get(reverse("web:home")).content
    assert b"js/keep-scroll.js" in body


@pytest.mark.parametrize("name", ["web:performance", "web:orders", "portfolio:index"])
def test_tabs_that_reload_the_page_keep_the_scroll(signed_in, name):
    """A tab is a link to the same page with a new query; marked, it comes
    back where the reader was rather than at the top."""
    assert b"data-keep-scroll" in signed_in.get(reverse(name)).content
