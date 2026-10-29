"""The operator's three levers (M15a).

Once placement has begun the tradesheet is frozen, so a person who wants to
change the day does it here: reduce the quantity, exclude a tick, or skip the
day altogether. Each is an immutable row against the sheet with actor, reason
and before/after, and none of them rewrites the ranking snapshot.

Two rules protect the record from the levers:

  * **a fill is never unwound.** There is no way to un-buy shares, so a
    quantity cannot be reduced below what has already filled and a day with
    fills is PARTIAL, never SKIPPED;
  * **a reduction below what is committed cancels orders, newest first**
    (M15b). This is the only cancellation before the fallback there is, and it
    exists because the alternative -- leaving orders resting that the day no
    longer wants -- buys shares the operator has just said not to buy.

A tick exclusion deliberately does *not* reduce the quantity. The slice it
skips stays unfilled and is bought by the fallback, unless the operator
reduces the quantity too. Conflating the two would mean "don't place at 11:05"
silently meant "buy less today".
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from broker.base import Broker
from broker.exceptions import BrokerError
from engine.models import OverrideKind, SkipReason, TradeSheet, TradesheetOverride, TradeSheetState
from orders import placement
from orders.models import DAILY_BUY_STREAM, Order

logger = logging.getLogger("steadystack.orders")


class OverrideRefused(Exception):
    """A decision the record cannot honour."""


@dataclass(frozen=True)
class ReductionOutcome:
    override: TradesheetOverride
    cancelled: list[str]
    #: Orders the broker would not confirm cancelled. These may still be live.
    unconfirmed: list[str] = field(default_factory=list)


def require_latest(tradesheet: TradeSheet) -> None:
    """Refuse a lever pulled on a version that is no longer the day's.

    A page open since before a regenerate or a replan still shows the old
    version; a decision made there is about a plan nobody is running.
    """
    latest = (
        TradeSheet.objects.filter(trading_date=tradesheet.trading_date)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )
    if latest != tradesheet.version:
        raise OverrideRefused(
            f"A later version (v{latest}) exists; v{tradesheet.version} can no longer be changed."
        )


def _write(
    tradesheet: TradeSheet,
    *,
    kind: str,
    actor: str,
    reason: str,
    before: dict,
    after: dict,
    tick_no: int | None = None,
) -> TradesheetOverride:
    override = TradesheetOverride.objects.create(
        tradesheet=tradesheet,
        kind=kind,
        tick_no=tick_no,
        before=before,
        after=after,
        reason=reason[:255],
        actor=actor,
        trace_id=current_trace_id(),
    )
    record(
        f"override.{kind}",
        actor=actor,
        entity=tradesheet,
        before=before,
        after={**after, "reason": reason},
    )
    logger.warning(
        "operator override",
        extra=fields(
            kind=kind,
            trading_date=str(tradesheet.trading_date),
            actor=actor,
            reason=reason,
        ),
    )
    return override


@transaction.atomic
def reduce_quantity(
    tradesheet: TradeSheet,
    *,
    quantity: int,
    actor: str,
    reason: str,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
) -> ReductionOutcome:
    """Reduce the day's quantity, cancelling newest orders if it must (M15b)."""
    require_latest(tradesheet)
    filled = placement.filled_quantity(tradesheet, stream=stream)

    if quantity > tradesheet.quantity:
        raise OverrideRefused(
            f"An override may only reduce the day's quantity, and {quantity} is "
            f"above the engine's {tradesheet.quantity}."
        )
    if quantity < 0:
        raise OverrideRefused("A quantity cannot be negative.")
    if quantity < filled:
        raise OverrideRefused(
            f"{filled} shares have already filled and cannot be un-bought; "
            f"the day cannot be reduced below that."
        )

    before = {"quantity": tradesheet.quantity, "filled": filled}

    # Cancel newest first: the most recent orders are the ones priced highest
    # up the ladder, and the ones least likely to have filled.
    cancelled: list[str] = []
    committed = placement.committed_quantity(tradesheet, stream=stream)
    open_orders = list(
        placement.open_orders(tradesheet, stream=stream).order_by("-tick_no", "-placed_at")
    )

    for order in open_orders:
        if committed <= quantity:
            break
        placement.cancel(order, broker=broker)
        committed -= order.unfilled_quantity
        cancelled.append(order.tag)

    tradesheet.quantity = quantity
    tradesheet.save(update_fields=["quantity"])

    override = _write(
        tradesheet,
        kind=OverrideKind.REDUCE_QUANTITY,
        actor=actor,
        reason=reason,
        before=before,
        after={"quantity": quantity, "cancelled": cancelled},
    )
    return ReductionOutcome(override=override, cancelled=cancelled)


