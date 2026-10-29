"""One instrument on one day: the Trading / symbol board.

What matters here is that the page is rebuilt from stored rows alone -- the
depth ladder is the book a tick kept, a resting order is marked where it sat
in that book, and the broker calls are the ones stamped with the day's trace.
"""

from datetime import date, datetime, time
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

pytestmark = pytest.mark.django_db

DAY = date(2026, 9, 18)

BOOK = {
    "buy": [
        {"price": 1650.50, "quantity": 400, "orders": 7},
        {"price": 1650.45, "quantity": 1200, "orders": 19},
    ],
    "sell": [
        {"price": 1650.60, "quantity": 250, "orders": 4},
        {"price": 1650.65, "quantity": 900, "orders": 11},
    ],
}


def at(hour, minute, second=0):
    return timezone.make_aware(datetime.combine(DAY, time(hour, minute, second)))


def a_day(instrument, *, quantity=2):
    """A daily-buy day with two one-share orders: one filled, one resting."""
    from engine.models import TradeSheet, TradeSheetState
    from orders.models import Fill, Order, OrderMode, OrderState, OrderType, Tick

    sheet = TradeSheet.objects.create(
        trading_date=DAY,
        version=1,
        state=TradeSheetState.RUNNING,
        trace_id="ts-2026-09-18",
        instrument=instrument,
        quantity=quantity,
        carry_in=Decimal("400.00"),
        spendable_cash=Decimal("3700.00"),
        reference_price=Decimal("1650.60"),
    )

    orders = []
    for tick_no, price, state, placed in (
        (0, "1650.50", OrderState.COMPLETE, at(9, 45, 2)),
        (1, "1650.50", OrderState.OPEN, at(9, 50, 1)),
    ):
        order = Order.objects.create(
            tag=f"t{tick_no}",
            tradesheet=sheet,
            instrument=instrument,
            trading_date=DAY,
            tick_no=tick_no,
            mode=OrderMode.PAPER,
            order_type=OrderType.LIMIT,
            quantity=1,
            limit_price=Decimal(price),
            state=state,
            trace_id=sheet.trace_id,
        )
        # auto_now_add ignores a value passed to create().
        Order.objects.filter(pk=order.pk).update(placed_at=placed)
        order.refresh_from_db()
        Tick.objects.create(
            tradesheet=sheet,
            trading_date=DAY,
            tick_no=tick_no,
            best_bid=Decimal("1650.50"),
            best_ask=Decimal("1650.60"),
            depth_snapshot=BOOK,
            slice_qty=1,
            committed_before=tick_no,
            order=order,
            trace_id=sheet.trace_id,
        )
        orders.append(order)

    Tick.objects.filter(trading_date=DAY).update(fired_at=at(9, 50, 1))
    Tick.objects.filter(trading_date=DAY, tick_no=0).update(fired_at=at(9, 45, 2))

    Order.objects.filter(pk=orders[0].pk).update(filled_quantity=1)
    Fill.objects.create(
        order=orders[0],
        kite_trade_id="paper-1",
        quantity=1,
        price=Decimal("1650.50"),
        charges=Decimal("0.42"),
        executed_at=at(10, 2),
        trace_id=sheet.trace_id,
    )
    return sheet


def board(client, instrument, **params):
    return client.get(
        reverse("web:instrument_day", args=[DAY.isoformat(), instrument.tradingsymbol]), params
    )


def test_it_is_not_reachable_anonymously(client, instrument):
    response = board(client, instrument)

    assert response.status_code == 302


def test_an_unknown_symbol_is_a_404(signed_in, no_broker):
    response = signed_in.get(reverse("web:instrument_day", args=[DAY.isoformat(), "NOPE"]))

    assert response.status_code == 404


def test_a_day_with_nothing_says_so(signed_in, instrument, no_broker):
    body = board(signed_in, instrument).content

    assert b"Nothing was placed for INFY" in body
    assert b"No tick stored a book" in body


def test_the_board_counts_filled_open_and_deployed(signed_in, instrument, no_broker):
    a_day(instrument)

    response = board(signed_in, instrument)
    context = response.context

    assert response.status_code == 200
    assert context["filled"] == 1
    assert context["target"] == 2
    assert context["open_count"] == 1
    assert context["committed_value"] == Decimal("1650.50")
    assert context["avg_price"] == Decimal("1650.50")
    assert context["gross"] == Decimal("1650.50")


def test_the_depth_ladder_is_the_book_the_tick_stored(signed_in, instrument, no_broker):
    a_day(instrument)

    ladder = board(signed_in, instrument).context["ladder"]

    assert [level.price for level in ladder["asks"]] == [Decimal("1650.65"), Decimal("1650.6")]
    assert [level.price for level in ladder["bids"]] == [Decimal("1650.5"), Decimal("1650.45")]
    assert ladder["spread"] == Decimal("0.10")
    assert ladder["tick"].tick_no == 1  # the latest, when none is asked for


