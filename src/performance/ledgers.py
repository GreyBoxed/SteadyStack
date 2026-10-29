"""The cash and share ledgers, as surfaces (M40a, M40b).

Both exist to be read by a person who is trying to work out where a number
came from. Neither computes anything the underlying tables do not already
hold -- they arrange, total, and show the reconciliation rather than asserting
it.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from audit.context import today_ist
from engine.ledger import engine_book, money, reconcile
from engine.models import CashLedger

logger = logging.getLogger("steadystack.performance")

ZERO = Decimal("0")


@dataclass(frozen=True)
class CashLedgerRow:
    trading_date: date | None
    opening_carry: Decimal
    daily_allocation: Decimal
    extra_allocation: Decimal
    #: Reinvested rebalance sale money, net of the sells' charges (D-S1): not
    #: part of `available`, which is what the day had before it spent.
    proceeds: Decimal
    cash_spent: Decimal
    charges: Decimal
    other: Decimal
    closing_carry: Decimal

    @property
    def available(self) -> Decimal:
        """What the day had to spend before it spent anything."""
        return self.opening_carry + self.daily_allocation + self.extra_allocation


@dataclass(frozen=True)
class CashLedgerView:
    rows: list[CashLedgerRow]
    totals: CashLedgerRow
    reconciliation: dict
    identity_holds: bool
    #: Today, read live from the journal because it has not settled. Shown
    #: apart: it is in neither `rows` nor the totals nor the reconciliation.
    provisional: CashLedgerRow | None = None
    #: Engine money already recorded but dated a later day, as (day, amount).
    allocated_ahead: tuple = ()


def provisional(day: date, *, book: str | None = None) -> CashLedgerRow | None:
    """What *day*'s row would say if it settled now, or None once it has.

    The same arithmetic `settle_day` writes (`engine.ledger.summarise_day`),
    asked without writing, so money recorded during the day shows before the
    close. Only a trading day has a row to anticipate.
    """
    from engine.ledger import is_settled, summarise_day
    from trading_calendar.sessions import is_trading_day

    if not is_trading_day(day) or is_settled(day, book=book):
        return None
    summary = summarise_day(day, book=book)
    return CashLedgerRow(
        trading_date=day,
        opening_carry=money(summary.opening_carry),
        daily_allocation=money(summary.daily_allocation),
        extra_allocation=money(summary.extra_allocation),
        proceeds=money(summary.proceeds),
        cash_spent=money(summary.cash_spent),
        charges=money(summary.charges),
        other=money(summary.other),
        closing_carry=money(summary.closing_carry),
    )


def allocated_ahead(day: date, *, book: str | None = None) -> list[tuple[date, Decimal]]:
    """Money given to the engine but dated after *day*, by the day it is for.

    A lumpsum recorded once a day has bought is the next trading day's money;
    it is in the journal already, and in no row until that day.
    """
    from django.db.models import Sum

    from journal.accounts import ENGINE_CASH
    from journal.models import EntryKind, JournalLine

    rows = (
        JournalLine.objects.filter(
            entry__book=engine_book(book),
            entry__kind=EntryKind.ALLOCATION,
            account_id=ENGINE_CASH,
            entry__occurred_on__gt=day,
        )
        .values_list("entry__occurred_on")
        .annotate(total=Sum("amount"))
        .order_by("entry__occurred_on")
    )
    return [(on, money(total)) for on, total in rows]


def cash(start: date | None = None, end: date | None = None) -> CashLedgerView:
    """Every trading day in the range, and the identity it has to satisfy.

    The totals row sums the flows and *spans* the positions: carry is a
    balance, not a movement, so the range opens where its first day opened and
    closes where its last day closed. Adding carry up would produce a number
    that means nothing.
    """
    book = engine_book()
    query = CashLedger.objects.filter(book=book).order_by("trading_date")
    if start is not None:
        query = query.filter(trading_date__gte=start)
    if end is not None:
        query = query.filter(trading_date__lte=end)

    rows = [
        CashLedgerRow(
            trading_date=row.trading_date,
            opening_carry=money(row.opening_carry),
            daily_allocation=money(row.daily_allocation),
            extra_allocation=money(row.extra_allocation),
            proceeds=money(row.proceeds),
            cash_spent=money(row.cash_spent),
            charges=money(row.charges),
            other=money(row.other),
            closing_carry=money(row.closing_carry),
        )
        for row in query
    ]

    result = reconcile(start, end, book=book)

    def total(field: str) -> Decimal:
        return money(sum((getattr(row, field) for row in rows), ZERO))

    return CashLedgerView(
        rows=rows,
        totals=CashLedgerRow(
            trading_date=None,
            opening_carry=rows[0].opening_carry if rows else ZERO,
            daily_allocation=total("daily_allocation"),
            extra_allocation=total("extra_allocation"),
            proceeds=total("proceeds"),
            cash_spent=total("cash_spent"),
            charges=total("charges"),
            other=total("other"),
            closing_carry=rows[-1].closing_carry if rows else ZERO,
        ),
        reconciliation=result,
        identity_holds=bool(result.get("ok")),
        **_today_in(start, end, book=book),
    )


def _today_in(start: date | None, end: date | None, *, book: str) -> dict:
    """Today's provisional row, when the range reaches today."""
    today = today_ist()
    if (start is not None and start > today) or (end is not None and end < today):
        return {}
    row = provisional(today, book=book)
    if row is None:
        return {}
    return {"provisional": row, "allocated_ahead": tuple(allocated_ahead(today, book=book))}


