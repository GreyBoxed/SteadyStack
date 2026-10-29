"""Postbacks: verified, then used as a trigger rather than an answer.

The endpoint is public, so the checksum tests come first. After that the
behaviour under test is that the payload's own numbers are never believed --
a postback claiming a fill that the broker's book does not show must not
change anything, because the book is authoritative and the postback is only
a reason to go and read it.
"""

import hashlib
import json
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from audit.models import AuditEvent
from orders import postback
from orders.models import Order, OrderType, QueuedPostback
from orders.postback import InvalidPostback
from orders.state import OrderState
from portfolio.instruments import refresh

TUESDAY = date(2026, 9, 22)
SECRET = "fake-api-secret-for-tests"
ORDER_ID = "251122000123456"
STAMP = "2026-09-22 10:05:00"

pytestmark = pytest.mark.django_db


class StubBroker:
    def __init__(self, book=None):
        self.book = book or []
        self.reads = 0

    def orders(self):
        self.reads += 1
        return self.book

    def place_order(self, **kwargs):
        return ORDER_ID

    def cancel_order(self, order_id):
        return order_id


@pytest.fixture(autouse=True)
def api_secret(settings):
    settings.KITE_API_SECRET = SECRET


@pytest.fixture
def instrument(db):
    refresh(
        [
            {
                "tradingsymbol": "INFY",
                "instrument_token": 408065,
                "name": "INFY",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "segment": "NSE",
                "tick_size": 0.05,
                "lot_size": 1,
            }
        ]
    )
    from portfolio.models import Instrument

    return Instrument.objects.get(pk="INFY")


@pytest.fixture
def order(instrument):
    from engine.models import TradeSheet, TradeSheetState

    sheet = TradeSheet.objects.create(
        trading_date=TUESDAY,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=4,
        reference_price=Decimal("1650.50"),
    )
    return Order.objects.create(
        tag="tx-postback-1",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=TUESDAY,
        tick_no=0,
        mode="live",
        order_type=OrderType.LIMIT,
        quantity=4,
        limit_price=Decimal("1650.00"),
        state=OrderState.OPEN,
        kite_order_id=ORDER_ID,
    )


def a_payload(**overrides) -> dict:
    payload = {
        "order_id": ORDER_ID,
        "order_timestamp": STAMP,
        "status": "COMPLETE",
        "filled_quantity": 4,
        "tag": "tx-postback-1",
    }
    payload.update(overrides)
    payload.setdefault(
        "checksum",
        hashlib.sha256(
            f"{payload['order_id']}{payload['order_timestamp']}{SECRET}".encode()
        ).hexdigest(),
    )
    return payload


# -- the checksum ----------------------------------------------------------


def test_a_correctly_signed_postback_verifies():
    postback.verify(a_payload(), api_secret=SECRET)


def test_a_wrong_checksum_is_refused():
    with pytest.raises(InvalidPostback, match="checksum does not match"):
        postback.verify(a_payload(checksum="0" * 64), api_secret=SECRET)


def test_a_missing_checksum_is_refused():
    payload = a_payload()
    del payload["checksum"]

    with pytest.raises(InvalidPostback, match="missing the fields"):
        postback.verify(payload, api_secret=SECRET)


def test_a_postback_signed_with_another_secret_is_refused():
    """Which is what somebody who found the URL would have."""
    forged = a_payload()
    forged["checksum"] = hashlib.sha256(f"{ORDER_ID}{STAMP}a-different-secret".encode()).hexdigest()

    with pytest.raises(InvalidPostback):
        postback.verify(forged, api_secret=SECRET)


def test_a_replayed_payload_with_a_changed_field_is_refused():
    """The checksum covers the order id and its timestamp, so a payload whose
    identity was altered no longer verifies."""
    tampered = a_payload()
    tampered["order_id"] = "999999999999"

    with pytest.raises(InvalidPostback):
        postback.verify(tampered, api_secret=SECRET)


def test_verification_without_a_configured_secret_refuses_everything():
    with pytest.raises(InvalidPostback, match="No API secret"):
        postback.verify(a_payload(), api_secret="")


# -- trigger, not applier (D8) --------------------------------------------


def test_a_postback_makes_the_system_re_read_the_broker(order):
    broker = StubBroker(book=[{"tag": order.tag, "status": "COMPLETE", "filled_quantity": 4}])

    outcome = postback.handle(a_payload(), broker=broker)

    assert outcome.matched is True
    assert broker.reads == 1


def test_the_brokers_book_decides_not_the_payload(order):
    """The payload claims a full fill; the book says two. The book wins."""
    broker = StubBroker(book=[{"tag": order.tag, "status": "OPEN", "filled_quantity": 2}])

    postback.handle(a_payload(filled_quantity=4, status="COMPLETE"), broker=broker)

    order.refresh_from_db()
    assert order.filled_quantity == 2
    assert order.state == OrderState.PARTIALLY_FILLED


