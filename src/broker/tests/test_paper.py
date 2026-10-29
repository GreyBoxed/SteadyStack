"""Paper placement.

What is being tested is mostly an absence: in paper mode the broker is never
called. The presence being tested is that everything else about the order is
identical, because that identity is what makes a paper day evidence about a
live one (M19a).
"""

from decimal import Decimal

import pytest

from broker import paper
from broker.exceptions import LiveOrdersRefused
from configuration.models import OrderMode
from configuration.store import update

REGISTERED = "203.0.113.7"


class RecordingBroker:
    """Records what it was asked to do, and asserts it was asked at all."""

    def __init__(self, order_id: str = "251122000123456") -> None:
        self.order_id = order_id
        self.placed: list[dict] = []
        self.cancelled: list[str] = []

    def place_order(self, **kwargs) -> str:
        self.placed.append(kwargs)
        return self.order_id

    def cancel_order(self, order_id: str) -> str:
        self.cancelled.append(order_id)
        return order_id


REQUEST = paper.PlacementRequest(
    tradingsymbol="INFY",
    exchange="NSE",
    transaction_type="BUY",
    quantity=1,
    order_type="LIMIT",
    price=Decimal("1650.50"),
    tag="ts-2026-09-22-t3",
)


@pytest.fixture
def outbound(monkeypatch):
    def _set(value):
        monkeypatch.setattr("broker.network.outbound_ip", lambda **_: value)

    _set(None)
    return _set


def go_live(outbound):
    outbound(REGISTERED)
    update(changed_by="operator", order_mode=OrderMode.LIVE, registered_static_ip=REGISTERED)


@pytest.mark.django_db
def test_a_paper_order_never_reaches_the_broker(outbound):
    broker = RecordingBroker()

    placement = paper.place(REQUEST, broker=broker)

    assert broker.placed == []
    assert placement.sent is False
    assert placement.is_paper is True


@pytest.mark.django_db
def test_a_paper_order_still_gets_an_identity(outbound):
    """It is a real order in every table; only the sending is skipped."""
    placement = paper.place(REQUEST, broker=RecordingBroker())
    assert placement.order_id


@pytest.mark.django_db
def test_a_paper_identity_cannot_be_mistaken_for_a_broker_one(outbound):
    placement = paper.place(REQUEST, broker=RecordingBroker())
    assert placement.order_id.startswith(paper.PAPER_ID_PREFIX)
    assert paper.is_paper_order_id(placement.order_id) is True


@pytest.mark.django_db
def test_paper_identities_are_unique(outbound):
    broker = RecordingBroker()
    first = paper.place(REQUEST, broker=broker)
    second = paper.place(REQUEST, broker=broker)
    assert first.order_id != second.order_id


@pytest.mark.django_db
def test_a_live_order_reaches_the_broker_unchanged(live_book_open, outbound):
    go_live(outbound)
    broker = RecordingBroker()

    placement = paper.place(REQUEST, broker=broker)

    assert placement.sent is True
    assert placement.order_id == broker.order_id
    assert broker.placed == [
        {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "price": Decimal("1650.50"),
            "tag": "ts-2026-09-22-t3",
            "market_protection": None,
        }
    ]


@pytest.mark.django_db
def test_live_from_the_wrong_address_places_nothing_at_all(live_book_open, outbound):
    """Not a paper order either: the operator asked for live and must be told
    it did not happen."""
    go_live(outbound)
    outbound("198.51.100.42")
    broker = RecordingBroker()

    with pytest.raises(LiveOrdersRefused):
        paper.place(REQUEST, broker=broker)

    assert broker.placed == []


@pytest.mark.django_db
def test_cancelling_a_paper_order_does_not_reach_the_broker(outbound):
    placement = paper.place(REQUEST, broker=RecordingBroker())
    broker = RecordingBroker()

    assert paper.cancel(placement.order_id, broker=broker) == placement.order_id
    assert broker.cancelled == []


@pytest.mark.django_db
def test_cancelling_a_live_order_reaches_the_broker(outbound):
    broker = RecordingBroker()

    paper.cancel("251122000123456", broker=broker)

    assert broker.cancelled == ["251122000123456"]


@pytest.mark.django_db
def test_a_market_order_carries_no_price(outbound):
    """The 15:00 fallback. It must survive the same path with price unset."""
    request = paper.PlacementRequest(
        tradingsymbol="INFY",
        exchange="NSE",
        transaction_type="BUY",
        quantity=2,
        order_type="MARKET",
    )

    placement = paper.place(request, broker=RecordingBroker())

    assert placement.is_paper is True
