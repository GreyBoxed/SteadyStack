"""The sell ladder, which mirrors the buy (M33).

A batch's names each run to an even schedule from the moment the batch was
executed to the placement close -- the buy ladder's schedule, the direction
reversed. In each round a name with a sell resting places nothing; otherwise
it places what its schedule owes, priced at the ask and giving ground toward
the bid as it falls behind (`orders.schedule.sell_price`). A sell resting past
`order_ttl_minutes` is cancelled and its shares go back into the schedule.
Nothing is ever modified. From the fallback time the close cancels what rests
and sells each name's remainder at market.

A batch's orders are found by its `stream`, which is unique to the batch, and
never by sheet version: every order hangs off the day's latest sheet as it
was when it was placed, so a replan leaves a batch's orders spread across
versions.

Nothing here sells a name the day's latest plan is buying (D-S2): CNC nets a
same-day buy against a same-day sell, and the shares would never move.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from audit.events import record
from audit.logging import fields
from broker.base import Broker
from broker.exceptions import BrokerError
from configuration.store import current as current_configuration
from orders import ladder as buy_ladder
from orders import placement, schedule
from orders.models import Fill, NoPlaceReason, OrderSide, OrderType
from orders.state import TERMINAL
from rebalance.models import BatchState, RebalanceSell, RowState
from trading_calendar.sessions import session_for

logger = logging.getLogger("steadystack.rebalance")


@dataclass
class BatchTick:
    round_no: int
    placed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def placed_anything(self) -> bool:
        return bool(self.placed)


# -- a batch's orders --------------------------------------------------------


def _orders(batch: RebalanceSell, instrument=None):
    return placement.stream_orders(batch.trading_date, stream=batch.stream, instrument=instrument)


def _open(batch: RebalanceSell, instrument=None):
    return _orders(batch, instrument).exclude(state__in=list(TERMINAL))


def _committed(batch: RebalanceSell, instrument) -> int:
    """Sold, plus what still rests: what the name has already put up for sale."""
    return sum(
        order.filled_quantity if order.state in TERMINAL else order.quantity
        for order in _orders(batch, instrument)
    )


def sold_by_row(batch: RebalanceSell) -> dict[str, int]:
    """Shares sold so far, by symbol, from the fills -- the one count every
    reader of a batch's progress uses."""
    sold = dict(
        Fill.objects.filter(
            order__trading_date=batch.trading_date,
            order__stream=batch.stream,
            order__side=OrderSide.SELL,
        )
        .values("order__instrument")
        .annotate(total=Sum("quantity"))
        .values_list("order__instrument", "total")
    )
    return {row.instrument_id: sold.get(row.instrument_id, 0) for row in batch.rows.all()}


def _buying_today(batch: RebalanceSell) -> set[str]:
    """The names the day's plan buys (D-S2), by the rule execute checks too."""
    from rebalance.batches import names_bought_today

    return names_bought_today(batch.trading_date)


# -- a round -----------------------------------------------------------------


@dataclass
class _Due:
    row: object
    book: buy_ladder.Book
    last_price: object
    quantity: int
    behind: float


