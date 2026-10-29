"""The tick feed, and what it is for.

Two jobs, and only two. It keeps a last-traded price for the instruments the
day cares about, and it fills open paper orders against real prices (M19c).
It decides nothing: no order is placed from here, and no state is inferred
from a missing tick.

The parts worth stating:

  * **reconnection is expected, not exceptional.** A websocket held open from
    09:15 to 15:30 every weekday for years will drop. It reconnects with
    exponential backoff and re-subscribes, because a feed that dies quietly at
    11:00 and is noticed at 15:30 has cost a day;
  * **a new session means a new connection.** The access token is part of the
    handshake, so yesterday's socket cannot survive today's login;
  * **staleness is judged against market hours.** "No tick for twenty minutes"
    means something at 11:00 on a Tuesday and nothing at all on a Sunday, and
    an indicator that cannot tell the difference gets ignored -- which is the
    same as not having one.

The connection itself is kept behind a seam so the logic above is testable
without a network: `KiteTickerClient` is the only part that touches
`KiteTicker`, and everything else works against ticks as plain dictionaries.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

from audit.logging import fields
from orders import paperfill
from portfolio.models import Instrument
from trading_calendar.sessions import session_for

logger = logging.getLogger("steadystack.orders")

#: Backoff between reconnection attempts, in seconds. Capped, because a feed
#: that has been down for an hour should still be trying every half minute.
BACKOFF_SECONDS = (1, 2, 5, 10, 30)

#: How long without a tick counts as stale, during market hours.
STALE_AFTER = timedelta(minutes=5)


@dataclass
class FeedHealth:
    """What the health endpoint and the dead-man check read."""

    connected: bool = False
    last_tick_at: datetime | None = None
    subscribed: int = 0
    reconnects: int = 0
    last_error: str = ""
    #: When the current connection was made: a fresh one gets its own grace.
    listening_since: datetime | None = None

    def is_deaf(self, *, now: datetime | None = None) -> bool:
        """Whether a connected feed with names to hear has stopped hearing.

        A socket can stay open and subscribed while delivering nothing -- on
        25 Sep one went quiet at 14:18 and was dropped by Kite at 15:31, and
        the close's market orders sat unfilled all that time. Silence counts
        only in market hours, only with something subscribed, and only past
        STALE_AFTER from the later of the last tick and the connection.
        """
        now = now or timezone.localtime()
        if not self.connected or self.subscribed <= 0:
            return False
        if not session_for(now.date()).covers(now.time()):
            return False
        heard = [t for t in (self.last_tick_at, self.listening_since) if t is not None]
        if not heard:
            return False
        return (now - max(heard)) > STALE_AFTER

    def is_stale(self, *, now: datetime | None = None) -> bool:
        """Whether the silence means anything.

        Outside market hours there is nothing to be stale about: the feed is
        quiet because the market is shut, and saying otherwise every weekend
        teaches everyone to ignore it.
        """
        now = now or timezone.localtime()
        session = session_for(now.date())

        if not session.covers(now.time()):
            return False
        if self.last_tick_at is None:
            return True

        return (now - self.last_tick_at) > STALE_AFTER

    def as_dict(self, *, now: datetime | None = None) -> dict:
        return {
            "connected": self.connected,
            "subscribed": self.subscribed,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "stale": self.is_stale(now=now),
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


def beat(health: FeedHealth, *, now: datetime | None = None) -> None:
    """Write the feed's pulse where the health endpoint can read it.

    Called from the feed's own loop every few seconds, connected or not: a
    pulse that says "alive, not connected" is the difference between "the
    process died" and "Kite is refusing us", and they are chased differently.
    """
    from orders.models import FeedHeartbeat

    FeedHeartbeat.objects.update_or_create(
        id=FeedHeartbeat.SINGLETON,
        defaults={
            "beat_at": now or timezone.now(),
            "connected": health.connected,
            "subscribed": health.subscribed,
            "last_tick_at": health.last_tick_at,
            "reconnects": health.reconnects,
            "last_error": health.last_error[:255],
        },
    )


def backoff_for(attempt: int) -> int:
    """Seconds to wait before the next reconnection attempt."""
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]


def price_of(tick: dict) -> Decimal:
    """The traded price out of a Kite tick, as a Decimal."""
    raw = tick.get("last_price")
    try:
        return Decimal(str(raw))
    except (TypeError, ArithmeticError, ValueError):
        return Decimal("0")


@dataclass
class TickRouter:
    """Turns ticks into paper fills.

    Deliberately thin: it looks up which instrument a token is, then hands the
    price to the fill path. Everything about *whether* an order fills lives in
    `paperfill`, where it is tested.
    """

    health: FeedHealth = field(default_factory=FeedHealth)
    _tokens: dict[int, str] = field(default_factory=dict)

    def refresh_subscriptions(self) -> list[int]:
        """The instrument tokens worth watching: those with open paper orders."""
        tokens = paperfill.instruments_to_watch()
        self._tokens = dict(
            Instrument.objects.filter(instrument_token__in=tokens).values_list(
                "instrument_token", "tradingsymbol"
            )
        )
        self.health.subscribed = len(self._tokens)
        return list(self._tokens)

    def on_ticks(self, ticks: list[dict]) -> int:
        """Apply a batch of ticks. Returns how many orders filled."""
        self.health.last_tick_at = timezone.localtime()
        filled = 0

        for tick in ticks:
            token = tick.get("instrument_token")
            symbol = self._tokens.get(token)
            if symbol is None:
                continue

            price = price_of(tick)
            if price <= 0:
                continue

            fills = paperfill.apply_tick(instrument_id=symbol, traded_price=price)
            filled += len(fills)

            if fills:
                # A fill changes what is worth watching: an order that just
                # filled no longer needs its instrument subscribed.
                self.refresh_subscriptions()

        return filled


class KiteTickerClient:
    """The only part that touches KiteTicker.

    Kept separate so everything above it is testable without a network, and
    so the reconnect policy is one thing rather than scattered through
    callbacks.
    """

    def __init__(self, *, api_key: str, access_token: str, router: TickRouter | None = None):
        self.api_key = api_key
        self.access_token = access_token
        self.router = router or TickRouter()
        self._ticker = None
        self._attempt = 0
        #: What the websocket is actually subscribed to. The router's own map
        #: is what is *worth* watching; the two differ until `resync` runs.
        self._subscribed: set[int] = set()

    def build(self):
        from kiteconnect import KiteTicker

        ticker = KiteTicker(self.api_key, self.access_token)
        ticker.on_ticks = self._on_ticks
        ticker.on_connect = self._on_connect
        ticker.on_close = self._on_close
        ticker.on_error = self._on_error
        ticker.on_reconnect = self._on_reconnect
        return ticker

    def start(self, *, threaded: bool = True) -> None:
        self._ticker = self.build()
        logger.info("connecting to the tick feed", extra=fields(api_key=self.api_key[:4] + "..."))
        self._ticker.connect(threaded=threaded)

    def stop(self) -> None:
        if self._ticker is not None:
            self._ticker.close()
        self.router.health.connected = False

    # -- callbacks ---------------------------------------------------------

    def _on_connect(self, ws, response) -> None:
        self.router.health.connected = True
        self._attempt = 0
        tokens = self.router.refresh_subscriptions()

        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
        self._subscribed = set(tokens)

        logger.info("tick feed connected", extra=fields(subscribed=len(tokens)))

    def resync(self, ws=None) -> tuple[int, int]:
        """Subscribe to names that now have open paper orders; drop the rest.

        The ladder places through the day, and a name's first order can come
        long after the feed connected. Without this the feed watches only what
        was open when it connected, and every later name sits unfilled for
        ever. Returns (added, dropped).
        """
        ws = ws or self._ticker
        if ws is None or not self.router.health.connected:
            return 0, 0

        wanted = set(self.router.refresh_subscriptions())
        added = sorted(wanted - self._subscribed)
        dropped = sorted(self._subscribed - wanted)
        if added:
            ws.subscribe(added)
            ws.set_mode(ws.MODE_FULL, added)
        if dropped:
            ws.unsubscribe(dropped)
        self._subscribed = wanted

        if added or dropped:
            logger.info(
                "tick feed subscriptions changed",
                extra=fields(added=len(added), dropped=len(dropped), watching=len(wanted)),
            )
        return len(added), len(dropped)

    def _on_ticks(self, ws, ticks) -> None:
        try:
            filled = self.router.on_ticks(ticks)
        except Exception:
            logger.exception("a tick could not be applied")
            return

        if filled:
            logger.info("paper orders filled from the feed", extra=fields(filled=filled))

    def _on_close(self, ws, code, reason) -> None:
        self.router.health.connected = False
        logger.warning("tick feed closed", extra=fields(code=code, reason=str(reason)))

    def _on_error(self, ws, code, reason) -> None:
        self.router.health.last_error = f"{code}: {reason}"
        logger.error("tick feed error", extra=fields(code=code, reason=str(reason)))

    def _on_reconnect(self, ws, attempts) -> None:
        self._attempt = attempts
        self.router.health.reconnects = attempts
        logger.warning(
            "tick feed reconnecting",
            extra=fields(attempt=attempts, waiting=backoff_for(attempts)),
        )


_NEVER_CHECKED = object()


class Supervisor:
    """Keeps one feed connected to the current Kite session, and its pulse written.

    The access token is part of the websocket handshake and dies each morning,
    so a feed started once and left alone is a feed that goes deaf at 06:00
    and never says so. The supervisor asks which session is current, and
    when the answer changes -- a new day's login, a logout, a session that was
    never there -- it drops the old connection and makes the new one. Started
    before anybody has logged in, it waits rather than exiting: a process that
    exits is restarted by systemd five times and then left down all day.
    """

    def __init__(self, *, api_key: str, router: TickRouter | None = None, client_factory=None):
        self.api_key = api_key
        self.router = router or TickRouter()
        self.client_factory = client_factory or KiteTickerClient
        self.client = None
        #: The token the connection was made with. Starts as a sentinel, so
        #: the first look at the session always reports what it found.
        self._token: object = _NEVER_CHECKED

    @property
    def health(self) -> FeedHealth:
        return self.router.health

    def ensure_connected(self, *, now: datetime | None = None) -> str:
        """Follow the current session, and replace a connection gone deaf.

        Returns what happened, for the log.
        """
        from broker import session

        now = now or timezone.localtime()
        state = session.status()
        token = state.session.access_token if state.valid else None
        if token == self._token:
            if self.client is None or not self.health.is_deaf(now=now):
                return "unchanged"
            logger.warning(
                "tick feed deaf; reconnecting",
                extra=fields(
                    last_tick_at=str(self.health.last_tick_at),
                    subscribed=self.health.subscribed,
                ),
            )
            self._drop()
            self._connect(token, now=now)
            return "reconnecting"

        self._drop()
        self._token = token

        if token is None:
            self.health.last_error = f"no usable Kite session: {state.reason}"[:255]
            logger.warning(
                "tick feed waiting for a Kite session", extra=fields(reason=state.reason)
            )
            return "waiting"

        self.health.last_error = ""
        self._connect(token, now=now)
        return "connecting"

    def _connect(self, token: str, *, now: datetime) -> None:
        self.router.refresh_subscriptions()
        self.client = self.client_factory(
            api_key=self.api_key, access_token=token, router=self.router
        )
        self.health.listening_since = now
        self.client.start(threaded=True)

    def resync(self) -> None:
        if self.client is not None:
            self.client.resync()

    def beat(self) -> None:
        beat(self.health)

    def stop(self) -> None:
        self._drop()
        self.beat()

    def _drop(self) -> None:
        if self.client is not None:
            self.client.stop()
            self.client = None
        self.health.connected = False