def test_a_resting_order_is_marked_where_it_sat(signed_in, instrument, no_broker):
    """At tick 1 the first order had not filled yet (it filled at 10:02), so
    both one-share orders were resting at the bid."""
    a_day(instrument)

    bids = board(signed_in, instrument, tick=1).context["ladder"]["bids"]

    assert bids[0].yours == 2
    assert bids[1].yours == 0


def test_an_earlier_tick_can_be_picked(signed_in, instrument, no_broker):
    a_day(instrument)

    ladder = board(signed_in, instrument, tick=0).context["ladder"]

    assert ladder["tick"].tick_no == 0
    assert ladder["previous"] is None
    assert ladder["next"] == 1
    assert ladder["bids"][0].yours == 1


def test_the_lifespan_chart_draws_every_order_and_fill(signed_in, instrument, no_broker):
    a_day(instrument)

    response = board(signed_in, instrument)
    chart = response.context["chart"]

    assert not chart["empty"]
    assert len(chart["bars"]) == 2
    assert {bar["state"] for bar in chart["bars"]} == {"complete", "open"}
    assert len(chart["fills"]) == 1
    assert chart["mid"].startswith("M")
    assert b"<svg" in response.content


def test_each_tab_renders(signed_in, instrument, no_broker):
    a_day(instrument)

    for tab, expected in (
        ("orders", b"COMMITTED"),
        ("ticks", b"Bid / ask"),
        ("fills", b"paper-1"),
        ("calls", b"No broker call"),
    ):
        response = board(signed_in, instrument, tab=tab)
        assert response.status_code == 200
        assert expected in response.content, tab


def test_an_unknown_tab_falls_back_to_orders(signed_in, instrument, no_broker):
    a_day(instrument)

    assert board(signed_in, instrument, tab="nonsense").context["tab"] == "orders"


def test_broker_calls_are_the_ones_that_touched_this_instrument(signed_in, instrument, no_broker):
    from audit.models import BrokerCall

    a_day(instrument)
    BrokerCall.objects.create(
        trace_id="ts-2026-09-18",
        endpoint="market.quote",
        method="GET",
        request={"instruments": ["NSE:INFY"]},
        response={"NSE:INFY": {}},
        latency_ms=80,
    )
    BrokerCall.objects.create(
        trace_id="ts-2026-09-18",
        endpoint="market.quote",
        method="GET",
        request={"instruments": ["NSE:ITC"]},
        latency_ms=80,
    )
    BrokerCall.objects.create(
        trace_id="ts-2026-09-17",
        endpoint="market.quote",
        method="GET",
        request={"instruments": ["NSE:INFY"]},
        latency_ms=80,
    )

    calls = board(signed_in, instrument, tab="calls").context["calls"]

    assert len(calls) == 1
    assert calls[0]["endpoint"] == "market.quote"


def test_the_cash_panel_projects_carry_from_fills_and_open_orders(signed_in, instrument, no_broker):
    a_day(instrument)

    cash = board(signed_in, instrument).context["cash"]

    assert cash["available"] == Decimal("3700.00")
    assert cash["new_money"] == Decimal("3300.00")
    assert cash["projected"] == Decimal("3700.00") - Decimal("1650.50") * 2 - Decimal("0.42")


def test_without_a_live_price_the_last_mid_is_used_and_labelled(signed_in, instrument, no_broker):
    a_day(instrument)

    response = board(signed_in, instrument)

    assert response.context["last_price"] == Decimal("1650.55")
    assert b"feed unavailable" in response.content


def test_the_orders_page_links_each_instrument_to_its_board(signed_in, instrument, no_broker):
    a_day(instrument)

    response = signed_in.get(reverse("web:orders"), {"date": DAY.isoformat()})

    link = reverse("web:instrument_day", args=[DAY.isoformat(), "INFY"])
    assert link.encode() in response.content


def test_a_later_sheet_that_decided_nothing_does_not_hide_the_buy(signed_in, instrument, no_broker):
    """A sell batch's carrier sheet can outrank the one that decided the buy;
    the board still reads the day's buy from the sheet its orders hang off."""
    from engine.models import TradeSheet, TradeSheetState

    a_day(instrument)
    TradeSheet.objects.create(
        trading_date=DAY, version=2, state=TradeSheetState.FILLED, trace_id="ts-2026-09-18"
    )

    orders_page = signed_in.get(reverse("web:orders"), {"date": DAY.isoformat()}).context
    instrument_page = board(signed_in, instrument).context

    assert orders_page["sheet"].version == 1
    assert instrument_page["is_daily"] is True
    assert instrument_page["target"] == 2


def test_a_resting_sell_is_marked_among_the_asks():
    from web.trading import depth_ladder

    ladder = depth_ladder(BOOK, resting={}, resting_asks={Decimal("1650.60"): 3})

    assert [level.yours for level in ladder["asks"]] == [0, 3]
    assert all(level.yours == 0 for level in ladder["bids"])


