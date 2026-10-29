"""Configuration: append-only, defaulted, and gated on the way to live.

The live switch carries the whole weight of M19b, so most of this file is
about the ways it must refuse. A gate that only holds when the network
cooperates is not a gate.
"""

from decimal import Decimal

import pytest
from django.db import DatabaseError

from audit.models import AuditEvent
from configuration.models import ConfigurationVersion, OrderMode
from configuration.store import ConfigurationError, current, update

REGISTERED = "203.0.113.7"


@pytest.fixture
def no_outbound_lookup(monkeypatch):
    """Never touch the network from a test. The default is 'unknown', which
    is also the safest thing for the gate to see."""
    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: None)


@pytest.fixture
def outbound_is_registered(monkeypatch):
    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: REGISTERED)


@pytest.fixture
def outbound_is_somewhere_else(monkeypatch):
    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: "198.51.100.42")


@pytest.mark.django_db
def test_an_unconfigured_system_reads_as_the_defaults():
    config = current()
    assert config.daily_amount == Decimal("1000.00")
    assert config.order_mode == OrderMode.PAPER
    assert config.version == 0


@pytest.mark.django_db
def test_the_default_mode_is_paper():
    """M19b. If this ever defaults to live, a fresh deployment trades."""
    assert current().order_mode == OrderMode.PAPER


@pytest.mark.django_db
def test_a_change_appends_a_version():
    update(changed_by="operator", daily_amount=Decimal("2000.00"))
    update(changed_by="operator", daily_amount=Decimal("2500.00"))

    assert ConfigurationVersion.objects.count() == 2
    assert current().version == 2
    assert current().daily_amount == Decimal("2500.00")


@pytest.mark.django_db
def test_unchanged_fields_carry_forward():
    update(changed_by="operator", daily_amount=Decimal("2000.00"))
    update(changed_by="operator", reserve_threshold_days=7)

    config = current()
    assert config.daily_amount == Decimal("2000.00")
    assert config.reserve_threshold_days == 7


@pytest.mark.django_db
def test_history_survives_a_change():
    """S7: the previous values are how a past day is explained."""
    update(changed_by="operator", daily_amount=Decimal("2000.00"))
    update(changed_by="operator", daily_amount=Decimal("3000.00"))

    amounts = list(
        ConfigurationVersion.objects.order_by("version").values_list("daily_amount", flat=True)
    )
    assert amounts == [Decimal("2000.00"), Decimal("3000.00")]


@pytest.mark.django_db(transaction=True)
def test_a_version_cannot_be_rewritten():
    version = update(changed_by="operator", daily_amount=Decimal("2000.00"))

    with pytest.raises(DatabaseError, match="append-only"):
        ConfigurationVersion.objects.filter(pk=version.pk).update(daily_amount=Decimal("9999.00"))


@pytest.mark.django_db
def test_a_change_is_audited_with_before_and_after():
    update(changed_by="operator", daily_amount=Decimal("2000.00"), reason="raised for a bonus")

    event = AuditEvent.objects.get(action="configuration.changed")
    assert event.actor == "operator"
    assert event.before["daily_amount"] == "1000.00"
    assert event.after["daily_amount"] == "2000.00"


@pytest.mark.django_db
def test_an_unknown_field_is_refused_rather_than_ignored():
    """A typo that silently changes nothing is found by a wrong trade."""
    with pytest.raises(ConfigurationError, match="daily_ammount"):
        update(changed_by="operator", daily_ammount=Decimal("2000.00"))


@pytest.mark.django_db
@pytest.mark.parametrize("band", [Decimal("0"), Decimal("-1.00")])
def test_a_fallback_band_that_would_withhold_every_market_order_is_refused(band):
    """No band is not "no limit". It withholds the close for any move at all,
    and it is sent as the market order's protection, which the exchange will
    not take at zero."""
    with pytest.raises(ConfigurationError, match="fallback_band_pct"):
        update(changed_by="operator", fallback_band_pct=band)


@pytest.mark.django_db
def test_going_live_without_a_registered_address_is_refused(no_outbound_lookup):
    with pytest.raises(ConfigurationError, match="no static IP"):
        update(changed_by="operator", order_mode=OrderMode.LIVE)

    assert current().order_mode == OrderMode.PAPER


@pytest.mark.django_db
def test_going_live_when_the_address_cannot_be_determined_is_refused(no_outbound_lookup):
    """Unknown is not a match. The gate fails closed (D12)."""
    with pytest.raises(ConfigurationError, match="could not be determined"):
        update(
            changed_by="operator",
            order_mode=OrderMode.LIVE,
            registered_static_ip=REGISTERED,
        )


@pytest.mark.django_db
def test_going_live_from_the_wrong_address_is_refused(outbound_is_somewhere_else):
    """This is the case that keeps a laptop from trading."""
    with pytest.raises(ConfigurationError, match="not the registered"):
        update(
            changed_by="operator",
            order_mode=OrderMode.LIVE,
            registered_static_ip=REGISTERED,
        )