ENGINE_BUY = "engine_buy"
REBALANCE_SELL = "rebalance_sell"
EXTERNAL = "external"
CORPORATE_ACTION = "corporate_action"
OPENING = "opening"


SOURCE_LABELS = {
    ENGINE_BUY: "Engine buy",
    REBALANCE_SELL: "Rebalance sell",
    EXTERNAL: "External",
    CORPORATE_ACTION: "Corporate action",
    OPENING: "Opening position",
}


@dataclass(frozen=True)
class ShareMovement:
    on: date
    instrument_id: str
    quantity: int
    source: str
    reference: str
    #: What each share changed hands at, where the source records one: a fill
    #: does, a broker-side transfer or a split may not.
    price: Decimal | None = None

    @property
    def value(self) -> Decimal | None:
        return abs(self.quantity) * self.price if self.price is not None else None

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)


@dataclass(frozen=True)
class ShareLedgerView:
    movements: list[ShareMovement]
    by_instrument: dict[str, int]
    divergences: list[dict]
    #: What the movements are netted against: "broker" (live, the broker's
    #: holdings) or "paper lots" (paper, what the paper lots hold) (m-3).
    compared_with: str = "broker"


def shares(
    instrument_id: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> ShareLedgerView:
    """Every quantity in and out, by instrument and by source.

    The point is not the list but the netting: what the movements add up to
    has to equal what the broker says is held, and where it does not the
    difference is named. Two records that agree are worth more than two that
    do not.

    One book at a time (m-3), the order mode's. Live: the live fills, the
    opening lots on the day they were acquired, external transactions and
    corporate actions, against the broker's holdings. Paper: the paper fills
    and what corporate actions did to the paper lots, against what the paper
    lots hold -- the broker's holdings are real shares the paper book never
    bought, and a paper fill is shares the broker never saw.
    """
    from configuration.store import current
    from orders.models import OrderMode

    book = OrderMode.LIVE if current().is_live else OrderMode.PAPER

    def everything(start, end):
        found = [
            *_traded(instrument_id, start, end, book=book),
            *_restructured(instrument_id, start, end, book=book),
        ]
        if book == OrderMode.LIVE:
            found += [
                *_opening(instrument_id, start, end),
                *_external(instrument_id, start, end),
            ]
        return found

    movements = everything(start, end)
    movements.sort(key=lambda movement: (movement.on, movement.instrument_id, movement.source))

    # The netting is always inception-to-date, whatever window is on screen.
    # A month of movements does not add up to what the broker holds, and
    # comparing the two would report a divergence for every instrument the
    # window happens to cut across -- an alert that is wrong by construction
    # is worse than no alert.
    net: dict[str, int] = {}
    for movement in everything(None, None):
        net[movement.instrument_id] = net.get(movement.instrument_id, 0) + movement.quantity

    return ShareLedgerView(
        movements=movements,
        by_instrument=net,
        divergences=(
            _live_disagreements(net, _held(instrument_id, book=book))
            if book == OrderMode.LIVE
            else _disagreements(net, _held(instrument_id, book=book))
        ),
        compared_with="broker" if book == OrderMode.LIVE else "paper lots",
    )


def _held(instrument_id, *, book: str) -> dict[str, int]:
    """What the movements should add up to, by symbol.

    Live, the broker's holdings. Paper, the paper lots' -- what settlement
    writes into a paper day's holdings, read from the lots themselves since
    the morning's broker sync overwrites the holdings table with real
    quantities. Narrowed to one instrument, the other holdings have no
    movements here to agree with, and comparing them would report every
    other name as a divergence.
    """
    from orders.models import OrderMode
    from portfolio import lots
    from portfolio.models import Holding, Lot

    if book == OrderMode.LIVE:
        holdings = Holding.objects.all()
        if instrument_id is not None:
            holdings = holdings.filter(instrument_id=instrument_id)
        return {holding.instrument_id: holding.quantity for holding in holdings}

    paper = Lot.objects.filter(pk__in=lots.paper_lot_ids(instrument_id))
    symbols = sorted(set(paper.values_list("instrument_id", flat=True)))
    return {symbol: lots.held_quantity(symbol, book=OrderMode.PAPER) for symbol in symbols}


def _traded(instrument_id, start, end, *, book: str) -> list[ShareMovement]:
    """Shares this system bought or sold through the broker, in *book*."""
    from orders.models import Fill, OrderSide

    fills = Fill.objects.filter(order__mode=book).select_related("order").order_by("executed_at")
    if instrument_id is not None:
        fills = fills.filter(order__instrument_id=instrument_id)
    if start is not None:
        fills = fills.filter(order__trading_date__gte=start)
    if end is not None:
        fills = fills.filter(order__trading_date__lte=end)

    movements = []
    for fill in fills:
        is_sell = fill.order.side == OrderSide.SELL
        movements.append(
            ShareMovement(
                on=fill.order.trading_date,
                instrument_id=fill.order.instrument_id,
                quantity=-fill.quantity if is_sell else fill.quantity,
                source=REBALANCE_SELL if is_sell else ENGINE_BUY,
                reference=fill.order.tag,
                price=fill.price,
            )
        )
    return movements


def _opening(instrument_id, start, end) -> list[ShareMovement]:
    """Shares the account held before this system did, on the day each lot
    was acquired (the opening import). A lot that replaced one is not
    counted again: a split's difference is a corporate action's."""
    from portfolio.models import Lot

    query = Lot.objects.filter(opening_import__isnull=False, supersedes__isnull=True)
    if instrument_id is not None:
        query = query.filter(instrument_id=instrument_id)
    if start is not None:
        query = query.filter(acquired_on__gte=start)
    if end is not None:
        query = query.filter(acquired_on__lte=end)
    return [
        ShareMovement(
            on=lot.acquired_on,
            instrument_id=lot.instrument_id,
            quantity=lot.quantity,
            source=OPENING,
            reference="opening import",
            price=lot.cost_per_unit,
        )
        for lot in query.order_by("acquired_on", "created_at")
    ]


def _external(instrument_id, start, end) -> list[ShareMovement]:
    """Shares that moved at the broker without this system asking."""
    from records.models import ExternalTransaction

    query = ExternalTransaction.objects.order_by("occurred_on")
    if instrument_id is not None:
        query = query.filter(instrument_id=instrument_id)
    if start is not None:
        query = query.filter(occurred_on__gte=start)
    if end is not None:
        query = query.filter(occurred_on__lte=end)

    return [
        ShareMovement(
            on=transaction.occurred_on,
            instrument_id=transaction.instrument_id,
            quantity=transaction.quantity if transaction.adds_shares else -transaction.quantity,
            source=EXTERNAL,
            reference=transaction.reason,
            price=transaction.price,
        )
        for transaction in query
    ]


def _restructured(instrument_id, start, end, *, book: str) -> list[ShareMovement]:
    """Shares a corporate action created or destroyed.

    A split writes no fill -- it supersedes each lot with a larger one -- so
    without this the ledger would net to the pre-split quantity and report a
    divergence against the broker every day afterwards.

    Only the *difference* counts. Finalising a basis against a contract note
    supersedes a lot too, but at the same quantity, so it contributes nothing
    and never appears here.
    """
    from portfolio.lots import in_book
    from portfolio.models import Lot

    query = Lot.objects.filter(superseded_by__isnull=False).select_related("superseded_by")
    if instrument_id is not None:
        query = query.filter(instrument_id=instrument_id)
    query = in_book(query, book, instrument_id=instrument_id)

    movements = []
    for lot in query:
        difference = lot.superseded_by.quantity - lot.quantity
        if difference == 0:
            continue
        on = lot.superseded_by.created_at.date()
        if (start is not None and on < start) or (end is not None and on > end):
            continue
        movements.append(
            ShareMovement(
                on=on,
                instrument_id=lot.instrument_id,
                quantity=difference,
                source=CORPORATE_ACTION,
                reference=f"{lot.quantity} -> {lot.superseded_by.quantity}",
            )
        )
    return movements


def _live_disagreements(net: dict[str, int], broker: dict[str, int]) -> list[dict]:
    """Where our arithmetic and the broker's holdings do not meet, with the
    divergence scan's two exemptions (gap m3).

    A holding that is not the portfolio's -- no live lot, not in the model,
    and no movement here -- is the adult's own, and is left out, as the scan
    lists it apart. And the broker is short, until the next morning, by the
    shares settlement has not delivered (`fifo.settlement_window`): tonight's
    resync reads Kite's holdings, where today's CNC buys are still positions.
    Anything outside that allowance is a real difference.
    """
    from audit.context import today_ist
    from gains import fifo
    from orders.models import OrderMode
    from records.divergence import _not_ours_ids

    not_ours = {symbol for symbol in _not_ours_ids(OrderMode.LIVE) if not net.get(symbol)}
    today = today_ist()
    found = []
    for symbol in sorted({*net, *broker} - not_ours):
        ours, theirs = net.get(symbol, 0), broker.get(symbol, 0)
        if ours == theirs:
            continue
        delivered, held = fifo.settlement_window(symbol, on=today, book=OrderMode.LIVE)
        if ours - (held - delivered) <= theirs <= ours:
            continue
        found.append({"instrument_id": symbol, "ours": ours, "broker": theirs})
    return found


def _disagreements(net: dict[str, int], broker: dict[str, int]) -> list[dict]:
    """Where our arithmetic and what is held do not meet."""
    found = []
    for symbol in sorted({*net, *broker}):
        ours, theirs = net.get(symbol, 0), broker.get(symbol, 0)
        if ours != theirs:
            found.append({"instrument_id": symbol, "ours": ours, "broker": theirs})
    return found
