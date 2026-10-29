"""The close: cancel what the ladder left resting, then buy the rest at market, paced.

From `market_fallback_time` a round runs every `placer_interval_seconds` until
`close_window_end`. The first cancels every resting limit order and waits for
**every** cancellation to be confirmed, then re-reads the fills (D8): sizing
from the pre-cancel snapshot over-buys whenever an order fills in the moments
before its cancel lands, which is exactly when it is most likely to.

Each round then buys a share of each name's remainder at market -- the
remainder divided by the rounds left -- so a name finishes across the close
rather than in one print at 15:00. For most names the exchange's closing
price is the volume-weighted price of the last half hour; buying across it
tracks it. The rounds share the rate limit with everything else, most-behind
name first, so a fifteen-name day never needs more than the breaker allows.

The market order is the one price nothing bounds, so it is bounded twice over.
Before placement, by the sanity band: if the last traded price has moved
further from the tradesheet's reference than the configured percentage, that
name is withheld -- the day ends PARTIAL or UNFILLED, the unspent amount
carries, and an alert is raised (M22a). At the exchange, by market protection
set to the same band; since April 2026 a market order without it is rejected.

The occasional unfilled day is what the band costs. It is cheaper than a day's
cash spent into a circuit move.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from broker.base import Broker
from broker.exceptions import BrokerError
from configuration.store import current as current_configuration
from engine.models import SkipReason, TradeSheet, TradeSheetState
from orders import ladder, placement, schedule
from orders.models import DAILY_BUY_STREAM, NoPlaceReason, Order, OrderType, Tick
from orders.state import TERMINAL

logger = logging.getLogger("steadystack.orders")

HUNDRED = Decimal("100")

#: The close's first round number. Far above any placing round (a day of
#: thirty-second rounds ends near 700), so each close round -- 999, 1000, ...
#: -- is unmistakable in the day's record and cannot collide with one.
FALLBACK_TICK = 999

#: States that mean the day has already stopped buying, and the 15:00 market
#: order must not restart it. SKIPPED is the operator's decision (M15a) or a
#: closed market; HALTED is the broker refusing this day's orders three times
#: over, after which an unbounded market order is the worst thing available.
STOPPED = frozenset({TradeSheetState.SKIPPED, TradeSheetState.HALTED})


class CancellationsUnconfirmed(Exception):
    """Some cancellations did not confirm, so nothing may be sized yet.

    Raised rather than worked around: an order believed cancelled that is in
    fact resting is a share bought twice, once by it and once by the market
    order sized as though it were gone.
    """

    def __init__(self, tags: list[str]) -> None:
        super().__init__(
            "These orders were not confirmed cancelled, so the market order was "
            f"not sized: {', '.join(tags)}."
        )
        self.tags = tags


@dataclass(frozen=True)
class FallbackOutcome:
    cancelled: int
    placed: bool
    quantity: int = 0
    withheld_reason: str = ""
    waiting: int = 0

    @property
    def withheld(self) -> bool:
        return bool(self.withheld_reason)


def within_band(*, last_price: Decimal, reference: Decimal, band_pct: Decimal) -> bool:
    """Whether the market has stayed close enough to what the day planned for.

    Measured against the tradesheet's own reference price, not against the
    morning's -- the day decided to spend a certain amount at roughly a
    certain price, and this asks whether that decision still describes the
    market.
    """
    if reference <= 0 or last_price <= 0:
        return False

    move = abs(last_price - reference) / reference * HUNDRED
    return move <= band_pct


def cancel_everything_open(
    tradesheet: TradeSheet,
    *,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
    ladder_only: bool = False,
) -> int:
    """Cancel every open order and confirm each one.

    A cancellation that fails is not swallowed: the caller must not size a
    market order while an order might still be resting. `ladder_only` leaves
    the close's own market orders alone -- a round cancelling the one the
    round before placed would be the close fighting itself.
    """
    open_orders = list(placement.open_orders(tradesheet, stream=stream))
    if ladder_only:
        open_orders = [order for order in open_orders if not order.is_fallback]
    unconfirmed: list[str] = []

    for order in open_orders:
        try:
            placement.cancel(order, broker=broker)
        except BrokerError:
            unconfirmed.append(order.tag)

    if unconfirmed:
        raise CancellationsUnconfirmed(unconfirmed)

    return len(open_orders)


def _reread_fills(tradesheet: TradeSheet, *, broker: Broker, stream: str) -> None:
    """Take the broker's word for what filled, by tag (D8).

    Kite is authoritative for order state. Whatever this system believed a
    moment ago, the numbers that size the market order come from the broker's
    own order book.
    """
    try:
        book = broker.orders()
    except BrokerError as failure:
        # Paper orders have no broker-side existence, and a read failure here
        # is not fatal: the local rows are all a paper day has, and for a live
        # day the reconciliation task will catch up.
        logger.warning("could not re-read the order book", extra=fields(error=str(failure)))
        return

    by_tag = {row.get("tag"): row for row in book if row.get("tag")}
    for order in Order.objects.filter(tradesheet=tradesheet, stream=stream):
        row = by_tag.get(order.tag)
        if row is None:
            continue

        filled = int(row.get("filled_quantity") or 0)
        if filled != order.filled_quantity:
            order.filled_quantity = min(filled, order.quantity)
            order.save(update_fields=["filled_quantity", "updated_at"])


def round_number(moment: datetime, config) -> int:
    """Which close round the clock says this is, counted from FALLBACK_TICK."""
    opens = datetime.combine(moment.date(), config.market_fallback_time, tzinfo=moment.tzinfo)
    if moment <= opens:
        return FALLBACK_TICK
    step = max(1, config.placer_interval_seconds)
    return FALLBACK_TICK + int((moment - opens).total_seconds() // step)


def run(
    *,
    tradesheet: TradeSheet,
    broker: Broker,
    last_price: Decimal | None = None,
    last_prices: dict[str, Decimal] | None = None,
    stream: str = DAILY_BUY_STREAM,
    moment: datetime | None = None,
) -> FallbackOutcome:
    """One round of the close (M22), across every name the day set out to buy.

    Whatever the ladder left resting is cancelled and confirmed, the fills
    re-read, and then each name that still owes shares is due its remainder
    over the rounds left. As many as the rate limit has room for are placed
    at market, most-behind first; the rest go next round. `last_price` alone
    stands for the sheet's single instrument, the shape a sheet from before
    lines had.

    A day that has already stopped buys nothing here. An override never
    rewrites the ranking snapshot (M15a), so a skipped day keeps every line it
    was planned with, and without this the close read those lines and bought
    the whole day at market -- reversing the operator's decision hours after
    they made it, and overwriting SKIPPED on the way past.
    """
    config = current_configuration()
    moment = moment or timezone.localtime()
    tick_no = round_number(moment, config)

    # A run of refusals the book reported after the ladder's last round --
    # orders Kite acknowledged and then rejected -- halts the day here too,
    # before an unbounded market order is sized into the same refusal.
    if tradesheet.state in ladder.STILL_BUYING:
        ladder.halt_if_refused_repeatedly(tradesheet, stream=stream)

    # Two ways a day arrives here with nothing to do, asked in this order
    # because the first already knows why: a day that stopped has a state
    # that explains it, truer than "nobody authorized it".
    if tradesheet.state in STOPPED:
        return _close_without_buying(tradesheet, broker=broker, stream=stream, tick_no=tick_no)

    if stream == DAILY_BUY_STREAM and not tradesheet.is_authorized:
        return _never_authorized(tradesheet, broker=broker, stream=stream, tick_no=tick_no)

    cancelled = cancel_everything_open(tradesheet, broker=broker, stream=stream, ladder_only=True)
    _reread_fills(tradesheet, broker=broker, stream=stream)

    if last_prices is None:
        last_prices = {tradesheet.instrument_id: last_price} if last_price is not None else {}

    opens = datetime.combine(moment.date(), config.market_fallback_time, tzinfo=moment.tzinfo)
    closes = datetime.combine(moment.date(), config.close_window_end, tzinfo=moment.tzinfo)
    rounds_left = schedule.slices_left(
        moment=moment, close=closes, interval_seconds=config.placer_interval_seconds
    )
    if moment < opens:
        # Run by hand before the close's window -- closing a day early -- it
        # is the only round there will be, so everything left is due.
        rounds_left = 1

    due = []
    withheld: list[str] = []
    for line in tradesheet.buy_lines():
        instrument = line.instrument
        committed = placement.committed_quantity(tradesheet, stream=stream, instrument=instrument)
        remainder = max(0, line.quantity - committed)
        if remainder == 0:
            # Nothing to buy and nothing to say: its orders show it is done.
            continue

        reason = _out_of_band(line, last_prices.get(line.instrument_id), config.fallback_band_pct)
        if reason:
            withheld.append(reason)
            _withhold(
                tradesheet,
                instrument=instrument,
                cancelled=cancelled,
                reason=reason,
                last_price=last_prices.get(line.instrument_id),
                reference=line.reference_price or Decimal("0"),
                band_pct=config.fallback_band_pct,
                stream=stream,
                tick_no=tick_no,
            )
            continue

        due.append((line, remainder, -(-remainder // rounds_left)))

    # Most still to buy, as a share of the name, goes first.
    due.sort(key=lambda d: (-d[1] / max(1, d[0].quantity), d[0].instrument_id))
    room = _room(config)

    placed_quantity = 0
    for index, (line, remainder, quantity) in enumerate(due):
        if room is not None and index >= room:
            _note(
                tradesheet,
                tick_no=tick_no,
                stream=stream,
                instrument=line.instrument,
                reason=NoPlaceReason.WAITING_TURN,
                detail=f"{remainder} still to buy; the rate limit is used for this window",
            )
            continue
        placed_quantity += _buy_at_market(
            tradesheet,
            line=line,
            quantity=quantity,
            remainder=remainder,
            broker=broker,
            band_pct=config.fallback_band_pct,
            cancelled=cancelled,
            stream=stream,
            tick_no=tick_no,
            last_price=last_prices.get(line.instrument_id),
        )

    # The evening settlement sets the day's final state from confirmed fills;
    # this is the close's own view once it has nothing left to place.
    if not due or rounds_left <= 1:
        _settle_state(tradesheet, filled=placement.filled_quantity(tradesheet, stream=stream))

    return FallbackOutcome(
        cancelled=cancelled,
        placed=placed_quantity > 0,
        quantity=placed_quantity,
        withheld_reason="; ".join(withheld),
        waiting=max(0, len(due) - room) if room is not None else 0,
    )


def _room(config) -> int | None:
    """What the rate limit leaves this round (`placement.room`)."""
    return placement.room(config)


def _out_of_band(line, last_price: Decimal | None, band_pct: Decimal) -> str:
    reference = line.reference_price or Decimal("0")
    if last_price is not None and within_band(
        last_price=last_price, reference=reference, band_pct=band_pct
    ):
        return ""
    return (
        f"{line.instrument_id}: the last traded price {last_price or 0} is more than "
        f"{band_pct}% from the tradesheet's reference {reference}"
    )


def _buy_at_market(
    tradesheet: TradeSheet,
    *,
    line,
    quantity: int,
    remainder: int,
    broker: Broker,
    band_pct: Decimal,
    cancelled: int,
    stream: str,
    tick_no: int,
    last_price: Decimal | None,
) -> int:
    instrument = line.instrument
    outcome = placement.place(
        tradesheet=tradesheet,
        instrument=instrument,
        tick_no=tick_no,
        quantity=quantity,
        broker=broker,
        order_type=OrderType.MARKET,
        limit_price=None,
        stream=stream,
        is_fallback=True,
        # The exchange rejects a market order without it. The sanity band is
        # the same question asked the other way round -- how far from the
        # plan this order may go -- so it is the natural value.
        market_protection=band_pct,
    )

    _record_tick(
        tradesheet,
        tick_no=tick_no,
        stream=stream,
        instrument=instrument,
        order=outcome.order,
        slice_qty=quantity,
        reason=NoPlaceReason.REJECTED if outcome.failure is not None else "",
        detail=(
            str(outcome.failure)
            if outcome.failure is not None
            else f"market order for {quantity} of the {remainder} left"
        ),
    )
    if outcome.failure is not None:
        return 0

    record(
        "fallback.placed",
        entity=tradesheet,
        after={
            "symbol": instrument.tradingsymbol,
            "cancelled": cancelled,
            "quantity": quantity,
            "remainder": remainder,
            "last_price": str(last_price),
            "reference_price": str(line.reference_price),
        },
    )
    logger.info(
        "fallback market order placed",
        extra=fields(
            trading_date=str(tradesheet.trading_date),
            symbol=instrument.tradingsymbol,
            quantity=quantity,
            remainder=remainder,
        ),
    )
    return quantity


def _close_without_buying(
    tradesheet: TradeSheet, *, broker: Broker, stream: str, tick_no: int = FALLBACK_TICK
) -> FallbackOutcome:
    """Close a day that has stopped: cancel, buy nothing, keep the state.

    Not buying is not the same as leaving orders resting. Anything still open
    is cancelled exactly as it would be at any other 15:00 -- a DAY order left
    out there is one the operator did not want and cannot see -- and the state
    that explains the day is left alone.
    """
    cancelled = cancel_everything_open(tradesheet, broker=broker, stream=stream)

    _record_tick(
        tradesheet,
        stream=stream,
        tick_no=tick_no,
        reason=NoPlaceReason.DAY_SKIPPED,
        detail=f"the day is {tradesheet.state}; nothing is bought at market",
    )
    record(
        "fallback.not_run",
        entity=tradesheet,
        after={
            "state": tradesheet.state,
            "skip_reason": tradesheet.skip_reason,
            "cancelled": cancelled,
        },
    )
    logger.info(
        "the fallback bought nothing: the day had already stopped",
        extra=fields(
            trading_date=str(tradesheet.trading_date),
            state=tradesheet.state,
            skip_reason=tradesheet.skip_reason,
            cancelled=cancelled,
        ),
    )
    return FallbackOutcome(cancelled=cancelled, placed=False)


def _never_authorized(
    tradesheet: TradeSheet, *, broker: Broker, stream: str, tick_no: int = FALLBACK_TICK
) -> FallbackOutcome:
    """M15f at 15:00: a day nobody authorized buys nothing at market either.

    The ladder placed nothing for it, so there is nothing to cancel in the
    normal course; anything open is cancelled all the same. The day ends
    skipped and its whole amount carries.
    """
    cancelled = cancel_everything_open(tradesheet, broker=broker, stream=stream)
    _record_tick(
        tradesheet,
        stream=stream,
        tick_no=tick_no,
        reason=NoPlaceReason.NOT_AUTHORIZED,
        detail="the day was never authorized to trade",
    )
    # A replanned version never authorized still belongs to a day that bought
    # under an earlier one; that day is not skipped (settlement says partial).
    if not placement.day_has_bought(tradesheet.trading_date, stream=stream):
        tradesheet.state = TradeSheetState.SKIPPED
        tradesheet.skip_reason = SkipReason.NOT_AUTHORIZED
        tradesheet.save(update_fields=["state", "skip_reason"])
    record(
        "fallback.withheld",
        entity=tradesheet,
        after={"reason": "never authorized", "cancelled": cancelled},
    )
    return FallbackOutcome(
        cancelled=cancelled, placed=False, withheld_reason="the day was never authorized"
    )


def _withhold(
    tradesheet: TradeSheet,
    *,
    instrument,
    cancelled: int,
    reason: str,
    last_price: Decimal | None,
    reference: Decimal,
    band_pct: Decimal,
    stream: str,
    tick_no: int,
) -> None:
    """M22a: the market has moved too far, so nothing more of this name is bought.

    Said once, not every round: the row stands until something changes, and
    the alert is raised with it.
    """
    if not _note(
        tradesheet,
        tick_no=tick_no,
        stream=stream,
        instrument=instrument,
        reason=NoPlaceReason.WITHHELD,
        detail=reason,
    ):
        return

    record(
        "fallback.withheld",
        entity=tradesheet,
        after={
            "symbol": instrument.tradingsymbol,
            "reason": reason,
            "cancelled": cancelled,
            "filled": placement.filled_quantity(tradesheet, stream=stream, instrument=instrument),
            "last_price": str(last_price),
            "reference_price": str(reference),
        },
    )
    logger.error(
        "fallback market order withheld: the market moved outside the sanity band",
        extra=fields(
            trading_date=str(tradesheet.trading_date),
            symbol=instrument.tradingsymbol,
            last_price=str(last_price),
            reference_price=str(reference),
            band_pct=str(band_pct),
        ),
    )


def _settle_state(tradesheet: TradeSheet, *, filled: int) -> None:
    """Where the day ends up: FILLED, PARTIAL or UNFILLED.

    A day that already stopped keeps the state that explains it. "Skipped by
    the operator" and "unfilled" are different answers to what happened on the
    14th, and only one of them is true.
    """
    if tradesheet.state in STOPPED:
        return

    if filled >= tradesheet.quantity and tradesheet.quantity > 0:
        state = TradeSheetState.FILLED
    elif filled > 0:
        state = TradeSheetState.PARTIAL
    else:
        state = TradeSheetState.UNFILLED

    if tradesheet.state != state:
        tradesheet.state = state
        tradesheet.save(update_fields=["state"])


def _record_tick(
    tradesheet: TradeSheet,
    *,
    stream: str,
    tick_no: int = FALLBACK_TICK,
    instrument=None,
    order: Order | None = None,
    slice_qty: int = 0,
    reason: str = "",
    detail: str = "",
) -> Tick:
    tick, _ = Tick.objects.get_or_create(
        trading_date=tradesheet.trading_date,
        stream=stream,
        tick_no=tick_no,
        instrument=instrument,
        defaults={
            "tradesheet": tradesheet,
            "trace_id": current_trace_id(),
            "order": order,
            "slice_qty": slice_qty,
            "no_place_reason": reason,
            "detail": detail[:255],
            "committed_before": placement.filled_quantity(
                tradesheet, stream=stream, instrument=instrument
            ),
        },
    )
    return tick


def _note(
    tradesheet: TradeSheet, *, tick_no: int, stream: str, instrument, reason: str, detail: str = ""
) -> Tick | None:
    """A no-place row for a name, written when its answer changes, not every round."""
    last = (
        Tick.objects.filter(
            tradesheet=tradesheet, stream=stream, instrument=instrument, tick_no__gte=FALLBACK_TICK
        )
        .order_by("-tick_no")
        .first()
    )
    if last is not None and last.order_id is None and last.no_place_reason == reason:
        return None
    return _record_tick(
        tradesheet,
        tick_no=tick_no,
        stream=stream,
        instrument=instrument,
        reason=reason,
        detail=detail,
    )


def close_day(tradesheet: TradeSheet, *, stream: str = DAILY_BUY_STREAM) -> None:
    """Settle the day's state once every order has reached a terminal state."""
    still_open = (
        Order.objects.filter(tradesheet=tradesheet, stream=stream)
        .exclude(state__in=list(TERMINAL))
        .exists()
    )
    if still_open:
        return

    _settle_state(tradesheet, filled=placement.filled_quantity(tradesheet, stream=stream))
