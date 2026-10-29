"""The Kite client: what it records, what it retries, and what it refuses.

No network is touched. A stand-in KiteConnect is injected, because what is
under test is the wrapper -- persistence, translation, retry policy and the
placement gate -- and not Zerodha's implementation of HTTP.

The three properties worth stating plainly:

  * every call leaves a `BrokerCall` row, failures included (M44);
  * a read may be retried, a placement never is -- a timed-out placement may
    have reached the exchange, and retrying is how a day buys twice;
  * the access token never reaches the table, on the one call that returns it.
"""

from decimal import Decimal

import pytest
import requests
from kiteconnect import exceptions as kite_exceptions

from audit.models import BrokerCall
from broker.exceptions import (
    BrokerInputError,
    BrokerUnavailable,
    LiveOrdersRefused,
    OrderRejected,
    SessionExpired,
    TransientBrokerError,
    UnknownBrokerError,
)
from broker.kite import KiteBroker, translate
from configuration.models import OrderMode
from configuration.store import update

REGISTERED = "203.0.113.7"


class FakeKiteConnect:
    """Answers the calls KiteBroker makes, and counts them."""

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.calls: list[str] = []
        self.place_order_kwargs: list[dict] = []

    def _answer(self, name: str):
        self.calls.append(name)
        answer = self.answers.get(name, {})
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer()
        return answer

    def login_url(self):
        return "https://kite.zerodha.com/connect/login?v=3&api_key=fake"

    def generate_session(self, request_token, api_secret):
        return self._answer("generate_session")

    def set_access_token(self, access_token):
        self.access_token = access_token

    def profile(self):
        return self._answer("profile")

    def instruments(self, exchange=None):
        return self._answer("instruments")

    def quote(self, *instruments):
        return self._answer("quote")

    def holdings(self):
        return self._answer("holdings")

    def positions(self):
        return self._answer("positions")

    def margins(self):
        return self._answer("margins")

    def orders(self):
        return self._answer("orders")

    def order_history(self, order_id):
        return self._answer("order_history")

    def trades(self):
        return self._answer("trades")

    def place_order(self, **kwargs):
        self.place_order_kwargs.append(kwargs)
        return self._answer("place_order")

    def cancel_order(self, variety, order_id):
        return self._answer("cancel_order")


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Retry backoff must not make the suite slow."""
    monkeypatch.setattr("broker.kite.time.sleep", lambda _seconds: None)


@pytest.fixture
def outbound(monkeypatch):
    def _set(value):
        monkeypatch.setattr("broker.network.outbound_ip", lambda **_: value)

    _set(None)
    return _set


def broker_with(**answers) -> tuple[KiteBroker, FakeKiteConnect]:
    client = FakeKiteConnect(**answers)
    return KiteBroker(api_key="fake-key", api_secret="fake-secret", client=client), client


# -- persistence ----------------------------------------------------------


@pytest.mark.django_db
def test_a_successful_call_is_persisted():
    broker, _ = broker_with(profile={"user_id": "AB1234"})

    broker.profile()

    call = BrokerCall.objects.get()
    assert call.endpoint == "user.profile"
    assert call.response == {"user_id": "AB1234"}
    assert call.error == ""


@pytest.mark.django_db
def test_a_failed_call_is_persisted_too():
    """The failures are the rows somebody will come looking for."""
    broker, _ = broker_with(profile=kite_exceptions.DataException("upstream is down"))

    with pytest.raises(BrokerUnavailable):
        broker.profile()

    call = BrokerCall.objects.latest("occurred_at")
    assert "upstream is down" in call.error
    assert call.response is None


@pytest.mark.django_db
def test_latency_is_recorded():
    broker, _ = broker_with(profile={"user_id": "AB1234"})
    broker.profile()
    assert BrokerCall.objects.get().latency_ms >= 0


@pytest.mark.django_db
def test_the_trace_id_is_stamped_on_the_call():
    from datetime import date

    from audit.context import bind_trading_day, reset_trace_id

    token = bind_trading_day(date(2026, 9, 22))
    try:
        broker, _ = broker_with(profile={"user_id": "AB1234"})
        broker.profile()
    finally:
        reset_trace_id(token)

    assert BrokerCall.objects.get().trace_id == "ts-2026-09-22"


@pytest.mark.django_db
def test_the_access_token_never_reaches_the_table():
    """generate_session returns the token in its response body. This is the
    call redaction exists for."""
    broker, _ = broker_with(
        generate_session={
            "user_id": "AB1234",
            "access_token": "fake-token-that-must-not-be-stored",
            "public_token": "fake-public-token",
        }
    )

    broker.create_session("fake-request-token")

    stored = str(BrokerCall.objects.get().response)
    assert "fake-token-that-must-not-be-stored" not in stored
    assert "[REDACTED]" in stored


@pytest.mark.django_db
def test_the_request_token_is_not_stored_either():
    broker, _ = broker_with(generate_session={"access_token": "fake-token"})

    broker.create_session("fake-request-token")

    assert "fake-request-token" not in str(BrokerCall.objects.get().request)


@pytest.mark.django_db
def test_a_response_with_datetimes_is_still_storable():
    """Kite parses its own date fields; JSON cannot encode them, and dropping
    the response would defeat the table."""
    from datetime import datetime

    broker, _ = broker_with(
        orders=[{"order_id": "1", "order_timestamp": datetime(2026, 9, 22, 9, 45)}]
    )

    broker.orders()

    assert "2026-09-22" in str(BrokerCall.objects.get().response)


# -- retry policy ----------------------------------------------------------


@pytest.mark.django_db
def test_a_read_is_retried_after_a_transient_failure():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise kite_exceptions.NetworkException("connection reset")
        return {"user_id": "AB1234"}

    broker, _ = broker_with(profile=flaky)

    assert broker.profile() == {"user_id": "AB1234"}
    assert attempts["n"] == 2


@pytest.mark.django_db
def test_a_read_gives_up_after_its_budget():
    broker, client = broker_with(profile=kite_exceptions.NetworkException("still down"))

    with pytest.raises(TransientBrokerError):
        broker.profile()

    assert client.calls.count("profile") == 3  # one attempt plus two retries


@pytest.mark.django_db
def test_a_read_is_not_retried_when_the_failure_is_permanent():
    """Retrying a malformed request just sends it again."""
    broker, client = broker_with(profile=kite_exceptions.InputException("bad symbol"))

    with pytest.raises(BrokerInputError):
        broker.profile()

    assert client.calls.count("profile") == 1


@pytest.mark.django_db
def test_every_attempt_is_persisted():
    broker, _ = broker_with(profile=kite_exceptions.NetworkException("still down"))

    with pytest.raises(TransientBrokerError):
        broker.profile()

    assert BrokerCall.objects.count() == 3


@pytest.mark.django_db
def test_a_placement_is_never_retried(live_book_open, outbound):
    """A timed-out placement may have reached the exchange (D8)."""
    outbound(REGISTERED)
    update(changed_by="operator", order_mode=OrderMode.LIVE, registered_static_ip=REGISTERED)
    broker, client = broker_with(place_order=kite_exceptions.NetworkException("timeout"))

    with pytest.raises(TransientBrokerError):
        broker.place_order(
            tradingsymbol="INFY",
            exchange="NSE",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=Decimal("1650.50"),
        )

    assert len(client.place_order_kwargs) == 1


# -- the gate --------------------------------------------------------------


@pytest.mark.django_db
def test_placement_is_refused_in_paper_mode_before_any_call(outbound):
    broker, client = broker_with(place_order="251122000123456")

    with pytest.raises(LiveOrdersRefused):
        broker.place_order(
            tradingsymbol="INFY",
            exchange="NSE",
            transaction_type="BUY",
            quantity=1,
            order_type="MARKET",
        )

    assert client.place_order_kwargs == []
    assert BrokerCall.objects.count() == 0


@pytest.mark.django_db
def test_a_permitted_placement_sends_delivery_and_day_validity(live_book_open, outbound):
    outbound(REGISTERED)
    update(changed_by="operator", order_mode=OrderMode.LIVE, registered_static_ip=REGISTERED)
    broker, client = broker_with(place_order="251122000123456")

    order_id = broker.place_order(
        tradingsymbol="INFY",
        exchange="NSE",
        transaction_type="BUY",
        quantity=1,
        order_type="LIMIT",
        price=Decimal("1650.50"),
        tag="ts-2026-09-22-t3",
    )

    assert order_id == "251122000123456"
    sent = client.place_order_kwargs[0]
    assert sent["product"] == "CNC"  # delivery, never intraday
    assert sent["validity"] == "DAY"
    assert sent["variety"] == "regular"
    assert sent["price"] == 1650.5


@pytest.mark.django_db
def test_a_market_order_carries_its_protection(live_book_open, outbound):
    """Since 1 April 2026 the exchange rejects a market order without one."""
    outbound(REGISTERED)
    update(changed_by="operator", order_mode=OrderMode.LIVE, registered_static_ip=REGISTERED)
    broker, client = broker_with(place_order="251122000123457")

    broker.place_order(
        tradingsymbol="INFY",
        exchange="NSE",
        transaction_type="BUY",
        quantity=2,
        order_type="MARKET",
        tag="ts-2026-09-22-f",
        market_protection=Decimal("5.00"),
    )

    assert client.place_order_kwargs[0]["market_protection"] == 5.0


@pytest.mark.django_db
def test_a_market_order_without_protection_is_refused_before_any_call(live_book_open, outbound):
    outbound(REGISTERED)
    update(changed_by="operator", order_mode=OrderMode.LIVE, registered_static_ip=REGISTERED)
    broker, client = broker_with(place_order="251122000123458")

    with pytest.raises(ValueError, match="market_protection"):
        broker.place_order(
            tradingsymbol="INFY",
            exchange="NSE",
            transaction_type="BUY",
            quantity=2,
            order_type="MARKET",
        )

    assert client.place_order_kwargs == []
    assert not BrokerCall.objects.exists()


@pytest.mark.django_db
def test_there_is_no_way_to_modify_an_order():
    """Orders are never modified (M21). An interface that cannot express a
    reprice cannot grow one back."""
    from broker.base import Broker

    assert not hasattr(Broker, "modify_order")
    assert not hasattr(KiteBroker, "modify_order")


# -- translation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (kite_exceptions.TokenException("token expired"), SessionExpired),
        (kite_exceptions.InputException("bad input"), BrokerInputError),
        (kite_exceptions.PermissionException("not permitted"), BrokerInputError),
        (kite_exceptions.OrderException("rejected"), OrderRejected),
        (kite_exceptions.NetworkException("reset"), TransientBrokerError),
        (kite_exceptions.DataException("no data"), BrokerUnavailable),
        (kite_exceptions.GeneralException("who knows"), UnknownBrokerError),
        (TimeoutError("slow"), TransientBrokerError),
        (ConnectionError("refused"), TransientBrokerError),
        # What the Kite client actually raises on a slow or dropped socket:
        # requests' own classes, which are not the builtins above.
        (requests.exceptions.ReadTimeout("slow"), TransientBrokerError),
        (requests.exceptions.ConnectionError("reset"), TransientBrokerError),
        (RuntimeError("something else entirely"), UnknownBrokerError),
    ],
)
def test_failures_are_translated_into_the_taxonomy(raised, expected):
    assert isinstance(translate(raised, call="test"), expected)


def test_a_dead_session_is_never_confused_with_a_blip():
    """It has a human remedy and a deadline; a retry cannot fix it."""
    translated = translate(kite_exceptions.TokenException("expired"), call="user.profile")
    assert isinstance(translated, SessionExpired)
    assert not isinstance(translated, TransientBrokerError)


def test_the_translated_error_remembers_which_call_failed():
    assert translate(kite_exceptions.DataException("x"), call="market.quote").call == "market.quote"


# -- reads -----------------------------------------------------------------


@pytest.mark.django_db
def test_depth_is_projected_out_of_the_quote():
    book = {"buy": [{"price": 1650.0, "quantity": 10}], "sell": [{"price": 1650.5}]}
    broker, _ = broker_with(quote={"NSE:INFY": {"depth": book, "last_price": 1650.25}})

    assert broker.depth("NSE:INFY") == book


@pytest.mark.django_db
def test_depth_for_an_unknown_instrument_is_an_input_error():
    broker, _ = broker_with(quote={})

    with pytest.raises(BrokerInputError):
        broker.depth("NSE:NOSUCHTHING")


@pytest.mark.django_db
def test_reads_work_without_any_mode_configuration(outbound):
    """M19c: the static IP restricts placement, not reads. Every environment
    reads live market data, including a laptop."""
    broker, _ = broker_with(
        instruments=[{"tradingsymbol": "INFY", "tick_size": 0.05}],
        holdings=[{"tradingsymbol": "INFY", "quantity": 3}],
    )

    assert broker.instruments() == [{"tradingsymbol": "INFY", "tick_size": 0.05}]
    assert broker.holdings() == [{"tradingsymbol": "INFY", "quantity": 3}]


# -- what the live API actually returns ------------------------------------


def test_an_absent_access_token_reads_as_a_dead_session():
    """Observed against the live API: a quote without a usable token comes
    back as an InputException, not a TokenException. Classified as a malformed
    request it would never park the day or prompt a login (M7)."""
    raised = kite_exceptions.InputException("Invalid `api_key` or `access_token`.")
    assert isinstance(translate(raised, call="market.quote"), SessionExpired)


def test_an_expired_token_reads_as_a_dead_session_whatever_the_class():
    raised = kite_exceptions.GeneralException("access_token expired")
    assert isinstance(translate(raised, call="portfolio.holdings"), SessionExpired)


def test_an_ordinary_input_error_is_still_an_input_error():
    """The message check must not swallow real request errors."""
    raised = kite_exceptions.InputException("Invalid tradingsymbol NOSUCHCO")
    assert isinstance(translate(raised, call="order.place"), BrokerInputError)


def test_a_tick_size_rejection_is_not_mistaken_for_a_session_problem():
    raised = kite_exceptions.InputException("Price is not a multiple of tick size")
    assert isinstance(translate(raised, call="order.place"), BrokerInputError)


@pytest.mark.django_db
def test_a_dead_session_read_is_not_retried():
    """Retrying cannot fix it, and three attempts delay the alert."""
    broker, client = broker_with(
        holdings=kite_exceptions.InputException("Invalid `api_key` or `access_token`.")
    )

    with pytest.raises(SessionExpired):
        broker.holdings()

    assert client.calls.count("holdings") == 1
