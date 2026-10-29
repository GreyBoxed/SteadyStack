"""A whole trading day, end to end, through the real code paths.

Not a unit test: nothing here is stubbed except the broker itself, and the
broker is only asked to accept orders it never sends -- which is what paper
mode is. The tradesheet, the ladder, the fill, the lot, the ledger and the
reconciliation checks all run as they will on a live day.

These are the plan's integration scenarios. Each one is a day that has gone
differently, and the thing being asserted is always the same: the day's record
explains itself, and the money adds up.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
import time_machine
from django.utils import timezone

from configuration.store import current as current_configuration
from configuration.store import update as configure
from engine import settlement
from engine.ledger import opening_for, reconcile
from engine.models import CashLedger, TradeSheetState
from engine.selection import Quote
from engine.tradesheet import generate
from gains import fifo
from model_portfolio.uploads import store
from orders import fallback, ladder, paperfill
from orders.models import Fill, Order, Tick
from portfolio.instruments import refresh
from portfolio.models import Holding, Instrument, Lot
from records import divergence

pytestmark = pytest.mark.django_db

DAY = date(2026, 9, 22)
NEXT = date(2026, 9, 23)


def authorized(sheet):
    """These days test trading, not the gate: somebody authorized them (M15f)."""
    from django.utils import timezone

    # Before the day's window opens, on the day's own date: the schedule starts
    # from the later of the two, and a real clock is on another day entirely.
    sheet.authorized_at = timezone.make_aware(
        datetime.combine(sheet.trading_date, time(9, 0)), timezone.get_current_timezone()
    )
    sheet.authorized_by = "operator"
    sheet.save(update_fields=["authorized_at", "authorized_by"])


def at(hour, minute):
    return timezone.make_aware(
        datetime.combine(DAY, time(hour, minute)), timezone.get_current_timezone()
    )


def master(symbol, token, tick="0.05"):
    return {
        "tradingsymbol": symbol,
        "instrument_token": token,
        "name": symbol,
        "exchange": "NSE",
        "instrument_type": "EQ",
        "segment": "NSE",
        "tick_size": float(tick),
        "lot_size": 1,
    }


def book(bid="1000.00", ask="1000.50"):
    return {
        "buy": [{"price": float(bid), "quantity": 500}],
        "sell": [{"price": float(ask), "quantity": 500}],
    }


class PaperBroker:
    """Accepts everything and sends nothing, which is what paper mode is."""

    def place_order(self, **kwargs):
        return "kite-never-sent"

    def cancel_order(self, order_id):
        return order_id

    def orders(self):
        return []

    def trades(self):
        return []


@pytest.fixture
def portfolio(db):
    refresh([master("AAA", 91), master("BBB", 92)])
    store("symbol,target_pct\nAAA,50\nBBB,50\n", uploaded_by="operator")
    # Five-minute rounds: the same schedule in fifty-one steps rather than five
    # hundred, so a whole day runs in seconds.
    configure(changed_by="operator", daily_amount=Decimal("5000"), placer_interval_seconds=300)


QUOTES = {
    "AAA": Quote("AAA", Decimal("1000"), Decimal("1000.50")),
    "BBB": Quote("BBB", Decimal("1200"), Decimal("1200.50")),
}


def fill_every_name(sheet, price=Decimal("999.00")):
    """A trade below every resting buy, in every name the day bought."""
    for line in sheet.buy_lines():
        paperfill.apply_tick(instrument_id=line.instrument_id, traded_price=price, at=at(14, 30))


def quotes_for(sheet, depth=None):
    """Every name on the sheet, quoted off the same book."""
    depth = depth if depth is not None else book()
    last = depth["buy"][0]["price"] if depth["buy"] else 0
    return {line.instrument_id: {"depth": depth, "last_price": last} for line in sheet.buy_lines()}


def one_round(sheet, broker, moment, *, depth=None):
    with time_machine.travel(moment, tick=False):
        return ladder.run_round(
            tradesheet=sheet, quotes=quotes_for(sheet, depth), broker=broker, moment=moment
        )


def run_the_ladder(sheet, broker, *, depth=None, until=None):
    """Fire every round of the placement window, as the timer would, with the
    clock moving too -- an order's age and the rate window are wall-clock."""
    config = current_configuration()
    moment = at(9, 45)
    close = at(config.placement_window_close.hour, config.placement_window_close.minute)
    placed = 0

    while moment < close:
        if until and moment.time() >= until:
            break
        placed += sum(o.placed for o in one_round(sheet, broker, moment, depth=depth))
        moment += timedelta(seconds=config.placer_interval_seconds)

    return placed


