"""The tick feed's logic, without a socket.

What is tested here is everything except the connection: which instruments are
worth watching, what a tick does, and -- the one that decides whether anybody
trusts the indicator -- when silence means something.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from engine.models import TradeSheet, TradeSheetState
from orders import ticker
from orders.models import Fill, Order, OrderMode, OrderType
from orders.state import OrderState
from portfolio.instruments import refresh
from portfolio.models import Instrument

pytestmark = pytest.mark.django_db

TUE = date(2026, 9, 22)
SAT = date(2026, 9, 26)
INFY_TOKEN = 408065


def at(day, hour, minute):
    return timezone.make_aware(
        datetime.combine(day, time(hour, minute)), timezone.get_current_timezone()
    )


@pytest.fixture
def instrument(db):
    refresh(
        [
            {
                "tradingsymbol": "INFY",
                "instrument_token": INFY_TOKEN,
                "name": "INFY",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "segment": "NSE",
                "tick_size": 0.05,
                "lot_size": 1,
            }
        ]
    )
    return Instrument.objects.get(pk="INFY")


@pytest.fixture
def sheet(instrument):
    return TradeSheet.objects.create(
        trading_date=TUE,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=4,
        reference_price=Decimal("1650.50"),
    )


def an_order(sheet, instrument, *, limit=Decimal("1650.00"), quantity=2, mode=OrderMode.PAPER):
    return Order.objects.create(
        tag=f"tx-tick-{quantity}-{limit}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=sheet.trading_date,
        tick_no=0,
        mode=mode,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=limit,
        state=OrderState.OPEN,
    )


# -- what to watch ---------------------------------------------------------


def test_only_instruments_with_open_paper_orders_are_watched(sheet, instrument):
    """Subscribing to the whole universe would mean every tick of every name
    walking a query that can only ever match a handful of rows."""
    router = ticker.TickRouter()
    assert router.refresh_subscriptions() == []

    an_order(sheet, instrument)
    assert router.refresh_subscriptions() == [INFY_TOKEN]


def test_a_filled_order_stops_being_watched(sheet, instrument):
    an_order(sheet, instrument, quantity=2)
    router = ticker.TickRouter()
    router.refresh_subscriptions()

    router.on_ticks([{"instrument_token": INFY_TOKEN, "last_price": 1600.0}])

    assert router.refresh_subscriptions() == []


def test_a_live_order_is_not_watched_for_paper_fills(sheet, instrument):
    an_order(sheet, instrument, mode=OrderMode.LIVE)
    assert ticker.TickRouter().refresh_subscriptions() == []


# -- applying ticks --------------------------------------------------------


def test_a_tick_fills_a_matching_order(sheet, instrument):
    an_order(sheet, instrument, limit=Decimal("1650.00"))
    router = ticker.TickRouter()
    router.refresh_subscriptions()

    filled = router.on_ticks([{"instrument_token": INFY_TOKEN, "last_price": 1649.0}])

    assert filled == 1
    assert Fill.objects.count() == 1


def test_a_tick_for_an_unwatched_instrument_is_ignored(sheet, instrument):
    an_order(sheet, instrument)
    router = ticker.TickRouter()
    router.refresh_subscriptions()

    assert router.on_ticks([{"instrument_token": 999999, "last_price": 1.0}]) == 0


def test_a_tick_with_no_price_is_ignored(sheet, instrument):
    an_order(sheet, instrument)
    router = ticker.TickRouter()
    router.refresh_subscriptions()

    assert router.on_ticks([{"instrument_token": INFY_TOKEN, "last_price": 0}]) == 0


def test_a_tick_marks_the_feed_alive(sheet, instrument):
    router = ticker.TickRouter()
    router.on_ticks([{"instrument_token": INFY_TOKEN, "last_price": 1649.0}])

    assert router.health.last_tick_at is not None


def test_a_price_is_read_as_a_decimal():
    assert ticker.price_of({"last_price": 1649.35}) == Decimal("1649.35")


def test_a_junk_price_reads_as_zero():
    assert ticker.price_of({"last_price": "not a number"}) == Decimal("0")


# -- staleness -------------------------------------------------------------


def test_silence_during_market_hours_is_stale(instrument):
    health = ticker.FeedHealth(connected=True, last_tick_at=at(TUE, 10, 0))

    assert health.is_stale(now=at(TUE, 10, 30)) is True


def test_a_recent_tick_is_not_stale(instrument):
    health = ticker.FeedHealth(connected=True, last_tick_at=at(TUE, 10, 0))

    assert health.is_stale(now=at(TUE, 10, 2)) is False


def test_silence_outside_market_hours_is_not_stale(instrument):
    """Saying otherwise every weekend teaches everyone to ignore it."""
    health = ticker.FeedHealth(connected=True, last_tick_at=at(TUE, 15, 0))

    assert health.is_stale(now=at(TUE, 18, 0)) is False


def test_silence_on_a_saturday_is_not_stale(instrument):
    health = ticker.FeedHealth(connected=True, last_tick_at=at(TUE, 15, 0))

    assert health.is_stale(now=at(SAT, 11, 0)) is False


def test_no_tick_at_all_during_market_hours_is_stale(instrument):
    health = ticker.FeedHealth(connected=True, last_tick_at=None)

    assert health.is_stale(now=at(TUE, 11, 0)) is True


def test_health_reports_itself_for_the_endpoint(instrument):
    health = ticker.FeedHealth(connected=True, last_tick_at=at(TUE, 11, 0), subscribed=3)

    reported = health.as_dict(now=at(TUE, 11, 1))

    assert reported["connected"] is True
    assert reported["subscribed"] == 3
    assert reported["stale"] is False


# -- reconnection ----------------------------------------------------------


def test_backoff_grows_then_settles():
    """A feed down for an hour should still be trying every half minute."""
    waits = [ticker.backoff_for(attempt) for attempt in range(8)]

    assert waits[0] < waits[3]
    assert waits[-1] == max(ticker.BACKOFF_SECONDS)
    assert waits == sorted(waits)


def test_the_stale_threshold_is_minutes_not_hours():
    """A feed that dies at 11:00 and is noticed at 15:30 has cost a day."""
    assert ticker.STALE_AFTER <= timedelta(minutes=10)


# -- picking up names placed after the feed connected -----------------------


class FakeSocket:
    MODE_FULL = "full"

    def __init__(self):
        self.subscribed, self.unsubscribed = [], []

    def subscribe(self, tokens):
        self.subscribed.append(list(tokens))

    def set_mode(self, mode, tokens):
        pass

    def unsubscribe(self, tokens):
        self.unsubscribed.append(list(tokens))


def test_a_name_ordered_after_the_feed_connected_is_subscribed(sheet, instrument):
    """The feed connected before the day's first order; the order that came
    later must still get watched, or it never fills."""
    from orders.ticker import KiteTickerClient

    client = KiteTickerClient(api_key="k", access_token="t")
    socket = FakeSocket()
    client._on_connect(socket, {})
    assert socket.subscribed == []  # nothing open yet

    an_order(sheet, instrument)
    added, dropped = client.resync(socket)

    assert (added, dropped) == (1, 0)
    assert socket.subscribed == [[INFY_TOKEN]]


def test_a_name_with_nothing_left_open_is_unsubscribed(sheet, instrument):
    from orders.ticker import KiteTickerClient

    order = an_order(sheet, instrument)
    client = KiteTickerClient(api_key="k", access_token="t")
    socket = FakeSocket()
    client._on_connect(socket, {})

    Order.objects.filter(pk=order.pk).update(state=OrderState.CANCELLED)
    added, dropped = client.resync(socket)

    assert (added, dropped) == (0, 1)
    assert socket.unsubscribed == [[INFY_TOKEN]]
    assert client.resync(socket) == (0, 0)  # nothing changes twice


# -- the pulse and the supervisor --------------------------------------------


class FakeClient:
    """Records what the supervisor asks of a connection; touches no network."""

    made: list["FakeClient"] = []

    def __init__(self, *, api_key, access_token, router):
        self.access_token = access_token
        self.router = router
        self.started = False
        self.stopped = False
        self.resyncs = 0
        FakeClient.made.append(self)

    def start(self, *, threaded=True):
        self.started = True
        self.router.health.connected = True

    def stop(self):
        self.stopped = True
        self.router.health.connected = False

    def resync(self):
        self.resyncs += 1


@pytest.fixture
def kite(monkeypatch):
    """Which Kite session is current, as the supervisor will read it."""
    from types import SimpleNamespace

    from broker import session

    state = {"token": None}

    def status():
        if state["token"] is None:
            return SimpleNamespace(
                valid=False, session=None, reason="no session has been established"
            )
        return SimpleNamespace(
            valid=True, session=SimpleNamespace(access_token=state["token"]), reason=""
        )

    monkeypatch.setattr(session, "status", status)
    FakeClient.made = []
    return state


def supervisor():
    return ticker.Supervisor(api_key="key", client_factory=FakeClient)


@pytest.mark.django_db
def test_without_a_session_the_feed_waits_rather_than_exiting(kite):
    feed = supervisor()

    assert feed.ensure_connected() == "waiting"
    assert feed.client is None
    assert "no usable Kite session" in feed.health.last_error


@pytest.mark.django_db
def test_a_login_later_in_the_morning_connects_the_waiting_feed(kite):
    feed = supervisor()
    feed.ensure_connected()

    kite["token"] = "monday"
    assert feed.ensure_connected() == "connecting"
    assert feed.client.access_token == "monday"
    assert feed.health.connected is True


@pytest.mark.django_db
def test_the_same_session_leaves_the_connection_alone(kite):
    kite["token"] = "monday"
    feed = supervisor()
    feed.ensure_connected()

    assert feed.ensure_connected() == "unchanged"
    assert len(FakeClient.made) == 1


@pytest.mark.django_db
def test_a_new_days_login_replaces_yesterdays_connection(kite):
    """The token is in the handshake and dies each morning; a feed that kept
    yesterday's would go deaf and never say so."""
    kite["token"] = "monday"
    feed = supervisor()
    feed.ensure_connected()
    monday = feed.client

    kite["token"] = "tuesday"
    assert feed.ensure_connected() == "connecting"
    assert monday.stopped is True
    assert feed.client.access_token == "tuesday"


