"""The daily Kite session.

The property that carries the most weight: the access token is never readable
from the row that stores it. Everything else -- expiry, revocation, the status
the home page shows -- exists so that "can this system trade this morning?" is
answered before 09:45 rather than at it.
"""

from datetime import datetime, timedelta

import pytest
import time_machine
from cryptography.fernet import Fernet
from django.test import override_settings
from django.utils import timezone

from audit.models import AuditEvent
from broker import get_broker
from broker import session as kite_session
from broker.crypto import TokenEncryptionUnavailable, decrypt, encrypt
from broker.exceptions import BrokerError
from broker.models import KiteSession

FAKE_TOKEN = "fake-access-token-value"
TEST_KEY = Fernet.generate_key().decode()

pytestmark = pytest.mark.django_db


class StubBroker:
    """Answers the two calls the session flow makes."""

    def __init__(self, payload=None, raises=None):
        self.payload = (
            payload
            if payload is not None
            else {
                "access_token": FAKE_TOKEN,
                "user_id": "AB1234",
                "user_name": "Operator",
            }
        )
        self.raises = raises
        self.attached = []

    def login_url(self):
        return "https://kite.zerodha.com/connect/login?v=3&api_key=fake"

    def create_session(self, request_token):
        if self.raises:
            raise self.raises
        return self.payload

    def set_access_token(self, token):
        self.attached.append(token)


@pytest.fixture(autouse=True)
def encryption_key(settings):
    settings.KITE_TOKEN_KEY = TEST_KEY


# -- encryption ------------------------------------------------------------


def test_a_token_survives_a_round_trip():
    assert decrypt(encrypt(FAKE_TOKEN)) == FAKE_TOKEN


def test_the_ciphertext_does_not_contain_the_token():
    assert FAKE_TOKEN.encode() not in encrypt(FAKE_TOKEN)


def test_two_encryptions_differ():
    """Fernet is randomised, so the same token stored twice is not the same
    ciphertext -- a reader cannot tell that nothing changed."""
    assert encrypt(FAKE_TOKEN) != encrypt(FAKE_TOKEN)


@override_settings(KITE_TOKEN_KEY="")
def test_a_missing_key_refuses_rather_than_storing_plaintext():
    """A system that silently stops encrypting is worse than one that will
    not start: nobody finds out until the wrong person reads a backup."""
    with pytest.raises(TokenEncryptionUnavailable, match="KITE_TOKEN_KEY"):
        encrypt(FAKE_TOKEN)


@override_settings(KITE_TOKEN_KEY="not-a-real-fernet-key")
def test_a_malformed_key_is_refused():
    with pytest.raises(TokenEncryptionUnavailable, match="valid Fernet key"):
        encrypt(FAKE_TOKEN)


# -- establishing ----------------------------------------------------------


def test_a_session_is_established_from_a_request_token():
    session = kite_session.establish("fake-request-token", broker=StubBroker())

    assert session.kite_user_id == "AB1234"
    assert KiteSession.objects.count() == 1


def test_the_stored_token_is_not_the_plaintext():
    kite_session.establish("fake-request-token", broker=StubBroker())

    row = KiteSession.objects.get()
    assert FAKE_TOKEN.encode() not in bytes(row.encrypted_access_token)


def test_the_token_can_still_be_read_back_for_use():
    kite_session.establish("fake-request-token", broker=StubBroker())
    assert KiteSession.objects.get().access_token == FAKE_TOKEN


def test_the_token_is_not_in_the_rows_string_form():
    """That string reaches admin pages, logs and exception messages."""
    kite_session.establish("fake-request-token", broker=StubBroker())
    assert FAKE_TOKEN not in str(KiteSession.objects.get())


def test_establishing_is_audited_without_the_token():
    kite_session.establish("fake-request-token", broker=StubBroker())

    event = AuditEvent.objects.get(action="kite_session.established")
    assert event.after["kite_user_id"] == "AB1234"
    assert FAKE_TOKEN not in str(event.after)


def test_a_response_without_a_token_is_an_error():
    with pytest.raises(BrokerError, match="without an access token"):
        kite_session.establish("fake-request-token", broker=StubBroker(payload={"user_id": "AB"}))

    assert KiteSession.objects.count() == 0


def test_a_new_login_revokes_the_previous_session():
    """Kept rather than deleted: which token was live when an order was placed
    is part of that day's explanation."""
    kite_session.establish("first", broker=StubBroker())
    kite_session.establish("second", broker=StubBroker())

    assert KiteSession.objects.count() == 2
    assert KiteSession.objects.filter(revoked_at__isnull=True).count() == 1


