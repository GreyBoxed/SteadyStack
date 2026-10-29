"""Which instrument today buys, and how much of it.

The whole decision, in order:

  1. the universe is the current model portfolio, minus anything priced above
     the standing ceiling (M11) and anything without a usable price;
  2. each name is valued at its last traded price, giving a current weight and
     a gap against target;
  3. the ranking is by gap ascending, with the tie-break stated and tested --
     on day one every gap is identical in an equal-weight portfolio, so an
     arbitrary order would make the same inputs produce different purchases;
  4. the walk takes the first name the day can actually buy, tested against
     the *ask* rather than the last traded price, because an order the budget
     cannot reach can never fill;
  5. the quantity is capped so that one day's buy cannot carry a name past its
     target weight (M15c).

Prices are Decimal throughout. A float here would be a rounding error in a
ledger that is supposed to reconcile to zero forever.
"""

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from configuration.models import ConfigurationVersion

HUNDRED = Decimal("100")

#: Why a name is not in today's universe. Recorded rather than dropped: a name
#: that silently disappears from the ranking is a name nobody can ask about.
EXCLUDED_ABOVE_CEILING = "above the maximum price per share"
EXCLUDED_NO_PRICE = "no usable price"
EXCLUDED_INACTIVE = "no longer trading"
#: A CNC buy and sell of one name on the same day net off: the shares never
#: arrive, and a lot would be created and consumed without anything moving.
EXCLUDED_BEING_SOLD = "a rebalance sell of this name is running today"


@dataclass(frozen=True)
class Quote:
    """What the market says about one name, right now."""

    tradingsymbol: str
    last_price: Decimal
    ask: Decimal
    #: Where `ask` came from: the live book, or the previous close when the
    #: book had nobody offering (M15e).
    source: str = "ask"

    @property
    def is_usable(self) -> bool:
        """A zero or negative price is not a price.

        It means the feed has nothing for this instrument -- suspended, not
        yet traded today, or a bad tick -- and ranking on it would value a
        holding at nothing and send the day to buy it.
        """
        return self.last_price > 0 and self.ask > 0


@dataclass(frozen=True)
class Candidate:
    tradingsymbol: str
    target_pct: Decimal
    quantity_held: int
    last_price: Decimal
    ask: Decimal
    value_held: Decimal
    current_pct: Decimal
    gap: Decimal
    #: "ask", or "close" when the book had no offer and the previous close
    #: sized it (M15e).
    price_source: str = "ask"

    @property
    def is_underweight(self) -> bool:
        return self.gap < 0


@dataclass(frozen=True)
class Excluded:
    tradingsymbol: str
    target_pct: Decimal
    reason: str


@dataclass
class Ranking:
    candidates: list[Candidate] = field(default_factory=list)
    excluded: list[Excluded] = field(default_factory=list)
    portfolio_value: Decimal = Decimal("0")

    @property
    def is_empty(self) -> bool:
        return not self.candidates

    def as_snapshot(self) -> dict:
        """The permanent explanation of why a share was bought (M16).

        Stored on the tradesheet so the selection is readable years later
        without recomputing anything -- and without needing the prices that
        produced it to still exist anywhere.
        """
        return {
            "portfolio_value": str(self.portfolio_value),
            "ranked": [
                {
                    "rank": position,
                    "symbol": candidate.tradingsymbol,
                    "target_pct": str(candidate.target_pct),
                    "current_pct": str(candidate.current_pct),
                    "gap": str(candidate.gap),
                    "quantity_held": candidate.quantity_held,
                    "last_price": str(candidate.last_price),
                    "ask": str(candidate.ask),
                    "price_source": candidate.price_source,
                    "value_held": str(candidate.value_held),
                }
                for position, candidate in enumerate(self.candidates, start=1)
            ],
            "excluded": [
                {
                    "symbol": excluded.tradingsymbol,
                    "target_pct": str(excluded.target_pct),
                    "reason": excluded.reason,
                }
                for excluded in self.excluded
            ],
        }


