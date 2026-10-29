"""Fixtures shared by the page tests.

The OTP middleware denies by default, so there is exactly one way to produce a
usable session and it lives here. A second way invented in a test file is a
test that passes for the wrong reason.
"""

import pytest
from django.contrib.auth import get_user_model
from django_otp.plugins.otp_totp.models import TOTPDevice

FAKE_PASSWORD = "fake-password-for-tests"


@pytest.fixture
def signed_in(client, db):
    """A client whose session is authenticated *and* OTP-verified."""
    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.login(username="operator", password=FAKE_PASSWORD)

    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()

    return client


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


@pytest.fixture
def no_broker(monkeypatch):
    """No page may need the network to render.

    Every dashboard reads live prices, and every dashboard has to stay
    readable when the feed is down -- which is exactly when somebody is
    looking at it.
    """
    from broker.exceptions import BrokerError

    def refuse(*_args, **_kwargs):
        raise BrokerError("no session in a test")

    monkeypatch.setattr("engine.quotes.fetch", refuse)