def run_tick(
    *,
    batch: RebalanceSell,
    quotes: dict[str, dict],
    broker: Broker,
    moment: datetime | None = None,
    room: int | None = None,
) -> BatchTick:
    """One round of the sell ladder for one batch.

    *quotes* are Kite quotes by symbol (depth and last price), as the buy
    round takes them; *room* is how many orders the rate limit leaves this
    batch, or None for no limit.
    """
    moment = moment or timezone.localtime()
    config = current_configuration()
    outcome = BatchTick(round_no=buy_ladder.round_number(moment, config))

    if batch.state != BatchState.RUNNING:
        outcome.skipped["*"] = f"the batch is {batch.get_state_display().lower()}"
        return outcome
    if not session_for(batch.trading_date).covers(moment.time()):
        outcome.skipped["*"] = NoPlaceReason.OUTSIDE_SESSION
        return outcome

    start, close = _window(batch, moment, config)
    if moment >= close:
        outcome.skipped["*"] = "the placement window has closed"
        return outcome

    interval = max(1, config.placer_interval_seconds)
    ahead = schedule.progress(moment=moment, start=start, close=close, lookahead=interval)
    last_round = moment + timedelta(seconds=interval) >= close
    buying = _buying_today(batch)
    sold = sold_by_row(batch)
    due: list[_Due] = []

    for row in batch.rows.select_related("instrument"):
        symbol, instrument = row.instrument_id, row.instrument

        if symbol in buying:
            outcome.skipped[symbol] = "the day's plan buys this name; it is not sold today"
            continue

        try:
            expired = (
                0
                if last_round
                else buy_ladder.expire(
                    _open(batch, instrument), moment=moment, config=config, broker=broker
                )
            )
        except BrokerError as failure:
            outcome.skipped[symbol] = f"a stale sell did not cancel: {failure}"
            continue
        if expired or _open(batch, instrument).exists():
            outcome.skipped[symbol] = NoPlaceReason.RESTING
            continue

        committed = _committed(batch, instrument)
        unsold = row.quantity - committed
        if unsold <= 0:
            outcome.skipped[symbol] = NoPlaceReason.FULLY_COMMITTED
            continue

        owed = schedule.scheduled(row.quantity, ahead) - committed
        if owed <= 0:
            outcome.skipped[symbol] = NoPlaceReason.ON_SCHEDULE
            continue

        book, last_price = buy_ladder.quote_parts(quotes.get(symbol) or {})
        if not book.is_usable:
            outcome.skipped[symbol] = (
                NoPlaceReason.LOCKED_BOOK if (book.bid or book.ask) else NoPlaceReason.NO_DEPTH
            )
            continue

        due.append(
            _Due(
                row=row,
                book=book,
                last_price=last_price,
                quantity=min(unsold, max(config.slice_quantity, owed)),
                behind=schedule.seconds_behind(
                    quantity=row.quantity,
                    filled=sold.get(symbol, 0),
                    moment=moment,
                    start=start,
                    close=close,
                ),
            )
        )

    due.sort(key=lambda d: (-d.behind, d.row.instrument_id))
    for index, item in enumerate(due):
        symbol = item.row.instrument_id
        if room is not None and index >= room:
            outcome.skipped[symbol] = NoPlaceReason.WAITING_TURN
            continue
        price = schedule.sell_price(
            bid=item.book.bid,
            ask=item.book.ask,
            urgency=schedule.urgency(
                behind_seconds=item.behind, catch_up_minutes=config.catch_up_minutes
            ),
            tick_size=item.row.instrument.tick_size,
            last_price=item.last_price,
        )
        failure = _place(
            batch,
            item.row.instrument,
            round_no=outcome.round_no,
            quantity=item.quantity,
            broker=broker,
            order_type=OrderType.LIMIT,
            limit_price=price,
        )
        if failure is None:
            outcome.placed.append(symbol)
        else:
            outcome.skipped[symbol] = failure

    logger.info(
        "rebalance round",
        extra=fields(
            batch=str(batch.id),
            round_no=outcome.round_no,
            placed=outcome.placed,
            skipped=list(outcome.skipped),
        ),
    )
    return outcome


def _window(batch: RebalanceSell, moment: datetime, config) -> tuple[datetime, datetime]:
    """From the later of the window's open and the moment the batch was
    executed, to the placement close -- as the buy's schedule starts at the
    later of the open and its authorisation."""
    tz = moment.tzinfo
    opens = datetime.combine(moment.date(), config.tradesheet_time, tzinfo=tz)
    close = datetime.combine(moment.date(), config.placement_window_close, tzinfo=tz)
    start = opens
    if batch.started_at is not None:
        start = max(opens, timezone.localtime(batch.started_at, tz))
    return min(start, close), close


def _place(batch, instrument, *, round_no, quantity, broker, order_type, limit_price=None):
    """Place one sell for the batch, or say why not."""
    config = current_configuration()
    outcome = placement.place(
        tradesheet=_tradesheet_for(batch),
        instrument=instrument,
        tick_no=round_no,
        quantity=quantity,
        broker=broker,
        order_type=order_type,
        limit_price=limit_price,
        side=OrderSide.SELL,
        stream=batch.stream,
        is_fallback=order_type == OrderType.MARKET,
        market_protection=config.fallback_band_pct if order_type == OrderType.MARKET else None,
    )
    if outcome.failure is not None:
        return str(outcome.failure)[:120]
    return None


def _tradesheet_for(batch: RebalanceSell):
    """A sell order still hangs off a tradesheet, because every order does:
    the day's latest. A batch is only executed once the day has one (`start`)."""
    from engine.lock import latest_sheet

    sheet = latest_sheet(batch.trading_date)
    if sheet is None:
        raise BrokerError("There is no tradesheet for the day to record the sale against.")
    return sheet


# -- the close ---------------------------------------------------------------


