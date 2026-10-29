"""Validating, previewing and executing a rebalance sell.

The shape is deliberately the same as the model portfolio upload: a file, then
a preview, then a confirmation. The difference is that this one sells shares,
so the confirmation is bound to a token rather than to a session.

Two interlocks run in both directions, and they exist because of one specific
disaster. CNC buys and sells net off intraday: if the day buys a name in the
morning and sells it in the afternoon, the shares never arrive, a lot is
created and consumed on the same day, and the ledger records a purchase that
the depository never saw. So:

  * an upload naming an instrument the day is buying is **refused**;
  * selection excludes an instrument with a live sell in this batch.

The engine never initiates any of this (M34). A person uploads the file.
"""

import csv
import hashlib
import io
import logging
import secrets
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.context import current_trace_id, today_ist
from audit.events import record
from audit.logging import fields
from gains import fifo
from portfolio.models import Instrument
from portfolio.opening import require_complete
from rebalance.models import (
    TOKEN_TTL,
    BatchState,
    RebalanceSell,
    RebalanceSellRow,
    RowState,
)

logger = logging.getLogger("steadystack.rebalance")

SYMBOL_ALIASES = frozenset({"symbol", "tradingsymbol", "trading_symbol", "ticker"})
QUANTITY_ALIASES = frozenset({"quantity", "qty", "shares"})


