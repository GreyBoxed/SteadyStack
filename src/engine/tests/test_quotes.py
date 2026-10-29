"""Reading prices out of a Kite quote.

The distinction this module exists to preserve: the last traded price is what
a holding is worth, the best ask is what the next share costs. Collapsing them
is how a day sizes an order the budget can never reach.
"""

from decimal import Decimal

import pytest

from engine.quotes import best_ask, fetch, from_payload
from portfolio.instruments import refresh


def payload(last=1650.25, buy=1650.00, sell=1650.50, depth=True):
    body = {"last_price": last}
    if depth:
        body["depth"] = {
            "buy": [{"price": buy, "quantity": 10}],
            "sell": [{"price": sell, "quantity": 5}] if sell is not None else [],
        }
    return {"NSE:INFY": body}


class StubBroker:
    def __init__(self, response):
        self.response = response
        self.asked = []

    def quote(self, instruments):
        self.asked.append(list(instruments))
        return self.response


def test_the_ask_comes_from_the_sell_side_of_the_book():
    assert best_ask(payload()["NSE:INFY"]) == Decimal("1650.5")


def test_the_last_price_is_kept_separate_from_the_ask():
    quotes = from_payload(payload())
    assert quotes["INFY"].last_price == Decimal("1650.25")
    assert quotes["INFY"].ask == Decimal("1650.5")


def test_the_symbol_is_taken_without_its_exchange_prefix():
    assert "INFY" in from_payload(payload())


def test_a_quote_without_depth_falls_back_to_the_last_price():
    """Some responses carry no book. A price is better than no price."""
    assert best_ask(payload(depth=False)["NSE:INFY"]) == Decimal("1650.25")


def test_an_empty_sell_side_is_not_a_price():
    """Nothing on offer is not the same as free, and must not be treated as
    the last traded price either -- there is nothing to buy."""
    assert best_ask(payload(sell=None)["NSE:INFY"]) == Decimal("0")


def test_a_name_with_no_offer_is_unusable():
    quotes = from_payload(payload(sell=None))
    assert quotes["INFY"].is_usable is False


def test_a_zero_priced_level_is_skipped():
    body = {
        "last_price": 100,
        "depth": {"buy": [], "sell": [{"price": 0}, {"price": 101.5}]},
    }
    assert best_ask(body) == Decimal("101.5")


def test_a_missing_last_price_becomes_zero_rather_than_raising():
    quotes = from_payload({"NSE:INFY": {}})
    assert quotes["INFY"].last_price == Decimal("0")
    assert quotes["INFY"].is_usable is False


def test_prices_are_decimals_not_floats():
    """A float here would be a rounding error in the ledger downstream."""
    quotes = from_payload(payload())
    assert isinstance(quotes["INFY"].last_price, Decimal)


@pytest.mark.django_db
def test_fetch_asks_the_broker_in_its_own_key_format():
    refresh(
        [
            {
                "tradingsymbol": "INFY",
                "instrument_token": 408065,
                "name": "INFY",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "segment": "NSE",
                "tick_size": 0.05,
                "lot_size": 1,
            }
        ]
    )
    broker = StubBroker(payload())

    quotes = fetch(["INFY"], broker=broker)

    assert broker.asked == [["NSE:INFY"]]
    assert quotes["INFY"].ask == Decimal("1650.5")


@pytest.mark.django_db
def test_an_unknown_symbol_is_simply_absent():
    """The ranking excludes it with a reason rather than guessing a price."""
    broker = StubBroker({})
    assert fetch(["NOSUCHCO"], broker=broker) == {}


# -- the previous close stands in when nobody is offering (M15e) ------------


def test_a_name_with_no_offer_is_sized_at_the_previous_close():
    """Before the open the book is last night's leftovers: many names have
    buyers and no sellers. They are sized at yesterday's close rather than
    dropped from the day."""
    body = payload(sell=None)
    body["NSE:INFY"]["ohlc"] = {"close": 1648.0}

    quote = from_payload(body)["INFY"]

    assert quote.ask == Decimal("1648.0")
    assert quote.source == "close"
    assert quote.is_usable is True


def test_a_live_ask_is_preferred_to_the_close():
    body = payload()
    body["NSE:INFY"]["ohlc"] = {"close": 1600.0}

    quote = from_payload(body)["INFY"]

    assert quote.ask == Decimal("1650.5")
    assert quote.source == "ask"


def test_a_name_with_no_last_price_is_valued_at_the_close():
    quote = from_payload({"NSE:INFY": {"ohlc": {"close": 1648.0}, "depth": {"sell": []}}})["INFY"]

    assert quote.last_price == Decimal("1648.0")
    assert quote.is_usable is True
