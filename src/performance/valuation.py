"""What the portfolio is worth, now and on the evening of a given day.

Worth is always last traded price, never the ask: the ask is what the next
share would cost, which is a different question and the one `engine.selection`
asks. A name the feed has nothing for is reported as unpriced rather than
valued at zero -- a silent zero understates the portfolio and would poison
every number downstream of it.

Every function here takes an optional `book`: `None` is the reader's book
(`portfolio.lots.reader_book`) -- the live lots when live, every open lot in
paper -- which is what most screens want; `measure()` and `snapshot()` pass the
performance book explicitly, because a value and the flows it is measured
against must come from the same book (spec §8) or XIRR compares one
portfolio's value with another's cash.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from audit.context import current_trace_id
from audit.logging import fields
from engine.ledger import closing_carry_before
from gains.fifo import money4
from journal.models import Book
from performance.models import DailyValuation
from portfolio.models import Lot

logger = logging.getLogger("steadystack.performance")

ZERO = Decimal("0")


@dataclass(frozen=True)
class PortfolioValue:
    total: Decimal
    per_instrument: dict[str, Decimal]
    priced: set[str] = field(default_factory=set)
    unpriced: set[str] = field(default_factory=set)


def _open_lots(book: str | None = None):
    """*book*'s open lots; with none given, the reader's book (I1)."""
    from portfolio.lots import reader_book

    book = book if book is not None else reader_book()
    if book is None:
        return Lot.objects.filter(remaining_quantity__gt=0, superseded_by__isnull=True)
    from journal.external import live_open_lots, paper_open_lots

    return paper_open_lots() if book == Book.PAPER else live_open_lots()


def held_quantities(book: str | None = None) -> dict[str, int]:
    """Open quantity by symbol, from the lots rather than from `Holding`.

    The lots are what the gains report and FIFO both read, so valuing against
    anything else would let two of our own numbers disagree.
    """
    held: dict[str, int] = {}
    for lot in _open_lots(book):
        held[lot.instrument_id] = held.get(lot.instrument_id, 0) + lot.remaining_quantity
    return held


def invested_to_date(book: str | None = None) -> Decimal:
    """The cost basis still sitting in open lots."""
    total = ZERO
    for lot in _open_lots(book):
        total += lot.cost_per_unit * lot.remaining_quantity
    return money4(total)


def value_now(prices: dict[str, Decimal], *, book: str | None = None) -> PortfolioValue:
    per_instrument: dict[str, Decimal] = {}
    priced: set[str] = set()
    unpriced: set[str] = set()

    for symbol, quantity in held_quantities(book).items():
        price = prices.get(symbol)
        if price is None or price <= ZERO:
            unpriced.add(symbol)
            continue
        priced.add(symbol)
        per_instrument[symbol] = money4(price * quantity)

    return PortfolioValue(
        total=money4(sum(per_instrument.values(), ZERO)),
        per_instrument=per_instrument,
        priced=priced,
        unpriced=unpriced,
    )


def evenings():
    """The stored evenings of the performance book, the one series a chart
    or a weight reads (M13)."""
    from journal.performance import book_for_performance

    return DailyValuation.objects.filter(book=book_for_performance())


def snapshot(
    day: date,
    *,
    prices: dict[str, Decimal],
    benchmark_level: Decimal | None = None,
    book: str | None = None,
) -> DailyValuation | None:
    """Write the day's value in *book*. Returns None if it already has a row.

    The series is append-only: a day is valued once per book, on the day, and
    a second run is a no-op rather than a correction. A wrong snapshot is
    fixed by recording why, not by rewriting history. With no *book*, the
    performance book's (M13).
    """
    from journal.performance import book_for_performance

    book = book or book_for_performance()
    if DailyValuation.objects.filter(trading_date=day, book=book).exists():
        logger.info("valuation already recorded", extra=fields(trading_date=str(day), book=book))
        return None

    value = value_now(prices, book=book)
    held = held_quantities(book)

    row = DailyValuation.objects.create(
        trading_date=day,
        book=book,
        portfolio_value=value.total,
        invested_to_date=invested_to_date(book),
        cash_carry=closing_carry_before(day, book=book),
        benchmark_level=money4(benchmark_level) if benchmark_level else None,
        holdings_snapshot={
            symbol: {"quantity": quantity, "price": str(prices.get(symbol, ""))}
            for symbol, quantity in sorted(held.items())
        },
        trace_id=current_trace_id(),
    )

    logger.info(
        "portfolio valued",
        extra=fields(
            trading_date=str(day),
            book=book,
            value=str(value.total),
            unpriced=sorted(value.unpriced),
        ),
    )
    return row
