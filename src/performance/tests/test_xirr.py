"""The money-weighted return, and its refusals.

Daily irregular contributions make a simple percentage return meaningless, so
XIRR is the headline metric (M41). It is also the metric most easily made to
print nonsense: one contribution annualises a one-day move into a number in
the thousands of percent, and same-sign flows have no solution at all. Both
are refusals here rather than numbers on a dashboard.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from performance.xirr import Flow, InsufficientHistory, NoSolution, solve

START = date(2025, 1, 1)


def test_doubling_over_a_year_is_about_one_hundred_percent():
    rate = solve(
        [Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("2000"))]
    )

    assert abs(rate - Decimal("1")) < Decimal("0.001")


def test_no_growth_is_zero():
    rate = solve(
        [Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("1000"))]
    )

    assert abs(rate) < Decimal("0.0001")


def test_a_loss_is_negative():
    rate = solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("900"))])

    assert Decimal("-0.11") < rate < Decimal("-0.09")


def test_irregular_contributions_are_weighted_by_time():
    """Money in later has less time to work, so it moves the rate less."""
    flows = [
        Flow(START, Decimal("-1000")),
        Flow(START + timedelta(days=180), Decimal("-1000")),
        Flow(START + timedelta(days=365), Decimal("2100")),
    ]

    rate = solve(flows)

    assert Decimal("0.04") < rate < Decimal("0.12")


def test_one_flow_is_refused():
    with pytest.raises(InsufficientHistory):
        solve([Flow(START, Decimal("-1000"))])


def test_a_short_history_is_refused():
    """A single day's move annualised is not a return, it is an artefact."""
    with pytest.raises(InsufficientHistory):
        solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=5), Decimal("1010"))])


def test_flows_of_one_sign_have_no_solution():
    with pytest.raises(NoSolution):
        solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("-500"))])


def test_a_total_loss_does_not_hang():
    """The bracket's floor is just above -100%, so a wipeout converges to it
    rather than dividing by zero."""
    rate = solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("1"))])

    assert rate < Decimal("-0.99")


def test_flows_need_not_arrive_in_order():
    ordered = solve(
        [Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("1200"))]
    )
    shuffled = solve(
        [Flow(START + timedelta(days=365), Decimal("1200")), Flow(START, Decimal("-1000"))]
    )

    assert ordered == shuffled


# -- the solver's own invariant ------------------------------------------


def test_the_rate_it_returns_zeroes_the_present_value():
    """Whatever the solver returns, discounting the flows at that rate must
    come back to zero. This is the definition, and the only check that does
    not just restate the implementation."""
    from performance.xirr import _net_present_value

    flows = [
        Flow(START, Decimal("-500")),
        Flow(START + timedelta(days=90), Decimal("-750")),
        Flow(START + timedelta(days=200), Decimal("-250")),
        Flow(START + timedelta(days=400), Decimal("1700")),
    ]

    rate = solve(flows)

    assert abs(_net_present_value(flows, float(rate), START)) < 0.01


@given(
    outflows=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=900),
            st.decimals(min_value=Decimal("1"), max_value=Decimal("100000"), places=2),
        ),
        min_size=1,
        max_size=12,
    ),
    final=st.decimals(min_value=Decimal("1"), max_value=Decimal("5000000"), places=2),
)
@settings(max_examples=200, deadline=None)
def test_any_rate_it_returns_really_is_a_root(outflows, final):
    """The property that matters for a hand-rolled solver: whatever comes
    back, discounting the flows at it lands on zero. Examples would only ever
    prove the shapes somebody thought of."""
    from performance.xirr import _net_present_value

    flows = [Flow(START + timedelta(days=day), -amount) for day, amount in outflows]
    flows.append(Flow(START + timedelta(days=1000), final))

    try:
        rate = solve(flows)
    except (InsufficientHistory, NoSolution):
        return  # a refusal is a valid answer, and the other tests pin those

    # Discounted from the earliest flow, which is the origin `solve` itself
    # uses. The choice is arithmetically free -- moving the origin multiplies
    # every term by one common factor, so a root stays a root -- and
    # numerically it is the whole ballgame. Measured from a date before the
    # first flow that factor is (1+r) to a negative power, which near a total
    # loss is astronomically large: the terms blow up past 1e20 and what
    # survives the cancellation says nothing about the rate.
    origin = min(flow.on for flow in flows)
    r = float(rate)

    # Asserted in *rate* space, not in value space. Near a wipeout the present
    # value is vertical -- a rate pinned to the last bit of a float still
    # leaves f at 1e-4, because f' is 1e11 -- so "f is small" is a claim about
    # the steepness and not about the answer. The Newton correction f/f' is
    # the distance from here to the true root, in the units the answer is
    # given in, and that is what has to be negligible.
    value = _net_present_value(flows, r, origin)
    step = 1e-9 * max(abs(r), 1.0)
    slope = (
        _net_present_value(flows, r + step, origin) - _net_present_value(flows, r - step, origin)
    ) / (2 * step)

    assert slope != 0, "a flat present value would mean the rate is not determined at all"
    assert abs(value / slope) < 1e-6
