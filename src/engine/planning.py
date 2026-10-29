"""Plan a day from the broker: holdings, then quotes, then the tradesheet.

One path, used by the scheduled command and by the Regenerate button alike,
so a sheet made by hand is made exactly the way the timer makes one. Nothing
here places an order in any mode.
"""

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from audit.logging import fields
from broker import get_broker
from broker.exceptions import BrokerError
from engine import quotes as quote_reader
from engine.tradesheet import CashAvailable, GenerationRefused, generate

logger = logging.getLogger("steadystack.engine")

#: Where a live plan's cap came from, as the snapshot and the log name it.
KITE_LIVE_BALANCE = "kite_live_balance"
JOURNAL = "journal"


class PlanningFailed(Exception):
    """The broker could not be read, so no version was written."""


class MarketClosed(PlanningFailed):
    """Today's market is not in session, so its book is not one to plan from."""


def market_closed_reason(trading_date: date, now=None) -> str | None:
    """Why today's book is not one to plan from right now, or None if it is.

    Before the open the book is last night's leftovers: a handful of names
    have an unmatched seller and the rest have none, and a plan built on that
    is chosen by accident. After the close nothing it plans can fill. Only
    today is judged -- another date's plan is a replay or a test -- and a day
    the exchange is shut is left to write its own closed-day record.
    """
    from django.utils import timezone

    from trading_calendar.sessions import session_for

    now = now or timezone.localtime()
    if trading_date != now.date():
        return None
    session = session_for(trading_date)
    if not session.is_open or session.covers(now.time()):
        return None
    if session.opens_at and now.time() < session.opens_at:
        return (
            f"the market opens at {session.opens_at:%H:%M}; until then the book is last "
            "night's leftovers"
        )
    return f"the market closed at {session.closes_at:%H:%M}; nothing planned now could fill"


def cash_available(broker, *, on: date) -> CashAvailable:
    """What Zerodha holds to spend today, which a live plan may not exceed (I4).

    Kite's `margins()["equity"]["available"]["live_balance"]`. The field is
    **unverified** against a real response (DEPLOY.md §8, verification 1), so
    it is read defensively: a failed call, a missing key or a value that is
    not a number falls back to the journal's own figure for Zerodha's cash --
    1100 + 1110 + 1200 + 1210, floored at nought -- and the log says which
    source it was.
    """
    from journal.balances import zerodha_cash
    from journal.models import Book

    try:
        raw = broker.margins()["equity"]["available"]["live_balance"]
        amount = Decimal(str(raw))
        if not amount.is_finite():
            raise InvalidOperation(raw)
        return CashAvailable(amount=amount, source=KITE_LIVE_BALANCE)
    except (BrokerError, KeyError, TypeError, ValueError, InvalidOperation) as failure:
        amount = max(Decimal("0"), zerodha_cash(Book.LIVE, through=on))
        logger.warning(
            "could not read zerodha's available cash; the plan is capped by the journal",
            extra=fields(trading_date=str(on), reason=repr(failure), journal=str(amount)),
        )
        return CashAvailable(amount=amount, source=JOURNAL)


def plan_from_broker(
    *,
    trading_date: date | None = None,
    actor: str = "system",
    carry_in: Decimal | None = None,
    skip_holdings: bool = False,
    broker=None,
    allow_closed_market: bool = False,
):
    """Read what is held and what things cost, then plan the day.

    `carry_in` left as None is the engine's balance in the journal, which
    already holds every lumpsum and correction, and planning allocates the
    day's amount. Passing a number is an override for a test or a repair,
    never the normal path.
    """
    from model_portfolio.models import ModelPortfolio
    from portfolio import holdings as holdings_sync

    portfolio = ModelPortfolio.objects.order_by("-version").first()
    if portfolio is None:
        raise GenerationRefused("Upload a model portfolio first.")

    if not allow_closed_market:
        from audit.context import today_ist

        closed = market_closed_reason(trading_date or today_ist())
        if closed:
            raise MarketClosed(f"{closed}.")

    broker = broker or get_broker()

    if not skip_holdings:
        try:
            holdings_sync.sync(broker.holdings(), actor=actor)
        except BrokerError as exc:
            raise PlanningFailed(f"Could not read holdings: {exc}") from exc

    try:
        prices = quote_reader.fetch(sorted(portfolio.weights()), broker=broker)
    except BrokerError as exc:
        raise PlanningFailed(f"Could not read prices: {exc}") from exc

    # A live plan spends only what the account holds (spec §7.4, I4): a sweep
    # the books do not know of yet, or an under-funded allocation, would
    # otherwise size orders the exchange rejects.
    available = None
    if carry_in is None:
        from audit.context import today_ist
        from configuration.store import current

        if current().is_live:
            available = cash_available(broker, on=trading_date or today_ist())

    return generate(
        trading_date=trading_date,
        quotes=prices,
        carry_in=carry_in,
        generated_by=actor,
        cash_available=available,
    )
