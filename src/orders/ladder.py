"""Placing the daily buy, in rounds.

Every `placer_interval_seconds` a round looks at each name the day buys and
places a *new* limit order for any that has fallen behind its schedule
(`orders.schedule`): an even spread of the name's quantity across the
placement window. Orders are never modified (M21). One that rests unfilled
past `order_ttl_minutes` is cancelled and its shares rescheduled, so the day
never piles up orders at prices the market has left.

Three rules do the work:

  * **the schedule spreads the day** (M11a). A name should have filled the
    fraction of its quantity that the window has run; what it owes, it
    places. A small day places a share now and then; a large one places
    more each round;
  * **how far behind sets the price** (D10). On schedule, the bid; behind,
    toward the ask in proportion, reaching it `catch_up_minutes` behind;
    never outside the exchange's order-to-trade exemption band;
  * **the rate limit is shared** (M11b). A round places at most what the
    breaker's window has room for, most-behind names first, so the system
    as a whole stays within `max_orders_per_window` and no name starves.

A name whose answer changes -- resting, on schedule, waiting its turn,
placed -- writes a row. A day whose record has gaps cannot answer "why did
nothing happen at 11:05?", and that is asked exactly when something has gone
wrong.

The rung helpers further down (`tick_number`, `ticks_remaining`,
`slice_for`) are the rebalance sell ladder's, which still steps on a clock
of its own (M33).
"""

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal

from django.utils import timezone

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from broker.base import Broker
from broker.exceptions import BrokerError, LiveOrdersRefused
from configuration.models import ConfigurationVersion
from configuration.store import current as current_configuration
from engine.models import TradeSheet, TradeSheetState
from orders import overrides, placement, schedule
from orders.models import DAILY_BUY_STREAM, NoPlaceReason, Order, OrderType, Tick
from orders.state import OrderState
from trading_calendar.sessions import session_for

logger = logging.getLogger("steadystack.orders")

#: How many rejections in a row end the day (§11). A rejection is ordinary --
#: a price band, a margin shortfall -- and the ladder carries on through one.
#: A run of them is a condition that will not clear by five o'clock, and
#: re-alerting every five minutes until the close teaches the operator to
#: ignore the alert. Three, as the PRD states it; not configuration, because
#: nothing about the number is an operating preference.
HALT_AFTER_REJECTIONS = 3

#: States in which a day may still place, and so may still be halted. A day
#: that was skipped or has ended keeps the state that explains it.
STILL_BUYING = frozenset(
    {TradeSheetState.DRAFT, TradeSheetState.AWAITING_SESSION, TradeSheetState.RUNNING}
)


@dataclass(frozen=True)
class Book:
    """The top of the order book, as one tick read it."""

    bid: Decimal
    ask: Decimal
    snapshot: dict
    #: The day's volume-weighted average price, when the quote carried one.
    vwap: Decimal | None = None

    @property
    def is_usable(self) -> bool:
        """A locked or one-sided book is not a book to price against.

        Bid equal to ask, either side empty, or an instrument frozen in a
        circuit: the tick records what it saw and places nothing (M20a).
        """
        return self.bid > 0 and self.ask > 0 and self.bid < self.ask


def book_from_depth(depth: dict) -> Book:
    """Read the top of book out of a Kite depth payload."""
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []

    bid = Decimal(str(buys[0].get("price", 0))) if buys else Decimal("0")
    ask = Decimal(str(sells[0].get("price", 0))) if sells else Decimal("0")

    return Book(bid=bid, ask=ask, snapshot={"buy": buys[:5], "sell": sells[:5]})