@transaction.atomic
def adjust_line(
    tradesheet: TradeSheet,
    *,
    symbol: str,
    quantity: int,
    actor: str,
    reason: str,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
    skip: bool = False,
) -> ReductionOutcome:
    """Change how many of one name the day buys, or skip it (M15a).

    Lower cancels that name's newest open orders until what is committed fits,
    as a day-wide reduction does (M15b); shares already filled are never
    unwound, so a name cannot go below them. Higher is allowed only while the
    day's cash still covers every line at the price it was sized at -- the
    operator may override the target cap, never the money. The sheet's total
    follows its lines.
    """
    from engine.models import TradeSheetLine

    require_latest(tradesheet)

    line = next((line for line in tradesheet.buy_lines() if line.instrument_id == symbol), None)
    if line is None or line.pk is None:
        if line is not None and not skip:
            # A sheet from before lines: its one instrument is the whole day.
            return reduce_quantity(
                tradesheet,
                quantity=quantity,
                actor=actor,
                reason=reason,
                broker=broker,
                stream=stream,
            )
        raise OverrideRefused(f"{symbol} is not one of the names this day buys.")

    instrument = line.instrument
    filled = placement.filled_quantity(tradesheet, stream=stream, instrument=instrument)
    if skip:
        quantity = filled
    if quantity < 0:
        raise OverrideRefused("A quantity cannot be negative.")
    if quantity < filled:
        raise OverrideRefused(
            f"{filled} {symbol} have already filled and cannot be un-bought; "
            "it cannot go below that."
        )

    if quantity > line.quantity:
        others = sum(
            (other.planned_cost for other in tradesheet.buy_lines() if other.pk != line.pk),
            Decimal("0"),
        )
        needed = others + line.reference_price * quantity
        if needed > tradesheet.spendable_cash:
            raise OverrideRefused(
                f"{quantity} {symbol} would bring the day to ₹{needed:.2f}, more than the "
                f"₹{tradesheet.spendable_cash:.2f} it has."
            )

    before = {"symbol": symbol, "quantity": line.quantity, "filled": filled}

    cancelled: list[str] = []
    committed = placement.committed_quantity(tradesheet, stream=stream, instrument=instrument)
    for order in placement.open_orders(tradesheet, stream=stream, instrument=instrument).order_by(
        "-tick_no", "-placed_at"
    ):
        if committed <= quantity:
            break
        placement.cancel(order, broker=broker)
        committed -= order.unfilled_quantity
        cancelled.append(order.tag)

    line.quantity = quantity
    line.save(update_fields=["quantity"])
    tradesheet.quantity = sum(
        TradeSheetLine.objects.filter(tradesheet=tradesheet).values_list("quantity", flat=True)
    )
    tradesheet.save(update_fields=["quantity"])

    override = _write(
        tradesheet,
        kind=OverrideKind.SKIP_LINE if skip else OverrideKind.ADJUST_LINE,
        actor=actor,
        reason=reason,
        before=before,
        after={"symbol": symbol, "quantity": quantity, "cancelled": cancelled},
    )
    return ReductionOutcome(override=override, cancelled=cancelled)