# -- the ordinary day ------------------------------------------------------


def test_a_day_that_fills_completely(portfolio):
    """The happy path, whole: plan, place, fill, settle, reconcile."""
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(sheet)

    assert sheet.quantity > 0
    run_the_ladder(sheet, broker)

    fill_every_name(sheet)

    result = settlement.settle(trading_date=DAY, broker=broker)

    assert result.tradesheet.state == TradeSheetState.FILLED
    assert result.ledger.reconciles is True
    assert Lot.objects.count() == Fill.objects.count()


def test_the_days_record_has_no_gaps(portfolio):
    """Every tick wrote a row, and the ones that placed nothing say why."""
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(sheet)
    run_the_ladder(sheet, broker)

    ticks = Tick.objects.all()
    names = {line.instrument_id for line in sheet.buy_lines()}
    assert set(ticks.filter(tick_no=0).values_list("instrument_id", flat=True)) == names
    assert ticks.filter(order__isnull=False).count() == Order.objects.count()
    assert not ticks.filter(order__isnull=True, no_place_reason="").exists()


def test_holdings_lots_and_the_ledger_all_agree_afterwards(portfolio):
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(sheet)
    run_the_ladder(sheet, broker)
    fill_every_name(sheet)
    settlement.settle(trading_date=DAY, broker=broker)

    symbol = sheet.instrument_id
    assert Holding.objects.get(instrument_id=symbol).quantity == fifo.available(symbol)
    assert divergence.scan(detected_on=DAY).opened == []
    assert fifo.check_projection() == []


def test_what_is_not_spent_carries_to_the_next_day(portfolio):
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(sheet)
    run_the_ladder(sheet, broker)
    fill_every_name(sheet)
    result = settlement.settle(trading_date=DAY, broker=broker)

    assert opening_for(NEXT).carry == result.ledger.closing_carry
    assert result.ledger.closing_carry > Decimal("0")


# -- days that go differently ---------------------------------------------


def test_a_day_that_never_fills_carries_everything(portfolio):
    """M17: an unfilled day carries the full amount."""
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(sheet)
    run_the_ladder(sheet, broker)

    result = settlement.settle(trading_date=DAY, broker=broker)

    assert result.tradesheet.state == TradeSheetState.UNFILLED
    assert result.ledger.closing_carry == Decimal("5000.00")
    assert result.ledger.reconciles is True


def test_a_day_that_fills_in_part_is_partial(portfolio):
    """One slice placed and filled, out of a day that wanted more."""
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(sheet)
    assert sheet.quantity > 1  # otherwise there is no partial to have

    one_round(sheet, broker, at(9, 45))
    paperfill.apply_tick(
        instrument_id=sheet.instrument_id, traded_price=Decimal("999.00"), at=at(11, 30)
    )
    result = settlement.settle(trading_date=DAY, broker=broker)

    assert result.filled_quantity < sheet.quantity
    assert result.tradesheet.state == TradeSheetState.PARTIAL
    assert result.ledger.reconciles is True


def test_a_locked_book_all_day_places_nothing_and_explains_itself(portfolio):
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(sheet)

    placed = run_the_ladder(sheet, broker, depth=book("1000.00", "1000.00"))

    assert placed == 0
    assert Order.objects.count() == 0
    assert Tick.objects.exclude(no_place_reason="").count() == Tick.objects.count()


def test_the_fallback_closes_a_day_the_ladder_could_not(portfolio):
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(sheet)
    run_the_ladder(sheet, broker)

    outcome = fallback.run(
        tradesheet=sheet, broker=broker, last_price=sheet.reference_price, moment=at(15, 25)
    )

    assert outcome.placed is True
    assert Order.objects.filter(is_fallback=True).count() == 1


def test_the_fallback_is_withheld_when_the_market_has_run_away(portfolio):
    """An unfilled day is cheaper than a day's cash spent into a circuit move."""
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(sheet)
    run_the_ladder(sheet, broker)

    outcome = fallback.run(
        tradesheet=sheet,
        broker=broker,
        last_price=sheet.reference_price * Decimal("1.5"),
        moment=at(15, 25),
    )

    assert outcome.withheld is True
    assert Order.objects.filter(is_fallback=True).count() == 0

    result = settlement.settle(trading_date=DAY, broker=broker)
    assert result.ledger.closing_carry == Decimal("5000.00")