# -- expiry ----------------------------------------------------------------


@time_machine.travel("2026-09-22 09:30:00+05:30")
def test_a_morning_login_lasts_until_the_next_day():
    expiry = kite_session.next_expiry()
    assert expiry.astimezone(timezone.get_current_timezone()).date() == datetime(2026, 9, 23).date()


@time_machine.travel("2026-09-22 05:00:00+05:30")
def test_a_login_before_the_expiry_hour_lasts_only_until_it():
    """Which is correct, and is why the screen shows a time and not a yes."""
    expiry = kite_session.next_expiry()
    assert expiry.astimezone(timezone.get_current_timezone()).date() == datetime(2026, 9, 22).date()


def test_a_fresh_session_is_valid():
    kite_session.establish("fake-request-token", broker=StubBroker())
    assert kite_session.status().valid is True


def test_an_expired_session_is_not_valid():
    kite_session.establish("fake-request-token", broker=StubBroker())
    KiteSession.objects.update(expires_at=timezone.now() - timedelta(minutes=1))

    state = kite_session.status()

    assert state.valid is False
    assert "expired" in state.reason


def test_no_session_at_all_says_so():
    state = kite_session.status()
    assert state.valid is False
    assert "no session" in state.reason
    assert state.expires_at is None


def test_yesterdays_session_does_not_count_as_todays():
    """ "It worked yesterday" is never evidence: the token lives one day."""
    with time_machine.travel("2026-09-21 09:30:00+05:30"):
        kite_session.establish("fake-request-token", broker=StubBroker())

    with time_machine.travel("2026-09-22 09:00:00+05:30"):
        assert kite_session.status().valid is False


# -- revoking and attaching ------------------------------------------------


def test_revoking_ends_the_session():
    kite_session.establish("fake-request-token", broker=StubBroker())

    kite_session.revoke(actor="system", reason="rejected by Kite")

    assert kite_session.status().valid is False
    assert AuditEvent.objects.filter(action="kite_session.revoked").exists()


def test_revoking_with_no_session_is_harmless():
    assert kite_session.revoke(actor="system", reason="nothing to do") == 0


def test_attaching_gives_the_broker_the_stored_token():
    kite_session.establish("fake-request-token", broker=StubBroker())
    broker = StubBroker()

    assert kite_session.attach(broker) is True
    assert broker.attached == [FAKE_TOKEN]


def test_attaching_without_a_session_reports_rather_than_raising():
    """The caller checks the answer instead of discovering it in a broker
    exception three calls later."""
    broker = StubBroker()

    assert kite_session.attach(broker) is False
    assert broker.attached == []


class StubClient:
    """Enough of KiteConnect to see what the factory handed it."""

    access_token = None

    def set_access_token(self, access_token):
        self.access_token = access_token


def test_the_factory_hands_back_a_client_carrying_the_stored_token():
    """Asking for "the Kite client" gets an authenticated one.

    It did not, once. Every page, the tradesheet and the evening valuation
    built a client and never attached the token, so each one failed with
    "Invalid `api_key` or `access_token`" while a live session sat in the
    table -- and the dashboard's fallback dutifully reported a portfolio
    worth nothing. Fourteen call sites had to remember the extra line and
    four did, which is the argument for it not being a line anybody has to
    remember.
    """
    kite_session.establish("fake-request-token", broker=StubBroker())
    client = StubClient()

    get_broker(client=client)

    assert client.access_token == FAKE_TOKEN


def test_the_factory_works_before_there_is_a_session():
    """The login flow asks for a client too, and has no token yet by
    definition. Nothing about that is an error."""
    client = StubClient()

    get_broker(client=client)

    assert client.access_token is None


def test_an_explicit_token_is_not_overwritten_by_the_stored_one():
    """A caller that named a token meant that token."""
    kite_session.establish("fake-request-token", broker=StubBroker())
    client = StubClient()

    get_broker(client=client, access_token="a-different-token")

    assert client.access_token is None


# -- a token belongs to the key that issued it ----------------------------


def test_a_session_records_which_api_key_issued_it(settings):
    settings.KITE_API_KEY = "fake-key-one"
    broker = StubBroker()
    broker.api_key = "fake-key-one"

    kite_session.establish("fake-request-token", broker=broker)

    assert KiteSession.objects.get().api_key == "fake-key-one"