def test_a_past_day_is_never_priced_from_todays_market(signed_in, instrument, monkeypatch):
    def refuse(_symbols):
        raise AssertionError("a past day asked for today's price")

    monkeypatch.setattr("web.views._prices_now", refuse)
    a_day(instrument)

    response = board(signed_in, instrument)

    assert response.context["price_source"].startswith("mid at tick")


def test_a_sliced_day_half_done_is_not_complete_on_today(instrument):
    """Seven shares planned, one filled: the target is the plan, not the one
    order placed so far."""
    from web.deck import running

    sheet = a_day(instrument, quantity=7)

    row = next(r for r in running(sheet.trading_date) if r["symbol"] == "INFY")

    assert row["target"] == 7
    assert row["filled"] == 1
    assert row["state"] == "WORKING"
    assert row["value"] == Decimal("1650.50")


def test_a_name_between_ticks_is_working_not_done(instrument):
    """Paper orders fill on their own tick, so a running day often has nothing
    resting for a name it is a third of the way through. That is not done."""
    from orders.models import Order, OrderState
    from web.trading import board as the_board

    sheet = a_day(instrument, quantity=7)
    Order.objects.filter(tradesheet=sheet).update(state=OrderState.COMPLETE)

    row = next(r for r in the_board(day=DAY, prices={})["instruments"] if r["symbol"] == "INFY")

    assert row["state"] == "WORKING"


def test_a_closed_day_short_of_its_target_is_done(instrument):
    from engine.models import TradeSheetState
    from orders.models import Order, OrderState
    from web.trading import board as the_board

    sheet = a_day(instrument, quantity=7)
    Order.objects.filter(tradesheet=sheet).update(state=OrderState.CANCELLED)
    sheet.state = TradeSheetState.PARTIAL
    sheet.save(update_fields=["state"])

    row = next(r for r in the_board(day=DAY, prices={})["instruments"] if r["symbol"] == "INFY")

    assert row["state"] == "DONE"


def test_a_replaced_versions_tick_is_not_shown_as_todays(signed_in, instrument, no_broker):
    """v1 was skipped at night and v2 replaced it: the page shows v2's ticks
    only, so a replaced version's 'day skipped' does not read as today's."""
    from engine.models import TradeSheet, TradeSheetState
    from orders.models import NoPlaceReason, Tick

    sheet = a_day(instrument)
    TradeSheet.objects.filter(pk=sheet.pk).update(version=2)
    old = TradeSheet.objects.create(
        trading_date=DAY, version=1, trace_id="ts-2026-09-18", state=TradeSheetState.SKIPPED
    )
    Tick.objects.create(
        tradesheet=old, trading_date=DAY, tick_no=40, no_place_reason=NoPlaceReason.DAY_SKIPPED
    )

    ticks = board(signed_in, instrument).context["ticks"]

    assert all(t.tradesheet_id == sheet.pk for t in ticks)
    assert NoPlaceReason.DAY_SKIPPED not in {t.no_place_reason for t in ticks}


# -- the trading board's instrument rows -----------------------------------


def test_the_board_row_has_the_avg_fill_and_the_markets_vwap(instrument):
    """Two different numbers: what our fills averaged, and what the whole
    market's volume averaged, as the latest round stored it."""
    from orders.models import Tick
    from web import trading

    a_day(instrument)
    Tick.objects.filter(trading_date=DAY, tick_no=1).update(
        instrument=instrument, vwap=Decimal("1649.10")
    )

    [row] = trading.board(day=DAY, prices={})["instruments"]

    assert row["avg_price"] == Decimal("1650.50")
    assert row["vwap"] == Decimal("1649.10")


def test_the_board_has_no_vwap_before_a_round_stores_one(instrument):
    from web import trading

    a_day(instrument)

    [row] = trading.board(day=DAY, prices={})["instruments"]

    assert row["vwap"] is None


def test_the_orders_page_heads_avg_price_and_vwap_not_placed(signed_in, instrument, no_broker):
    a_day(instrument)

    body = signed_in.get(reverse("web:orders"), {"date": DAY.isoformat()}).content.decode()
    instruments_head = body.split("Instruments with orders")[1].split("</thead>")[0]

    assert "<th>Avg price</th>" in instruments_head
    assert "<th>VWAP</th>" in instruments_head
    assert "Placed" not in instruments_head


def test_the_symbol_board_shows_avg_price_and_the_markets_vwap(signed_in, instrument, no_broker):
    from orders.models import Tick

    a_day(instrument)
    Tick.objects.filter(trading_date=DAY, tick_no=1).update(
        instrument=instrument, vwap=Decimal("1649.10")
    )

    response = board(signed_in, instrument)
    body = response.content.decode()

    assert response.context["avg_price"] == Decimal("1650.50")
    assert response.context["market_vwap"] == Decimal("1649.10")
    assert "AVG PRICE" in body
    assert "MARKET VWAP" in body
    assert "FILL VWAP" not in body