def test_a_day_with_no_session_parks_and_places_nothing(portfolio):
    broker = PaperBroker()
    sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=False)

    placed = run_the_ladder(sheet, broker)

    assert sheet.state == TradeSheetState.AWAITING_SESSION
    assert placed == 0


# -- several days in a row -------------------------------------------------


def test_three_days_chain_and_reconcile(portfolio):
    """The property the whole system is judged by, over a run of days."""
    broker = PaperBroker()
    days = [date(2026, 9, 21), DAY, NEXT]

    for index, day in enumerate(days):
        sheet = generate(trading_date=day, quotes=QUOTES, has_session=True)
        authorized(sheet)
        if sheet.instrument_id:
            one_round(
                sheet,
                broker,
                timezone.make_aware(
                    datetime.combine(day, time(9, 45)), timezone.get_current_timezone()
                ),
            )
            if index % 2 == 0:  # fills on some days and not others
                paperfill.apply_tick(
                    instrument_id=sheet.instrument_id,
                    traded_price=Decimal("999.00"),
                    at=timezone.make_aware(
                        datetime.combine(day, time(11, 0)), timezone.get_current_timezone()
                    ),
                )
        settlement.settle(trading_date=day, broker=broker)

    rows = list(CashLedger.objects.order_by("trading_date"))
    assert len(rows) == 3
    assert all(row.reconciles for row in rows)

    for previous, current in pairwise(rows):
        assert current.opening_carry == previous.closing_carry

    assert reconcile()["ok"]
    assert fifo.check_projection() == []


def test_the_portfolio_converges_toward_its_targets(portfolio):
    """Paper mode has to converge, or it proves nothing about a live one:
    the engine must see what it bought yesterday."""
    broker = PaperBroker()
    bought = []

    for day in [date(2026, 9, 21), DAY, NEXT, date(2026, 9, 24), date(2026, 9, 25)]:
        sheet = generate(trading_date=day, quotes=QUOTES, has_session=True)
        authorized(sheet)
        if not sheet.instrument_id:
            continue

        symbol = sheet.instrument_id
        bought.append(symbol)

        # Each name is priced off its own book, as it would be in reality.
        quote = QUOTES[symbol]
        one_round(
            sheet,
            broker,
            timezone.make_aware(
                datetime.combine(day, time(9, 45)), timezone.get_current_timezone()
            ),
            depth=book(str(quote.last_price), str(quote.ask)),
        )
        paperfill.apply_tick(
            instrument_id=symbol,
            traded_price=quote.last_price - Decimal("1"),
            at=timezone.make_aware(
                datetime.combine(day, time(11, 0)), timezone.get_current_timezone()
            ),
        )
        settlement.settle(trading_date=day, broker=broker)

    # Both names get bought rather than the first one for ever, which is what
    # "the ranking means what it says" comes down to.
    assert set(bought) == {"AAA", "BBB"}
    assert Holding.objects.filter(quantity__gt=0).count() == 2


# -- a day stopped and replanned -------------------------------------------


def test_a_day_stopped_and_replanned_buys_nothing_twice_and_settles_once(
    portfolio, monkeypatch, settings, tmp_path
):
    """The morning buys part of the day and is stopped; the model portfolio
    changes; the rest of the day is replanned, authorised and run. The day
    must spend no more than it had, and settle into one row that adds up."""
    from engine.models import TradeSheet
    from engine.replan import replan
    from orders import overrides

    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
    monkeypatch.setattr("engine.replan.quote_reader.fetch", lambda symbols, broker: QUOTES)
    monkeypatch.setattr("engine.replan.session_is_valid", lambda: True)
    broker = PaperBroker()

    morning = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
    authorized(morning)
    run_the_ladder(morning, broker, until=time(11, 0))
    fill_every_name(morning)
    assert Fill.objects.exists()

    with time_machine.travel(at(11, 5), tick=False):
        overrides.stop_day(morning, actor="operator", reason="new model", broker=broker)
        store("symbol,target_pct\nAAA,80\nBBB,20\n", uploaded_by="operator")
        rest = replan(morning, actor="operator", reason="new model", broker=broker)
    rest.authorized_at, rest.authorized_by = at(11, 10), "operator"
    rest.save(update_fields=["authorized_at", "authorized_by"])

    moment = at(11, 15)
    while moment < at(14, 0):
        one_round(rest, broker, moment)
        moment += timedelta(minutes=5)
    fill_every_name(rest)

    result = settlement.settle(trading_date=DAY, broker=broker)

    # What the day had before it spent: the journal has netted the fills out
    # of the engine balance by now, so the row's own `available` is asked.
    opening = result.ledger.available
    assert result.ledger.closing_carry == opening_for(DAY).spendable
    assert CashLedger.objects.filter(trading_date=DAY).count() == 1
    assert result.ledger.reconciles is True
    assert result.ledger.cash_spent + result.ledger.charges <= opening
    assert result.ledger.cash_spent == sum(f.quantity * f.price for f in Fill.objects.all())
    assert Tick.objects.filter(tradesheet=rest).exists()
    assert TradeSheet.objects.get(pk=morning.pk).state == TradeSheetState.HALTED


