"""The value series, and what the data subscription does to the return.

The Performance page draws three lines -- what the portfolio was worth, what
was put into it, and what was put in plus the market-data subscription -- and
states one more rate: XIRR with that subscription counted as the outflow it
is. All of it is read from rows written on the day (`DailyValuation`, the
cash ledger); nothing here is reconstructed from today's prices.

`invested` is the same definition XIRR uses: cash spent on shares plus
charges. Money waiting in carry has not been put to work, so it is neither
cost nor value on this chart.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from engine.ledger import money
from performance.xirr import Flow, InsufficientHistory, NoSolution, solve

ZERO = Decimal("0")

#: A month of subscription, as the cost-drag panel counts one.
DAYS_PER_MONTH = 30.44


def _first_day() -> date | None:
    from engine.ledger import engine_book
    from engine.models import CashLedger

    first = (
        CashLedger.objects.filter(book=engine_book())
        .order_by("trading_date")
        .values_list("trading_date", flat=True)
    )
    return first.first()


def months_paid(since: date, on: date) -> int:
    """Subscription months billed from `since` up to and including `on`.

    The first month is billed on the first day, so a system one day old has
    paid one month, not zero.
    """
    if on < since:
        return 0
    return int((on - since).days // DAYS_PER_MONTH) + 1


def subscription_paid(on: date) -> Decimal:
    from configuration.store import current as current_configuration

    first = _first_day()
    monthly = current_configuration().market_data_monthly_cost
    if first is None or monthly <= ZERO:
        return ZERO
    return money(monthly * months_paid(first, on))


@dataclass(frozen=True)
class Point:
    on: date
    value: Decimal
    invested: Decimal
    with_subscription: Decimal


def value_history(start: date | None = None, end: date | None = None) -> list[Point]:
    """One point per stored evening, oldest first."""
    from configuration.store import current as current_configuration
    from performance.valuation import evenings

    rows = evenings().order_by("trading_date")
    if start:
        rows = rows.filter(trading_date__gte=start)
    if end:
        rows = rows.filter(trading_date__lte=end)

    first = _first_day()
    monthly = current_configuration().market_data_monthly_cost

    points = []
    for row in rows:
        subscription = (
            money(monthly * months_paid(first, row.trading_date))
            if first and monthly > ZERO
            else ZERO
        )
        points.append(
            Point(
                on=row.trading_date,
                value=money(row.portfolio_value),
                invested=money(row.invested_to_date),
                with_subscription=money(row.invested_to_date + subscription),
            )
        )
    return points


def subscription_flows(as_of: date) -> list[Flow]:
    """One outflow per billed month, on the day it was billed."""
    from configuration.store import current as current_configuration

    first = _first_day()
    monthly = current_configuration().market_data_monthly_cost
    if first is None or monthly <= ZERO:
        return []

    return [
        Flow(on=min(as_of, first + timedelta(days=round(n * DAYS_PER_MONTH))), amount=-monthly)
        for n in range(months_paid(first, as_of))
    ]


def xirr_net_of_subscription(flows: list[Flow], *, as_of: date) -> tuple[Decimal | None, str]:
    """The return with the data feed paid for out of it.

    The feed is what lets the engine rank and size at all, so it is a cost of
    this strategy, not an overhead of somebody else's. Stated beside the gross
    XIRR rather than instead of it: the gross rate is what the choices earned,
    and the difference is what running them cost.
    """
    extra = subscription_flows(as_of)
    if not flows or not extra:
        return None, "No subscription cost is recorded."
    try:
        return solve(sorted([*flows, *extra], key=lambda flow: flow.on)), ""
    except (InsufficientHistory, NoSolution) as exc:
        return None, str(exc)


@dataclass(frozen=True)
class Totals:
    contributed: Decimal
    days_run: int
    days_on_record: int
    subscription: Decimal
    gain_after_subscription: Decimal | None


def totals(*, gain: Decimal | None, as_of: date) -> Totals:
    """The figures beside the headline: money put in, days that bought."""
    from engine.ledger import engine_book
    from engine.models import CashLedger
    from journal.performance import book_for_performance
    from journal.performance import contributed as journal_contributed

    rows = list(CashLedger.objects.filter(book=engine_book(), trading_date__lte=as_of))
    contributed = money(journal_contributed(book=book_for_performance(), as_of=as_of))
    paid = subscription_paid(as_of)
    return Totals(
        contributed=contributed,
        days_run=sum(1 for row in rows if row.cash_spent > ZERO),
        days_on_record=len(rows),
        subscription=paid,
        gain_after_subscription=money(gain - paid) if gain is not None else None,
    )


def drift_history(targets: dict[str, Decimal], *, start: date | None = None) -> list[dict]:
    """How far from its targets the portfolio sat each evening: the sum of |weight - target|.

    Read from the holdings each evening wrote down, weighed against the
    targets given -- today's model portfolio, so the line answers "how far
    was it from where I want it now", which is the question a rebalance asks.
    """
    from performance.valuation import evenings

    rows = evenings().order_by("trading_date")
    if start:
        rows = rows.filter(trading_date__gte=start)

    points = []
    for row in rows:
        values: dict[str, Decimal] = {}
        for symbol, held in (row.holdings_snapshot or {}).items():
            if not isinstance(held, dict) or not held.get("price"):
                continue
            values[symbol] = Decimal(str(held["price"])) * int(held.get("quantity") or 0)
        total = sum(values.values(), ZERO)
        if total <= ZERO:
            continue
        drift = sum(
            (
                abs(values.get(symbol, ZERO) / total * 100 - target)
                for symbol, target in targets.items()
            ),
            ZERO,
        )
        points.append({"date": row.trading_date, "drift": money(drift)})
    return points