def test_a_duplicate_postback_changes_nothing_the_second_time(order):
    broker = StubBroker(book=[{"tag": order.tag, "status": "COMPLETE", "filled_quantity": 4}])

    postback.handle(a_payload(), broker=broker)
    postback.handle(a_payload(), broker=broker)

    order.refresh_from_db()
    assert order.filled_quantity == 4
    assert order.state == OrderState.COMPLETE


def test_a_reordered_pair_does_not_go_backwards(order):
    """The 'open' postback arrives after the 'complete' one. Re-reading means
    the order cannot regress."""
    broker = StubBroker(book=[{"tag": order.tag, "status": "COMPLETE", "filled_quantity": 4}])

    postback.handle(a_payload(status="COMPLETE"), broker=broker)
    postback.handle(a_payload(status="OPEN", order_timestamp="2026-09-22 10:04:00"), broker=broker)

    order.refresh_from_db()
    assert order.state == OrderState.COMPLETE


def test_handling_is_audited(order):
    broker = StubBroker(book=[{"tag": order.tag, "status": "OPEN", "filled_quantity": 0}])

    postback.handle(a_payload(status="OPEN"), broker=broker)

    assert AuditEvent.objects.filter(action="postback.handled").exists()


# -- postbacks that arrive too early --------------------------------------


def test_an_unmatched_postback_is_queued_not_dropped(db):
    """It is the only notice this system gets that something happened."""
    outcome = postback.handle(a_payload(tag="tx-not-here-yet"), broker=StubBroker())

    assert outcome.matched is False
    assert outcome.queued is True
    assert QueuedPostback.objects.count() == 1


def test_the_same_unmatched_postback_queues_once(db):
    postback.handle(a_payload(tag="tx-not-here-yet"), broker=StubBroker())
    postback.handle(a_payload(tag="tx-not-here-yet"), broker=StubBroker())

    assert QueuedPostback.objects.count() == 1


def test_a_queued_postback_resolves_once_the_order_exists(instrument, order):
    """The usual race: the postback overtook the placement response."""
    QueuedPostback.objects.create(
        order_id=ORDER_ID,
        received_at_key=STAMP,
        payload=a_payload(),
    )
    broker = StubBroker(book=[{"tag": order.tag, "status": "COMPLETE", "filled_quantity": 4}])

    resolved = postback.drain_queue(broker=broker)

    assert resolved == 1
    order.refresh_from_db()
    assert order.state == OrderState.COMPLETE


def test_a_postback_that_never_finds_an_order_stays_queued(db):
    postback.handle(a_payload(tag="tx-never"), broker=StubBroker())

    assert postback.drain_queue(broker=StubBroker()) == 0
    assert postback.unresolved_count() == 1


# -- the endpoint ----------------------------------------------------------


def test_the_endpoint_accepts_a_signed_postback(client, order, monkeypatch):
    broker = StubBroker(book=[{"tag": order.tag, "status": "COMPLETE", "filled_quantity": 4}])
    monkeypatch.setattr("orders.views.get_broker", lambda **_: broker)

    response = client.post(
        reverse("orders:kite_postback"),
        data=json.dumps(a_payload()),
        content_type="application/json",
    )

    assert response.status_code == 200


def test_the_endpoint_is_reachable_without_a_session(client, order, monkeypatch):
    """Kite's servers post here, not the operator's browser."""
    monkeypatch.setattr("orders.views.get_broker", lambda **_: StubBroker())

    response = client.post(
        reverse("orders:kite_postback"),
        data=json.dumps(a_payload()),
        content_type="application/json",
    )

    assert response.status_code != 302


def test_the_endpoint_refuses_an_unsigned_post(client, monkeypatch):
    monkeypatch.setattr("orders.views.get_broker", lambda **_: StubBroker())

    response = client.post(
        reverse("orders:kite_postback"),
        data=json.dumps({"order_id": ORDER_ID, "status": "COMPLETE"}),
        content_type="application/json",
    )

    assert response.status_code == 403
    assert AuditEvent.objects.filter(action="postback.rejected").exists()


def test_the_endpoint_refuses_junk(client, monkeypatch):
    monkeypatch.setattr("orders.views.get_broker", lambda **_: StubBroker())

    response = client.post(
        reverse("orders:kite_postback"), data="not json", content_type="application/json"
    )

    assert response.status_code == 400


def test_the_endpoint_only_accepts_a_post(client):
    assert client.get(reverse("orders:kite_postback")).status_code == 405
