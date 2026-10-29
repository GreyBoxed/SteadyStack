"""A trading day, rebuilt from what it stored.

The tradesheet page is both the morning's decision and, later, the record of
a past day. Everything here reads rows the day wrote -- the frozen ranking,
the orders and the ticks that placed them, the fills, the ledger row -- and
never asks the market or the engine to decide anything again. A decision
column that recomputed "would it buy this today" would answer the wrong
question.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal

from journal.cash import LUMPSUM, REINVEST, RESTORE

ZERO = Decimal("0")


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def ranking_rows(sheet) -> list[dict]:
    """The stored ranking, with what the sheet's own cash could do at each row.

    `max_qty` is the day's spendable cash over the ask stored on the row.
    `decision` says what the walk did: bought, passed over (and why, when the
    sheet recorded it), or never reached because a name above was bought.
    """
    snapshot = sheet.ranking_snapshot or {}
    ranked = snapshot.get("ranked", [])
    passed = snapshot.get("passed_over") or {}
    unaffordable = set(passed.get("unaffordable", []))
    capped = set(passed.get("capped", []))
    recorded = bool(passed)

    bought = sheet.bought_quantities()
    chosen_rank = max(
        (row.get("rank", 0) for row in ranked if row.get("symbol") in bought), default=None
    )
    largest_gap = max((abs(_dec(row.get("gap")) or ZERO) for row in ranked), default=ZERO)

    rows = []
    for row in ranked:
        symbol = row.get("symbol") or row.get("tradingsymbol")
        ask = _dec(row.get("ask"))
        gap = _dec(row.get("gap")) or ZERO
        # No stored ask means nothing can be said about affordability.
        max_qty = (
            int((sheet.spendable_cash / ask).to_integral_value(rounding=ROUND_FLOOR))
            if ask and ask > ZERO
            else None
        )

        if bought.get(symbol):
            # The plan, not the outcome: fills are shown per name above.
            decision, tone = f"buy {bought[symbol]}", "good"
        elif symbol in unaffordable or (not recorded and max_qty == 0):
            decision, tone = "passed · cannot afford one", "bad"
        elif symbol in capped:
            decision, tone = "passed · would overshoot target", "warn"
        elif chosen_rank is not None and row.get("rank", 0) > chosen_rank:
            decision, tone = "not reached", ""
        elif chosen_rank is not None:
            decision, tone = "passed over", "warn"
        else:
            decision, tone = "—", ""

        rows.append(
            {
                **row,
                "gap_value": gap,
                # Half the bar is under, half is over; the widest gap fills its half.
                "gap_pct": float(abs(gap) / largest_gap * 50) if largest_gap else 0,
                "max_qty": max_qty,
                "symbol": symbol,
                "affordable": bool(max_qty),
                "decision": decision,
                "tone": tone,
                "chosen": bool(bought.get(symbol)),
            }
        )
    return rows


def neighbours(day: date) -> dict:
    """The trading days either side of this one that have a sheet."""
    from engine.models import TradeSheet

    days = TradeSheet.objects.values_list("trading_date", flat=True).distinct()
    earlier = days.filter(trading_date__lt=day).order_by("-trading_date").first()
    later = days.filter(trading_date__gt=day).order_by("trading_date").first()

    def latest(on):
        if on is None:
            return None
        return TradeSheet.objects.filter(trading_date=on).order_by("-version").first()

    return {
        "previous": latest(earlier),
        "next": latest(later),
        "on_record": days.order_by().count(),
    }


def order_rows(orders, ticks) -> list[dict]:
    """Each order with the book its tick read and what it filled at."""
    by_order = {tick.order_id: tick for tick in ticks if tick.order_id}
    rows = []
    for order in orders:
        fills = list(order.fills.all())
        quantity = sum(f.quantity for f in fills)
        tick = by_order.get(order.pk)
        rows.append(
            {
                "order": order,
                "tick": tick,
                "filled_at": max((f.executed_at for f in fills), default=None),
                "price": (sum((f.price * f.quantity for f in fills), ZERO) / quantity)
                if quantity
                else None,
                "trades": [f.kite_trade_id for f in fills],
            }
        )
    return rows


@dataclass(frozen=True)
class ChargeLine:
    label: str
    amount: Decimal


def charge_lines(fills) -> list[ChargeLine]:
    """The day's charges taken apart by component, from the rate card.

    A fill stores one total; the components are re-estimated here so the day
    can be read against a contract note line by line. The recorded total is
    shown beside them, and where the two differ the recorded one is the one
    that was booked.
    """
    from portfolio.charges import estimate

    totals: dict[str, Decimal] = {}
    for fill in fills:
        parts = estimate(
            turnover=fill.price * fill.quantity,
            side=fill.order.side,
            exchange=fill.order.instrument.exchange,
        )
        for key, value in parts.as_dict().items():
            if key == "total":
                continue
            totals[key] = totals.get(key, ZERO) + Decimal(value)

    labels = {
        "securities_transaction_tax": "STT",
        "exchange_txn": "exchange transaction",
        "stamp_duty": "stamp duty",
        "gst": "GST",
        "sebi_turnover": "SEBI turnover",
        "brokerage": "brokerage",
        "dp_charge": "DP charge",
    }
    return [ChargeLine(labels[key], amount) for key, amount in totals.items() if amount > ZERO]


#: What a non-daily allocation is, as the day page names it.
EXTRA_LABELS = {
    LUMPSUM: "lumpsum",
    REINVEST: "reinvested proceeds",
    RESTORE: "carry restored",
}


def lumpsums(day: date) -> dict:
    """Money given to the engine for this day beyond its daily amount, split by
    whether the day's latest sheet was planned after it arrived.

    Everything allocated since the previous trading day counts: a lumpsum
    that arrived on a Saturday is Monday's money. Reinvested proceeds and
    restored carry are allocations too, and are labelled as what they are.
    """
    from engine.ledger import engine_book
    from engine.models import TradeSheet
    from journal.accounts import ENGINE_CASH
    from journal.cash import DAILY
    from journal.models import EntryKind, JournalLine
    from trading_calendar.sessions import previous_trading_day

    sheet = TradeSheet.objects.filter(trading_date=day).order_by("-version").first()
    lines = JournalLine.objects.filter(
        entry__book=engine_book(),
        entry__kind=EntryKind.ALLOCATION,
        entry__occurred_on__lte=day,
        account_id=ENGINE_CASH,
    )
    since = previous_trading_day(day)
    if since is not None:
        lines = lines.filter(entry__occurred_on__gt=since)
    rows = [
        {
            "amount": line.amount,
            "reference": line.entry.description,
            "recorded_at": line.entry.created_at,
            "recorded_by": line.entry.actor,
            "received_on": line.entry.occurred_on,
            "source": line.entry.source_type,
            "label": EXTRA_LABELS.get(line.entry.source_type, "allocation"),
        }
        for line in lines.exclude(entry__source_type=DAILY)
        .select_related("entry")
        .order_by("entry__created_at")
    ]
    planned = sheet.generated_at if sheet else None
    return {
        "in_sheet": [r for r in rows if planned and r["recorded_at"] <= planned],
        "after": [r for r in rows if not planned or r["recorded_at"] > planned],
    }


def line_rows(sheet, *, stream: str | None = None) -> list[dict]:
    """Each name the day buys: planned, committed, filled, and what it cost."""
    from orders import placement
    from orders.models import DAILY_BUY_STREAM, Fill

    stream = stream or DAILY_BUY_STREAM
    rows = []
    for line in sheet.buy_lines():
        fills = Fill.objects.filter(
            order__tradesheet=sheet, order__stream=stream, order__instrument=line.instrument
        )
        filled = sum(f.quantity for f in fills)
        rows.append(
            {
                "line": line,
                "filled": filled,
                "committed": placement.committed_quantity(
                    sheet, stream=stream, instrument=line.instrument
                ),
                "spent": sum((f.price * f.quantity for f in fills), ZERO),
                "pct": min(100, round(filled / line.quantity * 100)) if line.quantity else 100,
            }
        )
    return rows
