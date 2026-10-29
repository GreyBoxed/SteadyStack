"""Ranking and selection.

The cases that matter are the degenerate ones. A portfolio worth nothing is
0/0 and not zero; a universe where everything is excluded must say so rather
than crash; and the ranking must be a total order, because on day one every
gap in an equal-weight portfolio is identical and an arbitrary tie-break would
make the same inputs buy different things on different runs.
"""

from decimal import Decimal

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from engine.selection import (
    EMPTY_UNIVERSE,
    EXCLUDED_ABOVE_CEILING,
    EXCLUDED_INACTIVE,
    EXCLUDED_NO_PRICE,
    NO_AFFORDABLE_INSTRUMENT,
    Quote,
    rank,
    select,
    shares_to_reach_target,
)


def quote(symbol, last, ask=None):
    return Quote(
        tradingsymbol=symbol,
        last_price=Decimal(str(last)),
        ask=Decimal(str(ask if ask is not None else last)),
    )


EQUAL_THIRDS = {
    "AAA": Decimal("33.33"),
    "BBB": Decimal("33.33"),
    "CCC": Decimal("33.34"),
}


# -- ranking ---------------------------------------------------------------


def test_day_one_ranks_every_gap_as_minus_its_target():
    """Nothing held, nothing worth anything. 0/0 is not 0/1."""
    ranking = rank(
        weights={"AAA": Decimal("40"), "BBB": Decimal("60")},
        holdings={},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 200)},
    )

    gaps = {c.tradingsymbol: c.gap for c in ranking.candidates}
    assert gaps == {"AAA": Decimal("-40"), "BBB": Decimal("-60")}


def test_a_portfolio_worth_nothing_does_not_divide_by_zero():
    ranking = rank(
        weights={"AAA": Decimal("100")},
        holdings={"AAA": 0},
        quotes={"AAA": quote("AAA", 100)},
    )
    assert ranking.portfolio_value == Decimal("0")
    assert ranking.candidates[0].current_pct == Decimal("0")


def test_the_most_underweight_name_ranks_first():
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={"AAA": 10, "BBB": 1},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 100)},
    )
    assert ranking.candidates[0].tradingsymbol == "BBB"


def test_current_weight_is_computed_at_last_traded_price():
    """Not at the ask: the ask decides affordability, never valuation."""
    ranking = rank(
        weights={"AAA": Decimal("100")},
        holdings={"AAA": 2},
        quotes={"AAA": quote("AAA", 100, ask=110)},
    )
    assert ranking.portfolio_value == Decimal("200")


def test_a_name_above_the_ceiling_still_counts_toward_what_is_held():
    """The ceiling excludes a name from *buying*, not from the portfolio.

    Its shares are still owned and still worth what they are worth (§6.2).
    Leaving them out shrinks the denominator every other weight is measured
    against, so every remaining name reads heavier than it is and the gaps
    that drive the day are wrong.
    """
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={"AAA": 10, "BBB": 10},
        quotes={"AAA": quote("AAA", 1000), "BBB": quote("BBB", 5000)},
        max_price_per_share=Decimal("2000"),
    )

    assert [e.tradingsymbol for e in ranking.excluded] == ["BBB"]
    assert ranking.portfolio_value == Decimal("60000")
    assert ranking.candidates[0].current_pct.quantize(Decimal("0.01")) == Decimal("16.67")


def test_a_name_being_sold_still_counts_toward_what_is_held():
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={"AAA": 10, "BBB": 10},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 100)},
        being_sold={"BBB"},
    )
    assert ranking.portfolio_value == Decimal("2000")


def test_a_name_that_no_longer_trades_still_counts_toward_what_is_held():
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={"AAA": 10, "BBB": 10},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 100)},
        inactive={"BBB"},
    )
    assert ranking.portfolio_value == Decimal("2000")


def test_a_name_with_no_price_cannot_be_valued_and_is_left_out():
    """The one exclusion that genuinely cannot contribute: there is no price
    to value the holding at, and inventing one would be worse."""
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={"AAA": 10, "BBB": 10},
        quotes={"AAA": quote("AAA", 100)},
    )
    assert ranking.portfolio_value == Decimal("1000")


