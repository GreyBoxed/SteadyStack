"""Replanning the rest of a day that has already bought.

Regenerate stops being possible the moment the day buys anything (D13): a new
version that started from nothing committed would buy the day again. A
replan is the way through once the day has been stopped. It writes a new
version that

  * ranks against everything the day already bought, because the paper book
    holds it (`engine.book`);
  * spends only what the day has left: its opening, less every version's
    spend and charges so far (`settlement.day_spend`);
  * plans against the model portfolio as it is now, which is usually why
    anyone replans;
  * leaves out any name sold today, because CNC would net the two;
  * carries the reserve over rather than recounting it against a smaller
    budget; and
  * needs authorising, like any version (M15f).

The day still settles into one ledger row, which sums every version's fills.

Paper only for now. A live day's fills are recorded at settlement, so a live
replan would net against nothing and spend the day twice; its preconditions
are in the plan (docs/plans/2026-09-24-feat-replan-day-net-of-fills-plan.md).
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.context import bind_trading_day, reset_trace_id, today_ist
from audit.events import record
from audit.logging import fields
from configuration.pause import active_pause
from configuration.store import current as current_configuration
from engine import quotes as quote_reader
from engine.ledger import day_settled
from engine.lock import latest_sheet, live_orders_on
from engine.models import TradeSheet, TradeSheetState
from engine.planning import market_closed_reason
from engine.reserve import ReserveState
from engine.selection import select
from engine.settlement import day_spend
from engine.tradesheet import _rank, _write, gather
from model_portfolio.models import ModelPortfolio
from orders.idempotency import order_lock
from orders.models import DAILY_BUY_STREAM, Fill, Order
from trading_calendar.sessions import session_for

logger = logging.getLogger("steadystack.engine")

#: No replan this close to the market fallback: the close's first round must
#: not lose the lock to one, and a version authorised later would have no
#: ladder left to buy on.
CUTOFF_BEFORE_FALLBACK = timedelta(minutes=2)

#: What Stop today leaves a version as: halted once it has bought, skipped if
#: it had not. Either way nothing more is placed for it.
STOPPED = frozenset({TradeSheetState.HALTED, TradeSheetState.SKIPPED})


class ReplanRefused(Exception):
    """The day cannot be replanned, and no version was written.

    Not a `GenerationRefused`: a caller handling one should not quietly
    handle the other.
    """


def session_is_valid() -> bool:
    """Whether a Kite login is in place, which decides only whether the new
    version can run or waits for one -- never what it buys."""
    from broker import session as kite_session

    return bool(kite_session.status().valid)


def why_not(sheet: TradeSheet, *, now: datetime | None = None) -> str | None:
    """Why this sheet's day cannot be replanned now, or None if it can."""
    now = now or timezone.localtime()
    config = current_configuration()
    day = sheet.trading_date

    if day != today_ist():
        return "Only today can be replanned."
    if config.is_live:
        return "Replanning is paper only until it has been checked against a live day."
    if active_pause(on=day) is not None:
        return "Trading is stopped. Resume before replanning."
    if day_settled(day):
        return f"{day} has settled; a settled day is a record, not a plan."
    if not session_for(day).is_open or (closed := market_closed_reason(day, now=now)):
        return f"The market is not in session{f': {closed}' if closed else ''}."
    cutoff = datetime.combine(day, config.market_fallback_time) - CUTOFF_BEFORE_FALLBACK
    if now.replace(tzinfo=None) >= cutoff:
        return f"Too close to the close: replanning stops at {cutoff:%H:%M}."

    latest = latest_sheet(day)
    if latest is None or latest.pk != sheet.pk:
        return "A later version exists; replan that one."
    if not Fill.objects.filter(order__trading_date=day, order__stream=DAILY_BUY_STREAM).exists():
        return "Nothing has been bought today. Use Regenerate instead."
    # The daily buy's orders only: Stop today cancels those, and a rebalance
    # sale resting is not a reason to refuse -- its name is left out instead.
    resting = live_orders_on(day).filter(stream=DAILY_BUY_STREAM)
    if out := list(resting.values_list("tag", flat=True)[:5]):
        return f"Orders are still out ({', '.join(out)}). Stop today first."
    if sheet.state not in STOPPED and Order.objects.filter(tradesheet=sheet).exists():
        return "The day is still running. Stop today first."
    return None