def tick_number(moment: datetime, config: ConfigurationVersion) -> int:
    """Which rung the clock says we are on.

    Tick 0 is the tradesheet time. Derived from the clock so that a ladder
    resuming after an outage prices where the day has got to, rather than
    starting again at the bid.
    """
    start = datetime.combine(moment.date(), config.tradesheet_time, tzinfo=moment.tzinfo)
    if moment < start:
        return 0

    elapsed = moment - start
    return int(elapsed.total_seconds() // (config.ladder_interval_minutes * 60))


def ticks_remaining(moment: datetime, config: ConfigurationVersion) -> int:
    """How many placing ticks are left before the window closes (M11a).

    At least one: the current tick always counts, so a day that starts late
    still places rather than dividing by zero.
    """
    close = datetime.combine(moment.date(), config.placement_window_close, tzinfo=moment.tzinfo)
    if moment >= close:
        return 0

    interval = timedelta(minutes=config.ladder_interval_minutes)
    return max(1, int((close - moment) / interval))


def slice_for(*, uncommitted: int, remaining_ticks: int, config: ConfigurationVersion) -> int:
    """How many shares this tick places (M11a).

    The remainder rather than a rounded-up slice when the day is nearly done:
    rounding up at the end is how a day buys one more share than it decided
    to.
    """
    if uncommitted <= 0:
        return 0

    spread = -(-uncommitted // max(1, remaining_ticks))  # ceil
    return min(uncommitted, max(config.slice_quantity, spread))


@dataclass(frozen=True)
class TickOutcome:
    tick: Tick | None
    placed: bool
    reason: str = ""
    symbol: str = ""


@dataclass
class _Due:
    """A name the schedule says should place this round."""

    line: object
    book: Book
    last_price: Decimal | None
    quantity: int
    committed: int
    behind: float
    due_fraction: float


def _record_tick(
    *,
    tradesheet: TradeSheet,
    tick_no: int,
    stream: str,
    book: Book | None,
    committed: int,
    slice_qty: int = 0,
    order=None,
    reason: str = "",
    detail: str = "",
    instrument=None,
) -> Tick:
    tick, _created = Tick.objects.get_or_create(
        trading_date=tradesheet.trading_date,
        stream=stream,
        tick_no=tick_no,
        instrument=instrument,
        defaults={
            "tradesheet": tradesheet,
            "trace_id": current_trace_id(),
            "best_bid": book.bid if book else None,
            "best_ask": book.ask if book else None,
            "vwap": book.vwap if book else None,
            "depth_snapshot": book.snapshot if book else {},
            "slice_qty": slice_qty,
            "committed_before": committed,
            "order": order,
            "no_place_reason": reason,
            "detail": detail[:255],
        },
    )
    return tick


def round_number(moment: datetime, config: ConfigurationVersion) -> int:
    """Which placing round the clock says this is. Round 0 opens the window.

    From the clock, not from a count, for the same reason the ladder's rung
    was: a day that resumes after an outage places where the day has got to.
    """
    opens = datetime.combine(moment.date(), config.tradesheet_time, tzinfo=moment.tzinfo)
    if moment < opens:
        return 0
    return int((moment - opens).total_seconds() // max(1, config.placer_interval_seconds))


def missed_rounds(
    *, start: datetime, moment: datetime, config: ConfigurationVersion, rounds_run: set[int]
) -> list[tuple[datetime, datetime]]:
    """The stretches since *start* when no placing round ran, oldest first.

    A round that ran left a claim; one that did not -- the program down, the
    laptop shut, no Kite session, the system paused -- left none. Those are
    what the schedule re-spreads over, rather than asking for their shares in
    one burst at the ask. A round that ran and got no fill is not here: the
    market not filling is being behind, and is chased. The round now running
    is not counted either way.
    """
    interval = timedelta(seconds=max(1, config.placer_interval_seconds))
    opens = datetime.combine(moment.date(), config.tradesheet_time, tzinfo=moment.tzinfo)
    gaps: list[tuple[datetime, datetime]] = []
    for n in range(round_number(start, config), round_number(moment, config)):
        if n in rounds_run:
            continue
        since = max(start, opens + n * interval)
        until = min(moment, opens + (n + 1) * interval)
        if gaps and gaps[-1][1] == since:
            gaps[-1] = (gaps[-1][0], until)
        else:
            gaps.append((since, until))
    return gaps


def schedule_window(
    tradesheet: TradeSheet, moment: datetime, config: ConfigurationVersion
) -> tuple[datetime, datetime]:
    """Where a name's schedule starts and ends.

    It starts at the window's open, or at the authorization if that came
    later: a day authorized at 11:20 spreads what it has over what is left,
    instead of arriving two hours behind and paying the ask for everything.
    """
    tz = moment.tzinfo
    opens = datetime.combine(moment.date(), config.tradesheet_time, tzinfo=tz)
    close = datetime.combine(moment.date(), config.placement_window_close, tzinfo=tz)
    start = opens
    if tradesheet.authorized_at is not None:
        start = max(opens, timezone.localtime(tradesheet.authorized_at, tz))
    return min(start, close), close


def quote_parts(quote: dict) -> tuple[Book, Decimal | None]:
    """The book and the last traded price out of one Kite quote."""
    book = book_from_depth(quote.get("depth") or {})
    vwap = quote.get("average_price")
    if vwap:
        book = replace(book, vwap=Decimal(str(vwap)))
    last = quote.get("last_price")
    return book, (Decimal(str(last)) if last else None)


def _note(
    *,
    tradesheet: TradeSheet,
    tick_no: int,
    stream: str,
    instrument,
    reason: str,
    book: Book | None = None,
    committed: int = 0,
    detail: str = "",
) -> Tick | None:
    """Record why a name placed nothing -- once per change, not once per round.

    A round runs every thirty seconds and most of them find most names
    resting or on schedule. A row for each would be eight thousand a day that
    all say the same thing; a row whenever the answer changes keeps every
    "why did nothing happen at 11:05?" answerable -- the row in force then --
    without burying the rows that matter.
    """
    last = (
        Tick.objects.filter(tradesheet=tradesheet, stream=stream, instrument=instrument)
        .order_by("-tick_no")
        .first()
    )
    if last is not None and last.order_id is None and last.no_place_reason == reason:
        return None
    return _record_tick(
        tradesheet=tradesheet,
        tick_no=tick_no,
        stream=stream,
        book=book,
        committed=committed,
        reason=reason,
        detail=detail,
        instrument=instrument,
    )


def expire_stale(
    tradesheet: TradeSheet,
    *,
    instrument,
    moment: datetime,
    config: ConfigurationVersion,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
) -> int:
    """Cancel this name's buys that have rested unfilled past `order_ttl_minutes`.

    An order is never modified (M21). One that has sat at a price the market
    walked away from is cancelled instead, and its unfilled shares go back
    into the schedule, to be placed again at what the book says now. The
    alternative is a day that piles up resting orders at stale prices and
    buys them all at market at 15:00.

    Raises `BrokerError` if a cancellation does not confirm; the caller must
    not size anything for this name while an order might still be resting.
    """
    return expire(
        placement.open_orders(tradesheet, stream=stream, instrument=instrument),
        moment=moment,
        config=config,
        broker=broker,
    )


def expire(orders, *, moment: datetime, config: ConfigurationVersion, broker: Broker) -> int:
    """Cancel those of *orders* resting past `order_ttl_minutes`; either side.

    The rule `expire_stale` applies to a sheet's buys, over any set of open
    orders -- a rebalance batch's sells are found by their stream, not by a
    sheet version. A close order (`is_fallback`) is never expired.
    """
    if config.order_ttl_minutes <= 0:
        return 0
    cutoff = moment - timedelta(minutes=config.order_ttl_minutes)
    stale = [order for order in orders if not order.is_fallback and order.placed_at <= cutoff]
    for order in stale:
        placement.cancel(order, broker=broker)
        record(
            "order.expired",
            entity=order,
            after={"tag": order.tag, "ttl_minutes": config.order_ttl_minutes},
        )
    return len(stale)


def run_round(
    *,
    tradesheet: TradeSheet,
    quotes: dict,
    broker: Broker,
    moment: datetime | None = None,
    stream: str = DAILY_BUY_STREAM,
    rounds_run: set[int] | None = None,
) -> list[TickOutcome]:
    """One placing round across every name the day buys (M11a, M15).

    `rounds_run` is the round numbers that have claimed a run today. Rounds
    missing from it are an outage, and the schedule re-spreads what is left
    over what is left of the window. Left out, every round is taken to have
    run.

    `quotes` is keyed by symbol, each a Kite quote with `depth` and
    `last_price`. For each name the round asks, in order: are its old orders
    stale; is one still resting; is anything due on its schedule; is its book
    usable. What is due is then placed most-behind first, as many as the rate
    limit has room for in its window -- the rest wait a round, and being
    further behind by then, go first next time. A stable order would starve
    the tail of a long list without ever saying so.

    The only failures that propagate are the engine refusing a live placement
    and the rate breaker: both mean the program, not the market, is wrong.
    """
    moment = moment or timezone.localtime()
    config = current_configuration()
    tick_no = round_number(moment, config)
    lines = tradesheet.buy_lines()

    def whole_day(reason: str, detail: str = "") -> list[TickOutcome]:
        tick = _record_tick(
            tradesheet=tradesheet,
            tick_no=tick_no,
            stream=stream,
            book=None,
            committed=0,
            reason=reason,
            detail=detail,
            instrument=None,
        )
        logger.info(
            "round placed nothing",
            extra=fields(
                trading_date=str(tradesheet.trading_date),
                tick_no=tick_no,
                reason=reason,
                detail=detail,
            ),
        )
        return [TickOutcome(tick=tick, placed=False, reason=reason)]

    if tradesheet.state in STILL_BUYING:
        # A refusal that came after its order was acknowledged -- an id from
        # Kite, then REJECTED in the book -- arrives through reconciliation,
        # not through a placement. Counted here, it halts the day just as a
        # refusal on the spot would.
        halt_if_refused_repeatedly(tradesheet, stream=stream)

    if tradesheet.state == TradeSheetState.SKIPPED:
        return whole_day(NoPlaceReason.DAY_SKIPPED, tradesheet.skip_reason)
    if tradesheet.state == TradeSheetState.HALTED:
        # Halted by the broker's refusals, or stopped by the operator.
        return whole_day(
            NoPlaceReason.DAY_HALTED,
            tradesheet.get_skip_reason_display()
            if tradesheet.skip_reason
            else f"{HALT_AFTER_REJECTIONS} rejections in a row",
        )
    if tradesheet.state == TradeSheetState.AWAITING_SESSION:
        return whole_day(NoPlaceReason.NO_SESSION)
    if stream == DAILY_BUY_STREAM and not tradesheet.is_authorized:
        # M15f: the plan is automatic, placing it is not.
        return whole_day(NoPlaceReason.NOT_AUTHORIZED)

    session = session_for(tradesheet.trading_date)
    if not session.covers(moment.time()):
        return whole_day(NoPlaceReason.OUTSIDE_SESSION, str(session.kind))

    start, close = schedule_window(tradesheet, moment, config)
    if moment >= close:
        # The fallback owns what is left.
        return whole_day(NoPlaceReason.OUTSIDE_SESSION, "placement window closed")

    if overrides.is_excluded(tradesheet, tick_no):
        return whole_day(NoPlaceReason.EXCLUDED)

    if not lines:
        return whole_day(NoPlaceReason.FULLY_COMMITTED, "the day buys nothing")

    outcomes: list[TickOutcome] = []
    due: list[_Due] = []
    interval = max(1, config.placer_interval_seconds)
    gaps = (
        missed_rounds(start=start, moment=moment, config=config, rounds_run=rounds_run)
        if rounds_run is not None
        else []
    )
    ahead = schedule.progress(
        moment=moment, start=start, close=close, lookahead=interval, gaps=gaps
    )

    # The last round cancels nothing: its replacement would have no round to
    # go out in. What rests then rests until the close, as the window intends.
    last_round = moment + timedelta(seconds=interval) >= close

    for line in lines:
        instrument = line.instrument
        symbol = instrument.tradingsymbol

        def nothing(reason, *, book=None, committed=0, detail="", _i=instrument, _s=symbol):
            tick = _note(
                tradesheet=tradesheet,
                tick_no=tick_no,
                stream=stream,
                instrument=_i,
                reason=reason,
                book=book,
                committed=committed,
                detail=detail,
            )
            outcomes.append(TickOutcome(tick=tick, placed=False, reason=reason, symbol=_s))

        try:
            expired = (
                0
                if last_round
                else expire_stale(
                    tradesheet,
                    instrument=instrument,
                    moment=moment,
                    config=config,
                    broker=broker,
                    stream=stream,
                )
            )
        except BrokerError as failure:
            nothing(NoPlaceReason.FAILED, detail=f"a stale order did not cancel: {failure}")
            continue

        committed = placement.committed_quantity(tradesheet, stream=stream, instrument=instrument)
        if expired:
            # A cancel can race a fill. The next round sizes from what the
            # broker has confirmed by then, not from what this one believes.
            nothing(
                NoPlaceReason.RESTING,
                committed=committed,
                detail=f"cancelled {expired} stale; replaced next round",
            )
            continue

        if placement.open_orders(tradesheet, stream=stream, instrument=instrument).exists():
            nothing(NoPlaceReason.RESTING, committed=committed)
            continue

        uncommitted = line.quantity - committed
        if uncommitted <= 0:
            # No row: the name's own orders already say it is done, and a
            # row per finished name per round is noise that buries the rest.
            outcomes.append(
                TickOutcome(
                    tick=None, placed=False, reason=NoPlaceReason.FULLY_COMMITTED, symbol=symbol
                )
            )
            continue

        owed = schedule.scheduled(line.quantity, ahead) - committed
        if owed <= 0:
            nothing(NoPlaceReason.ON_SCHEDULE, committed=committed)
            continue

        book, last_price = quote_parts(quotes.get(symbol) or {})
        if not book.is_usable:
            nothing(
                NoPlaceReason.LOCKED_BOOK if book.bid or book.ask else NoPlaceReason.NO_DEPTH,
                book=book,
                committed=committed,
                detail=f"bid {book.bid} ask {book.ask}",
            )
            continue

        filled = placement.filled_quantity(tradesheet, stream=stream, instrument=instrument)
        due.append(
            _Due(
                line=line,
                book=book,
                last_price=last_price,
                quantity=min(uncommitted, max(config.slice_quantity, owed)),
                committed=committed,
                behind=schedule.seconds_behind(
                    quantity=line.quantity,
                    filled=filled,
                    moment=moment,
                    start=start,
                    close=close,
                    gaps=gaps,
                ),
                due_fraction=owed / line.quantity,
            )
        )

    # Most behind first; then most owed; then by name, so a tie is stable.
    due.sort(key=lambda d: (-d.behind, -d.due_fraction, d.line.instrument.tradingsymbol))
    room = _room(config)

    for index, item in enumerate(due):
        if room is not None and index >= room:
            tick = _note(
                tradesheet=tradesheet,
                tick_no=tick_no,
                stream=stream,
                instrument=item.line.instrument,
                reason=NoPlaceReason.WAITING_TURN,
                book=item.book,
                committed=item.committed,
                detail=(
                    f"{config.max_orders_per_window} orders per "
                    f"{config.order_rate_window_seconds}s already used"
                ),
            )
            outcomes.append(
                TickOutcome(
                    tick=tick,
                    placed=False,
                    reason=NoPlaceReason.WAITING_TURN,
                    symbol=item.line.instrument.tradingsymbol,
                )
            )
            continue
        outcomes.append(
            _place(
                tradesheet=tradesheet,
                item=item,
                tick_no=tick_no,
                stream=stream,
                broker=broker,
                config=config,
            )
        )

    return outcomes


def _room(config: ConfigurationVersion) -> int | None:
    """How many orders the rate limit leaves for this round (`placement.room`)."""
    return placement.room(config)


def _place(
    *,
    tradesheet: TradeSheet,
    item: _Due,
    tick_no: int,
    stream: str,
    broker: Broker,
    config: ConfigurationVersion,
) -> TickOutcome:
    instrument = item.line.instrument
    symbol = instrument.tradingsymbol
    price = schedule.buy_price(
        bid=item.book.bid,
        ask=item.book.ask,
        urgency=schedule.urgency(
            behind_seconds=item.behind, catch_up_minutes=config.catch_up_minutes
        ),
        tick_size=instrument.tick_size,
        last_price=item.last_price,
    )

    def row(**extra) -> Tick:
        return _record_tick(
            tradesheet=tradesheet,
            tick_no=tick_no,
            stream=stream,
            book=item.book,
            committed=item.committed,
            instrument=instrument,
            **extra,
        )

    try:
        outcome = placement.place(
            tradesheet=tradesheet,
            instrument=instrument,
            tick_no=tick_no,
            quantity=item.quantity,
            broker=broker,
            order_type=OrderType.LIMIT,
            limit_price=price,
            stream=stream,
        )
    except LiveOrdersRefused as refusal:
        row(reason=NoPlaceReason.LIVE_REFUSED, detail=str(refusal))
        raise
    except placement.RateBreakerTripped as breaker:
        # The row is written -- "the breaker tripped at 11:05" is the line
        # somebody will look for -- and then it propagates, because a rate
        # this system cannot explain is not something to carry on through.
        row(reason=NoPlaceReason.RATE_BREAKER, detail=str(breaker))
        raise

    if outcome.failure is not None:
        tick = row(
            slice_qty=item.quantity,
            order=outcome.order,
            reason=NoPlaceReason.REJECTED
            if isinstance(outcome.failure, BrokerError) and outcome.sent
            else NoPlaceReason.FAILED,
            detail=str(outcome.failure),
        )
        halt_if_refused_repeatedly(tradesheet, stream=stream)
        return TickOutcome(tick=tick, placed=False, reason=str(outcome.failure), symbol=symbol)

    tick = row(slice_qty=item.quantity, order=outcome.order)

    if tradesheet.state == TradeSheetState.DRAFT:
        tradesheet.state = TradeSheetState.RUNNING
        tradesheet.save(update_fields=["state"])

    logger.info(
        "round placed",
        extra=fields(
            trading_date=str(tradesheet.trading_date),
            tick_no=tick_no,
            symbol=symbol,
            quantity=item.quantity,
            price=str(price),
            bid=str(item.book.bid),
            ask=str(item.book.ask),
            behind_seconds=round(item.behind),
            committed=item.committed,
        ),
    )
    return TickOutcome(tick=tick, placed=True, symbol=symbol)


def consecutive_rejections(tradesheet: TradeSheet, *, stream: str = DAILY_BUY_STREAM) -> int:
    """How many of the day's most recent orders were refused, in a row.

    Counted back from the newest order the day placed, across every name: the
    conditions that produce a run of rejections -- no margin, a frozen
    instrument, a session the broker has stopped honouring -- are conditions
    about the day, not about one symbol. An order that went through ends the
    run, because the thing that was wrong evidently is not any more.
    """
    recent = Order.objects.filter(tradesheet=tradesheet, stream=stream).order_by(
        "-placed_at", "-tick_no"
    )

    run = 0
    for order in recent:
        if order.state != OrderState.REJECTED:
            break
        run += 1
    return run


def halt_if_refused_repeatedly(tradesheet: TradeSheet, *, stream: str = DAILY_BUY_STREAM) -> bool:
    """End the day once the broker has refused it `HALT_AFTER_REJECTIONS` times
    running (§11).

    The alternative is a day that keeps placing into the same refusal every
    five minutes until the close, raising the same alert each time. The state
    is what stops it: the ladder will not place into a halted day, and the
    fallback will not buy one at market either.
    """
    if tradesheet.state == TradeSheetState.HALTED:
        return True
    if consecutive_rejections(tradesheet, stream=stream) < HALT_AFTER_REJECTIONS:
        return False

    before = tradesheet.state
    tradesheet.state = TradeSheetState.HALTED
    tradesheet.save(update_fields=["state"])

    record(
        "tradesheet.halted",
        entity=tradesheet,
        before={"state": before},
        after={
            "state": TradeSheetState.HALTED,
            "rejections": HALT_AFTER_REJECTIONS,
            "stream": stream,
        },
    )
    logger.error(
        "the day is halted: the broker refused it repeatedly",
        extra=fields(
            trading_date=str(tradesheet.trading_date),
            rejections=HALT_AFTER_REJECTIONS,
            stream=stream,
        ),
    )
    return True


def ladder_times(trading_date: date, config: ConfigurationVersion) -> list[time]:
    """Every moment a placing round is due, for the schedule and for the pages."""
    moments = []
    cursor = datetime.combine(trading_date, config.tradesheet_time)
    close = datetime.combine(trading_date, config.placement_window_close)
    step = timedelta(seconds=max(1, config.placer_interval_seconds))

    while cursor < close:
        moments.append(cursor.time())
        cursor += step

    return moments


def floor_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    """Exposed for the fallback's sanity band, which floors the same way."""
    if tick_size <= 0:
        return value
    ticks = (value / tick_size).to_integral_value(rounding=ROUND_FLOOR)
    return (ticks * tick_size).quantize(tick_size)
