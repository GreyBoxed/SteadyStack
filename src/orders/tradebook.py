"""A past live day's fills, from Zerodha Console's tradebook export (B2).

Kite's `trades()` holds today's trades only. The rounds and the evening
settle record a live day's fills from it; a day that did not settle before
midnight IST has lost that source, and Console's tradebook (Reports →
Tradebook, downloaded as CSV) is the one left.

The columns are Console's documented tradebook export. No real file has been
seen yet: the first one must be checked against `COLUMNS` (with
`--dry-run`) before it is imported. A file carrying a column this does not
know, or missing one it needs, is refused whole rather than read by guess.

Nothing is written unless every row is accounted for: each trade names one
of this system's live orders by its Kite order id or the exchange's order
number, on that order's own trading date, for its symbol and side, and never
for more than the order asked. A trade naming no order here goes to its day's
one live order of that symbol and side, when there is exactly one and it has
no exchange number yet -- the order the box never saw in the order book --
and the import says so. A trade already recorded -- by a round, the
settle or an earlier import -- is skipped by its trade id.

Every other live order of an imported day still open here is levelled from
its fills (`placement.expire_unseen`): it ended with the day, and Kite no
longer holds it to be asked.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from audit.events import record
from orders.state import TERMINAL

#: Console's tradebook columns. `symbol` is Console's name for the trading
#: symbol; `tradingsymbol` (Kite's) is accepted in its place.
REQUIRED = (
    "trade_date",
    "exchange",
    "trade_type",
    "quantity",
    "price",
    "trade_id",
    "order_id",
    "order_execution_time",
)
SYMBOL = ("symbol", "tradingsymbol")
#: Present in Console's export, read by nothing here.
IGNORED = ("isin", "segment", "series", "auction")
#: In newer exports, and empty for an equity trade. Ignored while empty; a
#: value is a derivative this command does not know how to read.
EMPTY_FOR_EQUITY = ("expiry_date",)
COLUMNS = frozenset((*REQUIRED, *SYMBOL, *IGNORED, *EMPTY_FOR_EQUITY))


class TradebookError(Exception):
    """The file was refused; nothing was written."""


@dataclass(frozen=True)
class Trade:
    line: int
    trade_id: str
    order_id: str
    symbol: str
    side: str
    trade_date: date
    quantity: int
    price: Decimal
    executed_at: datetime


@dataclass
class Imported:
    recorded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    orders: list[str] = field(default_factory=list)
    #: The day's other live orders, closed with the day (NI1).
    levelled: list[str] = field(default_factory=list)
    #: Console order ids no order here carries, each matched to the day's one
    #: live order of its symbol and side, as "id -> tag" (I-1).
    matched: list[str] = field(default_factory=list)


def parse(raw: bytes) -> list[Trade]:
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    header = [name.strip() for name in (reader.fieldnames or [])]
    _check_columns(header)
    reader.fieldnames = header
    symbol_column = "symbol" if "symbol" in header else "tradingsymbol"

    trades = []
    for line, raw_row in enumerate(reader, start=2):
        row = {key: (value or "").strip() for key, value in raw_row.items() if key}
        if not any(row.values()):
            continue
        trades.append(_trade(line, row, symbol_column))
    if not trades:
        raise TradebookError("The file holds no trades.")
    return trades


def _check_columns(header: list[str]) -> None:
    unknown = sorted(set(header) - COLUMNS)
    missing = [name for name in REQUIRED if name not in header]
    if not set(SYMBOL) & set(header):
        missing.append("symbol")
    if unknown or missing:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown {', '.join(unknown)}")
        raise TradebookError(
            f"Not a Console tradebook as this command knows it ({'; '.join(parts)}). "
            f"Expected the columns {', '.join(sorted(COLUMNS))}. If Console's export has "
            "changed, check the file by hand and update orders/tradebook.py."
        )


def _trade(line: int, row: dict, symbol_column: str) -> Trade:
    for name in EMPTY_FOR_EQUITY:
        if row.get(name):
            raise TradebookError(
                f"Line {line}: {name} is {row[name]!r}; an equity trade leaves it empty."
            )
    try:
        trade_date = date.fromisoformat(row["trade_date"])
        executed = datetime.fromisoformat(row["order_execution_time"])
        quantity = _whole(row["quantity"])
        price = Decimal(row["price"])
    except (ValueError, InvalidOperation) as exc:
        raise TradebookError(f"Line {line}: {exc}.") from exc
    side = row["trade_type"].lower()
    if side not in ("buy", "sell"):
        raise TradebookError(f"Line {line}: trade_type {row['trade_type']!r} is not buy or sell.")
    if quantity <= 0 or price <= 0:
        raise TradebookError(f"Line {line}: quantity and price must be positive.")
    if not row["trade_id"] or not row["order_id"]:
        raise TradebookError(f"Line {line}: a trade needs its trade_id and order_id.")
    if timezone.is_naive(executed):
        executed = timezone.make_aware(executed, timezone.get_default_timezone())
    return Trade(
        line=line,
        trade_id=row["trade_id"],
        order_id=row["order_id"],
        symbol=row[symbol_column].upper(),
        side=side,
        trade_date=trade_date,
        quantity=quantity,
        price=price,
        executed_at=executed,
    )


def _whole(raw: str) -> int:
    """A share count, written `2` or `2.000000`; `2.5` is refused."""
    quantity = Decimal(raw)
    if quantity != quantity.to_integral_value():
        raise ValueError(f"quantity {raw!r} is not a whole number of shares")
    return int(quantity)


def import_trades(trades: list[Trade], *, actor: str, dry_run: bool = False) -> Imported:
    """Record every trade not yet recorded, book it, and level the day's orders.

    Under the order lock, as every order-touching task is: a `settle_day
    --date` of the same day must not read the fills half-written. Refuses
    the whole file on any trade it cannot account for. A dry run checks
    everything and writes nothing.
    """
    from orders import idempotency

    try:
        with idempotency.order_lock():
            return _import(trades, actor=actor, dry_run=dry_run)
    except idempotency.Busy as busy:
        raise TradebookError(f"{busy} Try again in a moment.") from busy


@transaction.atomic
def _import(trades: list[Trade], *, actor: str, dry_run: bool) -> Imported:
    from audit.context import bind_trading_day, reset_trace_id, today_ist
    from engine.ledger import day_settled
    from orders.models import Fill, Order, OrderMode, OrderSide

    today = today_ist()
    ids = {trade.order_id for trade in trades}
    # Console may name an order by Kite's id or by the exchange's (NI1).
    orders = {}
    for order in Order.objects.select_for_update().filter(
        Q(kite_order_id__in=ids) | Q(exchange_order_id__in=ids)
    ):
        for known in (order.kite_order_id, order.exchange_order_id):
            if known in ids:
                orders[known] = order
    result = Imported()
    _match_by_symbol_and_side(trades, orders, result=result)
    known = dict(
        Fill.objects.filter(kite_trade_id__in=[t.trade_id for t in trades]).values_list(
            "kite_trade_id", "order_id"
        )
    )

    new_by_order: dict = {}
    seen: set[str] = set()
    for trade in trades:
        order = orders.get(trade.order_id)
        where = f"Line {trade.line} (trade {trade.trade_id})"
        if order is None or order.mode != OrderMode.LIVE:
            raise TradebookError(_unmatched(trade, orders, where))
        if trade.trade_date >= today:
            raise TradebookError(
                f"{where}: {trade.trade_date} is today; Kite's own trade book still holds "
                "today's trades, and the rounds and settle_day read it."
            )
        if trade.trade_date != order.trading_date:
            raise TradebookError(
                f"{where}: traded on {trade.trade_date}, but order {trade.order_id} is "
                f"{order.trading_date}'s."
            )
        if trade.symbol != order.instrument_id:
            raise TradebookError(
                f"{where}: {trade.symbol}, but order {trade.order_id} is for {order.instrument_id}."
            )
        expected = "buy" if order.side == OrderSide.BUY else "sell"
        if trade.side != expected:
            raise TradebookError(
                f"{where}: a {trade.side}, but order {trade.order_id} is a {expected}."
            )
        if day_settled(order.trading_date):
            raise TradebookError(
                f"{where}: {order.trading_date} has already settled, and its ledger row "
                "cannot take fills recorded after it. Stop and reconcile by hand."
            )
        if trade.trade_id in known and known[trade.trade_id] != order.pk:
            raise TradebookError(
                f"{where}: trade {trade.trade_id} is already recorded on another order. "
                "Stop and reconcile by hand."
            )
        if trade.trade_id in known or trade.trade_id in seen:
            result.skipped.append(trade.trade_id)
            continue
        seen.add(trade.trade_id)
        new_by_order.setdefault(order, []).append(trade)

    for order, new in new_by_order.items():
        already = sum(Fill.objects.filter(order=order).values_list("quantity", flat=True))
        total = already + sum(trade.quantity for trade in new)
        if total > order.quantity:
            raise TradebookError(
                f"Order {new[0].order_id} would record {total} shares filled, more than the "
                f"{order.quantity} it asked for."
            )

    days = sorted({trade.trade_date for trade in trades})
    unseen = list(
        Order.objects.filter(trading_date__in=days, mode=OrderMode.LIVE)
        .exclude(state__in=list(TERMINAL))
        .exclude(pk__in=[order.pk for order in new_by_order])
        .order_by("trading_date", "pk")
    )

    if dry_run:
        result.recorded = [t.trade_id for new in new_by_order.values() for t in new]
        result.orders = sorted(new[0].order_id for new in new_by_order.values())
        result.levelled = [order.tag for order in unseen]
        transaction.set_rollback(True)
        return result

    for day in days:
        # Everything written for a day -- fills, their entries, the orders'
        # audit events -- carries that day's trace, whenever it is imported.
        token = bind_trading_day(day)
        try:
            _write_day(day, new_by_order, unseen, actor=actor, result=result)
        finally:
            reset_trace_id(token)
    return result


def _unseen_candidates(trade: Trade, orders: dict) -> list:
    """The live orders a trade naming no known order could belong to: the
    non-terminal ones of its date, symbol and side that carry no exchange
    number -- the orders the box never saw in the order book -- and that no
    other id in the file already names (gap m1).

    A sibling that carries its own exchange number is named by it and is no
    rival; a REJECTED or confirmed-cancelled order never traded.
    """
    from orders.models import Order, OrderMode, OrderSide
    from orders.state import TERMINAL

    side = OrderSide.BUY if trade.side == "buy" else OrderSide.SELL
    named = {order.pk for order in orders.values()}
    return [
        order
        for order in Order.objects.select_for_update()
        .filter(
            mode=OrderMode.LIVE,
            trading_date=trade.trade_date,
            instrument_id=trade.symbol,
            side=side,
            exchange_order_id="",
        )
        .exclude(state__in=list(TERMINAL))
        .order_by("pk")
        if order.pk not in named
    ]


def _match_by_symbol_and_side(trades: list[Trade], orders: dict, *, result: Imported) -> None:
    """Give a trade whose order id nothing here carries the day's one order
    of its symbol and side the box never saw (I-1, gap m1).

    An order the box never saw in the order book has no exchange number,
    and Console may name it by nothing else. When exactly one such order is
    left (`_unseen_candidates`), it is the order. Anything else is left
    unmatched, and refused.
    """
    for trade in trades:
        if trade.order_id in orders:
            continue
        candidates = _unseen_candidates(trade, orders)
        if len(candidates) != 1:
            continue
        order = candidates[0]
        orders[trade.order_id] = order
        result.matched.append(f"{trade.order_id} -> {order.tag}")


def _unmatched(trade: Trade, orders: dict, where: str) -> str:
    """Why a trade naming no order here is refused, and what to do."""
    candidates = _unseen_candidates(trade, orders)
    if candidates:
        tags = ", ".join(order.tag for order in candidates)
        return (
            f"{where}: order {trade.order_id} is not one of this system's live orders by "
            f"that id, and {len(candidates)} open live {trade.side}(s) of {trade.symbol} on "
            f"{trade.trade_date} have no exchange number yet ({tags}). If it is one of those, "
            "record it -- from Kite's order history or the contract note -- with manage.py "
            f"record_exchange_order_id --order TAG --exchange-id {trade.order_id} --actor NAME "
            "(OPERATIONS.md §7), then import again."
        )
    return (
        f"{where}: order {trade.order_id} is not one of this system's live orders. "
        "A trade made at the broker is recorded as an external transaction instead."
    )


def _write_day(day: date, new_by_order: dict, unseen: list, *, actor: str, result: Imported):
    from orders import booking
    from orders.models import Fill
    from orders.placement import apply_event, expire_unseen
    from orders.state import OrderEvent, is_terminal

    recorded: list[str] = []
    named: list[str] = []
    for order, new in new_by_order.items():
        if order.trading_date != day:
            continue
        for trade in new:
            fill = Fill.objects.create(
                order=order,
                kite_trade_id=trade.trade_id,
                quantity=trade.quantity,
                price=trade.price,
                executed_at=trade.executed_at,
                trace_id=order.trace_id,
            )
            booking.book(fill)
            recorded.append(trade.trade_id)

        # Level the order with its fills. A DAY order still open here after
        # its day has ended at the exchange: filled in full, or cancelled at
        # the close with its fills standing.
        filled = sum(Fill.objects.filter(order=order).values_list("quantity", flat=True))
        if filled != order.filled_quantity:
            order.filled_quantity = filled
            order.save(update_fields=["filled_quantity", "updated_at"])
        if not is_terminal(order.state):
            event = OrderEvent.FILLED if filled >= order.quantity else OrderEvent.CANCELLED
            apply_event(order, event, detail="levelled from the Console tradebook")
        named.append(new[0].order_id)

    # The day's other live orders, which traded nothing Console lists: ended
    # at the exchange with the day, and unknown to Kite's books by now.
    levelled = []
    for order in unseen:
        if order.trading_date == day:
            expire_unseen(order, detail="the day ended; levelled on the tradebook import")
            levelled.append(order.tag)

    result.recorded += recorded
    result.orders += named
    result.levelled += levelled
    record(
        "orders.tradebook_imported",
        actor=actor,
        entity_type="orders.Fill",
        entity_id=str(day),
        after={
            "recorded": recorded,
            "skipped": result.skipped,
            "orders": named,
            "levelled": levelled,
            "matched_by_symbol_and_side": result.matched,
        },
    )