@pytest.mark.django_db
def test_a_refused_switch_is_audited(no_outbound_lookup):
    """An attempt to go live leaves a trace whether or not it succeeded."""
    with pytest.raises(ConfigurationError):
        update(changed_by="operator", order_mode=OrderMode.LIVE)

    assert AuditEvent.objects.filter(action="configuration.live_refused").count() == 1


@pytest.mark.django_db
def test_a_refused_switch_writes_no_version(no_outbound_lookup):
    with pytest.raises(ConfigurationError):
        update(changed_by="operator", order_mode=OrderMode.LIVE)

    assert ConfigurationVersion.objects.count() == 0


@pytest.mark.django_db
def test_going_live_from_the_registered_address_is_allowed(live_book_open, outbound_is_registered):
    version = update(
        changed_by="operator",
        order_mode=OrderMode.LIVE,
        registered_static_ip=REGISTERED,
        reason="first live day",
    )

    assert version.is_live is True
    assert current().order_mode == OrderMode.LIVE


@pytest.mark.django_db
def test_recording_the_address_alone_does_not_go_live(no_outbound_lookup):
    """Registering the address is a separate act from switching the mode."""
    update(changed_by="operator", registered_static_ip=REGISTERED)
    assert current().order_mode == OrderMode.PAPER


@pytest.mark.django_db
def test_an_unrelated_change_while_live_is_not_re_gated(
    live_book_open, outbound_is_registered, monkeypatch
):
    """Once live, changing the daily amount must not require the gate again --
    otherwise a network blip locks the operator out of their own settings."""
    update(
        changed_by="operator",
        order_mode=OrderMode.LIVE,
        registered_static_ip=REGISTERED,
    )

    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: None)
    update(changed_by="operator", daily_amount=Decimal("1500.00"))

    assert current().daily_amount == Decimal("1500.00")
    assert current().order_mode == OrderMode.LIVE


@pytest.mark.django_db
def test_returning_to_paper_is_always_allowed(live_book_open, outbound_is_registered, monkeypatch):
    """The safe direction never needs permission."""
    update(
        changed_by="operator",
        order_mode=OrderMode.LIVE,
        registered_static_ip=REGISTERED,
    )

    monkeypatch.setattr("broker.network.outbound_ip", lambda **_: None)
    update(changed_by="operator", order_mode=OrderMode.PAPER, reason="stand down")

    assert current().order_mode == OrderMode.PAPER


# -- the benchmark index (M41a) ------------------------------------------


@pytest.mark.django_db
def test_the_benchmark_defaults_to_the_nifty_fifty():
    assert current().benchmark_symbol == "NIFTY 50"


@pytest.mark.django_db
def test_the_benchmark_can_be_changed_and_the_change_is_audited(no_outbound_lookup):
    """An index the return is judged against is a number-changing setting, so
    it goes through the same audited append as the rest."""
    update(
        changed_by="operator",
        reason="judge against the broader market",
        benchmark_symbol="NIFTY 500",
    )

    assert current().benchmark_symbol == "NIFTY 500"
    event = AuditEvent.objects.filter(action__startswith="configuration").latest("occurred_at")
    assert event.after["benchmark_symbol"] == "NIFTY 500"


# -- what the runtime role is actually allowed to do ---------------------


