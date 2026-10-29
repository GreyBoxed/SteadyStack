"""Waiting for a name the day cannot afford.

When the most underweight instrument costs more than the day's cash, the
engine buys the next affordable name instead -- but not for ever, or the
ranking stops meaning what it says and an expensive name is never bought at
all. After a run of days in which the same name has ranked first and been
unaffordable, the day places nothing and carries its whole amount, and keeps
doing so until that name can be bought (D5, M15d).

The reserve holds no money. Carry is the only balance there is, and an idle
day simply does not spend it, so money conservation is untouched by any of
this. What is stored is a count of days, and it lives on the tradesheet so
that a deliberately idle day can explain itself.

Two guards keep the waiting from becoming permanent:

  * a name that leaves the universe clears the count, because waiting for
    something nobody is targeting any more is just not buying;
  * a name whose price is climbing faster than carry accumulates raises an
    alert rather than idling the portfolio indefinitely -- the operator can
    lower the ceiling, change the weights, or add a lumpsum, but somebody has
    to be told.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from audit.events import record
from audit.logging import fields
from engine.selection import Ranking, Selection

logger = logging.getLogger("steadystack.engine")


@dataclass(frozen=True)
class ReserveState:
    """Where the wait stands today."""

    symbol: str
    days: int
    engaged: bool
    runaway: bool = False

    @property
    def is_waiting(self) -> bool:
        return bool(self.symbol) and self.days > 0

    @classmethod
    def idle(cls) -> "ReserveState":
        return cls(symbol="", days=0, engaged=False)


def leader_is_unaffordable(ranking: Ranking, selection: Selection) -> str:
    """The top-ranked name, if the day could not afford it.

    Being passed over for any other reason does not count: a name that is
    within one share of its target is not one the portfolio is waiting for.
    """
    if ranking.is_empty:
        return ""

    leader = ranking.candidates[0]
    if selection.bought(leader.tradingsymbol):
        return ""
    # Only unaffordability counts. A leader held back by the concentration cap
    # is one the day chose not to overshoot on, and idling for it would be the
    # cap and the reserve fighting each other.
    if leader.tradingsymbol not in selection.unaffordable:
        return ""
    return leader.tradingsymbol


def advance(
    *,
    ranking: Ranking,
    selection: Selection,
    previous_symbol: str,
    previous_days: int,
    threshold: int,
    spendable_cash: Decimal,
) -> ReserveState:
    """Move the counter on by one day and decide whether to engage.

    Called once per generated tradesheet, with the previous day's count. The
    counter resets rather than decays: the wait is for a *consecutive* run, so
    one day on which the name was bought or stopped leading ends it.
    """
    leader = leader_is_unaffordable(ranking, selection)

    if not leader:
        return ReserveState.idle()

    days = previous_days + 1 if leader == previous_symbol else 1
    engaged = days >= threshold

    runaway = False
    if engaged:
        leading = ranking.candidates[0]
        # Climbing away: even the whole of today's cash is nowhere near, and
        # the wait has already run to twice its threshold. Idling for ever is
        # not a decision anybody made.
        runaway = days >= threshold * 2 and leading.ask > spendable_cash * 2

    return ReserveState(symbol=leader, days=days, engaged=engaged, runaway=runaway)


def held_back(
    state: ReserveState,
    *,
    ranking: Ranking,
    spendable_cash: Decimal,
    daily_amount: Decimal,
    threshold: int,
) -> Decimal:
    """What an engaged reserve keeps back from today's buying (M15d).

    One more daily amount for each day the reserve has been engaged, never
    more than the name's share price and never more than the day has. The
    rest is spent across the other names, so waiting for one expensive name
    no longer idles the whole portfolio -- and because the amount grows day
    by day, carry climbs until the name can be bought.
    """
    if not state.engaged or ranking.is_empty:
        return Decimal("0")
    leader = ranking.candidates[0]
    engaged_days = max(1, state.days - threshold + 1)
    return min(spendable_cash, leader.ask, daily_amount * engaged_days)


def announce(state: ReserveState, *, trading_date, previously_engaged: bool) -> None:
    """Raise the notice on the day the reserve begins, and the alert if the
    name is running away.

    Only on the day it changes: an alert repeated every morning for a month
    stops being read, which is the same as not raising it.
    """
    if state.engaged and not previously_engaged:
        record(
            "reserve.engaged",
            entity_type="engine.TradeSheet",
            entity_id=str(trading_date),
            after={"symbol": state.symbol, "days": state.days},
        )
        logger.warning(
            "reserve engaged: saving toward the leader and buying the rest",
            extra=fields(symbol=state.symbol, days=state.days),
        )

    if state.runaway:
        record(
            "reserve.runaway",
            entity_type="engine.TradeSheet",
            entity_id=str(trading_date),
            after={"symbol": state.symbol, "days": state.days},
        )
        logger.error(
            "reserve target is climbing faster than carry accumulates",
            extra=fields(symbol=state.symbol, days=state.days),
        )
