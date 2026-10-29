"""The trading board and one instrument's day on it, read from what was stored.

Everything here is rebuilt from rows: orders, the ticks that read the book,
the fills and the broker calls. Nothing is fetched to draw these pages except
the last traded price, and a page that cannot get one says so and carries on
-- the depth ladder is the book *as the tick stored it*, which is exactly the
book the order was priced against.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from web import charts

ZERO = Decimal("0")

#: The orders tab groups by what the operator is waiting on first.
OPEN_FIRST = {"PLACED": 0, "OPEN": 0, "PARTIALLY_FILLED": 0}


def _local(moment: datetime | None) -> datetime | None:
    return timezone.localtime(moment) if moment else None


def intended(day: date, *, symbol: str, stream: str, sheet, orders) -> int:
    """How many shares the day meant to move for one instrument in one stream.

    Never the sum of orders placed so far: the ladder slices, so that sum
    grows through the day and a half-done day would read as complete. The
    daily buy's intent is its tradesheet; a sell batch's is its uploaded row.
    """
    from orders.models import DAILY_BUY_STREAM
    from rebalance.models import RebalanceSellRow

    if stream == DAILY_BUY_STREAM:
        if sheet is not None:
            planned = sheet.bought_quantities()
            if symbol in planned:
                return planned[symbol]
    else:
        rows = RebalanceSellRow.objects.filter(
            batch__stream=stream, batch__trading_date=day, instrument_id=symbol
        ).values_list("quantity", flat=True)
        if rows:
            return sum(rows)
    return sum(o.quantity for o in orders if o.state != "REJECTED")


def buy_sheet(day: date):
    """The tradesheet that decided the day's buy.

    The latest one the daily-buy orders hang off when there are any -- a
    later version can exist that decided nothing, a sell batch's carrier for
    one -- and otherwise the latest. The exception is a replan: a day stopped
    and replanned is run by its replanned version from the moment it exists,
    before its first order.
    """
    from engine.models import TradeSheet
    from orders.models import DAILY_BUY_STREAM, Order

    sheets = TradeSheet.objects.filter(trading_date=day).select_related("instrument")
    latest = sheets.order_by("-version").first()
    if latest is not None and latest.quantity and (latest.ranking_snapshot or {}).get("replan"):
        return latest

    owner = (
        Order.objects.filter(trading_date=day, stream=DAILY_BUY_STREAM)
        .order_by("-tradesheet__version")
        .values_list("tradesheet_id", flat=True)
        .first()
    )
    if owner:
        return sheets.get(pk=owner)
    return latest


def _avg_fill(fills) -> Decimal | None:
    """What our fills averaged, weighted by quantity -- not the market's VWAP."""
    quantity = sum(fill.quantity for fill in fills)
    if not quantity:
        return None
    return sum((fill.price * fill.quantity for fill in fills), ZERO) / quantity


@dataclass(frozen=True)
class Level:
    price: Decimal
    quantity: int
    orders: int
    pct: float
    yours: int = 0


def depth_ladder(
    snapshot: dict,
    *,
    resting: dict[Decimal, int] | None = None,
    resting_asks: dict[Decimal, int] | None = None,
) -> dict:
    """Five asks above the spread, five bids below, as Kite returned them.

    Asks are listed highest first so the ladder reads top to bottom as a
    price axis. `resting` is quantity this system had open at each price,
    which is the only way to see an order sitting in the queue.
    """
    resting = resting or {}
    resting_asks = resting_asks or {}

    def level_list(side: str) -> list[dict]:
        return [
            {
                "price": Decimal(str(row.get("price", 0))),
                "quantity": int(row.get("quantity") or 0),
                "orders": int(row.get("orders") or 0),
            }
            for row in (snapshot.get(side) or [])
            if row.get("price")
        ]

    asks = level_list("sell")
    bids = level_list("buy")
    largest = max((row["quantity"] for row in asks + bids), default=0) or 1

    def shaped(rows: list[dict], mine: dict) -> list[Level]:
        return [
            Level(
                price=row["price"],
                quantity=row["quantity"],
                orders=row["orders"],
                pct=round(row["quantity"] / largest * 100, 1),
                yours=mine.get(row["price"], 0),
            )
            for row in rows
        ]

    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    spread = (best_ask - best_bid) if best_bid and best_ask else None

    return {
        # A buy rests among the bids, a sell among the asks.
        "asks": list(reversed(shaped(asks, resting_asks))),
        "bids": shaped(bids, resting),
        "spread": spread,
        "bid_qty": sum(row["quantity"] for row in bids),
        "ask_qty": sum(row["quantity"] for row in asks),
        "empty": not asks and not bids,
    }