def replan(sheet: TradeSheet, *, actor: str, reason: str, broker) -> TradeSheet:
    """Plan what is left of a stopped day as a new, unauthorised version.

    Takes the sheet the operator was looking at, so a stale page is refused
    ("a later version exists") -- but judges it as it is now, not as the copy
    passed in: a stop may have landed since that copy was read.
    """
    sheet = TradeSheet.objects.get(pk=sheet.pk)
    token = bind_trading_day(sheet.trading_date)
    try:
        if refusal := why_not(sheet):
            raise ReplanRefused(refusal)

        portfolio = ModelPortfolio.objects.order_by("-version").first()
        if portfolio is None:
            raise ReplanRefused("No model portfolio has been uploaded.")
        # Prices first, outside the lock: a quote failure then changes nothing.
        quotes = quote_reader.fetch(sorted(portfolio.weights()), broker=broker)
        has_session = session_is_valid()

        with order_lock(), transaction.atomic():
            current = TradeSheet.objects.select_for_update().get(pk=sheet.pk)
            if refusal := why_not(current):
                raise ReplanRefused(refusal)
            new = _write_replan(
                current, quotes=quotes, actor=actor, reason=reason, has_session=has_session
            )
    finally:
        reset_trace_id(token)

    logger.info(
        "day replanned",
        extra=fields(
            trading_date=str(new.trading_date),
            version=new.version,
            supersedes=sheet.version,
            spendable_cash=str(new.spendable_cash),
        ),
    )
    return new


def replan_from_broker(sheet: TradeSheet, *, actor: str, reason: str) -> TradeSheet:
    """Replan with the system's own broker and session, for the browser and
    the command alike."""
    from broker import get_broker, session

    broker = get_broker()
    session.attach(broker)
    return replan(sheet, actor=actor, reason=reason, broker=broker)


def _write_replan(
    current: TradeSheet, *, quotes, actor: str, reason: str, has_session: bool
) -> TradeSheet:
    config = current_configuration()
    day = current.trading_date
    spend = day_spend(day)
    budget = spend.remaining

    inputs = gather(
        trading_date=day,
        quotes=quotes,
        carry_in=current.carry_in,
        excluded=_sold_today(day),
    )
    ranking = _rank(inputs, config=config)

    # The reserve is where the morning left it. What it held back then is
    # still held back, as far as what is left allows; recounting it here
    # would call the leader unaffordable only because the morning spent.
    reserve_state = ReserveState(
        symbol=current.reserve_symbol,
        days=current.reserve_days,
        engaged=current.reserve_engaged,
    )
    reserved = min(budget, Decimal(current.ranking_snapshot.get("reserved") or "0"))
    selection = select(ranking, spendable_cash=budget - reserved, reachable_cash=budget)

    if not selection.bought_something:
        raise ReplanRefused("Nothing is left to buy; the day stays stopped.")

    new = _write(
        trading_date=day,
        inputs=inputs,
        ranking=ranking,
        selection=selection,
        reserve_state=reserve_state,
        spendable_cash=budget,
        generated_by=actor,
        has_session=has_session,
        reserved=reserved,
        replan={
            "supersedes_version": current.version,
            "reason": reason[:255],
            "opening": str(spend.opening),
            "spent_before": str(spend.spent),
            "charges_before": str(spend.charges),
            "fills_before": dict(sorted(spend.fills.items())),
            "excluded_sold_today": sorted(inputs.excluded),
        },
    )
    record(
        "tradesheet.replanned",
        actor=actor,
        entity=new,
        before={"version": current.version, "state": current.state},
        after={
            "version": new.version,
            "reason": reason[:255],
            "spendable_cash": str(budget),
            "spent_before": str(spend.spent),
            "charges_before": str(spend.charges),
            "lines": {line.instrument_id: line.quantity for line in new.buy_lines()},
        },
    )
    return new


def _sold_today(day) -> frozenset[str]:
    """The names no plan of the day may buy back (`rebalance.batches.names_sold_today`)."""
    from rebalance.batches import names_sold_today

    return frozenset(names_sold_today(day))
