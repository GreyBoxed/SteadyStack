"""Trades in the journal: fills and their settlement (spec §6).

A fill owes the broker until the trade date settles, so it posts against
*Broker settlement* rather than cash. Engine buys have their own settlement
account, which lets the engine's spendable balance net what its own unsettled
buys owe (spec §4).

Charges stay part of the shares' cost (D6), and each component is its own
line so a contract note can be checked against it line by line.
"""

import logging
from datetime import date
from decimal import Decimal

from django.db import transaction

from audit.events import SYSTEM_ACTOR
from audit.logging import fields
from journal.accounts import (
    ENGINE_CASH,
    INVESTMENTS,
    REALISED,
    SETTLE_ENGINE,
    SETTLE_OTHER,
    UNALLOCATED,
)
from journal.balances import balance
from journal.models import Book, Component, EntryKind, JournalEntry
from journal.posting import Line, lock_book, post, q6

logger = logging.getLogger("steadystack.journal")

ZERO = Decimal("0")

COMPONENTS = {
    "brokerage": Component.BROKERAGE,
    "securities_transaction_tax": Component.STT,
    "exchange_txn": Component.EXCHANGE_TXN,
    "sebi_turnover": Component.SEBI,
    "stamp_duty": Component.STAMP_DUTY,
    "gst": Component.GST,
    "dp_charge": Component.DP,
}


def settlement_account(order) -> str:
    from orders.models import DAILY_BUY_STREAM, OrderSide

    if order.side == OrderSide.BUY and order.stream == DAILY_BUY_STREAM:
        return SETTLE_ENGINE
    return SETTLE_OTHER


def _charge_lines(fill, *, account: str) -> tuple[list[Line], Decimal, Decimal]:
    """One line per component. A stored total the rate card disagrees with
    keeps that total through an `other` line. Returns (lines, gross, charges).
    """
    from orders.booking import _dp_already_charged
    from portfolio.charges import cost_of

    order = fill.order
    gross, charges = cost_of(
        quantity=fill.quantity,
        price=fill.price,
        side=order.side,
        exchange=order.instrument.exchange,
        dp_charged=_dp_already_charged(fill),
    )
    total = q6(fill.charges) if fill.charges else q6(charges.total)
    lines = [
        Line(account, q6(getattr(charges, field)), order.instrument_id, component)
        for field, component in COMPONENTS.items()
    ]
    difference = total - q6(charges.total)
    if difference != ZERO:
        lines.append(Line(account, difference, order.instrument_id, Component.OTHER))
    return lines, q6(gross), total


def post_fill(fill, *, consumption=None) -> JournalEntry | None:
    """The fill's entry. Idempotent: a replayed booking returns the first."""
    from orders.models import OrderSide

    key = f"fill:{fill.id}"
    existing = JournalEntry.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing

    order = fill.order
    symbol = order.instrument_id

    if order.side == OrderSide.BUY:
        charge_lines, gross, total = _charge_lines(fill, account=INVESTMENTS)
        lines = [
            Line(INVESTMENTS, gross, symbol, Component.GROSS),
            *charge_lines,
            Line(settlement_account(order), -(gross + total)),
        ]
        kind = EntryKind.BUY_FILL
    else:
        if consumption is None:
            # booking.book logged the shortfall as a divergence; the cash
            # will still arrive and the statement import will surface it.
            logger.error(
                "a sell with no lot consumption posts no entry", extra=fields(fill=str(fill.id))
            )
            return None
        charge_lines, gross, total = _charge_lines(fill, account=REALISED)
        cost = q6(consumption.cost)
        lines = [
            Line(settlement_account(order), gross - total),
            *charge_lines,
            Line(INVESTMENTS, -cost, symbol),
            Line(REALISED, -(gross - cost), symbol),
        ]
        kind = EntryKind.SELL_FILL

    return post(
        book=order.mode,
        kind=kind,
        occurred_on=order.trading_date,
        description=f"{order.side} {fill.quantity} {symbol} @ {fill.price}",
        lines=lines,
        source_type="fill",
        source_id=str(fill.id),
        idempotency_key=key,
    )


