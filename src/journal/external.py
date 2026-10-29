"""Entries for what happened outside the engine (spec §6).

A direct buy at Zerodha settles like any trade. An IPO paid through UPI never
touches Zerodha's funds, so it is a contribution made in shares. Transfers are
contributions and withdrawals in kind, at cost: what a transfer out was *for*
is not this system's decision (M36d), so it books no gain.
"""

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from audit.events import SYSTEM_ACTOR
from journal.accounts import (
    CONTRIBUTIONS,
    DIVIDENDS,
    ENGINE_CASH,
    INVESTMENTS,
    OPENING,
    REALISED,
    SETTLE_ENGINE,
    SETTLE_OTHER,
    UNALLOCATED,
    WITHDRAWALS,
)
from journal.models import Book, Component, EntryKind, JournalEntry
from journal.posting import Line, lock_book, post, q6

ZERO = Decimal("0")


def post_external(tx, *, consumption=None) -> JournalEntry | None:
    from journal.trades import settle_trades
    from records.models import ExternalSide, PaidFrom

    symbol = tx.instrument_id
    gross = q6(Decimal(tx.price) * tx.quantity)
    charges = q6(tx.charges)

    if tx.side in (ExternalSide.BUY, ExternalSide.TRANSFER_IN):
        if tx.side == ExternalSide.BUY and tx.paid_from == PaidFrom.ZERODHA:
            credit = SETTLE_OTHER
        else:
            credit = CONTRIBUTIONS
        lines = [
            Line(INVESTMENTS, gross, symbol, Component.GROSS),
            Line(INVESTMENTS, charges, symbol, Component.OTHER),
            Line(credit, -(gross + charges)),
        ]
        kind = EntryKind.EXTERNAL_ACQUISITION
    else:
        if consumption is None:
            return None
        cost = q6(consumption.cost)
        if tx.side == ExternalSide.SELL:
            lines = [
                Line(SETTLE_OTHER, gross - charges),
                Line(REALISED, charges, symbol, Component.OTHER),
                Line(INVESTMENTS, -cost, symbol),
                Line(REALISED, -(gross - cost), symbol),
            ]
        else:  # TRANSFER_OUT: a withdrawal of shares, at cost
            lines = [Line(WITHDRAWALS, cost, symbol), Line(INVESTMENTS, -cost, symbol)]
        kind = EntryKind.EXTERNAL_DISPOSAL

    entry = post(
        book=Book.LIVE,
        kind=kind,
        occurred_on=tx.occurred_on,
        description=f"{tx.side} {tx.quantity} {symbol}: {tx.reason}",
        lines=lines,
        actor=tx.actor,
        source_type="external_transaction",
        source_id=str(tx.id),
        idempotency_key=f"external:{tx.id}",
    )
    settle_trades(tx.occurred_on, book=Book.LIVE)
    return entry


def record_dividend(
    instrument_id: str,
    amount,
    *,
    paid_on: date,
    actor: str,
    reference: str = "",
    idempotency_key: str | None = None,
):
    """The portfolio's income, paid to the owner's bank: income and a withdrawal (J6)."""
    amount = q6(amount)
    if amount <= ZERO:
        raise ValueError("A dividend must be a positive amount.")
    return post(
        book=Book.LIVE,
        kind=EntryKind.DIVIDEND,
        occurred_on=paid_on,
        description=f"dividend {instrument_id} {reference}".strip(),
        lines=[Line(WITHDRAWALS, amount), Line(DIVIDENDS, -amount, instrument_id)],
        actor=actor,
        source_type="dividend",
        source_id=reference,
        idempotency_key=idempotency_key,
    )


def live_open_lots():
    """Open lots that are real: not bought by a paper fill, nor replacing one."""
    from portfolio.lots import in_book
    from portfolio.models import Lot

    lots = Lot.objects.filter(superseded_by__isnull=True, remaining_quantity__gt=0)
    return in_book(lots, Book.LIVE)


def paper_open_lots():
    from portfolio.lots import in_book
    from portfolio.models import Lot

    lots = Lot.objects.filter(superseded_by__isnull=True, remaining_quantity__gt=0)
    return in_book(lots, Book.PAPER)