class UploadRefused(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class ExecutionRefused(Exception):
    """A batch that may not run as asked."""


@dataclass
class ParsedRow:
    line: int
    symbol: str
    quantity: int


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def content_hash(self) -> str:
        canonical = ";".join(
            f"{row.symbol}={row.quantity}" for row in sorted(self.rows, key=lambda r: r.symbol)
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse(raw: bytes | str) -> ParseResult:
    """Read a sell file, collecting every problem with it."""
    result = ParseResult()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    text = text.lstrip("﻿")

    if not text.strip():
        result.errors.append("The file is empty.")
        return result

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        result.errors.append("The file has no header row.")
        return result

    headers = {(name or "").strip().lower(): name for name in reader.fieldnames}
    symbol_key = next((headers[h] for h in headers if h in SYMBOL_ALIASES), None)
    quantity_key = next((headers[h] for h in headers if h in QUANTITY_ALIASES), None)

    if symbol_key is None or quantity_key is None:
        result.errors.append("The header must name a symbol column and a quantity column.")
        return result

    seen: dict[str, int] = {}

    for line, row in enumerate(reader, start=2):
        symbol = (row.get(symbol_key) or "").strip().upper()
        quantity_text = (row.get(quantity_key) or "").strip()

        if not symbol and not quantity_text:
            continue
        if not symbol:
            result.errors.append(f"Line {line}: no symbol.")
            continue

        try:
            quantity = int(quantity_text)
        except ValueError:
            result.errors.append(f"Line {line}: {symbol} has a quantity that is not a number.")
            continue

        if quantity <= 0:
            result.errors.append(f"Line {line}: {symbol} has a quantity of {quantity}.")
            continue
        if symbol in seen:
            result.errors.append(
                f"Line {line}: {symbol} appears twice (also on line {seen[symbol]})."
            )
            continue

        seen[symbol] = line
        result.rows.append(ParsedRow(line=line, symbol=symbol, quantity=quantity))

    if not result.rows and not result.errors:
        result.errors.append("The file has a header but no rows.")

    return result


def validate(result: ParseResult, *, trading_date: date | None = None) -> ParseResult:
    """Check the file against what the portfolio can actually sell."""
    trading_date = trading_date or today_ist()
    known = {
        instrument.tradingsymbol: instrument
        for instrument in Instrument.objects.filter(
            tradingsymbol__in=[row.symbol for row in result.rows]
        )
    }

    from configuration.store import current

    book = current().order_mode
    for row in result.rows:
        instrument = known.get(row.symbol)

        if instrument is None:
            result.errors.append(f"Line {row.line}: {row.symbol} is not in the instrument master.")
            continue

        # Only the lots of the book the sale runs in can cover it (I6).
        held = fifo.available(row.symbol, book=book)
        if held < row.quantity:
            result.errors.append(
                f"Line {row.line}: {row.symbol} has {held} shares in {book} lots, "
                f"not the {row.quantity} being sold."
            )
            continue

        if _is_being_bought_today(row.symbol, trading_date):
            # CNC nets off intraday: the shares would never arrive, and a lot
            # would be created and consumed the same day.
            result.errors.append(
                f"Line {row.line}: {row.symbol} is being bought today. Selling it the "
                "same day would net off before the shares are delivered."
            )

    return result


def names_bought_today(trading_date: date) -> set[str]:
    """Every name any version of the day's plan buys (D-S2).

    Any version that was not skipped: a version stopped after it bought still
    bought, and CNC nets a sale of its names the same day. A skipped version
    bought nothing, whatever its lines say. The one rule an upload, an
    execute and every sell round check.
    """
    from engine.models import TradeSheet, TradeSheetLine, TradeSheetState

    sheets = TradeSheet.objects.filter(trading_date=trading_date).exclude(
        state=TradeSheetState.SKIPPED
    )
    leads = sheets.exclude(instrument_id=None).values_list("instrument_id", flat=True)
    lines = TradeSheetLine.objects.filter(tradesheet__in=sheets, quantity__gt=0).values_list(
        "instrument_id", flat=True
    )
    return set(leads) | set(lines)


def _is_being_bought_today(symbol: str, trading_date: date) -> bool:
    return symbol in names_bought_today(trading_date)


def upload(
    raw: bytes | str,
    *,
    uploaded_by: str,
    source_filename: str = "",
    trading_date: date | None = None,
    reinvest: bool = True,
) -> RebalanceSell:
    """Store a validated batch in draft, with a token bound to its content.

    Refused while the opening import is incomplete: FIFO against half a lot
    set decides which shares left, and gets it wrong invisibly.

    Validation happens outside the transaction that writes the batch, so the
    record of a refused upload is not rolled back along with the refusal. A
    file somebody tried to sell a portfolio with is a thing that happened.
    """
    require_complete("Executing a rebalance sell")

    result = validate(parse(raw), trading_date=trading_date)

    if not result.ok:
        record(
            "rebalance.rejected",
            actor=uploaded_by,
            entity_type="rebalance.RebalanceSell",
            after={"filename": source_filename, "errors": result.errors},
        )
        raise UploadRefused(result.errors)

    return _store(
        result,
        uploaded_by=uploaded_by,
        source_filename=source_filename,
        reinvest=reinvest,
    )


@transaction.atomic
def _store(
    result: ParseResult, *, uploaded_by: str, source_filename: str, reinvest: bool = True
) -> RebalanceSell:
    batch = RebalanceSell.objects.create(
        uploaded_by=uploaded_by,
        source_filename=source_filename[:255],
        content_hash=result.content_hash,
        state=BatchState.DRAFT,
        reinvest=reinvest,
        execute_token=secrets.token_urlsafe(32),
        token_expires_at=timezone.now() + TOKEN_TTL,
        trace_id=current_trace_id(),
    )

    RebalanceSellRow.objects.bulk_create(
        [
            RebalanceSellRow(
                batch=batch,
                instrument_id=row.symbol,
                quantity=row.quantity,
                line=row.line,
                validation_state=RowState.VALID,
            )
            for row in result.rows
        ]
    )

    record(
        "rebalance.uploaded",
        actor=uploaded_by,
        entity=batch,
        after={
            "filename": source_filename,
            "rows": len(result.rows),
            "symbols": sorted(row.symbol for row in result.rows),
            "content_hash": result.content_hash,
        },
    )
    logger.info(
        "rebalance sell uploaded",
        extra=fields(rows=len(result.rows), uploaded_by=uploaded_by),
    )
    return batch


def preview(batch: RebalanceSell) -> list[dict]:
    """What executing this batch would do, and what it would realise.

    The gain figures are what the lots say today. They are shown because a
    person selling shares should see what the sale realises before confirming
    it, not afterwards.
    """
    from configuration.store import current
    from rebalance.ladder import sold_by_row

    # The sale consumes only its own book's lots (I6): the mode it will run in.
    book = current().order_mode
    # Sold so far from the fills once the batch has run -- the count settle
    # writes to the rows arrives only at the end of the day.
    sold = sold_by_row(batch) if batch.stream else {}
    rows = []
    for row in batch.rows.select_related("instrument").order_by("line"):
        lots = list(fifo.open_lots(row.instrument_id, book=book, lock=False))

        # The lots FIFO would take, oldest first, and how much of each. This
        # is what the lots say today; the fills decide what is actually taken.
        taking, still = [], row.quantity
        for lot in lots:
            if still <= 0:
                break
            take = min(still, lot.remaining_quantity)
            taking.append(
                {
                    "acquired_on": lot.acquired_on,
                    "take": take,
                    "of": lot.remaining_quantity,
                    "cost_per_unit": lot.cost_per_unit,
                    "cost": lot.cost_per_unit * take,
                }
            )
            still -= take

        rows.append(
            {
                "symbol": row.instrument_id,
                "quantity": row.quantity,
                "sold": max(sold.get(row.instrument_id, 0), row.sold_quantity),
                "held": fifo.available(row.instrument_id, book=book),
                "oldest_lot": lots[0].acquired_on.isoformat() if lots else None,
                "basis_of_oldest": str(lots[0].cost_per_unit) if lots else None,
                "lots": taking,
                "cost": sum((lot["cost"] for lot in taking), Decimal("0")),
            }
        )
    return rows


def start(
    batch: RebalanceSell,
    *,
    token: str,
    actor: str,
    trading_date: date | None = None,
    moment=None,
) -> RebalanceSell:
    """Execute a batch, taken in turn with every round, cancel and settle.

    Under the order lock, waiting a few seconds for a round in flight: two
    executes at once each see the other's shares as spoken for, and one that
    races the evening's settle either lands before its sweep or finds the
    day settled.
    """
    from orders.idempotency import order_lock
    from orders.overrides import STOP_WAIT_SECONDS

    with order_lock(wait_seconds=STOP_WAIT_SECONDS):
        return _start(batch, token=token, actor=actor, trading_date=trading_date, moment=moment)


@transaction.atomic
def _start(
    batch: RebalanceSell,
    *,
    token: str,
    actor: str,
    trading_date: date | None = None,
    moment=None,
) -> RebalanceSell:
    """Begin executing, if the token still stands for what was previewed.

    Single use and time limited: a stale tab cannot sell a portfolio somebody
    was looking at an hour ago, and a double submit cannot sell twice. And
    only when the day can take a sale (`why_not_now`): a trading day, in
    paper, before the close, after the day's plan exists, not settled, with
    no name the plan buys and enough shares for every running batch.
    """
    from configuration.pause import PauseError, require_running

    # Stopping has to mean stopping. The ladder and the fallback already
    # refuse while paused, and starting a sale was the one way round them --
    # a batch begun here would place on the next tick of its own stream.
    try:
        require_running("Executing a rebalance sell")
    except PauseError as paused:
        raise ExecutionRefused(str(paused)) from paused

    moment = moment or timezone.localtime()
    trading_date = trading_date or today_ist()
    locked = RebalanceSell.objects.select_for_update().get(pk=batch.pk)

    if locked.state != BatchState.DRAFT:
        raise ExecutionRefused(f"This batch is already {locked.get_state_display().lower()}.")
    if not locked.token_is_live:
        raise ExecutionRefused(
            "That preview has expired or has already been used. Upload the file again."
        )
    if not secrets.compare_digest(locked.execute_token, token):
        raise ExecutionRefused("That confirmation does not match the preview.")
    if refusal := why_not_now(locked, trading_date=trading_date, moment=moment):
        raise ExecutionRefused(refusal)

    from audit.context import trace_id_for

    locked.state = BatchState.RUNNING
    locked.trading_date = trading_date
    locked.stream = f"rebalance-{str(locked.id)[:8]}"
    locked.token_used_at = timezone.now()
    locked.started_at = moment
    # The batch sells on its trading day, whatever day it was uploaded on.
    locked.trace_id = trace_id_for(trading_date)
    locked.save(
        update_fields=[
            "state",
            "trading_date",
            "stream",
            "token_used_at",
            "started_at",
            "trace_id",
        ]
    )

    record(
        "rebalance.started",
        actor=actor,
        entity=locked,
        after={
            "trading_date": str(locked.trading_date),
            "stream": locked.stream,
            "rows": locked.rows.count(),
        },
    )
    logger.warning(
        "rebalance sell started",
        extra=fields(batch=str(locked.id), stream=locked.stream, actor=actor),
    )
    return locked


def why_not_now(batch: RebalanceSell, *, trading_date: date, moment) -> str | None:
    """Why this batch cannot start selling on *trading_date* now, or None.

    A sale needs a day that can take it: the market open that day, paper
    mode (a live sale's fills are not yet recorded), a day not yet settled
    (its ledger row could never show the money), time left before the close,
    the day's plan already made -- which is what makes "never sell what the
    day is buying" checkable (D-S2) -- and shares enough for this batch after
    what every other running batch still means to sell.
    """
    from datetime import datetime

    from configuration.store import current as current_configuration
    from engine.ledger import day_settled
    from engine.models import TradeSheet
    from trading_calendar.sessions import is_trading_day

    config = current_configuration()
    if not is_trading_day(trading_date):
        return f"{trading_date} is not a trading day."
    if config.is_live:
        return "Rebalance sells run in paper only until live sale fills are recorded."
    if day_settled(trading_date):
        return f"{trading_date} has settled; a sale now could never reach its ledger row."
    close_end = datetime.combine(trading_date, config.close_window_end, tzinfo=moment.tzinfo)
    if moment >= close_end:
        return f"The close ended at {config.close_window_end:%H:%M}; nothing more sells today."
    if not TradeSheet.objects.filter(trading_date=trading_date).exists():
        return (
            "Execute after the day's 09:45 plan exists: a sale is checked against "
            "what the day is buying, and there is nothing to check yet."
        )

    buying = [
        row.instrument_id
        for row in batch.rows.all()
        if _is_being_bought_today(row.instrument_id, trading_date)
    ]
    if buying:
        return f"Today's plan buys {', '.join(buying)}; a name is never sold the day it is bought."

    short = _short_across_batches(batch, trading_date=trading_date)
    if short:
        return f"Not enough shares for every running sale: {', '.join(short)}."
    return None


def _short_across_batches(batch: RebalanceSell, *, trading_date: date) -> list[str]:
    """Names whose lots, less what other running batches still mean to sell,
    fall short of this batch's quantity."""
    from rebalance.ladder import sold_by_row

    others = RebalanceSell.objects.filter(
        state=BatchState.RUNNING, trading_date=trading_date
    ).exclude(pk=batch.pk)
    promised: dict[str, int] = {}
    for other in others:
        sold = sold_by_row(other)
        for row in other.rows.all():
            unsold = max(0, row.quantity - sold.get(row.instrument_id, 0))
            promised[row.instrument_id] = promised.get(row.instrument_id, 0) + unsold

    from configuration.store import current

    # Only the lots of the book the sale runs in can cover it (I6).
    book = current().order_mode
    held = {
        row.instrument_id: fifo.available(row.instrument_id, book=book) for row in batch.rows.all()
    }
    return [
        f"{row.instrument_id} (holds {held[row.instrument_id]}, "
        f"{promised.get(row.instrument_id, 0)} already being sold)"
        for row in batch.rows.all()
        if held[row.instrument_id] - promised.get(row.instrument_id, 0) < row.quantity
    ]


def names_sold_today(trading_date: date) -> set[str]:
    """Every name with a sell resting or filled today, in any stream.

    What no plan of the day may buy (D-S2): a batch cancelled after it sold
    no longer shows in `instruments_with_a_live_sell`, and CNC would still
    net a buy of its name against the sale. A sell cancelled or rejected
    without a fill sold nothing and keeps nothing out.
    """
    from django.db.models import Q

    from orders.models import Order, OrderSide
    from orders.state import TERMINAL

    return set(
        Order.objects.filter(trading_date=trading_date, side=OrderSide.SELL)
        .filter(Q(filled_quantity__gt=0) | ~Q(state__in=list(TERMINAL)))
        .values_list("instrument_id", flat=True)
    )


def cancel(batch: RebalanceSell, *, actor: str, reason: str, broker) -> RebalanceSell:
    """Stop a running batch (M32a), after any round in flight has finished.

    Under the order lock, as Stop today is: a sell round that had already read
    the batch as running would otherwise place after the cancel. Placement
    stops, open orders are cancelled, and fills already taken stand: there is
    no way to un-sell shares, so the batch closes from its fills -- partial,
    or cancelled when nothing sold -- rather than pretending it did not run.
    """
    from orders.idempotency import order_lock
    from orders.overrides import STOP_WAIT_SECONDS

    with order_lock(wait_seconds=STOP_WAIT_SECONDS):
        return _cancel(batch, actor=actor, reason=reason, broker=broker)


@transaction.atomic
def _cancel(batch: RebalanceSell, *, actor: str, reason: str, broker) -> RebalanceSell:
    from orders import placement
    from orders.models import Order
    from orders.state import TERMINAL
    from rebalance.ladder import close_batch

    locked = RebalanceSell.objects.select_for_update().get(pk=batch.pk)
    if locked.state != BatchState.RUNNING:
        raise ExecutionRefused(
            f"Only a running batch can be cancelled; this one is "
            f"{locked.get_state_display().lower()}."
        )

    open_orders = Order.objects.filter(
        trading_date=locked.trading_date, stream=locked.stream
    ).exclude(state__in=list(TERMINAL))
    for order in open_orders:
        placement.cancel(order, broker=broker)

    closed = close_batch(locked, reason=reason[:255], actor=actor)
    sold = sum(row.sold_quantity for row in closed.rows.all())

    record(
        "rebalance.cancelled",
        actor=actor,
        entity=closed,
        after={"reason": reason, "sold": sold, "state": closed.state},
    )
    logger.warning(
        "rebalance sell cancelled",
        extra=fields(batch=str(closed.id), sold=sold, reason=reason),
    )
    return closed


def instruments_with_a_live_sell(trading_date: date | None = None) -> set[str]:
    """Names the daily buy must not select (the other half of the interlock).

    A buy and a sell of the same name on the same day net off in CNC, and the
    shares never move.
    """
    trading_date = trading_date or today_ist()
    running = RebalanceSell.objects.filter(
        state=BatchState.RUNNING, trading_date=trading_date
    ).values_list("id", flat=True)

    return set(
        RebalanceSellRow.objects.filter(batch_id__in=running).values_list(
            "instrument_id", flat=True
        )
    )
