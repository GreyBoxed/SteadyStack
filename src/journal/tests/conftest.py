from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django_otp.plugins.otp_totp.models import TOTPDevice

DAY = date(2026, 9, 22)
IST = ZoneInfo("Asia/Kolkata")

HEADER = "particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance\n"

FAKE_PASSWORD = "fake-password-for-tests"


@pytest.fixture
def signed_in(client, db):
    """A client whose session is authenticated *and* OTP-verified."""
    user = get_user_model().objects.create_user(username="operator", password=FAKE_PASSWORD)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    client.login(username="operator", password=FAKE_PASSWORD)

    session = client.session
    session["otp_device_id"] = device.persistent_id
    session.save()

    return client


@pytest.fixture
def infy(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


def csv_of(opening, rows, voucher_type="Journal Entry"):
    """rows: (particulars, date, debit, credit); the running balance is computed."""
    balance = Decimal(opening)
    lines = [HEADER, f"Opening Balance,,,,,,{opening}\n"]
    for particulars, day, debit, credit in rows:
        balance = balance - Decimal(debit) + Decimal(credit)
        lines.append(f"{particulars},{day},NSE-EQ - Z,{voucher_type},{debit},{credit},{balance}\n")
    lines.append(f"Closing Balance,,,,,,{balance}\n")
    return "".join(lines).encode()


@pytest.fixture
def live_mode(monkeypatch):
    """The engine in live mode, without the static-IP checks going live needs.

    Only `current().order_mode` is read by the code under test, so the
    configuration object is wrapped rather than a live version written.
    """
    from configuration import store

    real = store.current

    class Live:
        def __init__(self, config):
            self._config = config

        def __getattr__(self, name):
            return getattr(self._config, name)

        order_mode = "live"

    monkeypatch.setattr(store, "current", lambda: Live(real()))


def _order_and_fill(*, side, quantity, price, charges, stream, seq, mode=None, day=DAY):
    from engine.models import TradeSheet
    from orders.models import Fill, Order, OrderMode, OrderState, OrderType
    from portfolio.models import Instrument

    instrument, _ = Instrument.objects.get_or_create(
        tradingsymbol="INFY", defaults={"instrument_token": 408065, "name": "Infosys"}
    )
    sheet, _ = TradeSheet.objects.get_or_create(trading_date=day, version=1)
    order = Order.objects.create(
        tag=f"t{seq}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=day,
        stream=stream,
        tick_no=seq,
        side=side,
        mode=mode or OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        filled_quantity=quantity,
        limit_price=price,
        state=OrderState.COMPLETE,
    )
    return Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-t{seq}",
        quantity=quantity,
        price=price,
        charges=charges or Decimal("0"),
        executed_at=datetime(day.year, day.month, day.day, 10, seq % 60, tzinfo=IST),
    )


@pytest.fixture
def paper_buy_fill(db):
    counter = iter(range(1, 10_000))

    def make(quantity, price, charges=None, stream=None):
        from orders.models import DAILY_BUY_STREAM, OrderSide

        return _order_and_fill(
            side=OrderSide.BUY,
            quantity=quantity,
            price=Decimal(price),
            charges=charges,
            stream=stream or DAILY_BUY_STREAM,
            seq=next(counter),
        )

    return make


@pytest.fixture
def paper_sell_fill(db):
    counter = iter(range(5_000, 10_000))

    def make(quantity, price, stream="rebalance:test"):
        from orders.models import OrderSide

        return _order_and_fill(
            side=OrderSide.SELL,
            quantity=quantity,
            price=Decimal(price),
            charges=None,
            stream=stream,
            seq=next(counter),
        )

    return make


LIVE_DAY = date(2026, 8, 14)


def _live_fill_booked_and_settled(side, quantity, price, stream, seq):
    """A live fill on LIVE_DAY, booked into lots and the journal, then settled."""
    from journal.models import Book
    from journal.trades import settle_trades
    from orders.booking import book
    from orders.models import OrderMode

    fill = _order_and_fill(
        side=side,
        quantity=quantity,
        price=Decimal(price),
        charges=None,
        stream=stream,
        seq=seq,
        mode=OrderMode.LIVE,
        day=LIVE_DAY,
    )
    book(fill)
    settle_trades(LIVE_DAY, book=Book.LIVE)
    return fill


@pytest.fixture
def live_buy_fill(db):
    # Order tags are unique: live counters sit clear of the paper ones.
    counter = iter(range(10_000, 15_000))

    def make(quantity, price, stream=None):
        from orders.models import DAILY_BUY_STREAM, OrderSide

        return _live_fill_booked_and_settled(
            OrderSide.BUY, quantity, price, stream or DAILY_BUY_STREAM, next(counter)
        )

    return make


@pytest.fixture
def live_sell_fill(db):
    counter = iter(range(15_000, 20_000))

    def make(quantity, price, stream="rebalance:test"):
        from orders.models import OrderSide

        return _live_fill_booked_and_settled(OrderSide.SELL, quantity, price, stream, next(counter))

    return make
