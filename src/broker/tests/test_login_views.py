"""The login round trip.

The callback is the one endpoint outside the OTP gate, so it is tested the way
an exposed endpoint should be: with nothing, with junk, with a failure from
Kite, and only then with a real handshake.
"""

import pytest
from cryptography.fernet import Fernet
from django.urls import reverse

from broker.exceptions import BrokerError
from broker.models import KiteSession

FAKE_TOKEN = "fake-access-token-value"
TEST_KEY = Fernet.generate_key().decode()

pytestmark = pytest.mark.django_db


class StubBroker:
    def __init__(self, raises=None):
        self.raises = raises
        self.exchanged = []

    def login_url(self):
        return "https://kite.zerodha.com/connect/login?v=3&api_key=fake"

    def create_session(self, request_token):
        self.exchanged.append(request_token)
        if self.raises:
            raise self.raises
        return {"access_token": FAKE_TOKEN, "user_id": "AB1234", "user_name": "Operator"}

    def set_access_token(self, token):
        pass


@pytest.fixture(autouse=True)
def encryption_key(settings):
    settings.KITE_TOKEN_KEY = TEST_KEY


@pytest.fixture(autouse=True)
def market_shut(monkeypatch):
    """A login plans the day when the market is open (below). Every other test
    here is about the login alone, so for them the market is shut."""
    monkeypatch.setattr(
        "engine.planning.market_closed_reason", lambda day, now=None: "shut for the test"
    )


@pytest.fixture
def broker(monkeypatch):
    stub = StubBroker()
    monkeypatch.setattr("broker.views.get_broker", lambda **_: stub)
    return stub


@pytest.fixture
def failing_broker(monkeypatch):
    stub = StubBroker(raises=BrokerError("Kite said no"))
    monkeypatch.setattr("broker.views.get_broker", lambda **_: stub)
    return stub


@pytest.fixture
def operator(client, django_user_model):
    """A signed-in, OTP-verified operator.

    Everything but the callback sits behind the gate, so most of these tests
    need one.
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    user = django_user_model.objects.create_user(
        username="operator", password="fake-password-for-tests"
    )
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.login(username="operator", password="fake-password-for-tests")
    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()
    return user


def callback(client, **params):
    """Arrive back from Kite, carrying a state this system issued unless the
    test says otherwise -- which is what a real round trip carries."""
    from broker import session

    if "state" not in params:
        params["state"] = session.mint_login_state(started_by="operator")
    elif params["state"] is None:
        del params["state"]
    return client.get(reverse("broker:callback"), params)


# -- starting --------------------------------------------------------------


def test_starting_the_login_redirects_to_kite(client, broker, operator):
    response = client.post(reverse("broker:start_login"))

    assert response.status_code == 302
    assert "kite.zerodha.com" in response["Location"]


def test_the_login_cannot_be_started_by_a_link(client, broker, operator):
    """A GET would let any page the operator visits begin a handshake."""
    assert client.get(reverse("broker:start_login")).status_code == 405


def test_starting_the_login_needs_a_verified_operator(client, broker):
    """Only the callback is outside the OTP gate."""
    assert client.post(reverse("broker:start_login")).status_code == 302
    assert broker.exchanged == []


# -- the callback ----------------------------------------------------------


def test_the_callback_is_reachable_without_signing_in(client, broker):
    """It has to be: completing the handshake needs a session the handshake is
    what establishes. `accounts.middleware` exempts exactly this path."""
    response = callback(client, request_token="fake-request-token")
    assert response.status_code == 302


def test_a_request_token_establishes_a_session(client, broker):
    callback(client, request_token="fake-request-token")

    assert KiteSession.objects.count() == 1
    assert broker.exchanged == ["fake-request-token"]


def test_a_callback_without_a_token_establishes_nothing(client, broker):
    response = callback(client)

    assert KiteSession.objects.count() == 0
    assert response.status_code == 302


def test_a_failed_login_from_kite_establishes_nothing(client, broker):
    callback(client, status="failure", request_token="fake-request-token")

    assert KiteSession.objects.count() == 0
    assert broker.exchanged == []


def test_a_broker_error_is_reported_without_leaking_the_attempt(client, failing_broker):
    response = callback(client, request_token="fake-request-token", follow=False)

    assert KiteSession.objects.count() == 0
    assert response.status_code == 302


def test_the_token_never_appears_in_the_response(client, broker):
    response = callback(client, request_token="fake-request-token", follow=True)
    assert FAKE_TOKEN.encode() not in response.content


def test_the_home_page_shows_the_session_once_established(client, broker, operator):
    callback(client, request_token="fake-request-token")
    response = client.get("/")

    assert b"AB1234" in response.content
    assert FAKE_TOKEN.encode() not in response.content


# -- the state Kite hands back ----------------------------------------------


def test_starting_the_login_sends_a_state_for_kite_to_return(client, broker, operator):
    from urllib.parse import parse_qs, urlsplit

    from broker import session

    response = client.post(reverse("broker:start_login"))

    query = parse_qs(urlsplit(response["Location"]).query)
    (params,) = query["redirect_params"]
    state = parse_qs(params)["state"][0]
    assert session.consume_login_state(state) is True


def test_a_callback_without_a_state_establishes_nothing(client, broker):
    """A request token on its own could be anybody's: one minted by signing
    in to Zerodha as somebody else, then handed to the operator as a link."""
    callback(client, request_token="fake-request-token", state=None)

    assert KiteSession.objects.count() == 0
    assert broker.exchanged == []


def test_a_callback_with_a_state_this_system_never_issued_establishes_nothing(client, broker):
    callback(client, request_token="fake-request-token", state="made-up")

    assert KiteSession.objects.count() == 0
    assert broker.exchanged == []


def test_a_state_is_good_for_one_callback(client, broker):
    from broker import session

    state = session.mint_login_state(started_by="operator")
    callback(client, request_token="first", state=state)
    callback(client, request_token="second", state=state)

    assert broker.exchanged == ["first"]


def test_a_refused_state_is_audited(client, broker):
    from audit.models import AuditEvent

    callback(client, request_token="fake-request-token", state="made-up")

    assert AuditEvent.objects.filter(action="kite_session.login_refused").count() == 1


def test_a_login_as_another_account_is_refused(client, broker, settings):
    settings.KITE_USER_ID = "ZZ9999"

    response = callback(client, request_token="fake-request-token")

    assert KiteSession.objects.count() == 0
    said = " ".join(str(m) for m in response.wsgi_request._messages)
    assert "AB1234" in said
    assert "ZZ9999" in said


def test_the_phone_starts_the_same_login(client, broker, operator):
    """The phone's Connect button posts to the same view (test_phone checks the
    form), so a phone's login carries its state the same way."""
    response = client.post(
        reverse("broker:start_login"),
        HTTP_USER_AGENT="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile",
    )

    assert "redirect_params=state" in response["Location"]


