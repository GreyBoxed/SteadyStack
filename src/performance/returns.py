"""Portfolio performance (M41).

The flows are read from the journal (spec §8): every Investments debit from a
buy, an external acquisition or an IPO is an outflow on its date; net sale
proceeds and dividends come back as inflows; account charges are a cost of
holding. Money sitting in carry has never been a flow at all -- counting it as
though it had would understate the return of the part that was put to work.

Disposals return as inflows on the day they settled, and whatever is still
held is the terminal inflow at today's prices. The result is the money-weighted
return of the equity portfolio, which is the only return a daily irregular
contribution schedule has (M41).
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from engine.ledger import money
from journal.performance import book_for_performance
from journal.performance import flows as journal_flows
from performance import valuation
from performance.xirr import Flow, InsufficientHistory, NoSolution, solve
from records.divergence import portfolio_caveats

logger = logging.getLogger("steadystack.performance")

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class InstrumentContribution:
    instrument_id: str
    invested: Decimal
    value: Decimal
    gain: Decimal
    share_of_gain: Decimal


@dataclass(frozen=True)
class Performance:
    invested: Decimal
    current_value: Decimal
    absolute_return: Decimal
    percent_return: Decimal
    xirr: Decimal | None
    xirr_unavailable: str
    benchmark_xirr: Decimal | None
    benchmark_unavailable: str
    per_instrument: list[InstrumentContribution]
    caveats: list[dict]

    @property
    def xirr_pct(self) -> Decimal | None:
        """The rate as a percentage, which is how anybody reads it."""
        return money(self.xirr * HUNDRED) if self.xirr is not None else None

    @property
    def benchmark_pct(self) -> Decimal | None:
        return money(self.benchmark_xirr * HUNDRED) if self.benchmark_xirr is not None else None

    @property
    def beat_the_benchmark(self) -> bool | None:
        """Whether the work was worth doing, when both numbers exist."""
        if self.xirr is None or self.benchmark_xirr is None:
            return None
        return self.xirr >= self.benchmark_xirr


def contribution_flows(as_of: date, terminal_value: Decimal) -> list[Flow]:
    """Every rupee that left for shares, every rupee that came back."""
    entries = journal_flows(book=book_for_performance(), as_of=as_of)
    flows = [Flow(on=day, amount=money(amount)) for day, amount in entries]

    if flows and terminal_value > ZERO:
        flows.append(Flow(on=as_of, amount=money(terminal_value)))

    return flows


def measure(prices: dict[str, Decimal], *, as_of: date) -> Performance:
    """Invested, worth, and whether either was worth doing."""
    book = book_for_performance()
    value = valuation.value_now(prices, book=book)
    invested = valuation.invested_to_date(book)
    absolute = money(value.total - invested)
    percent = money(absolute / invested * HUNDRED) if invested > ZERO else ZERO

    flows = contribution_flows(as_of, value.total)

    rate: Decimal | None = None
    unavailable = ""
    try:
        rate = solve(flows)
    except (InsufficientHistory, NoSolution) as exc:
        unavailable = str(exc)

    benchmark_rate, benchmark_unavailable = benchmark_xirr(flows, as_of=as_of)

    return Performance(
        invested=money(invested),
        current_value=money(value.total),
        absolute_return=absolute,
        percent_return=percent,
        xirr=rate,
        xirr_unavailable=unavailable,
        benchmark_xirr=benchmark_rate,
        benchmark_unavailable=benchmark_unavailable,
        per_instrument=_per_instrument(value, absolute, book),
        caveats=portfolio_caveats(),
    )


def _per_instrument(
    value, total_gain: Decimal, book: str | None = None
) -> list[InstrumentContribution]:
    """Where the gain came from, largest contributor first.

    Invested is read from the same book-filtered lots the value came from
    (`valuation._open_lots`, reused rather than duplicated): a paper lot's
    cost must not appear beside a live lot's value, or the two figures are
    about two different portfolios.
    """
    invested_by: dict[str, Decimal] = {}
    for lot in valuation._open_lots(book):
        invested_by[lot.instrument_id] = invested_by.get(lot.instrument_id, ZERO) + (
            lot.cost_per_unit * lot.remaining_quantity
        )

    rows = []
    for symbol, held_value in value.per_instrument.items():
        invested = invested_by.get(symbol, ZERO)
        gain = money(held_value - invested)
        rows.append(
            InstrumentContribution(
                instrument_id=symbol,
                invested=money(invested),
                value=money(held_value),
                gain=gain,
                share_of_gain=money(gain / total_gain * HUNDRED) if total_gain else ZERO,
            )
        )

    return sorted(rows, key=lambda row: row.gain, reverse=True)


def benchmark_xirr(flows: list[Flow], *, as_of: date) -> tuple[Decimal | None, str]:
    """What the same money, on the same days, would have returned in the index.

    Every outflow buys index units at that day's close; every inflow sells
    them; whatever remains is valued at the latest level we hold. Judging the
    return against the index rather than against zero is the point of M41a: a
    year that made 8% while the index made 14% is not a good year.

    A contribution date with no stored level makes the whole comparison
    unavailable rather than approximate. Interpolating would invent the number
    the operator is relying on.
    """
    from performance.models import DailyValuation

    if not flows:
        return None, "No contributions to compare."

    # The index level is a fact of the market, not of a book: either book's
    # evening may supply it (M13).
    levels = {
        row.trading_date: row.benchmark_level
        for row in DailyValuation.objects.filter(
            trading_date__lte=as_of, benchmark_level__isnull=False
        )
    }
    if not levels:
        return None, "No benchmark history has been recorded yet."

    units = ZERO
    mirrored: list[Flow] = []

    for flow in sorted(flows, key=lambda item: item.on):
        if flow.on == as_of:
            # The terminal flow is the portfolio's own value; the index
            # position is valued from the units it accumulated instead.
            continue
        level = levels.get(flow.on)
        if level is None or level <= ZERO:
            return None, f"No benchmark level recorded for {flow.on}."
        units -= flow.amount / level  # an outflow is negative, so this buys
        mirrored.append(flow)

    if not mirrored:
        return None, "No contributions to compare."

    latest = max(levels)
    terminal = money(units * levels[latest])
    if terminal <= ZERO:
        return None, "The mirrored index position is empty."

    mirrored.append(Flow(on=as_of, amount=terminal))

    try:
        return solve(mirrored), ""
    except (InsufficientHistory, NoSolution) as exc:
        return None, str(exc)