def test_ties_break_on_the_larger_target_then_the_symbol():
    """Stated in the plan, so stated here: gap ASC, target DESC, symbol ASC."""
    ranking = rank(
        weights={"CCC": Decimal("30"), "AAA": Decimal("30"), "BBB": Decimal("40")},
        holdings={},
        quotes={s: quote(s, 100) for s in ("AAA", "BBB", "CCC")},
    )

    assert [c.tradingsymbol for c in ranking.candidates] == ["BBB", "AAA", "CCC"]


def test_an_expensive_name_leaves_the_universe_entirely():
    """M11's ceiling is standing, not today's affordability test."""
    ranking = rank(
        weights={"AAA": Decimal("50"), "MRF": Decimal("50")},
        holdings={},
        quotes={"AAA": quote("AAA", 100), "MRF": quote("MRF", 142000)},
        max_price_per_share=Decimal("5000"),
    )

    assert [c.tradingsymbol for c in ranking.candidates] == ["AAA"]
    assert ranking.excluded[0].reason == EXCLUDED_ABOVE_CEILING


def test_an_excluded_name_is_recorded_rather_than_dropped():
    """A name that silently disappears is one nobody can ask about."""
    ranking = rank(
        weights={"MRF": Decimal("100")},
        holdings={},
        quotes={"MRF": quote("MRF", 142000)},
        max_price_per_share=Decimal("5000"),
    )
    assert ranking.excluded[0].tradingsymbol == "MRF"


def test_a_name_with_no_price_is_excluded():
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={},
        quotes={"AAA": quote("AAA", 100)},
    )
    assert ranking.excluded[0].reason == EXCLUDED_NO_PRICE


def test_a_zero_price_is_not_a_price():
    """A zero tick would value a holding at nothing and send the day to buy it."""
    ranking = rank(
        weights={"AAA": Decimal("100")},
        holdings={"AAA": 5},
        quotes={"AAA": quote("AAA", 0)},
    )
    assert ranking.is_empty is True
    assert ranking.excluded[0].reason == EXCLUDED_NO_PRICE


def test_a_zero_ask_is_not_a_price():
    ranking = rank(
        weights={"AAA": Decimal("100")},
        holdings={},
        quotes={"AAA": quote("AAA", 100, ask=0)},
    )
    assert ranking.is_empty is True


def test_a_delisted_name_is_excluded():
    ranking = rank(
        weights={"AAA": Decimal("100")},
        holdings={"AAA": 3},
        quotes={"AAA": quote("AAA", 100)},
        inactive={"AAA"},
    )
    assert ranking.excluded[0].reason == EXCLUDED_INACTIVE


def test_the_snapshot_carries_the_whole_ranking():
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={"AAA": 1},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 50)},
    )

    snapshot = ranking.as_snapshot()

    assert [row["symbol"] for row in snapshot["ranked"]] == ["BBB", "AAA"]
    assert snapshot["ranked"][0]["rank"] == 1
    assert snapshot["portfolio_value"] == "100"


def test_the_snapshot_is_json_serialisable():
    """It lands in a JSONField and must survive the trip exactly."""
    import json

    ranking = rank(
        weights={"AAA": Decimal("100")},
        holdings={"AAA": 2},
        quotes={"AAA": quote("AAA", "1650.55", ask="1650.60")},
    )
    assert json.loads(json.dumps(ranking.as_snapshot()))["ranked"][0]["ask"] == "1650.60"


# -- selection -------------------------------------------------------------


def test_the_first_affordable_underweight_name_is_selected():
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 200)},
    )

    selection = select(ranking, spendable_cash=Decimal("1000"))

    assert selection.bought_something is True


def test_affordability_is_tested_against_the_ask_not_the_last_price():
    """LTP 990 with an ask of 1010 passes an LTP test and can never fill."""
    ranking = rank(
        weights={"AAA": Decimal("100")},
        holdings={},
        quotes={"AAA": quote("AAA", 990, ask=1010)},
    )

    selection = select(ranking, spendable_cash=Decimal("1000"))

    assert selection.bought_something is False
    assert selection.reason == NO_AFFORDABLE_INSTRUMENT


