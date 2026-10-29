"""Closing the day out.

Runs after the fallback, when nothing can still fill. It does four things, in
this order, and the order matters:

  1. **reconcile against the broker**, so the fills being counted are the
     fills that happened rather than the ones this system noticed;
  2. **sum the `Fill` rows** across every order of the day -- never a broker
     average, never per order (D6). A day places many orders and the only
     figure that means anything is the total. Every order of the *day*, not
     of one version: a day stopped and replanned bought under both, and one
     ledger row carries both;
  3. **settle the trade date in the journal and append the ledger row**, a
     summary of what the engine account did, where tomorrow's carry is read;
  4. **settle the tradesheet's state**: FILLED, PARTIAL or UNFILLED.

Holdings are updated from lots for a paper day. A live day's holdings come
from the broker, which knows; in paper mode the broker knows nothing, and
without this the engine would rank the same name as unheld every morning and
buy it for ever. The two sources are kept apart everywhere else in this
system, so the one place they are joined says so loudly.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from audit.events import record
from audit.logging import fields
from broker.base import Broker
from broker.exceptions import BrokerError
from engine.ledger import book_of_day, money, opening_for, settle_day
from engine.models import CashLedger, TradeSheet, TradeSheetState
from orders import placement, reconcile
from orders.models import DAILY_BUY_STREAM, Fill, Order, OrderMode, OrderSide
from portfolio.charges import cost_of
from portfolio.models import Holding, Instrument

logger = logging.getLogger("steadystack.engine")

ZERO = Decimal("0.00")


class NothingToSettle(Exception):
    """The day has no tradesheet, so there is nothing to settle yet (NI3).

    Raised rather than returned so the run's claim is recorded failed, not
    succeeded: a settle run before the day is planned -- a caught-up timer,
    a hand run too early -- must leave the evening's settle its claim.

    Logged at ERROR, which alarms, except on a database holding no earlier
    tradesheet at all -- one just created or recreated, whose evening
    retries find nothing to settle by design (Nm3).
    """

    def __init__(self, message: str, *, fresh: bool = False):
        super().__init__(message)
        self.log_level = logging.INFO if fresh else logging.ERROR


@dataclass(frozen=True)
class Settlement:
    tradesheet: TradeSheet | None
    ledger: CashLedger | None
    filled_quantity: int
    cash_spent: Decimal
    charges: Decimal

    @property
    def carry_out(self) -> Decimal:
        return self.ledger.closing_carry if self.ledger else ZERO


def fills_on(trading_date: date, *, stream: str = DAILY_BUY_STREAM):
    """Every fill of the day in *stream*, whichever version placed it."""
    return Fill.objects.filter(order__trading_date=trading_date, order__stream=stream)


def spend_and_charges(trading_date: date, *, stream: str = DAILY_BUY_STREAM):
    """What the day actually cost, summed from fills (D6, M17).

    From actual fill prices and quantities, never from the planned price: a
    day that filled two of its four shares spent what those two cost, and the
    rest carries.
    """
    spend = day_spend(trading_date, stream=stream)
    return spend.spent, spend.charges


@dataclass(frozen=True)
class DaySpend:
    """What a day had, what it has spent, and so what is left of it."""

    opening: Decimal
    spent: Decimal
    charges: Decimal
    #: Shares bought net of shares sold, by symbol.
    fills: dict[str, int]

    @property
    def remaining(self) -> Decimal:
        return max(ZERO, money(self.opening - self.spent - self.charges))


def planned_budget(sheet: TradeSheet) -> Decimal:
    """What the day had to spend, as its plans saw it.

    A version's own budget, plus -- for a replanned version, whose budget is
    what was left -- what the versions before it spent and paid, as recorded
    when it was planned. Pages showing "what the day had" read this, and take
    `day_spend` off it, so the morning is never taken off twice.
    """
    netted = (sheet.ranking_snapshot or {}).get("replan") or {}
    return money(
        sheet.spendable_cash
        + Decimal(netted.get("spent_before") or "0")
        + Decimal(netted.get("charges_before") or "0")
    )


def day_spend(trading_date: date, *, stream: str = DAILY_BUY_STREAM) -> DaySpend:
    """The day's money, across every version of it.

    The one place a day's spend is summed, for settlement and for anything
    that shows or plans against what is left, so they cannot count it two
    ways. Charges not yet on a fill are estimated from the schedule, as
    settlement has always done; the contract note settles the difference
    later (D7).
    """
    gross = ZERO
    charges = ZERO
    shares: dict[str, int] = {}

    for fill in fills_on(trading_date, stream=stream).select_related("order__instrument"):
        side = fill.order.side
        fill_gross, estimated = cost_of(
            quantity=fill.quantity,
            price=fill.price,
            side=side,
            exchange=fill.order.instrument.exchange,
        )
        symbol = fill.order.instrument_id

        if side == OrderSide.BUY:
            gross += fill_gross
            shares[symbol] = shares.get(symbol, 0) + fill.quantity
        else:
            # A sell returns cash; it is not a spend. The charges on it are
            # still a cost of the day.
            gross -= fill_gross
            shares[symbol] = shares.get(symbol, 0) - fill.quantity

        charges += fill.charges or estimated.total

    # The journal has already netted the day's own fills out of the engine
    # balance -- they post as they happen -- so what the day opened with is
    # what is left plus what it spent.
    return DaySpend(
        opening=money(
            opening_for(trading_date, book=book_of_day(trading_date)).spendable
            + money(gross)
            + money(charges)
        ),
        spent=money(gross),
        charges=money(charges),
        fills=shares,
    )


@transaction.atomic
def settle(
    *,
    trading_date: date,
    broker: Broker,
    stream: str = DAILY_BUY_STREAM,
    reconcile_first: bool = True,
) -> Settlement:
    """Close the day and write the ledger row it earned."""
    tradesheet = TradeSheet.objects.filter(trading_date=trading_date).order_by("-version").first()

    if tradesheet is None:
        raise NothingToSettle(
            f"{trading_date} has no tradesheet; nothing was settled.",
            fresh=not TradeSheet.objects.filter(trading_date__lt=trading_date).exists(),
        )

    if not reconcile_first and _has_live_orders(trading_date, stream=stream):
        # A live day's fills are written from the broker's trade book and
        # nowhere else; without a session there is nothing to write them
        # from, and the row this would write can never be corrected.
        raise BrokerError(
            "A live day cannot settle without a Kite session: its fills come from the "
            "broker's trade book. Log in and run settle_day again."
        )

    if reconcile_first:
        try:
            reconcile.from_broker(trading_date=trading_date, broker=broker, stream=stream)
            reconcile.record_fills(trading_date=trading_date, broker=broker, stream=stream)
        except BrokerError as failure:
            # A paper day has nothing at the broker to reconcile against, and
            # a live day that cannot reach it must not settle against a guess.
            if _has_live_orders(trading_date, stream=stream):
                raise
            logger.info(
                "settling a paper day without the broker",
                extra=fields(reason=str(failure)),
            )

    _require_live_fills_recorded(trading_date, stream=stream)

    # The latest version settles on its own plan: an earlier one that was
    # stopped keeps the state that says so. The money is the whole day's.
    filled = placement.filled_quantity(tradesheet, stream=stream)
    cash_spent, charges = spend_and_charges(trading_date, stream=stream)

    _apply_paper_holdings(trading_date)
    _settle_state(tradesheet, filled=filled)

    from journal import trades

    # The day's own book, stored on its sheet when it was planned: a paper
    # day settled after the switch to live is still a paper day (M1).
    book = tradesheet.book
    modes = set(Order.objects.filter(trading_date=trading_date).values_list("mode", flat=True))
    # Live first, then paper: the order record_lumpsum takes the two books'
    # locks in, so a settle and a lumpsum never wait on each other (N1).
    for mode in sorted(modes | {book}, key=lambda mode: mode != OrderMode.LIVE):
        trades.settle_trades(trading_date, book=mode)

    ledger = settle_day(
        trading_date,
        book=book,
        note=_note(trading_date, latest=tradesheet, filled=filled, stream=stream),
    )

    record(
        "day.settled",
        entity=tradesheet,
        after={
            "trading_date": str(trading_date),
            "state": tradesheet.state,
            "filled_quantity": filled,
            "quantity": tradesheet.quantity,
            "cash_spent": str(cash_spent),
            "charges": str(charges),
            "proceeds": str(ledger.proceeds),
            "closing_carry": str(ledger.closing_carry),
        },
    )
    logger.info(
        "day settled",
        extra=fields(
            trading_date=str(trading_date),
            state=tradesheet.state,
            filled=filled,
            cash_spent=str(cash_spent),
            closing_carry=str(ledger.closing_carry),
        ),
    )

    return Settlement(
        tradesheet=tradesheet,
        ledger=ledger,
        filled_quantity=filled,
        cash_spent=cash_spent,
        charges=charges,
    )


@transaction.atomic
def settle_trades_behind(trading_date: date) -> list:
    """Post the settlement entry of a day a later row has already counted (NI1).

    When D+1's settle writes its row first, D can never have one: the later
    row has counted every line dated on or before it, D's included, and
    `settle_day(D)` refuses. D's trades still owe their settlement entry --
    without it they sit in the settlement account for good, and the Console
    statement's line for D has nothing to match. This posts only that, in
    every book the day traded in, live first. Refused while D could still
    settle whole, and while the day's live fills are not all recorded or an
    order of the day is still open.
    """
    from engine.ledger import LedgerError
    from journal import trades
    from orders.state import TERMINAL

    book = book_of_day(trading_date)
    if not CashLedger.objects.filter(book=book, trading_date__gt=trading_date).exists():
        raise LedgerError(
            f"No later day is summarised in the {book} book, so {trading_date} can settle "
            f"whole: run settle_day --date {trading_date}."
        )
    open_orders = Order.objects.filter(trading_date=trading_date).exclude(state__in=list(TERMINAL))
    if open_orders.exists():
        raise LedgerError(
            f"{open_orders.count()} order(s) of {trading_date} are still open. Import "
            f"{trading_date}'s tradebook first (import_tradebook, OPERATIONS.md §7), which "
            "levels them with their fills -- or, once Console's tradebook shows none of them "
            f"traded, run settle_day --date {trading_date} --trades-only --level-unseen "
            "--actor NAME."
        )
    try:
        _require_live_fills_recorded(trading_date, stream=DAILY_BUY_STREAM)
    except BrokerError as incomplete:
        raise LedgerError(f"{incomplete} Import the day's tradebook first.") from incomplete

    modes = set(Order.objects.filter(trading_date=trading_date).values_list("mode", flat=True))
    posted = []
    for mode in sorted(modes | {book}, key=lambda mode: mode != OrderMode.LIVE):
        entry = trades.settle_trades(trading_date, book=mode)
        if entry is not None:
            posted.append(entry)

    record(
        "day.trades_settled",
        entity_type="engine.CashLedger",
        entity_id=str(trading_date),
        after={"entries": [entry.number for entry in posted], "book": book},
    )
    logger.info(
        "a day's trades settled behind a later row",
        extra=fields(trading_date=str(trading_date), entries=len(posted)),
    )
    return posted


def day_proceeds(trading_date: date, *, book: str | None = None) -> Decimal:
    """The day's rebalance sale money given back to the engine (D-S1).

    Read from the journal: the `reinvest` allocations dated *trading_date* in
    *book* -- each a settled batch's sale money net of the sells' own
    charges, posted when the batch closes and only if it reinvests. What the
    day's ledger row carries in its `proceeds` column, and so what the next
    day may spend on top of its own budget. Without *book*, the book the
    day was planned in (m-6).
    """
    from journal.accounts import ENGINE_CASH
    from journal.cash import REINVEST
    from journal.models import EntryKind, JournalLine

    total = JournalLine.objects.filter(
        entry__book=book or book_of_day(trading_date),
        entry__kind=EntryKind.ALLOCATION,
        entry__source_type=REINVEST,
        entry__occurred_on=trading_date,
        account_id=ENGINE_CASH,
    ).aggregate(total=Sum("amount"))["total"]
    return money(total or ZERO)


def _require_live_fills_recorded(trading_date: date, *, stream: str) -> None:
    """Refuse when the live orders say more filled than the fills record.

    The orders' filled quantities come from the order book and postbacks; the
    fills, which are what the row sums, come only from the trade book. When
    they disagree the trade book is lagging or incomplete, and a row written
    now would carry money already spent into tomorrow.
    """
    live = Order.objects.filter(trading_date=trading_date, stream=stream, mode=OrderMode.LIVE)
    ordered = live.aggregate(total=Sum("filled_quantity"))["total"] or 0
    recorded = Fill.objects.filter(order__in=live).aggregate(total=Sum("quantity"))["total"] or 0
    if ordered != recorded:
        raise BrokerError(
            f"The live orders filled {ordered} shares but {recorded} are recorded as fills; "
            "the trade book is incomplete. Nothing was settled; run settle_day again shortly."
        )


def _has_live_orders(trading_date: date, *, stream: str) -> bool:
    return Order.objects.filter(
        trading_date=trading_date, stream=stream, mode=OrderMode.LIVE
    ).exists()


def _note(trading_date: date, *, latest: TradeSheet, filled: int, stream: str) -> str:
    """The ledger row's line. One version reads as it always has; a day that
    was replanned names each version that bought."""
    own = f"{filled} of {latest.quantity} filled"
    earlier = (
        Order.objects.filter(trading_date=trading_date, stream=stream, filled_quantity__gt=0)
        .exclude(tradesheet=latest)
        .values("tradesheet__version")
        .annotate(bought=Sum("filled_quantity"))
        .order_by("tradesheet__version")
    )
    if not earlier:
        return own
    parts = [f"v{row['tradesheet__version']} {row['bought']} filled" for row in earlier]
    return "; ".join([*parts, f"v{latest.version} {own}"])


def _settle_state(tradesheet: TradeSheet, *, filled: int) -> None:
    if tradesheet.state == TradeSheetState.SKIPPED:
        return  # a skipped day is already explained

    if tradesheet.quantity > 0 and filled >= tradesheet.quantity:
        state = TradeSheetState.FILLED
    elif filled > 0:
        state = TradeSheetState.PARTIAL
    else:
        state = TradeSheetState.UNFILLED

    if tradesheet.state != state:
        tradesheet.state = state
        tradesheet.save(update_fields=["state"])


def _apply_paper_holdings(trading_date: date) -> None:
    """Set paper holdings from the lots, for every name filled today.

    The broker is authoritative for holdings, and in paper mode it has nothing
    to say -- the orders were never sent. Every stream and both sides: a name
    a rebalance batch sold must read what the lots now say (0 when sold out;
    the row is kept, not deleted), or the evening's divergence scan flags it
    for ever. A live day is untouched here: the broker knows.
    """
    from portfolio import lots

    names = (
        Fill.objects.filter(order__trading_date=trading_date, order__mode=OrderMode.PAPER)
        .values_list("order__instrument", flat=True)
        .distinct()
    )
    for instrument_id in names:
        holding, _ = Holding.objects.get_or_create(
            instrument=Instrument.objects.get(pk=instrument_id),
            defaults={"quantity": 0},
        )
        # Set rather than increment: settlement is idempotent, and the paper
        # lots -- only those (M-1) -- are the record of what it holds.
        holding.quantity = lots.held_quantity(instrument_id, book=OrderMode.PAPER)
        holding.save(update_fields=["quantity", "reconciled_at"])