@transaction.atomic
def exclude_tick(
    tradesheet: TradeSheet,
    *,
    tick_no: int,
    actor: str,
    reason: str,
) -> TradesheetOverride:
    """Stop one tick from placing.

    The day's quantity is untouched: the slice this tick would have placed
    stays unfilled and the fallback buys it, unless the quantity is reduced
    as well.
    """
    require_latest(tradesheet)
    if is_excluded(tradesheet, tick_no):
        raise OverrideRefused(f"Tick {tick_no} is already excluded.")

    return _write(
        tradesheet,
        kind=OverrideKind.EXCLUDE_TICK,
        actor=actor,
        reason=reason,
        before={"tick_no": tick_no, "excluded": False},
        after={"tick_no": tick_no, "excluded": True},
        tick_no=tick_no,
    )


def is_excluded(tradesheet: TradeSheet, tick_no: int) -> bool:
    return TradesheetOverride.objects.filter(
        tradesheet=tradesheet, kind=OverrideKind.EXCLUDE_TICK, tick_no=tick_no
    ).exists()


def excluded_ticks(tradesheet: TradeSheet) -> set[int]:
    return set(
        TradesheetOverride.objects.filter(
            tradesheet=tradesheet, kind=OverrideKind.EXCLUDE_TICK
        ).values_list("tick_no", flat=True)
    )


class CancellationsIncomplete(Exception):
    """The day was stopped, but some of its orders did not confirm cancelled.

    Raised only after the stop itself is committed, so it never undoes it:
    the message is that orders may still be live at the broker, not that the
    day is still running.
    """

    def __init__(self, what: str, tags: list[str]) -> None:
        super().__init__(
            f"{what}, but these orders could not be confirmed cancelled and may still "
            f"be live at the broker: {', '.join(tags)}. Check the broker's own order book."
        )
        self.tags = tags


def _cancel_each(orders, *, broker: Broker) -> tuple[list[str], list[str]]:
    """Cancel every order, each committing on its own; (cancelled, unconfirmed).

    Never inside the caller's transaction. A cancel is a broker call that
    cannot be rolled back, so a failure at the second order must not roll
    back the record of the first -- that would leave it open here while it is
    cancelled at the broker. The same reasoning as `halt.stop_trading`.
    """
    cancelled: list[str] = []
    unconfirmed: list[str] = []
    for order in list(orders):
        try:
            placement.cancel(order, broker=broker)
        except BrokerError:
            # Logged by `cancel` already; the next order still has to be tried.
            unconfirmed.append(order.tag)
        else:
            cancelled.append(order.tag)
    return cancelled, unconfirmed


def skip_day(
    tradesheet: TradeSheet,
    *,
    actor: str,
    reason: str,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
) -> TradesheetOverride:
    """Stop the day entirely.

    Refused once anything has filled. There is no way to unwind a fill, so a
    day with fills is PARTIAL rather than SKIPPED -- calling it skipped would
    be a record that says nothing was bought on a day when something was.

    The state is committed before anything is cancelled, so no round can
    place into the day however the cancels go; a cancel that does not confirm
    is reported after the override is written (`CancellationsIncomplete`).
    """
    with transaction.atomic():
        require_latest(tradesheet)
        # The day's fills, not this version's: a replanned version with none
        # of its own still belongs to a day that bought.
        if placement.day_has_bought(tradesheet.trading_date, stream=stream):
            raise OverrideRefused(
                "Shares have already filled today, so the day cannot be skipped. "
                "Reduce the quantity or stop the day instead; it ends partial."
            )

        before = {"state": tradesheet.state, "quantity": tradesheet.quantity}
        tradesheet.state = TradeSheetState.SKIPPED
        tradesheet.skip_reason = SkipReason.OPERATOR
        tradesheet.save(update_fields=["state", "skip_reason"])

    cancelled, unconfirmed = _cancel_each(
        placement.open_orders(tradesheet, stream=stream), broker=broker
    )

    override = _write(
        tradesheet,
        kind=OverrideKind.SKIP_DAY,
        actor=actor,
        reason=reason,
        before=before,
        after={
            "state": TradeSheetState.SKIPPED,
            "skip_reason": SkipReason.OPERATOR,
            "cancelled": cancelled,
            "unconfirmed": unconfirmed,
        },
    )
    if unconfirmed:
        raise CancellationsIncomplete("The day is skipped", unconfirmed)
    return override