def test_the_quantity_is_sized_at_the_ask():
    ranking = rank(
        weights={"AAA": Decimal("100")},
        holdings={},
        quotes={"AAA": quote("AAA", 90, ask=100)},
    )

    selection = select(ranking, spendable_cash=Decimal("1000"))

    assert selection.quantity == 10


def test_an_unaffordable_leader_is_passed_over_for_the_next_name():
    # MRF carries the larger target, so with nothing held it leads on gap.
    ranking = rank(
        weights={"MRF": Decimal("60"), "AAA": Decimal("40")},
        holdings={},
        quotes={"MRF": quote("MRF", 142000), "AAA": quote("AAA", 100)},
    )

    selection = select(ranking, spendable_cash=Decimal("1000"))

    assert selection.candidate.tradingsymbol == "AAA"
    assert selection.passed_over == ["MRF"]


def test_nothing_affordable_says_so_rather_than_buying_something_else():
    ranking = rank(
        weights={"AAA": Decimal("100")},
        holdings={},
        quotes={"AAA": quote("AAA", 5000)},
    )

    selection = select(ranking, spendable_cash=Decimal("1000"))

    assert selection.candidate is None
    assert selection.reason == NO_AFFORDABLE_INSTRUMENT


def test_an_empty_universe_says_so():
    ranking = rank(weights={}, holdings={}, quotes={})
    assert select(ranking, spendable_cash=Decimal("1000")).reason == EMPTY_UNIVERSE


def test_a_fully_excluded_universe_is_an_empty_one():
    ranking = rank(
        weights={"MRF": Decimal("100")},
        holdings={},
        quotes={"MRF": quote("MRF", 142000)},
        max_price_per_share=Decimal("5000"),
    )
    assert select(ranking, spendable_cash=Decimal("1000")).reason == EMPTY_UNIVERSE


# -- the concentration cap (M15c) -----------------------------------------


def test_a_days_buy_cannot_carry_a_name_past_its_target():
    """A lumpsum must not put the whole portfolio into one name."""
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={"AAA": 10, "BBB": 10},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 100)},
    )

    selection = select(ranking, spendable_cash=Decimal("100000"))

    reachable = ranking.portfolio_value + Decimal("100000")
    for pick in selection.picks:
        assert pick.candidate.value_held + pick.cost <= reachable / 2


def test_a_name_underweight_by_less_than_one_share_is_passed_over():
    """The cap yielding zero means the walk moves on, not that the day stops."""
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={"AAA": 100, "BBB": 99},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 100)},
    )

    selection = select(ranking, spendable_cash=Decimal("100"))

    assert selection.quantity >= 1 or "BBB" in selection.passed_over


def test_the_first_ever_buy_is_not_refused_by_the_cap():
    """With nothing held, one share is 100% of the portfolio. Measured against
    today's holdings alone the cap would refuse every name for ever."""
    ranking = rank(
        weights={"AAA": Decimal("40"), "BBB": Decimal("60")},
        holdings={},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 100)},
    )

    selection = select(ranking, spendable_cash=Decimal("1000"))

    assert selection.bought_something is True


def test_the_cap_respects_the_target_share_of_reachable_value():
    ranking = rank(
        weights={"AAA": Decimal("40"), "BBB": Decimal("60")},
        holdings={},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 100)},
    )

    selection = select(ranking, spendable_cash=Decimal("1000"))

    # BBB leads on the larger target: 60% of the 1,000 reachable is 600, and
    # the walk carries on to AAA with the 400 left (M15).
    assert [(p.candidate.tradingsymbol, p.quantity) for p in selection.picks] == [
        ("BBB", 6),
        ("AAA", 4),
    ]
    assert selection.cost == Decimal("1000")


def test_a_name_at_its_target_yields_no_shares():
    candidate = rank(
        weights={"AAA": Decimal("50")},
        holdings={"AAA": 10},
        quotes={"AAA": quote("AAA", 100)},
    ).candidates[0]

    assert (
        shares_to_reach_target(
            candidate, portfolio_value=Decimal("1000"), spendable_cash=Decimal("0")
        )
        == 0
    )