def close_round(
    *,
    batch: RebalanceSell,
    quotes: dict[str, dict],
    broker: Broker,
    moment: datetime | None = None,
    room: int | None = None,
) -> BatchTick:
    """One close round: cancel what rests, then sell each remainder at market.

    Each name's remainder goes out once, as one MARKET sell; a later round
    that finds it resting does nothing more. A resting sell that will not
    confirm cancelled leaves its name alone this round -- sizing a market
    order against shares that might still sell would sell them twice.
    """
    from orders.fallback import round_number

    moment = moment or timezone.localtime()
    config = current_configuration()
    outcome = BatchTick(round_no=round_number(moment, config))

    if batch.state != BatchState.RUNNING:
        outcome.skipped["*"] = f"the batch is {batch.get_state_display().lower()}"
        return outcome

    buying = _buying_today(batch)
    placed = 0

    for row in batch.rows.select_related("instrument"):
        symbol, instrument = row.instrument_id, row.instrument
        if symbol in buying:
            outcome.skipped[symbol] = "the day's plan buys this name; it is not sold today"
            continue

        resting = list(_open(batch, instrument))
        if any(order.is_fallback for order in resting):
            outcome.skipped[symbol] = NoPlaceReason.RESTING
            continue
        try:
            for order in resting:
                placement.cancel(order, broker=broker)
        except BrokerError as failure:
            outcome.skipped[symbol] = f"a resting sell did not cancel: {failure}"
            continue

        remainder = row.quantity - _committed(batch, instrument)
        if remainder <= 0:
            outcome.skipped[symbol] = NoPlaceReason.FULLY_COMMITTED
            continue
        if room is not None and placed >= room:
            outcome.skipped[symbol] = NoPlaceReason.WAITING_TURN
            continue

        failure = _place(
            batch,
            instrument,
            round_no=outcome.round_no,
            quantity=remainder,
            broker=broker,
            order_type=OrderType.MARKET,
        )
        if failure is None:
            outcome.placed.append(symbol)
            placed += 1
        else:
            outcome.skipped[symbol] = failure

    logger.info(
        "rebalance close round",
        extra=fields(batch=str(batch.id), placed=outcome.placed, skipped=list(outcome.skipped)),
    )
    return outcome


# -- closing a batch ---------------------------------------------------------


@transaction.atomic
def close_batch(batch: RebalanceSell, *, reason: str, actor: str = "system") -> RebalanceSell:
    """Take a batch out of RUNNING, its state decided by what filled.

    Every sold row counted from the fills, then COMPLETE when every row sold
    in full, PARTIAL when something sold, CANCELLED when nothing did. The
    caller has already cancelled what rested; a batch never outlives its day.
    """
    locked = RebalanceSell.objects.select_for_update().get(pk=batch.pk)
    sold = sold_by_row(locked)

    for row in locked.rows.all():
        row.sold_quantity = min(sold.get(row.instrument_id, 0), row.quantity)
        row.validation_state = (
            RowState.FILLED
            if row.sold_quantity >= row.quantity
            else (RowState.PARTIAL if row.sold_quantity else RowState.UNSOLD)
        )
        row.save(update_fields=["sold_quantity", "validation_state"])

    rows = list(locked.rows.all())
    before = locked.state
    if rows and all(row.sold_quantity >= row.quantity for row in rows):
        locked.state = BatchState.COMPLETE
    elif any(row.sold_quantity for row in rows):
        locked.state = BatchState.PARTIAL
    else:
        locked.state = BatchState.CANCELLED
    locked.cancelled_reason = locked.cancelled_reason or (
        reason if locked.state != BatchState.COMPLETE else ""
    )
    locked.finished_at = locked.finished_at or timezone.now()
    locked.save(update_fields=["state", "cancelled_reason", "finished_at"])

    record(
        "rebalance.closed",
        actor=actor,
        entity=locked,
        before={"state": before},
        after={"state": locked.state, "reason": reason, "sold": sold},
    )
    _reinvest(locked)
    return locked


def _reinvest(batch: RebalanceSell) -> None:
    """Give what the batch sold to the engine, net of the sells' charges (D-S1).

    The journal's `reinvest` allocation, one per trade date, in each book the
    batch traded in; nothing when the batch opted out. Idempotent: a second
    close posts nothing new.
    """
    from journal.trades import reinvest
    from orders.models import Order

    for mode in sorted(
        set(Order.objects.filter(stream=batch.stream).values_list("mode", flat=True))
    ):
        reinvest(batch, book=mode)


def close_batches(trading_date) -> list[RebalanceSell]:
    """Close every batch still RUNNING on *trading_date* (day end)."""
    return [
        close_batch(batch, reason="day ended")
        for batch in RebalanceSell.objects.filter(
            trading_date=trading_date, state=BatchState.RUNNING
        )
    ]