@dataclass(frozen=True)
class Pick:
    """One name the day buys, and how many."""

    candidate: Candidate
    quantity: int

    @property
    def cost(self) -> Decimal:
        return self.candidate.ask * self.quantity


@dataclass(frozen=True)
class Selection:
    """The day's answer: every name its cash reaches, or "nothing".

    The two ways a name is passed over are kept apart, because only one of
    them means the portfolio is waiting for something. `unaffordable` is what
    the reserve counts (M15d): the cash left when the walk reached it could
    not buy one share. `capped` is a name that is affordable but already close
    enough to its target that a share would overshoot it -- which is the cap
    working, not a name to idle for.
    """

    picks: list[Pick] = field(default_factory=list)
    unaffordable: list[str] = field(default_factory=list)
    capped: list[str] = field(default_factory=list)
    reason: str = ""
    _walk_order: list[str] = field(default_factory=list)

    @property
    def passed_over(self) -> list[str]:
        """Everything the walk stepped over, in ranking order."""
        skipped = set(self.unaffordable) | set(self.capped)
        return [symbol for symbol in self._walk_order if symbol in skipped]

    @property
    def bought_something(self) -> bool:
        return any(pick.quantity > 0 for pick in self.picks)

    @property
    def candidate(self) -> Candidate | None:
        """The lead name: the most underweight one the day buys."""
        return self.picks[0].candidate if self.picks else None

    @property
    def quantity(self) -> int:
        """Shares across every name the day buys."""
        return sum(pick.quantity for pick in self.picks)

    @property
    def cost(self) -> Decimal:
        return sum((pick.cost for pick in self.picks), Decimal("0"))

    def bought(self, symbol: str) -> bool:
        return any(pick.candidate.tradingsymbol == symbol for pick in self.picks)


NO_AFFORDABLE_INSTRUMENT = "no instrument in the universe is affordable today"
EMPTY_UNIVERSE = "the universe is empty"


def rank(
    *,
    weights: dict[str, Decimal],
    holdings: dict[str, int],
    quotes: dict[str, Quote],
    max_price_per_share: Decimal | None = None,
    inactive: set[str] | None = None,
    being_sold: set[str] | None = None,
) -> Ranking:
    """Rank the universe by how far each name is below its target.

    The portfolio is valued at last traded price, because that is what a
    holding is worth; the ask appears only where affordability and sizing are
    decided.
    """
    inactive = inactive or set()
    being_sold = being_sold or set()
    ranking = Ranking()

    usable: list[tuple[str, Decimal, Quote, int]] = []
    #: Held value of names that are not buyable today but are still owned.
    #: Counted, because the exclusions below are about what the day may
    #: *buy*, and none of them is about what the portfolio is worth.
    held_elsewhere = Decimal("0")

    for symbol in sorted(weights):
        target = weights[symbol]
        quote = quotes.get(symbol)
        held = holdings.get(symbol, 0)

        if quote is None or not quote.is_usable:
            # The one exclusion that cannot contribute a value: there is no
            # price to value the holding at, and inventing one would be worse
            # than leaving it out.
            ranking.excluded.append(Excluded(symbol, target, EXCLUDED_NO_PRICE))
            continue

        reason = ""
        if symbol in inactive:
            reason = EXCLUDED_INACTIVE
        elif symbol in being_sold:
            reason = EXCLUDED_BEING_SOLD
        elif max_price_per_share is not None and quote.last_price > max_price_per_share:
            # A standing ceiling, not today's affordability test: a name above
            # it leaves the universe rather than ranking first and being
            # passed over every day (M11).
            reason = EXCLUDED_ABOVE_CEILING

        if reason:
            ranking.excluded.append(Excluded(symbol, target, reason))
            held_elsewhere += quote.last_price * held
            continue

        usable.append((symbol, target, quote, held))

    # Valued over the whole universe, including names held but priced out of
    # today's buying: they are still part of what the portfolio is worth
    # (§6.2). Leaving them out shrinks the denominator every other weight is
    # measured against, so every remaining name reads heavier than it is.
    portfolio_value = (
        sum(
            (quote.last_price * held for _symbol, _target, quote, held in usable),
            Decimal("0"),
        )
        + held_elsewhere
    )
    ranking.portfolio_value = portfolio_value

    for symbol, target, quote, held in usable:
        value_held = quote.last_price * held
        # Zero over zero is not zero. On day one nothing is held and nothing
        # is worth anything, so every current weight is 0 and every gap is
        # exactly minus its target -- which is correct, and is why this is a
        # branch rather than a division.
        current_pct = (
            (value_held / portfolio_value) * HUNDRED if portfolio_value > 0 else Decimal("0")
        )
        ranking.candidates.append(
            Candidate(
                tradingsymbol=symbol,
                target_pct=target,
                quantity_held=held,
                last_price=quote.last_price,
                ask=quote.ask,
                value_held=value_held,
                current_pct=current_pct,
                gap=current_pct - target,
                price_source=quote.source,
            )
        )

    # The tie-break is part of the specification, not an implementation
    # detail: on day one every gap is identical in an equal-weight portfolio,
    # and without it the same inputs could buy different things.
    ranking.candidates.sort(key=lambda c: (c.gap, -c.target_pct, c.tradingsymbol))
    return ranking