def test_cash_no_name_can_take_is_simply_not_spent():
    """Every name at target, and less than a share of anything left: the
    remainder carries (M15c)."""
    ranking = rank(
        weights={"AAA": Decimal("40"), "BBB": Decimal("60")},
        holdings={},
        quotes={"AAA": quote("AAA", 300), "BBB": quote("BBB", 300)},
    )

    selection = select(ranking, spendable_cash=Decimal("1000"))

    # BBB: 600 of target buys 2 (600); AAA: 400 buys 1 (300); 100 is left.
    assert selection.quantity == 3
    assert selection.cost == Decimal("900")


def test_a_day_buys_every_name_its_cash_reaches():
    """The whole point of M15: a large day is spread across the universe, not
    parked in one name with the rest carried."""
    weights = {s: Decimal("25") for s in ("AAA", "BBB", "CCC", "DDD")}
    ranking = rank(
        weights=weights,
        holdings={},
        quotes={s: quote(s, 100) for s in weights},
    )

    selection = select(ranking, spendable_cash=Decimal("10000"))

    assert {p.candidate.tradingsymbol: p.quantity for p in selection.picks} == {
        "AAA": 25,
        "BBB": 25,
        "CCC": 25,
        "DDD": 25,
    }


def test_the_reachable_portfolio_sets_targets_even_when_less_is_spent():
    """With cash set aside (the reserve), targets are still shares of the
    whole portfolio the day can reach -- only the budget is smaller."""
    ranking = rank(
        weights={"AAA": Decimal("50"), "BBB": Decimal("50")},
        holdings={},
        quotes={"AAA": quote("AAA", 100), "BBB": quote("BBB", 100)},
    )

    selection = select(ranking, spendable_cash=Decimal("600"), reachable_cash=Decimal("1000"))

    assert [(p.candidate.tradingsymbol, p.quantity) for p in selection.picks] == [
        ("AAA", 5),
        ("BBB", 1),
    ]


# -- properties ------------------------------------------------------------

symbols = st.text(alphabet="ABCDEFGH", min_size=2, max_size=4)
prices = st.decimals(min_value=Decimal("1"), max_value=Decimal("100000"), places=2)
quantities = st.integers(min_value=0, max_value=1000)


@settings(max_examples=200, deadline=None)
@given(
    universe=st.dictionaries(symbols, prices, min_size=1, max_size=8),
    holdings=st.dictionaries(symbols, quantities, max_size=8),
)
def test_the_ranking_is_a_total_order(universe, holdings):
    """Two runs over the same inputs must produce the same ranking, or the
    same portfolio buys different things on different days for no reason."""
    weights = {symbol: Decimal("100") / len(universe) for symbol in universe}
    quotes = {symbol: quote(symbol, price) for symbol, price in universe.items()}

    first = rank(weights=weights, holdings=holdings, quotes=quotes)
    second = rank(weights=weights, holdings=holdings, quotes=quotes)

    assert [c.tradingsymbol for c in first.candidates] == [
        c.tradingsymbol for c in second.candidates
    ]


@settings(max_examples=200, deadline=None)
@given(
    universe=st.dictionaries(symbols, prices, min_size=1, max_size=8),
    holdings=st.dictionaries(symbols, quantities, max_size=8),
)
def test_every_name_appears_exactly_once(universe, holdings):
    weights = {symbol: Decimal("100") / len(universe) for symbol in universe}
    quotes = {symbol: quote(symbol, price) for symbol, price in universe.items()}

    ranking = rank(weights=weights, holdings=holdings, quotes=quotes)

    names = [c.tradingsymbol for c in ranking.candidates]
    assert len(names) == len(set(names)) == len(universe)


@settings(max_examples=200, deadline=None)
@given(
    universe=st.dictionaries(symbols, prices, min_size=1, max_size=6),
    holdings=st.dictionaries(symbols, quantities, max_size=6),
    cash=st.decimals(min_value=Decimal("0"), max_value=Decimal("1000000"), places=2),
)
def test_a_selection_never_costs_more_than_the_day_has(universe, holdings, cash):
    """The one property the cash ledger depends on."""
    weights = {symbol: Decimal("100") / len(universe) for symbol in universe}
    quotes = {symbol: quote(symbol, price) for symbol, price in universe.items()}

    ranking = rank(weights=weights, holdings=holdings, quotes=quotes)
    selection = select(ranking, spendable_cash=cash)

    assert selection.cost <= cash


