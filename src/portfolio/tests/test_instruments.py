"""The instrument master.

Two things matter here and the rest is bookkeeping: the tick size that arrives
is the tick size that gets used (M11b), and an instrument that disappears from
the master is kept rather than deleted, because a lot bought years ago still
points at it.
"""

from decimal import Decimal

import pytest

from audit.models import AuditEvent
from portfolio.instruments import is_tradable_equity, refresh
from portfolio.models import Instrument


def master_row(symbol="INFY", token=408065, tick="0.05", **overrides):
    row = {
        "tradingsymbol": symbol,
        "instrument_token": token,
        "name": "INFOSYS LIMITED",
        "exchange": "NSE",
        "instrument_type": "EQ",
        "segment": "NSE",
        "tick_size": float(tick),
        "lot_size": 1,
    }
    row.update(overrides)
    return row


# -- filtering -------------------------------------------------------------


def test_nse_equity_is_tradable():
    assert is_tradable_equity(master_row(), "NSE") is True


def test_a_derivative_is_not_tradable():
    assert is_tradable_equity(master_row(instrument_type="FUT"), "NSE") is False


def test_an_index_is_not_tradable():
    """An index has a token and a symbol and can never be bought. Left in, it
    would rank in a universe it cannot be filled from. Kite files it as EQ
    under the INDICES segment, which is the shape that has to be refused."""
    assert is_tradable_equity(master_row(symbol="NIFTY 50", segment="INDICES"), "NSE") is False


@pytest.mark.parametrize(
    "symbol",
    [
        "0IRFC35-N0",
        "1003SCL30-BW",
        "815AP26-SG",
        "GS2033-GS",
        "182D250926-TB",
        "SGBAUG28-GB",
        "ABCSME-SM",
        "ABC-ST",
    ],
)
def test_debt_bond_and_sme_series_are_not_tradable(symbol):
    assert is_tradable_equity(master_row(symbol=symbol), "NSE") is False


def test_a_trade_for_trade_share_is_still_equity():
    assert is_tradable_equity(master_row(symbol="SOMECO-BE"), "NSE") is True


def test_a_share_that_trades_in_lots_is_not_tradable():
    assert is_tradable_equity(master_row(lot_size=50), "NSE") is False


@pytest.mark.django_db
def test_an_index_is_kept_for_the_benchmark_but_is_not_equity():
    refresh([master_row(), master_row(symbol="NIFTY 50", token=256265, segment="INDICES")])

    assert Instrument.objects.get(pk="NIFTY 50").segment == "INDICES"


@pytest.mark.django_db
def test_a_refresh_retires_the_debt_rows_an_older_master_let_in():
    # What an earlier, looser filter stored.
    Instrument.objects.create(tradingsymbol="815AP26-SG", instrument_token=9, active=True)

    refresh([master_row(), master_row(symbol="815AP26-SG", token=9)])

    assert Instrument.objects.get(pk="815AP26-SG").active is False


def test_another_exchange_is_not_tradable():
    assert is_tradable_equity(master_row(exchange="BSE"), "NSE") is False


def test_a_row_without_a_token_is_not_tradable():
    assert is_tradable_equity(master_row(instrument_token=None), "NSE") is False


# -- refresh ---------------------------------------------------------------


@pytest.mark.django_db
def test_a_refresh_creates_instruments():
    result = refresh([master_row(), master_row("TCS", 2953217)])

    assert result.created == 2
    assert Instrument.objects.count() == 2


@pytest.mark.django_db
def test_the_tick_size_comes_from_the_master():
    refresh([master_row(tick="0.01")])
    assert Instrument.objects.get(pk="INFY").tick_size == Decimal("0.0100")


@pytest.mark.django_db
def test_a_changed_tick_size_is_applied():
    """A stale tick size is a rejected order the day it changes."""
    refresh([master_row(tick="0.05")])
    refresh([master_row(tick="0.01")])

    assert Instrument.objects.get(pk="INFY").tick_size == Decimal("0.0100")


@pytest.mark.django_db
def test_a_second_refresh_changes_nothing_else():
    """Idempotent: the morning's timer may fire twice."""
    refresh([master_row()])
    result = refresh([master_row()])

    assert result.created == 0
    assert result.updated == 1
    assert Instrument.objects.count() == 1


@pytest.mark.django_db
def test_derivatives_are_filtered_out():
    refresh([master_row(), master_row("INFY26SEPFUT", 999, instrument_type="FUT")])
    assert list(Instrument.objects.values_list("tradingsymbol", flat=True)) == ["INFY"]


@pytest.mark.django_db
def test_a_vanished_instrument_is_deactivated_not_deleted():
    """A lot bought in 2029 still points at its instrument in 2046."""
    refresh([master_row(), master_row("TCS", 2953217)])

    result = refresh([master_row()])

    assert result.deactivated == 1
    assert Instrument.objects.count() == 2
    assert Instrument.objects.get(pk="TCS").active is False


@pytest.mark.django_db
def test_a_returning_instrument_is_reactivated():
    refresh([master_row(), master_row("TCS", 2953217)])
    refresh([master_row()])

    result = refresh([master_row(), master_row("TCS", 2953217)])

    assert result.reactivated == 1
    assert Instrument.objects.get(pk="TCS").active is True


@pytest.mark.django_db
def test_deactivating_twice_does_not_recount():
    refresh([master_row(), master_row("TCS", 2953217)])
    refresh([master_row()])

    result = refresh([master_row()])

    assert result.deactivated == 0


@pytest.mark.django_db
def test_an_empty_master_deactivates_everything_and_says_so():
    """The shape of a bad fetch. It must be visible, not silent."""
    refresh([master_row()])

    result = refresh([])

    assert result.deactivated == 1
    assert result.total == 0


@pytest.mark.django_db
def test_a_refresh_is_audited():
    refresh([master_row()])
    event = AuditEvent.objects.get(action="instruments.refreshed")
    assert event.after["created"] == 1


@pytest.mark.django_db
def test_a_missing_tick_size_falls_back_rather_than_crashing():
    refresh([master_row(tick_size=None)])
    assert Instrument.objects.get(pk="INFY").tick_size == Decimal("0.0500")


@pytest.mark.django_db
def test_the_instrument_key_is_the_form_kite_expects():
    refresh([master_row()])
    assert Instrument.objects.get(pk="INFY").instrument_key == "NSE:INFY"


# -- tick rounding ---------------------------------------------------------


@pytest.mark.django_db
def test_a_price_is_rounded_down_to_the_tick():
    """Down, never up: rounding a buy limit up pays more than was decided."""
    refresh([master_row(tick="0.05")])
    instrument = Instrument.objects.get(pk="INFY")

    assert instrument.round_to_tick(Decimal("1650.53")) == Decimal("1650.50")
    assert instrument.round_to_tick(Decimal("1650.59")) == Decimal("1650.55")


@pytest.mark.django_db
def test_a_price_already_on_a_tick_is_unchanged():
    refresh([master_row(tick="0.05")])
    instrument = Instrument.objects.get(pk="INFY")

    assert instrument.round_to_tick(Decimal("1650.50")) == Decimal("1650.50")


@pytest.mark.django_db
def test_rounding_respects_a_one_paisa_tick():
    refresh([master_row(tick="0.01")])
    instrument = Instrument.objects.get(pk="INFY")

    assert instrument.round_to_tick(Decimal("99.999")) == Decimal("99.99")