@pytest.mark.django_db
def test_a_session_that_ends_drops_the_connection_and_waits(kite):
    kite["token"] = "monday"
    feed = supervisor()
    feed.ensure_connected()

    kite["token"] = None
    assert feed.ensure_connected() == "waiting"
    assert feed.client is None
    assert feed.health.connected is False


# -- a deaf feed ---------------------------------------------------------------


def listening(kite, *, since, subscribed=1):
    """A feed connected on Monday's session at *since*, with names to hear."""
    kite["token"] = "monday"
    feed = supervisor()
    feed.ensure_connected(now=since)
    feed.health.subscribed = subscribed
    return feed


@pytest.mark.django_db
def test_a_connected_feed_that_hears_nothing_is_reconnected(kite):
    """25 Sep: the socket stayed open and subscribed from 14:18 and delivered
    nothing until Kite dropped it at 15:31, so the close's market orders sat
    unfilled. Silence with names to hear, in market hours, is a dead socket."""
    feed = listening(kite, since=at(TUE, 10, 0))
    feed.health.last_tick_at = at(TUE, 14, 18)
    deaf = feed.client

    assert feed.ensure_connected(now=at(TUE, 14, 24)) == "reconnecting"
    assert deaf.stopped is True
    assert feed.client is not deaf
    assert feed.client.access_token == "monday"
    assert feed.client.started is True


