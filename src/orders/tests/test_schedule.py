"""The schedule's arithmetic, on its own.

It has to be right at any quantity, any spread and any tick size -- MRF's
five rupees and a gold ETF's paisa run the same rule -- so it is tested here
without a database or a clock.
"""

import math
from datetime import datetime, timedelta
from decimal import Decimal

from orders import schedule

OPEN = datetime(2026, 9, 22, 9, 45)
CLOSE = datetime(2026, 9, 22, 14, 0)  # 15,300 seconds


def minutes(n):
    return OPEN + timedelta(minutes=n)


# -- progress and what is owed ---------------------------------------------


def test_progress_runs_from_nothing_to_everything():
    assert schedule.progress(moment=OPEN, start=OPEN, close=CLOSE) == 0
    assert schedule.progress(moment=CLOSE, start=OPEN, close=CLOSE) == 1
    assert schedule.progress(moment=minutes(255 / 2), start=OPEN, close=CLOSE) == 0.5


def test_lookahead_brings_the_next_round_forward():
    assert schedule.progress(moment=OPEN, start=OPEN, close=CLOSE, lookahead=153) == 0.01


def test_progress_never_exceeds_the_window():
    late = CLOSE + timedelta(hours=1)
    assert schedule.progress(moment=late, start=OPEN, close=CLOSE, lookahead=30) == 1


def test_an_empty_window_is_already_done():
    assert schedule.progress(moment=OPEN, start=CLOSE, close=CLOSE) == 1


def test_what_is_scheduled_rounds_up_and_never_past_the_quantity():
    assert schedule.scheduled(4, 0.01) == 1
    assert schedule.scheduled(4, 0.5) == 2
    assert schedule.scheduled(4, 0.51) == 3
    assert schedule.scheduled(4, 1.0) == 4


def test_an_exact_fraction_is_not_rounded_past_itself():
    """Floating point must not turn exactly half of four into three."""
    assert schedule.scheduled(4, 0.5) == 2
    assert schedule.scheduled(10, 0.3) == 3


# -- how far behind --------------------------------------------------------


def test_a_name_ahead_of_schedule_is_not_behind():
    assert (
        schedule.seconds_behind(quantity=4, filled=2, moment=minutes(60), start=OPEN, close=CLOSE)
        == 0
    )


def test_behind_is_measured_in_time():
    """Nothing filled halfway through: behind by half the window."""
    behind = schedule.seconds_behind(
        quantity=4, filled=0, moment=minutes(255 / 2), start=OPEN, close=CLOSE
    )
    assert behind == 7650


def test_behind_means_the_same_at_any_quantity():
    kwargs = {"moment": minutes(255 / 2), "start": OPEN, "close": CLOSE}
    assert schedule.seconds_behind(quantity=4, filled=1, **kwargs) == schedule.seconds_behind(
        quantity=400, filled=100, **kwargs
    )


def test_urgency_is_a_fraction_of_the_catch_up_time():
    assert schedule.urgency(behind_seconds=0, catch_up_minutes=30) == 0
    assert schedule.urgency(behind_seconds=900, catch_up_minutes=30) == Decimal("0.5")
    assert schedule.urgency(behind_seconds=1800, catch_up_minutes=30) == 1
    assert schedule.urgency(behind_seconds=9000, catch_up_minutes=30) == 1


def test_no_catch_up_time_means_pay_the_ask_at_once():
    assert schedule.urgency(behind_seconds=0, catch_up_minutes=0) == 1


# -- the price -------------------------------------------------------------


def price(**overrides):
    kwargs = {
        "bid": Decimal("1650.00"),
        "ask": Decimal("1651.00"),
        "urgency": Decimal(0),
        "tick_size": Decimal("0.05"),
    }
    kwargs.update(overrides)
    return schedule.buy_price(**kwargs)


def test_on_schedule_it_rests_at_the_bid():
    assert price() == Decimal("1650.00")


def test_fully_behind_it_pays_the_ask():
    assert price(urgency=Decimal(1)) == Decimal("1651.00")


def test_halfway_behind_it_bids_halfway():
    assert price(urgency=Decimal("0.5")) == Decimal("1650.50")


def test_the_price_lands_on_a_tick_and_rounds_down():
    assert price(urgency=Decimal("0.33")) == Decimal("1650.30")


def test_the_price_never_goes_above_the_ask():
    assert price(urgency=Decimal(1), ask=Decimal("1650.05")) == Decimal("1650.05")


def test_the_price_is_kept_inside_the_otr_band():
    """0.75% of 1000 is 7.50: a bid of 990 is raised, an ask of 1010 is capped."""
    wide = {"bid": Decimal("990"), "ask": Decimal("1010"), "last_price": Decimal("1000")}
    assert price(urgency=Decimal(0), **wide) == Decimal("992.50")
    assert price(urgency=Decimal(1), **wide) == Decimal("1007.50")


def test_the_otr_band_never_lifts_the_price_past_the_ask():
    assert price(bid=Decimal("900"), ask=Decimal("901"), last_price=Decimal("1000")) == Decimal(
        "901"
    )


def test_a_coarse_tick_steps_in_its_own_size():
    assert price(
        bid=Decimal("120000"),
        ask=Decimal("120050"),
        urgency=Decimal("0.5"),
        tick_size=Decimal("5"),
    ) == Decimal("120025")