# -- a late login resumes the day ---------------------------------------------


@pytest.fixture
def market(monkeypatch):
    """Today a trading day with the market open, unless a test says not."""
    state = {"trading_day": True, "closed": None}
    monkeypatch.setattr(
        "trading_calendar.sessions.is_trading_day", lambda day: state["trading_day"]
    )
    monkeypatch.setattr(
        "engine.planning.market_closed_reason", lambda day, now=None: state["closed"]
    )
    return state


@pytest.fixture
def planner(monkeypatch):
    planned = []

    def plan(**kwargs):
        planned.append(kwargs)

        class Sheet:
            version = 1
            names_label = "INFY"

        return Sheet()

    monkeypatch.setattr("engine.planning.plan_from_broker", plan)
    return planned


def test_a_login_while_the_market_is_open_plans_the_waiting_day(client, broker, market, planner):
    """The 09:45 plan cannot read holdings without a session, so a day whose
    operator signs in at 10:30 has no sheet. Signing in is when it can have one."""
    callback(client, request_token="fake-request-token")

    assert len(planner) == 1


def test_a_login_on_a_day_that_already_has_a_sheet_plans_nothing(client, broker, market, planner):
    from audit.context import today_ist
    from engine.models import TradeSheet

    today = today_ist()
    TradeSheet.objects.create(trading_date=today, version=1, trace_id=f"ts-{today:%Y-%m-%d}")

    callback(client, request_token="fake-request-token")

    assert planner == []


def test_a_login_outside_market_hours_plans_nothing(client, broker, market, planner):
    market["closed"] = "the market closed at 15:30"

    callback(client, request_token="fake-request-token")

    assert planner == []


def test_a_login_on_a_holiday_plans_nothing(client, broker, market, planner):
    market["trading_day"] = False

    callback(client, request_token="fake-request-token")

    assert planner == []


def test_a_plan_that_fails_does_not_undo_the_login(client, broker, market, monkeypatch):
    from engine.planning import PlanningFailed

    def refuse(**_kwargs):
        raise PlanningFailed("Could not read prices")

    monkeypatch.setattr("engine.planning.plan_from_broker", refuse)

    response = callback(client, request_token="fake-request-token")

    assert response.status_code == 302
    assert KiteSession.objects.count() == 1


def test_an_unexpected_failure_while_planning_does_not_undo_the_login(
    client, broker, market, monkeypatch
):
    def explode(**_kwargs):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr("engine.planning.plan_from_broker", explode)

    response = callback(client, request_token="fake-request-token")

    assert response.status_code == 302
    assert KiteSession.objects.count() == 1


def test_the_home_page_offers_a_login_when_there_is_no_session(client, operator):
    response = client.get("/")

    assert b"No usable Kite session" in response.content
    assert b"Sign in to Kite" in response.content


# -- the session screen --------------------------------------------------


@pytest.mark.django_db
def test_the_session_page_is_not_reachable_anonymously(client):
    from django.urls import reverse

    assert client.get(reverse("broker:session")).status_code == 302


@pytest.mark.django_db
def test_the_session_page_names_the_boundary(signed_in):
    """The page exists to make one thing obvious: secrets are typed at
    Zerodha, never here. Somebody who reads it will not be phished by a page
    that looks like this one."""
    from django.urls import reverse

    body = signed_in.get(reverse("broker:session")).content

    assert b"never on this one" in body
    assert b"the only place secrets are typed" in body


@pytest.mark.django_db
def test_without_a_session_it_says_what_is_blocked(signed_in):
    from django.urls import reverse

    body = signed_in.get(reverse("broker:session")).content

    assert b"BLOCKED" in body
    assert b"UNAFFECTED" in body


@pytest.mark.django_db
def test_the_token_itself_is_never_rendered(signed_in):
    """Only that one exists and when it dies."""
    from django.urls import reverse

    body = signed_in.get(reverse("broker:session")).content

    assert b"never logged, never rendered" in body
    assert b"access_token" not in body