def shares_to_reach_target(
    candidate: Candidate,
    *,
    portfolio_value: Decimal,
    spendable_cash: Decimal,
) -> int:
    """How many shares can be bought without carrying the name past target.

    Measured against the portfolio the day can actually reach -- what is held
    plus what is being spent -- rather than against the portfolio as it stands
    this second. Measured against the latter, the first ever buy is refused:
    with nothing held, one share of anything is 100% of the portfolio and
    overshoots every target below 100, so the walk would pass over the whole
    universe and buy nothing, every day, for ever.
    """
    reachable = portfolio_value + spendable_cash
    if reachable <= 0 or candidate.ask <= 0:
        return 0

    target_value = (candidate.target_pct / HUNDRED) * reachable
    headroom = target_value - candidate.value_held
    if headroom <= 0:
        return 0

    return int((headroom / candidate.ask).to_integral_value(rounding=ROUND_FLOOR))


def select(
    ranking: Ranking,
    *,
    spendable_cash: Decimal,
    reachable_cash: Decimal | None = None,
) -> Selection:
    """Walk the ranking and buy every name the cash reaches, each up to target.

    Most underweight first. Each name takes `min(what the remaining cash
    buys, what brings it to target)`, and the walk carries on with what is
    left until nothing further can be bought (M15, M15c). The targets are
    measured against the portfolio the day can reach -- held value plus
    `reachable_cash`, which is the whole day's cash even when the reserve has
    set some of it aside, because that money still belongs to the portfolio.
    """
    if ranking.is_empty:
        return Selection(reason=EMPTY_UNIVERSE)

    reachable = spendable_cash if reachable_cash is None else reachable_cash
    remaining = spendable_cash
    picks: list[Pick] = []
    unaffordable: list[str] = []
    capped: list[str] = []
    walked: list[str] = []

    for candidate in ranking.candidates:
        walked.append(candidate.tradingsymbol)

        if candidate.ask > remaining:
            unaffordable.append(candidate.tradingsymbol)
            continue

        affordable_quantity = int(
            (remaining / candidate.ask).to_integral_value(rounding=ROUND_FLOOR)
        )
        cap = shares_to_reach_target(
            candidate,
            portfolio_value=ranking.portfolio_value,
            spendable_cash=reachable,
        )
        quantity = min(affordable_quantity, cap)

        if quantity <= 0:
            # Affordable, but a share would carry it past target: underweight
            # by less than one share. The walk moves on (M15c).
            capped.append(candidate.tradingsymbol)
            continue

        picks.append(Pick(candidate=candidate, quantity=quantity))
        remaining -= candidate.ask * quantity

    return Selection(
        picks=picks,
        unaffordable=unaffordable,
        capped=capped,
        reason="" if picks else NO_AFFORDABLE_INSTRUMENT,
        _walk_order=walked,
    )


def ceiling_from(config: ConfigurationVersion) -> Decimal | None:
    return config.max_price_per_share
