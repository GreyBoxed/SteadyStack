"""When each name should have bought what, and what to pay when it has not.

The daily buy is placed in rounds, every `placer_interval_seconds`. Each name
the day buys runs to its own even schedule across the placement window: by any
moment it should have filled the same fraction of its quantity as the fraction
of the window gone. That one line does the spreading (M11a) and, because a name
behind schedule is exactly a name whose orders are not filling, it prices too:

  * **on schedule, rest at the bid.** The whole spread is the only saving a
    ladder can make on orders this size -- there is no market impact to
    manage at a few shares a name -- so a name that is keeping up asks for
    all of it;
  * **behind, step toward the ask in proportion.** How far behind is measured
    in time, so it means the same at any quantity and any spread: a name
    `catch_up_minutes` behind pays the ask (D10);
  * **never outside the exchange's order-to-trade exemption.** A cash-segment
    order priced within 0.75% of the last traded price is exempt from the
    OTR penalty. That is regulation rather than preference, so it is a
    constant and not configuration.

The schedule is anchored where the name could first place: the window's open,
or the moment the day was authorized if that came later (M15f). A day
authorized at 11:20 spreads what it has over what is left, rather than
arriving two hours behind and paying the ask for everything.

Everything here is arithmetic over values passed in: no database, no clock.
"""

import math
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

#: Orders within this fraction of the last traded price are exempt from the
#: exchange's order-to-trade penalty (SEBI, from 6 April 2026).
OTR_EXEMPT_BAND = Decimal("0.0075")


def _seconds(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def _held_back(*, gaps, start: datetime, close: datetime, moment: datetime) -> float:
    """How much the outages in *gaps* stretch what is left of the schedule.

    Each gap is a stretch of rounds that never ran. The schedule holds still
    through one and then spreads what was left over the time that is left,
    which multiplies the undone fraction by the time left at the gap's start
    over the time left at its end. No gaps, no stretch: the even schedule.
    """
    factor = 1.0
    for since, until in gaps:
        since, until = max(since, start), min(until, moment, close)
        if until <= since:
            continue
        left_after = _seconds(until, close)
        if left_after <= 0:
            continue
        factor *= _seconds(since, close) / left_after
    return factor


def progress(
    *,
    moment: datetime,
    start: datetime,
    close: datetime,
    lookahead: float = 0,
    gaps=(),
) -> float:
    """The fraction of its quantity a name should have committed at *moment*.

    With no *gaps*, the fraction of the window gone. The placer schedules one
    round ahead, so a name's last slice is due in the round before the window
    closes rather than in a round that never comes.

    *gaps* are the stretches when no round ran -- the program down, the
    laptop shut, the session lost. The schedule does not run on through them
    and then demand the missed hour in one burst at the ask: it stands still,
    and what is left is spread evenly over what is left of the window.
    """
    span = _seconds(start, close)
    if span <= 0:
        return 1.0
    held_back = _held_back(gaps=gaps, start=start, close=close, moment=moment)
    if held_back == 1.0:
        # Kept apart so an uninterrupted day's arithmetic is exactly as it was.
        return min(1.0, max(0.0, (_seconds(start, moment) + lookahead) / span))
    ahead = moment + timedelta(seconds=lookahead)
    undone = _seconds(ahead, close) / span * held_back
    return min(1.0, max(0.0, 1.0 - undone))


def scheduled(quantity: int, fraction: float) -> int:
    """How many shares the schedule says should be bought by now."""
    return min(quantity, math.ceil(quantity * fraction - 1e-9))


def seconds_behind(
    *,
    quantity: int,
    filled: int,
    moment: datetime,
    start: datetime,
    close: datetime,
    gaps=(),
) -> float:
    """How far behind its schedule a name's fills are, in seconds.

    Zero for a name on or ahead of schedule. Measured against the schedule
    without lookahead: this is about what has happened, not about the next
    round. The shortfall is turned into time at the pace the schedule is
    running now -- after an outage that is quicker than the day's first pace,
    since the same remainder has less time to go in.
    """
    if quantity <= 0:
        return 0.0
    expected = progress(moment=moment, start=start, close=close, gaps=gaps)
    seconds_per_whole = _seconds(start, close)
    left = _seconds(moment, close)
    stretched = _held_back(gaps=gaps, start=start, close=close, moment=moment) != 1.0
    if stretched and expected < 1 and left > 0:
        seconds_per_whole = left / (1 - expected)
    return max(0.0, (expected - filled / quantity) * seconds_per_whole)


def urgency(*, behind_seconds: float, catch_up_minutes: int) -> Decimal:
    """0 on schedule, 1 at `catch_up_minutes` behind or more."""
    if catch_up_minutes <= 0:
        return Decimal(1)
    ratio = Decimal(str(behind_seconds)) / Decimal(catch_up_minutes * 60)
    return min(Decimal(1), max(Decimal(0), ratio))


def _floor(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return ((value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick).quantize(tick)


def _ceil(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return ((value / tick).to_integral_value(rounding=ROUND_CEILING) * tick).quantize(tick)


def buy_price(
    *,
    bid: Decimal,
    ask: Decimal,
    urgency: Decimal,
    tick_size: Decimal,
    last_price: Decimal | None = None,
) -> Decimal:
    """The limit for a buy: the bid, raised toward the ask by *urgency*.

    Rounded down to the instrument's tick, never above the ask, and kept
    within the OTR-exempt band around the last traded price when there is
    one. A bid far under the last price is raised to the band's edge rather
    than placed where the exchange would count it against us.
    """
    price = _floor(bid + (ask - bid) * urgency, tick_size)
    price = max(price, bid)

    if last_price and last_price > 0:
        ceiling = _floor(last_price * (1 + OTR_EXEMPT_BAND), tick_size)
        floor = _ceil(last_price * (1 - OTR_EXEMPT_BAND), tick_size)
        price = min(max(price, floor), ceiling)

    return min(price, ask)


def sell_price(
    *,
    bid: Decimal,
    ask: Decimal,
    urgency: Decimal,
    tick_size: Decimal,
    last_price: Decimal | None = None,
) -> Decimal:
    """The limit for a sell: the ask, lowered toward the bid by *urgency*.

    The mirror of `buy_price`. Rounded *up* to the tick -- the seller's side
    of the spread -- never above the ask, kept inside the OTR-exempt band, and
    never under the bid: the bid is what somebody will actually pay, and a
    band edge below it would sell for less than the market is offering.
    """
    price = min(_ceil(ask - (ask - bid) * urgency, tick_size), ask)

    if last_price and last_price > 0:
        ceiling = _floor(last_price * (1 + OTR_EXEMPT_BAND), tick_size)
        floor = _ceil(last_price * (1 - OTR_EXEMPT_BAND), tick_size)
        price = min(max(price, floor), ceiling)

    return max(price, bid)


def slices_left(*, moment: datetime, close: datetime, interval_seconds: int) -> int:
    """Rounds remaining before *close*, counting this one. At least one."""
    if interval_seconds <= 0:
        return 1
    return max(1, math.ceil(_seconds(moment, close) / interval_seconds))