# -- a day that buys and sells ---------------------------------------------


def test_a_day_with_a_rebalance_sale_sells_once_and_settles_its_money(
    portfolio, monkeypatch, settings, tmp_path
):
    """CCC is held from an earlier day and is not in the model portfolio. A
    batch sells it through the day while the buy runs, the close sells what is
    left, and the settle books the sale's money on the day's one row."""
    from django.core.management import call_command

    from engine.settlement import day_proceeds
    from gains.models import RealisedGain
    from portfolio.models import BasisState, OpeningImport
    from rebalance import batches
    from rebalance import ladder as sell_ladder
    from rebalance.models import BatchState
    from rebalance.tests.conftest import paper_lot

    settings.ORDER_LOCK_PATH = str(tmp_path / "orders.lock")
    refresh([master("AAA", 91), master("BBB", 92), master("CCC", 93)])
    OpeningImport.objects.create(
        imported_by="operator", source="notes", completed_at=timezone.now()
    )
    # Bought by an earlier paper day: a paper sale consumes only paper lots (I6).
    paper_lot(
        Instrument.objects.get(pk="CCC"),
        acquired_on=date(2026, 9, 1),
        quantity=5,
        cost=Decimal("900"),
        basis_state=BasisState.FINAL,
    )
    broker = PaperBroker()
    sale_quote = {"CCC": {"depth": book("1000.00", "1000.50"), "last_price": 1000.25}}

    with time_machine.travel(at(9, 44), tick=False):
        sheet = generate(trading_date=DAY, quotes=QUOTES, has_session=True)
        authorized(sheet)
        batch = batches.upload(
            b"symbol,quantity\nCCC,5\n", uploaded_by="operator", trading_date=DAY
        )
        batch = batches.start(batch, token=batch.execute_token, actor="operator", trading_date=DAY)

    moment = at(9, 45)
    while moment < at(14, 0):
        with time_machine.travel(moment, tick=False):
            batch.refresh_from_db()
            sell_ladder.run_tick(
                batch=batch, quotes=sale_quote, broker=broker, moment=moment, room=1
            )
            paperfill.apply_tick(instrument_id="CCC", traded_price=Decimal("1001.00"), at=moment)
        one_round(sheet, broker, moment)
        moment += timedelta(minutes=30)
    fill_every_name(sheet)

    with time_machine.travel(at(15, 0), tick=False):
        batch.refresh_from_db()
        sell_ladder.close_round(
            batch=batch, quotes=sale_quote, broker=broker, moment=at(15, 0), room=1
        )
        paperfill.apply_tick(instrument_id="CCC", traded_price=Decimal("999.00"), at=at(15, 1))

    monkeypatch.setattr("orders.management.commands.settle_day.get_broker", lambda: broker)
    monkeypatch.setattr("orders.management.commands.settle_day.session.attach", lambda b: False)
    with time_machine.travel(at(15, 45), tick=False):
        call_command("settle_day", "--date", DAY.isoformat())

    batch.refresh_from_db()
    row = CashLedger.objects.get(trading_date=DAY)
    sold = Order.objects.filter(stream=batch.stream).exclude(state="CANCELLED")
    assert batch.state == BatchState.COMPLETE
    assert sum(sold.values_list("filled_quantity", flat=True)) == 5
    assert Lot.objects.get(instrument_id="CCC").remaining_quantity == 0
    assert RealisedGain.objects.filter(lot__instrument_id="CCC").exists()
    assert row.proceeds == day_proceeds(DAY) > 0
    assert row.reconciles
    assert row.extra_allocation == 0
    assert opening_for(NEXT).carry == row.closing_carry
    assert not Order.objects.filter(stream=batch.stream, instrument_id__in=["AAA", "BBB"]).exists()