#: States a day cannot be stopped from: it has already ended, one way or another.
ENDED = frozenset(
    {
        TradeSheetState.FILLED,
        TradeSheetState.PARTIAL,
        TradeSheetState.UNFILLED,
        TradeSheetState.HALTED,
        TradeSheetState.SKIPPED,
    }
)

#: How long a stop waits for a round in flight before giving up. A round is a
#: handful of broker calls; this is several of them.
STOP_WAIT_SECONDS = 15


def stop_day(
    tradesheet: TradeSheet,
    *,
    actor: str,
    reason: str,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
) -> ReductionOutcome | TradesheetOverride:
    """End today early, after any round in flight has finished.

    Under the lock every order-touching task shares. Without it a round that
    had already read the day as running could place after the stop had
    cancelled everything -- an order resting on a stopped day. The sheet is
    read again once the lock is held, for the same reason.
    """
    from orders.idempotency import order_lock

    with order_lock(wait_seconds=STOP_WAIT_SECONDS):
        return _stop_day(
            TradeSheet.objects.get(pk=tradesheet.pk),
            actor=actor,
            reason=reason,
            broker=broker,
            stream=stream,
        )


def _stop_day(
    tradesheet: TradeSheet,
    *,
    actor: str,
    reason: str,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
) -> ReductionOutcome | TradesheetOverride:
    """End today early: cancel what rests, place nothing more, keep the fills.

    Before any fill this is a skip, and the day says so. After one the day is
    HALTED -- the state the ladder and the fallback already refuse to place
    into -- and settlement calls it PARTIAL, because it bought some of what it
    meant to. The plan is left as it was: cutting the quantity down to the
    fills would settle as FILLED, a record claiming the day did what it set out
    to. Tomorrow is untouched; this is not the system-wide stop (`halt`).

    HALTED is committed first and each cancel commits on its own, as
    `halt.stop_trading` does. As one transaction, a cancel failing at the
    second order rolled back the first -- cancelled at the broker, open here
    -- and the halt with it, so the next round placed again. An order that
    will not confirm is returned in `unconfirmed` rather than raised.
    """
    with transaction.atomic():
        require_latest(tradesheet)
        if tradesheet.state in ENDED:
            raise OverrideRefused(
                f"The day has already ended as {tradesheet.get_state_display().lower()}."
            )

        bought = placement.day_has_bought(tradesheet.trading_date, stream=stream)
        if bought:
            before = {"state": tradesheet.state, "quantity": tradesheet.quantity}
            tradesheet.state = TradeSheetState.HALTED
            tradesheet.skip_reason = SkipReason.OPERATOR
            tradesheet.save(update_fields=["state", "skip_reason"])

    if not bought:
        return skip_day(tradesheet, actor=actor, reason=reason, broker=broker, stream=stream)

    cancelled, unconfirmed = _cancel_each(
        placement.open_orders(tradesheet, stream=stream), broker=broker
    )

    override = _write(
        tradesheet,
        kind=OverrideKind.STOP_DAY,
        actor=actor,
        reason=reason,
        before=before,
        after={
            "state": TradeSheetState.HALTED,
            "cancelled": cancelled,
            "unconfirmed": unconfirmed,
        },
    )
    if unconfirmed:
        logger.error(
            "the day is stopped, but some orders could not be cancelled and may still be live",
            extra=fields(trading_date=str(tradesheet.trading_date), unconfirmed=unconfirmed),
        )
    return ReductionOutcome(override=override, cancelled=cancelled, unconfirmed=unconfirmed)


def cancelled_by_reduction(tradesheet: TradeSheet) -> list[Order]:
    """Orders a reduction cancelled, for the day's detail view."""
    tags: set[str] = set()
    for override in TradesheetOverride.objects.filter(
        tradesheet=tradesheet, kind=OverrideKind.REDUCE_QUANTITY
    ):
        tags.update(override.after.get("cancelled", []))
    return list(Order.objects.filter(tag__in=tags))