@settings(max_examples=200, deadline=None)
@given(
    universe=st.dictionaries(symbols, prices, min_size=1, max_size=6),
    cash=st.decimals(min_value=Decimal("1"), max_value=Decimal("1000000"), places=2),
)
def test_a_selected_name_is_always_one_that_was_ranked(universe, cash):
    weights = {symbol: Decimal("100") / len(universe) for symbol in universe}
    quotes = {symbol: quote(symbol, price) for symbol, price in universe.items()}

    ranking = rank(weights=weights, holdings={}, quotes=quotes)
    selection = select(ranking, spendable_cash=cash)
    assume(selection.candidate is not None)

    assert selection.candidate.tradingsymbol in {c.tradingsymbol for c in ranking.candidates}


@settings(max_examples=100, deadline=None)
@given(
    universe=st.dictionaries(symbols, prices, min_size=2, max_size=6),
    cash=st.decimals(min_value=Decimal("1"), max_value=Decimal("100000"), places=2),
)
def test_the_walk_never_overspends_or_overshoots(universe, cash):
    """Whatever the universe and the cash: names are taken in ranking order,
    the day never spends more than it has, and no name passes its target."""
    weights = {symbol: Decimal("100") / len(universe) for symbol in universe}
    quotes = {symbol: quote(symbol, price) for symbol, price in universe.items()}

    ranking = rank(weights=weights, holdings={}, quotes=quotes)
    selection = select(ranking, spendable_cash=cash)

    order = [c.tradingsymbol for c in ranking.candidates]
    positions = [order.index(p.candidate.tradingsymbol) for p in selection.picks]
    assert positions == sorted(positions)
    assert selection.cost <= cash
    reachable = ranking.portfolio_value + cash
    for pick in selection.picks:
        share = pick.candidate.target_pct / Decimal("100") * reachable
        assert pick.candidate.value_held + pick.cost <= share


# -- why a name was passed over (M15d) -------------------------------------


def test_an_unaffordable_name_is_recorded_as_unaffordable():
    ranking = rank(
        weights={"MRF": Decimal("60"), "AAA": Decimal("40")},
        holdings={},
        quotes={"MRF": quote("MRF", 142000), "AAA": quote("AAA", 100)},
    )

    selection = select(ranking, spendable_cash=Decimal("1000"))

    assert selection.unaffordable == ["MRF"]
    assert selection.capped == []


def test_a_capped_name_is_recorded_as_capped_not_unaffordable():
    """The distinction the reserve depends on. A name the day could easily
    afford, but which a single share would carry past its target, is not a
    name the portfolio is waiting for -- and counting it as one would idle
    the portfolio for a name that was never out of reach."""
    ranking = rank(
        weights={"AAA": Decimal("35"), "BBB": Decimal("25"), "CCC": Decimal("40")},
        holdings={"CCC": 3},
        quotes={
            "AAA": quote("AAA", 3400, ask=3401),
            "BBB": quote("BBB", 1700, ask=1700.5),
            "CCC": quote("CCC", 1650, ask=1650.5),
        },
    )

    selection = select(ranking, spendable_cash=Decimal("3500"))

    assert "AAA" in selection.capped
    assert selection.unaffordable == []
    assert selection.candidate.tradingsymbol == "BBB"


def test_passed_over_still_reports_both_in_ranking_order():
    ranking = rank(
        weights={"AAA": Decimal("35"), "BBB": Decimal("25"), "CCC": Decimal("40")},
        holdings={"CCC": 3},
        quotes={
            "AAA": quote("AAA", 3400, ask=3401),
            "BBB": quote("BBB", 1700, ask=1700.5),
            "CCC": quote("CCC", 1650, ask=1650.5),
        },
    )

    selection = select(ranking, spendable_cash=Decimal("3500"))
    order = [c.tradingsymbol for c in ranking.candidates]

    assert selection.passed_over == [s for s in order if s in set(selection.capped)]