def test_a_session_from_another_key_is_not_usable(settings):
    """Observed against the live API: changing the app's credentials leaves
    every token issued under the old key dead, while its stored expiry still
    says tomorrow morning. Reporting that as valid sends the day out to
    discover it at 09:45."""
    settings.KITE_API_KEY = "fake-key-one"
    broker = StubBroker()
    broker.api_key = "fake-key-one"
    kite_session.establish("fake-request-token", broker=broker)

    settings.KITE_API_KEY = "fake-key-two"
    state = kite_session.status()

    assert state.valid is False
    assert "different API key" in state.reason


def test_a_session_from_the_same_key_is_usable(settings):
    settings.KITE_API_KEY = "fake-key-one"
    broker = StubBroker()
    broker.api_key = "fake-key-one"
    kite_session.establish("fake-request-token", broker=broker)

    assert kite_session.status().valid is True


def test_a_session_recorded_before_the_key_was_tracked_still_works(settings):
    """Rows written before this field existed carry no key, and must not be
    invalidated by its arrival."""
    settings.KITE_API_KEY = "fake-key-one"
    broker = StubBroker()
    broker.api_key = ""
    kite_session.establish("fake-request-token", broker=broker)

    assert kite_session.status().valid is True


# -- whose account ---------------------------------------------------------


def test_a_session_for_the_configured_account_is_stored(settings):
    settings.KITE_USER_ID = "AB1234"

    kite_session.establish("fake-request-token", broker=StubBroker())

    assert KiteSession.objects.get().kite_user_id == "AB1234"


def test_a_session_for_another_account_is_refused_and_the_live_one_kept(settings):
    """A request token proves somebody signed in to Zerodha, not that it was
    the account this system trades. Swapping to theirs would send the next
    order to their account."""
    settings.KITE_USER_ID = "AB1234"
    kite_session.establish("first", broker=StubBroker())

    stranger = StubBroker(payload={"access_token": "fake-other-token", "user_id": "ZZ9999"})
    with pytest.raises(kite_session.WrongAccount, match="ZZ9999"):
        kite_session.establish("second", broker=stranger)

    live = kite_session.current()
    assert live.kite_user_id == "AB1234"
    assert KiteSession.objects.count() == 1


def test_the_account_is_compared_without_regard_to_case(settings):
    settings.KITE_USER_ID = "AB1234"

    session = kite_session.establish(
        "fake-request-token",
        broker=StubBroker(payload={"access_token": FAKE_TOKEN, "user_id": "ab1234"}),
    )

    assert session.kite_user_id == "ab1234"


def test_with_no_account_configured_the_session_is_stored_with_a_warning(settings, caplog):
    """A laptop may not name one. The box must (prod settings refuse)."""
    settings.KITE_USER_ID = ""

    with caplog.at_level("WARNING", logger="steadystack.broker"):
        kite_session.establish("fake-request-token", broker=StubBroker())

    assert KiteSession.objects.count() == 1
    assert "KITE_USER_ID" in caplog.text


# -- the login's state -----------------------------------------------------


def test_a_login_state_is_good_once():
    state = kite_session.mint_login_state(started_by="operator")

    assert kite_session.consume_login_state(state) is True
    assert kite_session.consume_login_state(state) is False


def test_an_unknown_or_empty_state_is_refused():
    kite_session.mint_login_state(started_by="operator")

    assert kite_session.consume_login_state("made-up") is False
    assert kite_session.consume_login_state("") is False


def test_a_login_state_expires():
    with time_machine.travel("2026-09-22 09:00:00+05:30"):
        state = kite_session.mint_login_state(started_by="operator")
    with time_machine.travel("2026-09-22 09:11:00+05:30"):
        assert kite_session.consume_login_state(state) is False


def test_the_state_is_not_stored_as_given():
    """A database read must not yield a state somebody could still use."""
    from broker.models import KiteLoginState

    state = kite_session.mint_login_state(started_by="operator")

    assert not KiteLoginState.objects.filter(digest=state).exists()
    assert state not in str(list(KiteLoginState.objects.values()))


def test_the_login_url_carries_the_state_for_kite_to_hand_back():
    """Kite appends redirect_params, URL-decoded, to the callback's query."""
    from urllib.parse import parse_qs, urlsplit

    url = kite_session.login_url(StubBroker(), state="fake-state-value")

    query = parse_qs(urlsplit(url).query)
    assert query["redirect_params"] == ["state=fake-state-value"]
    assert query["api_key"] == ["fake"]
