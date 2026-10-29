"""Turning a Kite quote response into the prices the engine reasons with.

Two numbers per name and they are not interchangeable. The last traded price
is what a holding is worth, so it drives valuation, weights and the gap. The
best ask is what the next share would cost, so it decides affordability and
size (M15). Using the last traded price for both is the mistake that produces
an order the budget can never reach.

A name whose book has no sell side -- the market is shut, or nobody is
offering -- is sized at its previous close instead (M15e). An empty sell side
is never read as a price of zero, and the quote says which price it carries,
so the tradesheet can show it.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from audit.logging import fields
from broker.base import Broker
from engine.selection import Quote
from portfolio.models import Instrument

logger = logging.getLogger("steadystack.engine")

#: Kite takes at most this many instruments per quote call.
QUOTE_BATCH = 200


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def best_ask(payload: dict[str, Any]) -> Decimal:
    """The lowest price anyone is currently offering.

    Falls back to the last traded price only when the book is absent from the
    payload entirely -- a quote without depth, which some responses are. An
    *empty* sell side is different: it means nothing is on offer, and that
    must not be mistaken for a price.
    """
    depth = payload.get("depth")
    if depth is None:
        return _decimal(payload.get("last_price"))

    sells = depth.get("sell") or []
    for level in sells:
        price = _decimal(level.get("price"))
        if price > 0:
            return price
    return Decimal("0")


def previous_close(payload: dict[str, Any]) -> Decimal:
    """Yesterday's close, from the quote's OHLC block."""
    return _decimal((payload.get("ohlc") or {}).get("close"))


def from_payload(payload: dict[str, Any]) -> dict[str, Quote]:
    """Build quotes keyed by trading symbol, from a response keyed EXCHANGE:SYMBOL.

    Before the open, or on a name nobody is offering, the book has no ask; the
    previous close sizes it instead, and the quote is marked so (M15e). A name
    with neither an ask nor a close has no price at all and stays unusable.
    """
    quotes: dict[str, Quote] = {}
    for key, body in payload.items():
        symbol = key.split(":", 1)[-1]
        close = previous_close(body)
        last = _decimal(body.get("last_price")) or close
        ask = best_ask(body)
        source = "ask"
        if ask <= 0 and close > 0:
            ask, source = close, "close"
        quotes[symbol] = Quote(tradingsymbol=symbol, last_price=last, ask=ask, source=source)
    return quotes


def fetch(symbols: list[str], *, broker: Broker) -> dict[str, Quote]:
    """Live prices for *symbols*.

    Reads, so this works in every environment including a laptop (M19c). A
    symbol the broker does not answer for is simply absent, and the ranking
    excludes it with a reason rather than guessing at a price.
    """
    instruments = {
        instrument.tradingsymbol: instrument.instrument_key
        for instrument in Instrument.objects.filter(tradingsymbol__in=symbols)
    }

    quotes: dict[str, Quote] = {}
    keys = list(instruments.values())

    for start in range(0, len(keys), QUOTE_BATCH):
        batch = keys[start : start + QUOTE_BATCH]
        quotes.update(from_payload(broker.quote(batch)))

    missing = sorted(set(symbols) - set(quotes))
    if missing:
        logger.warning("no quote for some instruments", extra=fields(symbols=missing))

    return quotes