def standing_opening() -> JournalEntry | None:
    """The live book's opening entry that has not been reversed, if any."""
    return (
        JournalEntry.objects.filter(
            book=Book.LIVE, kind=EntryKind.OPENING_BALANCE, reversed_by__isnull=True
        )
        .order_by("-number")
        .first()
    )


def live_book_opened() -> bool:
    """Whether the live book holds an opening entry that has not been reversed."""
    return standing_opening() is not None


def _recorded_before_opening(accounts, on: date) -> Decimal:
    """What the live book already holds in *accounts* through *on*, openings aside.

    Every real event is posted to the live book whatever the order mode, so
    by go-live the book already holds lumpsums, dividends' cash, direct buys
    and account charges. An earlier, reversed opening and its reversal are
    left out: they net to nothing, but not necessarily through *on*.
    """
    from journal.models import JournalLine

    total = (
        JournalLine.objects.filter(
            entry__book=Book.LIVE,
            account_id__in=list(accounts),
            entry__occurred_on__lte=on,
        )
        .exclude(entry__kind=EntryKind.OPENING_BALANCE)
        .exclude(entry__reverses__kind=EntryKind.OPENING_BALANCE)
        .aggregate(total=Sum("amount"))["total"]
    )
    return q6(total or ZERO)


def _cash_recorded_before_opening(on: date) -> Decimal:
    from journal.accounts import ZERODHA_ACCOUNTS

    return _recorded_before_opening(ZERODHA_ACCOUNTS, on)


@transaction.atomic
def open_books(*, cash, on: date, actor: str = SYSTEM_ACTOR) -> JournalEntry:
    """The live book's opening entry (J10): Zerodha's cash on go-live day, and
    the cost of every live lot the journal does not already hold.

    *cash* is Zerodha's funds balance as the statement shows it. What the live
    book already holds through *on* is netted, so only the difference is
    posted and the book's Zerodha cash equals *cash* afterwards. A reversed
    opening does not count: the books may be opened again.

    The live engine opens at nought. Before go-live the live book can still
    collect engine balances -- a carry restore, or the engine's share of a
    sweep confirmed from a statement -- and those are moved into unallocated
    here, so the first live day neither spends a paper-period residue nor
    owes one back (`journal.cash.carry_owed` counts only sweeps after the
    opening). Zerodha's cash is unchanged by that move.
    """
    lock_book(Book.LIVE)
    if live_book_opened():
        raise ValueError("The live book is already open.")

    journalled = set(
        JournalEntry.objects.filter(
            book=Book.LIVE, source_type__in=["external_transaction", "fill"]
        ).values_list("source_id", flat=True)
    )
    engine = [
        Line(code, -held)
        for code in (ENGINE_CASH, SETTLE_ENGINE)
        if (held := _recorded_before_opening([code], on)) != ZERO
    ]
    moved = -sum((line.amount for line in engine), ZERO)
    lines = [Line(UNALLOCATED, q6(q6(cash) - _cash_recorded_before_opening(on) + moved)), *engine]
    for lot in live_open_lots().filter(acquired_on__lte=on):
        source = lot.created_from_fill_id or lot.external_transaction_id
        if source is not None and str(source) in journalled:
            continue
        # One line per lot, at the cost of what is still held, carrying the
        # lot: performance dates the outflow by `acquired_on` (I10). Shares
        # sold before go-live are outside the portfolio and outside XIRR.
        lines.append(
            Line(
                INVESTMENTS,
                q6(lot.cost_per_unit * lot.remaining_quantity),
                lot.instrument_id,
                lot_id=lot.id,
            )
        )

    total = sum((line.amount for line in lines), ZERO)
    lines.append(Line(OPENING, -total))
    earlier = JournalEntry.objects.filter(book=Book.LIVE, kind=EntryKind.OPENING_BALANCE).count()
    entry = post(
        book=Book.LIVE,
        kind=EntryKind.OPENING_BALANCE,
        occurred_on=on,
        description="opening balance at go-live",
        lines=lines,
        actor=actor,
        idempotency_key="opening_balance:live" + (f":{earlier + 1}" if earlier else ""),
        # Posted even when the journal already agrees with Zerodha: the entry
        # is what marks the live book open, and the live checks wait for it.
        keep_zero=True,
    )
    # Statement lines already uploaded that the opening's cash now holds.
    from journal.statement.reconcile import link_to_opening

    link_to_opening(entry)
    return entry