# -- the sell side: the mirror (M33) ---------------------------------------


def sell(**overrides):
    kwargs = {
        "bid": Decimal("1650.00"),
        "ask": Decimal("1651.00"),
        "urgency": Decimal(0),
        "tick_size": Decimal("0.05"),
    }
    kwargs.update(overrides)
    return schedule.sell_price(**kwargs)


def test_a_sell_on_schedule_rests_at_the_ask():
    assert sell() == Decimal("1651.00")


def test_a_sell_fully_behind_takes_the_bid():
    assert sell(urgency=Decimal(1)) == Decimal("1650.00")


def test_a_sell_halfway_behind_asks_halfway():
    assert sell(urgency=Decimal("0.5")) == Decimal("1650.50")


def test_a_sell_price_lands_on_a_tick_and_rounds_up():
    """The seller's side of the spread: rounding keeps the better price."""
    assert sell(urgency=Decimal("0.33")) == Decimal("1650.70")


def test_a_sell_is_kept_inside_the_otr_band():
    wide = {"bid": Decimal("990"), "ask": Decimal("1010"), "last_price": Decimal("1000")}
    assert sell(urgency=Decimal(0), **wide) == Decimal("1007.50")
    assert sell(urgency=Decimal(1), **wide) == Decimal("992.50")


def test_a_sell_never_goes_under_the_bid_even_when_the_band_would():
    assert sell(bid=Decimal("1100"), ask=Decimal("1101"), last_price=Decimal("1000")) == Decimal(
        "1100"
    )


# -- the close -------------------------------------------------------------


def test_rounds_left_counts_this_one():
    at_three = datetime(2026, 9, 22, 15, 0)
    end = datetime(2026, 9, 22, 15, 25)
    assert schedule.slices_left(moment=at_three, close=end, interval_seconds=30) == 50
    assert schedule.slices_left(moment=end, close=end, interval_seconds=30) == 1
    assert (
        schedule.slices_left(moment=end + timedelta(minutes=5), close=end, interval_seconds=30) == 1
    )


# -- an outage is re-spread, not caught up ----------------------------------


def test_no_gaps_is_the_even_schedule():
    assert schedule.progress(moment=minutes(60), start=OPEN, close=CLOSE, gaps=()) == (
        schedule.progress(moment=minutes(60), start=OPEN, close=CLOSE)
    )


def test_the_schedule_holds_still_through_an_outage():
    """Down 10:45-11:45: on resuming it owes what it owed when it went down."""
    gaps = [(minutes(60), minutes(120))]
    before = schedule.progress(moment=minutes(60), start=OPEN, close=CLOSE)
    after = schedule.progress(moment=minutes(120), start=OPEN, close=CLOSE, gaps=gaps)
    assert abs(after - before) < 1e-9


def test_what_is_left_is_spread_over_what_is_left():
    """After the outage the rest runs evenly to the close: halfway through the
    remaining time, half of what was left is due."""
    gaps = [(minutes(60), minutes(120))]
    at_resume = 60 / 255
    halfway = minutes(120 + (255 - 120) / 2)
    fraction = schedule.progress(moment=halfway, start=OPEN, close=CLOSE, gaps=gaps)
    assert abs(fraction - (at_resume + (1 - at_resume) / 2)) < 1e-9


def test_a_re_spread_day_still_finishes_at_the_close():
    gaps = [(minutes(60), minutes(120))]
    assert schedule.progress(moment=CLOSE, start=OPEN, close=CLOSE, gaps=gaps) == 1
    last_round = CLOSE - timedelta(seconds=30)
    assert (
        schedule.progress(moment=last_round, start=OPEN, close=CLOSE, lookahead=30, gaps=gaps) == 1
    )


def test_two_outages_compose():
    gaps = [(minutes(30), minutes(60)), (minutes(90), minutes(120))]
    before_second = schedule.progress(moment=minutes(90), start=OPEN, close=CLOSE, gaps=gaps[:1])
    after_second = schedule.progress(moment=minutes(120), start=OPEN, close=CLOSE, gaps=gaps)
    assert abs(after_second - before_second) < 1e-9


def test_a_name_on_its_re_spread_schedule_is_not_behind():
    gaps = [(minutes(60), minutes(120))]
    expected = schedule.progress(moment=minutes(120), start=OPEN, close=CLOSE, gaps=gaps)
    assert (
        schedule.seconds_behind(
            quantity=1000,
            filled=math.ceil(1000 * expected),
            moment=minutes(120),
            start=OPEN,
            close=CLOSE,
            gaps=gaps,
        )
        == 0
    )


def test_behind_after_a_re_spread_is_measured_at_the_new_pace():
    """Ten minutes' worth short at the re-spread pace is ten minutes behind."""
    gaps = [(minutes(60), minutes(120))]
    moment = minutes(180)
    expected = schedule.progress(moment=moment, start=OPEN, close=CLOSE, gaps=gaps)
    pace = (1 - expected) / (CLOSE - moment).total_seconds()  # fraction per second
    short = pace * 600
    behind = schedule.seconds_behind(
        quantity=1_000_000,
        filled=round(1_000_000 * (expected - short)),
        moment=moment,
        start=OPEN,
        close=CLOSE,
        gaps=gaps,
    )
    assert abs(behind - 600) < 1
