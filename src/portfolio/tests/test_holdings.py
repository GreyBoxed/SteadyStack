"""Reconciling holdings with the broker.

The t1 case is the one that matters: stock bought yesterday is owned today but
not yet delivered, and a tradesheet that ignored it would rank a name as
unheld and buy it again.
"""

import pytest

from audit.models import AuditEvent
from portfolio.holdings import sync
from portfolio.instruments import refresh
from portfolio.models import Holding


def master(symbol, token):
    return {
        "tradingsymbol": symbol,
        "instrument_token": token,
        "name": symbol,
        "exchange": "NSE",
        "instrument_type": "EQ",
        "segment": "NSE",
        "tick_size": 0.05,
        "lot_size": 1,
    }


@pytest.fixture
def universe(db):
    refresh([master("INFY", 408065), master("TCS", 2953217)])


@pytest.mark.django_db
def test_holdings_are_recorded(universe):
    sync([{"tradingsymbol": "INFY", "quantity": 7, "average_price": 1500.25}])

    holding = Holding.objects.get(instrument_id="INFY")
    assert holding.quantity == 7


@pytest.mark.django_db
def test_undelivered_stock_counts_as_held(universe):
    """Bought yesterday, not yet settled. Ignoring it buys the name twice."""
    sync([{"tradingsymbol": "INFY", "quantity": 5, "t1_quantity": 2}])

    assert Holding.objects.get(instrument_id="INFY").quantity == 7


@pytest.mark.django_db
def test_the_average_price_is_kept(universe):
    from decimal import Decimal

    sync([{"tradingsymbol": "INFY", "quantity": 1, "average_price": 1500.25}])

    assert Holding.objects.get(instrument_id="INFY").average_cost == Decimal("1500.2500")


@pytest.mark.django_db
def test_a_second_sync_updates_rather_than_duplicates(universe):
    sync([{"tradingsymbol": "INFY", "quantity": 5}])
    sync([{"tradingsymbol": "INFY", "quantity": 8}])

    assert Holding.objects.count() == 1
    assert Holding.objects.get(instrument_id="INFY").quantity == 8


@pytest.mark.django_db
def test_a_name_sold_out_is_zeroed_rather_than_removed(universe):
    """ "We hold none" is a fact with a timestamp; a missing row is not."""
    sync([{"tradingsymbol": "INFY", "quantity": 5}])

    result = sync([])

    assert result.zeroed == 1
    assert Holding.objects.get(instrument_id="INFY").quantity == 0


@pytest.mark.django_db
def test_a_symbol_the_master_does_not_know_is_reported_not_invented(universe):
    """It means the instrument master is stale, and inventing a row hides it."""
    result = sync([{"tradingsymbol": "NOSUCHCO", "quantity": 5}])

    assert result.unknown_symbols == ["NOSUCHCO"]
    assert Holding.objects.count() == 0


@pytest.mark.django_db
def test_a_sync_is_audited(universe):
    sync([{"tradingsymbol": "INFY", "quantity": 5}])

    event = AuditEvent.objects.get(action="holdings.reconciled")
    assert event.after["reconciled"] == 1


@pytest.mark.django_db
def test_an_empty_response_is_not_an_error(universe):
    assert sync([]).reconciled == 0