@pytest.mark.django_db
def test_a_feed_hearing_ticks_is_left_alone(kite):
    feed = listening(kite, since=at(TUE, 10, 0))
    feed.health.last_tick_at = at(TUE, 14, 22)

    assert feed.ensure_connected(now=at(TUE, 14, 24)) == "unchanged"
    assert len(FakeClient.made) == 1


@pytest.mark.django_db
def test_silence_with_nothing_to_hear_is_not_deafness(kite):
    """Subscribed to nothing, the feed is quiet because it should be."""
    feed = listening(kite, since=at(TUE, 10, 0), subscribed=0)
    feed.health.last_tick_at = at(TUE, 10, 0)

    assert feed.ensure_connected(now=at(TUE, 14, 24)) == "unchanged"


@pytest.mark.django_db
def test_silence_after_the_close_is_not_deafness(kite):
    feed = listening(kite, since=at(TUE, 10, 0))
    feed.health.last_tick_at = at(TUE, 15, 29)

    assert feed.ensure_connected(now=at(TUE, 18, 0)) == "unchanged"


@pytest.mark.django_db
def test_a_fresh_connection_gets_its_own_grace(kite):
    """No tick yet on a connection made two minutes ago is a connection still
    settling, not a deaf one; otherwise it reconnects every minute."""
    feed = listening(kite, since=at(TUE, 14, 22))
    feed.health.last_tick_at = at(TUE, 14, 0)

    assert feed.ensure_connected(now=at(TUE, 14, 24)) == "unchanged"


@pytest.mark.django_db
def test_the_pulse_is_written_where_the_health_endpoint_reads_it(kite):
    from orders.models import FeedHeartbeat

    kite["token"] = "monday"
    feed = supervisor()
    feed.ensure_connected()
    feed.health.subscribed = 4
    feed.beat()
    feed.beat()  # overwritten, not appended

    [pulse] = FeedHeartbeat.objects.all()
    assert pulse.connected is True
    assert pulse.subscribed == 4


@pytest.mark.django_db
def test_stopping_says_so_in_the_pulse(kite):
    from orders.models import FeedHeartbeat

    kite["token"] = "monday"
    feed = supervisor()
    feed.ensure_connected()
    feed.stop()

    assert FeedHeartbeat.objects.get().connected is False