def _resting_at(orders, moment: datetime, *, side: str = "BUY") -> dict[Decimal, int]:
    """What this system had open in the book at a moment, by price.

    An order counts if it was placed by then and had neither filled in full
    nor been cancelled -- reconstructed from its own timestamps, because the
    tick stored the market's book, not ours.
    """
    resting: dict[Decimal, int] = {}
    for order in orders:
        if order.side != side or order.limit_price is None or order.placed_at > moment:
            continue
        filled = sum(fill.quantity for fill in order.fills.all() if fill.executed_at <= moment)
        if order.state in {"CANCELLED", "REJECTED"} and order.updated_at <= moment:
            continue
        left = order.quantity - filled
        if left > 0:
            # Decimal hashes by value, so 1650.50 and 1650.5 are one key.
            resting[order.limit_price] = resting.get(order.limit_price, 0) + left
    return resting


def _order_end(order, now: datetime) -> datetime:
    """Where an order's bar stops: its last fill, its cancellation, or now."""
    fills = list(order.fills.all())
    if order.state == "COMPLETE" and fills:
        return max(fill.executed_at for fill in fills)
    if order.state in {"CANCELLED", "REJECTED"}:
        return order.updated_at
    return now


def lifespan_chart(*, day: date, orders, ticks, config, now: datetime) -> dict:
    """Every order as a bar at its limit, placement to end, over the mid price.

    The x axis runs from the first thing that happened to the session's
    close, so the 15:00 fallback always has somewhere to stand.
    """
    width, height = 868, 250
    left, right, top, bottom = 72, width - 8, 12, height - 30

    placed = [o for o in orders if o.limit_price is not None]
    mids = [
        (_local(t.fired_at), (t.best_bid + t.best_ask) / 2)
        for t in ticks
        if t.best_bid and t.best_ask
    ]
    fills = [(f, o) for o in orders for f in o.fills.all()]

    if not placed and not mids:
        return {"empty": True, "width": width, "height": height}

    start_min = charts.minutes(datetime.combine(day, config.tradesheet_time))
    times = [charts.minutes(_local(o.placed_at)) for o in placed] + [
        charts.minutes(m) for m, _ in mids
    ]
    if times:
        start_min = min(start_min, min(times))
    end_min = charts.minutes(datetime.combine(day, time(15, 30)))

    prices = [float(o.limit_price) for o in placed] + [float(p) for _, p in mids]
    prices += [float(f.price) for f, _ in fills]
    lo, hi = min(prices), max(prices)
    # A day priced at one level would otherwise stretch a paisa across the
    # whole chart. Never show less than a tenth of a percent of the price.
    floor_span = max(prices) * 0.001
    if hi - lo < floor_span:
        middle = (hi + lo) / 2
        lo, hi = middle - floor_span / 2, middle + floor_span / 2
    lo, hi = charts.padded(lo, hi, pad=0.15)

    x = charts.Scale(start_min, end_min, left, right)
    y = charts.Scale(lo, hi, top, bottom, flip=True)

    local_now = _local(now)
    now_min = charts.minutes(local_now) if local_now.date() == day else None
    if now_min is not None and not (start_min <= now_min <= end_min):
        now_min = None

    def clamp(value: float) -> float:
        return max(start_min, min(end_min, value))

    bars = []
    # Orders at one price would draw as one bar; nudge each repeat down a few
    # pixels, as the design does, so every order stays countable.
    seen_at_price: dict[Decimal, int] = {}
    bar_y: dict = {}
    for index, order in enumerate(sorted(placed, key=lambda o: o.placed_at), start=1):
        begin = charts.minutes(_local(order.placed_at))
        end = charts.minutes(_local(_order_end(order, now)))
        if local_now.date() != day and order.state not in {"COMPLETE", "CANCELLED", "REJECTED"}:
            end = end_min
        still_open = order.state not in {"COMPLETE", "CANCELLED", "REJECTED"}
        repeat = seen_at_price.get(order.limit_price, 0)
        seen_at_price[order.limit_price] = repeat + 1
        py = round(y(order.limit_price) + min(repeat, 6) * 4, 1)
        bar_y[order.pk] = (order.limit_price, py)
        bars.append(
            {
                "x1": x(clamp(begin)),
                "x2": max(x(clamp(end)), x(clamp(begin)) + 2),
                "y": py,
                # Only the first order at a price is labelled; the rest would overprint.
                "label": f"#{index}" if repeat == 0 else "",
                "lx": round(x(clamp(begin)) - 18, 1),
                "ly": round(py + 3, 1),
                "state": "open" if still_open else order.state.lower(),
            }
        )

    fallback = charts.minutes(datetime.combine(day, config.market_fallback_time))
    hours = [h * 60 for h in range(int(start_min // 60) + 1, int(end_min // 60) + 1)]

    return {
        "empty": False,
        "width": width,
        "height": height,
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "mid": charts.path([(x(charts.minutes(m)), y(p)) for m, p in mids]),
        "bars": bars,
        "fills": [
            {
                "x": x(clamp(charts.minutes(_local(f.executed_at)))),
                # On its order's bar when it filled at the limit, else at its own price.
                "y": bar_y[o.pk][1] if bar_y.get(o.pk, (None,))[0] == f.price else y(f.price),
            }
            for f, o in fills
        ],
        "grid": [
            {"y": y(v), "ty": round(y(v) + 3.5, 1), "label": f"{v:,.2f}"}
            for v in charts.nice_ticks(lo, hi)
        ],
        "x_labels": [{"x": x(m), "label": charts.hhmm(m)} for m in hours],
        "fallback_x": x(fallback) if start_min <= fallback <= end_min else None,
        "fallback_at": config.market_fallback_time,
        "now_x": x(now_min) if now_min is not None else None,
        "future_w": round(right - x(now_min), 1) if now_min is not None else 0,
    }


def broker_calls(*, trace_id: str, instrument, kite_ids: list[str]):
    """The calls that touched this instrument or its orders on the day."""
    from audit.models import BrokerCall

    match = Q(request__instruments__contains=[instrument.instrument_key]) | Q(
        request__tradingsymbol=instrument.tradingsymbol
    )
    if kite_ids:
        match |= Q(request__order_id__in=kite_ids)

    rows = []
    for call in BrokerCall.objects.filter(trace_id=trace_id).filter(match).order_by("-occurred_at"):
        size = len(json.dumps(call.response, default=str)) if call.response is not None else 0
        rows.append(
            {
                "at": _local(call.occurred_at),
                "method": call.method,
                "endpoint": call.endpoint,
                "ok": not call.error,
                "error": call.error,
                "latency_ms": call.latency_ms,
                "bytes": size,
            }
        )
    return rows


def _instrument_ticks(day: date, instrument, *, sheet, is_daily: bool) -> list:
    """The ticks that read this instrument's book on a day, in round order."""
    from orders.models import DAILY_BUY_STREAM, NoPlaceReason, Tick

    tick_filter = Q(order__instrument=instrument)
    if is_daily:
        # The current plan's ticks for this name, plus its whole-day rows --
        # never a replaced version's, which would read as today's outcome.
        tick_filter |= Q(stream=DAILY_BUY_STREAM, tradesheet=sheet) & (
            Q(instrument=instrument) | Q(instrument__isnull=True)
        )
    ticks = list(
        Tick.objects.filter(trading_date=day)
        .filter(tick_filter)
        .exclude(no_place_reason=NoPlaceReason.FULLY_COMMITTED, instrument__isnull=False)
        .select_related("order")
    )
    ticks.sort(key=lambda t: (t.tick_no, t.stream))
    return ticks


def _stored_ladder(with_depth: list, orders: list, tick_no: int | None) -> dict | None:
    """The depth a tick stored: the chosen one, else the latest."""
    chosen = None
    if tick_no is not None:
        chosen = next((t for t in with_depth if t.tick_no == tick_no), None)
    if chosen is None and with_depth:
        chosen = with_depth[-1]
    if chosen is None:
        return None

    ladder = depth_ladder(
        chosen.depth_snapshot,
        resting=_resting_at(orders, chosen.fired_at, side="BUY"),
        resting_asks=_resting_at(orders, chosen.fired_at, side="SELL"),
    )
    ladder["tick"] = chosen
    ladder["ticks"] = [t.tick_no for t in with_depth]
    position = with_depth.index(chosen)
    ladder["previous"] = with_depth[position - 1].tick_no if position > 0 else None
    ladder["next"] = with_depth[position + 1].tick_no if position + 1 < len(with_depth) else None
    return ladder


def latest_depth(*, day: date, instrument) -> dict:
    """Only the depth panel: the book the latest round stored for this name.

    What a live instrument page swaps in each round, without rebuilding the
    chart, the broker calls and the rest of the board to draw one card.
    """
    from orders.models import Order

    sheet = buy_sheet(day)
    is_daily = bool(sheet and instrument.pk in sheet.bought_quantities())
    orders = list(
        Order.objects.filter(trading_date=day, instrument=instrument).prefetch_related("fills")
    )
    ticks = _instrument_ticks(day, instrument, sheet=sheet, is_daily=is_daily)
    with_depth = [t for t in ticks if t.depth_snapshot]
    return {"instrument": instrument, "ladder": _stored_ladder(with_depth, orders, None)}


def instrument_day(*, day: date, instrument, tick_no: int | None, price: Decimal | None) -> dict:
    """One instrument on one trading day: the whole Trading / symbol board."""
    from audit.context import trace_id_for
    from configuration.store import current as current_configuration
    from engine.ledger import day_settled, row_for
    from orders.models import DAILY_BUY_STREAM, Order
    from portfolio.models import Lot

    config = current_configuration()
    now = timezone.now()
    trace_id = trace_id_for(day)

    sheet = buy_sheet(day)
    is_daily = bool(sheet and instrument.pk in sheet.bought_quantities())

    orders = list(
        Order.objects.filter(trading_date=day, instrument=instrument)
        .prefetch_related("fills")
        .order_by("tick_no", "placed_at")
    )
    ticks = _instrument_ticks(day, instrument, sheet=sheet, is_daily=is_daily)

    fills = sorted(
        ((fill, order) for order in orders for fill in order.fills.all()),
        key=lambda pair: pair[0].executed_at,
    )
    fill_rows = [fill for fill, _ in fills]

    open_orders = [o for o in orders if not o.is_terminal]
    committed_value = sum(
        ((o.limit_price or ZERO) * o.unfilled_quantity for o in open_orders), ZERO
    )
    gross = sum((f.price * f.quantity for f in fill_rows), ZERO)
    charges = sum((f.charges for f in fill_rows), ZERO)
    filled = sum(f.quantity for f in fill_rows)
    streams = {o.stream for o in orders} or ({DAILY_BUY_STREAM} if is_daily else set())
    target = sum(
        intended(
            day,
            symbol=instrument.pk,
            stream=stream,
            sheet=sheet,
            orders=[o for o in orders if o.stream == stream],
        )
        for stream in streams
    )
    avg_price = _avg_fill(fill_rows)

    with_depth = [t for t in ticks if t.depth_snapshot]
    ladder = _stored_ladder(with_depth, orders, tick_no)

    last_price = price
    price_source = "live" if price is not None else ""
    if last_price is None and with_depth:
        latest = with_depth[-1]
        if latest.best_bid and latest.best_ask:
            last_price = (latest.best_bid + latest.best_ask) / 2
            price_source = f"mid at tick {latest.tick_no}"

    order_rows = []
    for order in sorted(orders, key=lambda o: (OPEN_FIRST.get(o.state, 1), o.tick_no)):
        order_fills = list(order.fills.all())
        order_rows.append(
            {
                "order": order,
                "open": not order.is_terminal,
                "age": (now - order.placed_at) if not order.is_terminal else None,
                "vs_last": (order.limit_price - last_price)
                if order.limit_price is not None and last_price is not None
                else None,
                "filled_at": _local(max(f.executed_at for f in order_fills))
                if order_fills
                else None,
                "fill_price": _avg_fill(order_fills),
            }
        )

    placing = [t for t in ticks if t.order_id]
    first_placed = min((_local(o.placed_at) for o in orders), default=None)
    fully_committed = None
    if target:
        running = 0
        for order in sorted(orders, key=lambda o: o.placed_at):
            if order.state == "REJECTED":
                continue
            running += order.quantity
            if running >= target:
                fully_committed = _local(order.placed_at)
                break

    timeline = [
        {"at": first_placed, "label": "first slice placed", "done": first_placed is not None},
        {
            "at": fully_committed,
            "label": "day quantity fully committed",
            "done": fully_committed is not None,
        },
        {
            "at": config.placement_window_close,
            "label": "placement window closes",
            "done": _passed(day, config.placement_window_close),
        },
        {
            "at": config.market_fallback_time,
            "label": f"cancel {len(open_orders) or 'any'} open; market for the rest, in rounds",
            "done": _passed(day, config.market_fallback_time),
        },
        {
            "at": config.close_window_end,
            "label": "the close's last round",
            "done": _passed(day, config.close_window_end),
        },
        {
            "at": time(15, 45),
            "label": "reconcile, settle carry, close the row",
            "done": day_settled(day),
        },
    ]

    cash = None
    if is_daily:
        settled = row_for(day, book=sheet.book)
        available = sheet.spendable_cash
        cash = {
            "new_money": available - sheet.carry_in,
            "carry_in": sheet.carry_in,
            "available": available,
            "spent": gross,
            "open": committed_value,
            "charges": charges,
            "projected": available - gross - committed_value - charges,
            "settled": settled,
        }

    lots = Lot.objects.filter(created_from_fill__in=fill_rows).count() if fill_rows else 0

    return {
        "day": day,
        "instrument": instrument,
        "trace_id": trace_id,
        "sheet": sheet,
        "is_daily": is_daily,
        "side": orders[0].get_side_display() if orders else ("Buy" if is_daily else ""),
        "streams": sorted({o.stream for o in orders}),
        "orders": order_rows,
        "open_count": len(open_orders),
        "open_prices": sorted({o.limit_price for o in open_orders if o.limit_price}),
        "committed_value": committed_value,
        "ticks": ticks,
        "placing_count": len(placing),
        "fills": [{"fill": f, "order": o} for f, o in fills],
        "filled": filled,
        "target": target,
        "fill_pct": min(100, round(filled / target * 100)) if target else 0,
        "avg_price": avg_price,
        "market_vwap": stored_vwaps(day).get(instrument.pk),
        "gross": gross,
        "charges": charges,
        "last_price": last_price,
        "price_source": price_source,
        "vs_avg": (
            (last_price - avg_price) if last_price is not None and avg_price is not None else None
        ),
        "ladder": ladder,
        "chart": lifespan_chart(day=day, orders=orders, ticks=ticks, config=config, now=now),
        "calls": broker_calls(
            trace_id=trace_id,
            instrument=instrument,
            kite_ids=[o.kite_order_id for o in orders if o.kite_order_id],
        ),
        "slice": {
            "floor": config.slice_quantity,
            "catch_up": config.catch_up_minutes,
            "ttl": config.order_ttl_minutes,
            "interval": config.placer_interval_seconds,
            "placed": len(orders),
            "by_tick": max((o.tick_no for o in orders), default=None),
        },
        "timeline": timeline,
        "cash": cash,
        "lots": lots,
    }


def _passed(day: date, moment: time) -> bool:
    now = timezone.localtime()
    return now.date() > day or (now.date() == day and now.time() >= moment)


#: The blotter's filters: the key in the URL, its label, and the states it keeps.
BLOTTER_FILTERS = [
    ("all", "All", lambda o: True),
    ("open", "Open", lambda o: o.state in {"PLACED", "OPEN", "PARTIALLY_FILLED"}),
    ("filled", "Filled", lambda o: o.filled_quantity > 0),
    ("closed", "Cancelled or rejected", lambda o: o.state in {"CANCELLED", "REJECTED"}),
    ("buy", "Buys", lambda o: o.side == "BUY"),
    ("sell", "Sells", lambda o: o.side == "SELL"),
]


def _source(stream: str) -> str:
    from orders.models import DAILY_BUY_STREAM

    return "daily accumulation" if stream == DAILY_BUY_STREAM else stream


def symbols_on(day: date) -> list[str]:
    """Every instrument the board for a day will show."""
    from engine.models import TradeSheet
    from orders.models import Order

    symbols = set(Order.objects.filter(trading_date=day).values_list("instrument_id", flat=True))
    symbols |= set(
        TradeSheet.objects.filter(trading_date=day, instrument__isnull=False).values_list(
            "instrument_id", flat=True
        )
    )
    sheet = buy_sheet(day)
    if sheet is not None:
        symbols |= set(sheet.bought_quantities())
    return sorted(symbols)


def stored_mids(day: date) -> dict[str, Decimal]:
    """The mid of the book each name's latest round read, by symbol.

    What a refreshing page prices with instead of asking Kite: the ladder
    already reads depth every round and stores it, so the number is at most
    one round old and costs a query rather than a broker call. A round that
    saw one side of the book only has no mid and is passed over.
    """
    from orders.models import Tick

    mids: dict[str, Decimal] = {}
    rows = (
        Tick.objects.filter(
            trading_date=day,
            instrument__isnull=False,
            best_bid__isnull=False,
            best_ask__isnull=False,
        )
        .order_by("instrument_id", "-fired_at")
        .values_list("instrument_id", "best_bid", "best_ask")
    )
    for symbol, bid, ask in rows:
        if symbol not in mids and bid > 0 and ask > 0:
            mids[symbol] = (bid + ask) / 2
    return mids


def stored_vwaps(day: date) -> dict[str, Decimal]:
    """The market's day VWAP each name's latest round stored, by symbol.

    Read from the rounds, as the mids are, so a refreshing board costs a
    query and not a broker call. A name no round has quoted with a VWAP is
    absent, and the board shows a dash.
    """
    from orders.models import Tick

    vwaps: dict[str, Decimal] = {}
    rows = (
        Tick.objects.filter(trading_date=day, instrument__isnull=False, vwap__isnull=False)
        .order_by("instrument_id", "-fired_at")
        .values_list("instrument_id", "vwap")
    )
    for symbol, vwap in rows:
        vwaps.setdefault(symbol, vwap)
    return vwaps


#: A daily buy in one of these has not finished placing, whatever is resting.
BUYING_STATES = frozenset({"DRAFT", "AWAITING_SESSION", "RUNNING"})


def board(*, day: date, prices: dict, show: str = "all") -> dict:
    """Every instrument with orders on a day, and every order in one blotter."""
    from configuration.store import current as current_configuration
    from orders.ladder import ladder_times
    from orders.models import DAILY_BUY_STREAM, NoPlaceReason, Order, Tick

    sheet = buy_sheet(day)
    orders = list(
        Order.objects.filter(trading_date=day)
        .select_related("instrument")
        .prefetch_related("fills")
        .order_by("placed_at")
    )

    groups: dict[tuple[str, str, str], list] = {}
    for order in orders:
        key = (order.instrument_id, order.side, order.stream)
        groups.setdefault(key, []).append(order)

    # The day's chosen instrument belongs on the board before its first order.
    if sheet:
        for symbol, quantity in sheet.bought_quantities().items():
            if quantity:
                groups.setdefault((symbol, "BUY", DAILY_BUY_STREAM), [])

    instruments = []
    market_vwaps = stored_vwaps(day)

    def daily_first(item):
        (symbol, _side, stream), _rows = item
        return (stream != DAILY_BUY_STREAM, symbol)

    for (symbol, side, stream), rows in sorted(groups.items(), key=daily_first):
        fills = [fill for order in rows for fill in order.fills.all()]
        filled = sum(fill.quantity for fill in fills)
        open_qty = sum(o.unfilled_quantity for o in rows if not o.is_terminal)
        target = intended(day, symbol=symbol, stream=stream, sheet=sheet, orders=rows)
        avg_price = _avg_fill(fills)
        last_price = prices.get(symbol)
        slip = (
            (avg_price - last_price) if avg_price is not None and last_price is not None else None
        )
        # Paper orders fill on the tick they are placed, so between ticks a
        # name that is a third bought often has nothing resting. Short of its
        # target while its day is still buying, it is working, not done.
        still_buying = (
            stream == DAILY_BUY_STREAM and sheet is not None and sheet.state in BUYING_STATES
        )
        working = any(not o.is_terminal for o in rows) or (
            filled < target and (not rows or still_buying)
        )
        if working:
            state = "WORKING"
        else:
            state = "COMPLETE" if target and filled >= target else "DONE"
        instruments.append(
            {
                "symbol": symbol,
                "side": side,
                "source": _source(stream),
                "target": target,
                "filled": filled,
                "open": open_qty,
                "filled_pct": min(100, round(filled / target * 100)) if target else 0,
                "open_pct": min(100, round(open_qty / target * 100)) if target else 0,
                "avg_price": avg_price,
                "vwap": market_vwaps.get(symbol),
                "last_price": last_price,
                "slip": slip,
                # Paying less than the market on a buy, or getting more on a sell, is good.
                "slip_good": None if slip is None else (slip <= 0 if side == "BUY" else slip >= 0),
                "value": sum((f.price * f.quantity for f in fills), ZERO),
                "last": _local(max((o.placed_at for o in rows), default=None)),
                "state": state,
            }
        )

    keep = next((test for key, _, test in BLOTTER_FILTERS if key == show), None)
    if keep is None:
        show, keep = "all", BLOTTER_FILTERS[0][2]
    blotter = []
    for order in orders:
        if not keep(order):
            continue
        fills = list(order.fills.all())
        blotter.append(
            {
                "order": order,
                "filled_at": _local(max(f.executed_at for f in fills)) if fills else None,
                "value": sum((f.price * f.quantity for f in fills), ZERO),
                "batch": "daily" if order.stream == DAILY_BUY_STREAM else order.stream,
            }
        )

    buys = [row for row in instruments if row["side"] == "BUY"]
    sells = [row for row in instruments if row["side"] == "SELL"]

    config = current_configuration()
    next_tick = None
    local_now = timezone.localtime()
    if local_now.date() == day:
        for index, moment in enumerate(ladder_times(day, config)):
            if moment > local_now.time():
                next_tick = {"tick_no": index, "at": moment}
                break

    return {
        "sheet": sheet,
        "instruments": instruments,
        "blotter": blotter,
        "filters": [
            (key, label, sum(1 for o in orders if test(o))) for key, label, test in BLOTTER_FILTERS
        ],
        "show": show,
        "kpis": {
            "instruments": len(instruments),
            "buys": len(buys),
            "sells": len(sells),
            "orders": len(orders),
            "open": sum(1 for o in orders if not o.is_terminal),
            "deployed": sum((row["value"] for row in buys), ZERO),
            "available": sheet.spendable_cash if sheet else None,
            "proceeds": sum((row["value"] for row in sells), ZERO),
            "sold": sum(row["filled"] for row in sells),
            "to_sell": sum(row["target"] for row in sells),
            "next_tick": next_tick,
        },
        # Rows the old ladder wrote for a name already bought in full say
        # nothing its orders do not; they are kept in the record, not shown.
        "ticks": Tick.objects.filter(trading_date=day)
        .exclude(no_place_reason=NoPlaceReason.FULLY_COMMITTED, instrument__isnull=False)
        .select_related("order")
        .order_by("stream", "tick_no"),
    }