@pytest.mark.django_db
def test_appending_a_version_never_locks_the_append_only_row(no_outbound_lookup):
    """The tests connect as the schema owner; the application does not.

    UPDATE is revoked on this table for the runtime role -- that is what makes
    append-only a property of the database. Postgres requires UPDATE privilege
    for a row lock, so `SELECT ... FOR UPDATE` here fails in production and
    passes in every test. The lock has to be an advisory one, and this asserts
    it stays that way.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        update(changed_by="operator", reason="a change", daily_amount=Decimal("1500"))

    locking = [
        q["sql"]
        for q in queries
        if "configuration_configurationversion" in q["sql"].lower()
        and ("for update" in q["sql"].lower() or "for share" in q["sql"].lower())
    ]
    assert locking == [], f"row lock on an append-only table: {locking}"


@pytest.mark.django_db
def test_concurrent_appends_cannot_collide_on_a_version_number(no_outbound_lookup):
    """Belt and braces behind the advisory lock: the column is unique, so a
    race that slipped past the lock fails loudly instead of forking history."""
    from django.db import IntegrityError

    update(changed_by="operator", reason="first", daily_amount=Decimal("1100"))
    existing = ConfigurationVersion.objects.order_by("-version").first()

    with pytest.raises(IntegrityError):
        ConfigurationVersion.objects.create(
            version=existing.version,
            changed_by="racer",
            reason="same number",
            daily_amount=Decimal("1200"),
        )


# -- the go-live gate beyond the address (B1) ----------------------------


def _open_live_book():
    from datetime import date

    from journal.external import open_books

    open_books(cash=Decimal("0"), on=date(2026, 1, 1), actor="operator")


def _go_live():
    return update(
        changed_by="operator",
        order_mode=OrderMode.LIVE,
        registered_static_ip=REGISTERED,
        reason="first live day",
    )


@pytest.mark.django_db
def test_switching_to_live_is_refused_until_the_live_book_is_opened(outbound_is_registered):
    """Without an opening entry the live book is a fragment: the balance check
    and the funding warnings stay silent, and every allocation overdraws."""
    with pytest.raises(ConfigurationError, match="open_books"):
        _go_live()

    assert current().order_mode == OrderMode.PAPER
    assert AuditEvent.objects.filter(action="configuration.live_refused").count() == 1


@pytest.mark.django_db
def test_switching_to_live_is_refused_after_today_was_planned_in_paper(outbound_is_registered):
    """A mid-day switch leaves the day's budget in the paper book, and the
    live orders would then spend from a live engine that was never given it."""
    from audit.context import today_ist
    from engine.models import TradeSheet

    _open_live_book()
    TradeSheet.objects.create(trading_date=today_ist(), version=1)

    with pytest.raises(ConfigurationError, match="today"):
        _go_live()

    assert current().order_mode == OrderMode.PAPER


@pytest.mark.django_db
def test_switching_to_live_is_refused_after_a_paper_allocation_today(outbound_is_registered):
    from audit.context import today_ist
    from journal.cash import allocate
    from journal.models import Book

    _open_live_book()
    allocate(
        Decimal("1000"),
        book=Book.PAPER,
        on=today_ist(),
        actor="operator",
        description="daily amount",
        source_type="daily",
    )

    with pytest.raises(ConfigurationError, match="today"):
        _go_live()


@pytest.mark.django_db
def test_switching_to_live_is_refused_while_a_paper_order_is_open(outbound_is_registered):
    from datetime import timedelta

    from audit.context import today_ist
    from engine.models import TradeSheet
    from orders.models import DAILY_BUY_STREAM, Order, OrderMode, OrderSide, OrderType
    from orders.state import OrderState
    from portfolio.models import Instrument

    _open_live_book()
    yesterday = today_ist() - timedelta(days=1)
    Order.objects.create(
        tag="open-0",
        tradesheet=TradeSheet.objects.create(trading_date=yesterday, version=1),
        instrument=Instrument.objects.create(
            tradingsymbol="INFY", instrument_token=408065, name="Infosys"
        ),
        trading_date=yesterday,
        stream=DAILY_BUY_STREAM,
        tick_no=0,
        side=OrderSide.BUY,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=Decimal("100"),
        state=OrderState.OPEN,
    )

    with pytest.raises(ConfigurationError, match="paper order"):
        _go_live()


@pytest.mark.django_db
def test_an_open_paper_order_refuses_the_switch_even_once_today_has_settled(
    outbound_is_registered,
):
    """m-5: the evening switch, after today's settle, still waits for an
    order left open on an earlier day."""
    from datetime import timedelta

    from audit.context import today_ist
    from engine.models import CashLedger, TradeSheet
    from orders.models import DAILY_BUY_STREAM, Order, OrderMode, OrderSide, OrderType
    from orders.state import OrderState
    from portfolio.models import Instrument

    _open_live_book()
    TradeSheet.objects.create(trading_date=today_ist(), version=1, book="paper")
    CashLedger.objects.create(book="paper", trading_date=today_ist())
    earlier = today_ist() - timedelta(days=3)
    Order.objects.create(
        tag="open-earlier",
        tradesheet=TradeSheet.objects.create(trading_date=earlier, version=1),
        instrument=Instrument.objects.create(
            tradingsymbol="INFY", instrument_token=408065, name="Infosys"
        ),
        trading_date=earlier,
        stream=DAILY_BUY_STREAM,
        tick_no=0,
        side=OrderSide.BUY,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=Decimal("100"),
        state=OrderState.OPEN,
    )

    with pytest.raises(ConfigurationError, match="paper order"):
        _go_live()


@pytest.mark.django_db
def test_switching_to_live_is_allowed_once_the_paper_day_has_settled(outbound_is_registered):
    """After the paper day's row is written, the evening switch is the
    procedure DEPLOY.md §8 asks for."""
    from audit.context import today_ist
    from engine.models import CashLedger, TradeSheet

    _open_live_book()
    TradeSheet.objects.create(trading_date=today_ist(), version=1)
    CashLedger.objects.create(book="paper", trading_date=today_ist())

    assert _go_live().is_live is True


@pytest.mark.django_db
def test_a_live_row_for_today_does_not_settle_todays_paper_day(outbound_is_registered):
    """The gate reads the day's own book's row (M1): today was planned in
    paper, so only the paper row says it has settled."""
    from audit.context import today_ist
    from engine.models import CashLedger, TradeSheet

    _open_live_book()
    TradeSheet.objects.create(trading_date=today_ist(), version=1, book="paper")
    CashLedger.objects.create(book="live", trading_date=today_ist())

    with pytest.raises(ConfigurationError, match="already planned"):
        _go_live()
