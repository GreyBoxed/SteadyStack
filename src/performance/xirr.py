"""XIRR: the internal rate of return over irregularly dated flows.

Written here rather than imported because the alternative is scipy on a single
small instance, for one root-find over at most a few thousand points. Brent's
method over a bounded bracket, which is bisection whenever the interpolated
step misbehaves -- so it inherits bisection's guaranteed convergence and keeps
superlinear speed when the function is well behaved.

Actual/365. The bracket floors just above -100% -- at exactly -100% the
discount factor is zero and the function is undefined -- and ceilings at
1000%, which no real portfolio reaches and which stops a pathological set of
flows searching forever.

Floats are used deliberately inside the solver: this is numerical root-finding,
not bookkeeping, and Decimal's exactness buys nothing against an answer that is
converged to a tolerance anyway. The result comes back as a Decimal.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

DAYS_IN_YEAR = 365.0

#: A rate below this is indistinguishable from a wipeout, and at exactly
#: -1.0 the discount factor is zero.
RATE_FLOOR = -0.9999999
RATE_CEILING = 10.0

TOLERANCE = 1e-12
STEP_TOLERANCE = 1e-12
MAX_ITERATIONS = 300

#: Below this span, annualising is an artefact rather than a return.
MINIMUM_HISTORY_DAYS = 30


class InsufficientHistory(Exception):
    """Too few flows, or too short a span, for the answer to mean anything."""


class NoSolution(Exception):
    """The flows do not bracket a root -- usually because they share a sign."""


@dataclass(frozen=True)
class Flow:
    on: date
    amount: Decimal


def _net_present_value(flows: list[Flow], rate: float, origin: date) -> float:
    total = 0.0
    for flow in flows:
        years = (flow.on - origin).days / DAYS_IN_YEAR
        total += float(flow.amount) / (1.0 + rate) ** years
    return total


def solve(flows: list[Flow]) -> Decimal:
    """The annualised money-weighted return, as a fraction.

    `0.1234` is 12.34%. Raises rather than returning a number it cannot stand
    behind: `InsufficientHistory` when there is too little to annualise,
    `NoSolution` when no rate in the bracket sets the present value to zero.
    """
    if len(flows) < 2:
        raise InsufficientHistory("XIRR needs at least two dated flows.")

    ordered = sorted(flows, key=lambda flow: flow.on)
    origin = ordered[0].on
    span = (ordered[-1].on - origin).days
    if span < MINIMUM_HISTORY_DAYS:
        raise InsufficientHistory(
            f"{span} days of history is too short to annualise; "
            f"{MINIMUM_HISTORY_DAYS} is the minimum."
        )

    signs = {flow.amount > 0 for flow in ordered if flow.amount != 0}
    if len(signs) < 2:
        raise NoSolution("Flows must include both an outflow and an inflow.")

    low, high = RATE_FLOOR, RATE_CEILING
    f_low = _net_present_value(ordered, low, origin)
    f_high = _net_present_value(ordered, high, origin)
    if f_low * f_high > 0:
        raise NoSolution("No rate between -100% and 1000% sets the present value to zero.")

    return Decimal(str(_brent(ordered, origin, low, high, f_low, f_high)))


def _brent(flows, origin, a, b, f_a, f_b) -> float:
    """Brent's method over the bracket [a, b], where f(a) and f(b) differ in sign.

    `b` is always the better of the two guesses, `c` the previous `b` and `d`
    the one before that. The clause deciding whether to accept an interpolated
    step is Brent's: take it only when it lands inside the bracket and is
    actually shrinking things, otherwise bisect.
    """
    if abs(f_a) < abs(f_b):
        a, b = b, a
        f_a, f_b = f_b, f_a

    c, f_c = a, f_a
    d = c
    bisected = True

    for _ in range(MAX_ITERATIONS):
        if abs(f_b) < TOLERANCE or abs(b - a) < STEP_TOLERANCE:
            return b

        if f_a != f_c and f_b != f_c:
            s = (
                a * f_b * f_c / ((f_a - f_b) * (f_a - f_c))
                + b * f_a * f_c / ((f_b - f_a) * (f_b - f_c))
                + c * f_a * f_b / ((f_c - f_a) * (f_c - f_b))
            )
        elif f_b != f_a:
            s = b - f_b * (b - a) / (f_b - f_a)
        else:
            s = (a + b) / 2

        lower, upper = sorted(((3 * a + b) / 4, b))
        if (
            not lower < s < upper
            or (bisected and abs(s - b) >= abs(b - c) / 2)
            or (not bisected and abs(s - b) >= abs(c - d) / 2)
            or (bisected and abs(b - c) < STEP_TOLERANCE)
            or (not bisected and abs(c - d) < STEP_TOLERANCE)
        ):
            s = (a + b) / 2
            bisected = True
        else:
            bisected = False

        f_s = _net_present_value(flows, s, origin)
        d, c, f_c = c, b, f_b

        if f_a * f_s < 0:
            b, f_b = s, f_s
        else:
            a, f_a = s, f_s

        if abs(f_a) < abs(f_b):
            a, b = b, a
            f_a, f_b = f_b, f_a

    return b
