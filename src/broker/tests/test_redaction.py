"""Credential redaction happens before a BrokerCall row is written, never after.

The dangerous case is generate_session: its *response body* contains the access
token. An allow-everything-except-known-keys approach would have stored it on
the first successful login.
"""

import pytest
from kiteconnect import exceptions as kite_exceptions

from audit.models import BrokerCall
from broker.exceptions import BrokerError
from broker.kite import KiteBroker
from broker.redaction import redact, scrub


def test_an_ordinary_payload_is_left_alone():
    assert redact({"tradingsymbol": "INFY", "quantity": 1}) == {
        "tradingsymbol": "INFY",
        "quantity": 1,
    }


def test_api_secret_is_redacted():
    assert redact({"api_secret": "fake-api-secret-value"})["api_secret"] == "[REDACTED]"


def test_access_token_is_redacted():
    assert redact({"access_token": "abc123xyz"})["access_token"] == "[REDACTED]"


def test_the_generate_session_response_never_leaks_its_token():
    """The exact payload that would otherwise be stored on every daily login."""
    response = {
        "user_id": "AB1234",
        "user_name": "Operator",
        "access_token": "fake-access-token-value",
        "public_token": "fake-public-token-value",
        "refresh_token": "fake-refresh-token-value",
        "api_key": "kitefakekey00001",
    }

    redacted = redact(response)

    assert redacted["user_id"] == "AB1234"
    for secret in ("access_token", "public_token", "refresh_token", "api_key", "api_secret"):
        assert redacted.get(secret, "[REDACTED]") == "[REDACTED]"
    assert "fake-access-token-value" not in str(redacted)


def test_redaction_reaches_into_nested_structures():
    payload = {"data": {"session": {"access_token": "fake-deep-token-value"}}}
    assert "fake-deep-token-value" not in str(redact(payload))


def test_redaction_reaches_into_lists():
    payload = {"sessions": [{"access_token": "one"}, {"access_token": "two"}]}
    redacted = redact(payload)
    assert [s["access_token"] for s in redacted["sessions"]] == ["[REDACTED]", "[REDACTED]"]


def test_key_matching_ignores_case_and_separators():
    payload = {"AccessToken": "a", "ACCESS-TOKEN": "b", "Api_Secret": "c"}
    assert all(v == "[REDACTED]" for v in redact(payload).values())


def test_authorization_header_is_redacted():
    payload = {"headers": {"Authorization": "token kitefakekey00001:fake-access-token-value"}}
    assert "fake-access-token-value" not in str(redact(payload))


def test_unknown_keys_holding_a_token_shaped_value_are_kept():
    """Redaction is by key, not by guessing at values -- a price must survive."""
    assert redact({"last_price": "1650.50"})["last_price"] == "1650.50"


def test_none_and_empty_payloads_are_safe():
    assert redact(None) is None
    assert redact({}) == {}


def test_an_enctoken_is_redacted():
    """The cookie Kite's own web session runs on. It places orders as well as
    an access token does."""
    assert redact({"enctoken": "fake-enctoken-value"})["enctoken"] == "[REDACTED]"


def test_any_key_naming_a_token_is_redacted():
    """A credential under a name nobody listed yet is still a token."""
    redacted = redact({"session_token": "a", "csrfToken": "b", "tokens": ["c"]})
    assert all(value == "[REDACTED]" for value in redacted.values())


def test_instrument_and_exchange_tokens_are_identifiers_and_are_kept():
    """They name a scrip, not a person, and every quote carries them."""
    payload = {"instrument_token": 408065, "exchange_token": 1594}
    assert redact(payload) == payload


# -- free text ---------------------------------------------------------------


def test_scrubbing_removes_each_named_value_from_text():
    text = "Invalid api_key fake-key-1234 or access_token fake-access-token-value"

    scrubbed = scrub(text, "fake-key-1234", "fake-access-token-value")

    assert "fake-key-1234" not in scrubbed
    assert "fake-access-token-value" not in scrubbed
    assert "Invalid api_key [REDACTED]" in scrubbed


def test_scrubbing_ignores_blank_values():
    """An unset secret is "", and replacing "" would wreck the message."""
    assert scrub("upstream is down", "", None) == "upstream is down"


class _Refusing:
    """A KiteConnect whose answer is an error quoting the credentials it was
    given -- the shape a careless upstream message could take."""

    access_token = None

    def set_access_token(self, access_token):
        self.access_token = access_token

    def profile(self):
        raise kite_exceptions.InputException(
            "Invalid api_key fake-key-1234, secret fake-secret-5678, "
            f"access_token {self.access_token}"
        )


@pytest.mark.django_db
def test_a_failed_call_stores_its_error_without_the_credentials():
    broker = KiteBroker(api_key="fake-key-1234", api_secret="fake-secret-5678", client=_Refusing())
    broker.set_access_token("fake-access-token-value")

    with pytest.raises(BrokerError):
        broker.profile()

    error = BrokerCall.objects.get().error
    assert "Invalid api_key" in error
    for secret in ("fake-key-1234", "fake-secret-5678", "fake-access-token-value"):
        assert secret not in error