@transaction.atomic
def settle_trades(trade_date: date, *, book: str, actor: str = SYSTEM_ACTOR) -> JournalEntry | None:
    """Clear what a trade date still owes, or is owed, into cash.

    The amount is whatever the settlement accounts still hold for that date,
    so a second run posts nothing and a fill that arrived late is settled by a
    second, smaller entry. The book's lock stops two runs both seeing the
    same residual -- the same lock `post()` takes, so a caller already holding
    it (an external transaction, the evening's fills) never waits on a second
    lock taken in the other order.
    """
    lock_book(book)

    engine = balance(SETTLE_ENGINE, book=book, on=trade_date)
    other = balance(SETTLE_OTHER, book=book, on=trade_date)
    if engine == ZERO and other == ZERO:
        return None

    return post(
        book=book,
        kind=EntryKind.SETTLEMENT,
        occurred_on=trade_date,
        description=f"settlement of {trade_date} trades",
        lines=[
            Line(SETTLE_ENGINE, -engine),
            Line(ENGINE_CASH, engine),
            Line(SETTLE_OTHER, -other),
            Line(UNALLOCATED, other),
        ],
        actor=actor,
        source_type="trade_date",
        source_id=trade_date.isoformat(),
    )


def reinvest(batch, *, book: str) -> list[JournalEntry]:
    """Give a batch's net proceeds to the engine, one allocation per trade date.

    The proceeds are settled first, so the allocation moves money that is
    already in *unallocated*, in either book.
    """
    from journal.cash import REINVEST, allocate
    from journal.models import JournalLine
    from orders.models import Fill

    if not batch.reinvest:
        return []

    # Only this book's fills (M-5): the entries are filtered by book too, but
    # the set should say what it means.
    fill_ids = [
        str(pk)
        for pk in Fill.objects.filter(order__stream=batch.stream, order__mode=book).values_list(
            "pk", flat=True
        )
    ]
    proceeds_by_date: dict[date, Decimal] = {}
    for line in JournalLine.objects.filter(
        entry__kind=EntryKind.SELL_FILL,
        entry__book=book,
        entry__source_id__in=fill_ids,
        account_id=SETTLE_OTHER,
    ).select_related("entry"):
        day = line.entry.occurred_on
        proceeds_by_date[day] = proceeds_by_date.get(day, ZERO) + line.amount

    entries = []
    for trade_date, proceeds in sorted(proceeds_by_date.items()):
        settle_trades(trade_date, book=book)
        if proceeds > ZERO:
            entries.append(
                allocate(
                    proceeds,
                    book=book,
                    on=trade_date,
                    actor=SYSTEM_ACTOR,
                    description=f"reinvested proceeds of rebalance sell {batch.id}",
                    source_type=REINVEST,
                    source_id=str(batch.id),
                    idempotency_key=f"{REINVEST}:{book}:{batch.id}:{trade_date.isoformat()}",
                    source_account=UNALLOCATED,
                )
            )
    return entries


def post_charge_correction(
    lot, difference, *, actor: str, reference: str, on: date, key: str | None = None
) -> JournalEntry | None:
    """The contract note's charges replace the estimate (M35b).

    Positive *difference* means the broker charged more: cash leaves. The part
    that belongs to shares still held moves the Investments cost; the part
    that belongs to shares already sold adjusts their realised gain, because
    the lot's history cannot be re-costed after it was consumed.

    Keyed `charge_correction:{lot.id}` unless *key* says otherwise: a lot
    already final can still take a settlement's share (§7.3), once per line.
    """
    difference = q6(difference)
    if difference == ZERO:
        return None

    fill = _origin_fill(lot)
    book = fill.order.mode if fill is not None else Book.LIVE
    engine_bought = fill is not None and settlement_account(fill.order) == SETTLE_ENGINE
    cash_account = ENGINE_CASH if engine_bought else UNALLOCATED

    held = q6(difference * lot.remaining_quantity / lot.quantity)
    sold = difference - held
    return post(
        book=book,
        kind=EntryKind.CHARGE_CORRECTION,
        occurred_on=on,
        description=f"charges on {lot.acquired_on} settled against {reference}",
        lines=[
            Line(INVESTMENTS, held, lot.instrument_id, Component.OTHER),
            Line(REALISED, sold, lot.instrument_id, Component.OTHER),
            Line(cash_account, -difference),
        ],
        actor=actor,
        source_type="lot",
        source_id=str(lot.id),
        idempotency_key=key or f"charge_correction:{lot.id}",
    )


def _origin_fill(lot):
    """The fill that bought *lot*, following a finalised lot back to its original.

    A replacement written by `contract_note.finalise` has no fill of its own,
    but its money still moved through the fill's book and settlement account.
    """
    from portfolio.models import Lot

    fill = getattr(lot, "created_from_fill", None)
    while fill is None and isinstance(lot, Lot):
        # superseded_by is one-to-one, so there is at most one; ordered anyway.
        lot = Lot.objects.filter(superseded_by=lot).order_by("created_at").first()
        fill = getattr(lot, "created_from_fill", None)
    return fill
