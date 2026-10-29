"""Generating a tradesheet.

One function's worth of decision, assembled from the parts that each answer a
smaller question: what the weights are, what is held, what the market says,
what the day can spend, and whether the portfolio is waiting for something it
cannot yet afford.

Regeneration produces a new version rather than editing the old one (D13). In
Phase 1 nothing is placed, so nothing locks; Phase 2 adds the refusal to
regenerate while an order for that date is live, which is the point of the
versioning in the first place.

A day generated without a Kite session parks in AWAITING_SESSION rather than
failing (M7). The plan is still made -- the ranking, the choice, the
quantity -- because none of it needs a session; what needs one is placement,
and Phase 2's ladder resumes from the rung the clock implies once the operator
logs in.

A day the market does not open produces a skipped sheet naming which kind of
closure it was, rather than no sheet at all: "nothing happened on the 2nd"
should be a record, not an absence somebody has to interpret years later.

Carry comes from the ledger unless the caller states otherwise, so the day is
sized against what the previous days actually left (M14, M17).
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal

from django.db import transaction

from audit.context import bind_trading_day, reset_trace_id, today_ist, trace_id_for
from audit.events import record
from audit.logging import fields
from configuration.pause import active_pause
from configuration.store import current as current_configuration
from engine import reserve
from engine.book import book_for_ranking
from engine.ledger import opening_for
from engine.lock import require_unlocked
from engine.models import SkipReason, TradeSheet, TradeSheetLine, TradeSheetState
from engine.selection import (
    EMPTY_UNIVERSE,
    Quote,
    Ranking,
    Selection,
    rank,
    select,
)
from model_portfolio.models import ModelPortfolio
from portfolio.models import Instrument
from trading_calendar.sessions import session_for

logger = logging.getLogger("steadystack.engine")


class GenerationRefused(Exception):
    """The day could not be planned, and no version was written."""


@dataclass(frozen=True)
class CashAvailable:
    """What Zerodha holds to spend, and where that figure came from (I4)."""

    amount: Decimal
    source: str


@dataclass(frozen=True)
class Inputs:
    """Everything the decision reads, gathered in one place so a tradesheet
    can be generated from stored values as easily as from a live feed."""

    trading_date: date
    portfolio: ModelPortfolio
    holdings: dict[str, int]
    quotes: dict[str, Quote]
    carry_in: Decimal
    inactive: set[str]
    #: Names this plan may not buy, beyond those being sold by a rebalance
    #: batch: a replan leaves out anything sold earlier the same day.
    excluded: frozenset[str] = frozenset()


def gather(
    *,
    trading_date: date,
    quotes: dict[str, Quote],
    carry_in: Decimal = Decimal("0"),
    excluded: frozenset[str] = frozenset(),
) -> Inputs:
    """Read the stored side of the inputs; quotes come from the caller.

    Quotes are passed in rather than fetched here so that generating a
    tradesheet needs no broker, and so a past day can be replayed against the
    prices it actually saw.
    """
    portfolio = ModelPortfolio.objects.order_by("-version").first()
    if portfolio is None:
        raise GenerationRefused("No model portfolio has been uploaded.")

    weights = portfolio.weights()
    holdings = book_for_ranking(weights)
    inactive = set(
        Instrument.objects.filter(tradingsymbol__in=weights, active=False).values_list(
            "tradingsymbol", flat=True
        )
    )

    return Inputs(
        trading_date=trading_date,
        portfolio=portfolio,
        holdings=holdings,
        quotes=quotes,
        carry_in=carry_in,
        inactive=inactive,
        excluded=excluded,
    )


def previous_sheet(trading_date: date) -> TradeSheet | None:
    """The most recent version of the most recent earlier trading day.

    Used only to carry the reserve counter forward. Looking at the latest
    version specifically matters: a regenerated day is the one that counts.
    """
    return (
        TradeSheet.objects.filter(trading_date__lt=trading_date)
        .order_by("-trading_date", "-version")
        .first()
    )


@transaction.atomic
def generate(
    *,
    trading_date: date | None = None,
    quotes: dict[str, Quote],
    carry_in: Decimal | None = None,
    generated_by: str = "system",
    has_session: bool | None = None,
    cash_available: CashAvailable | None = None,
) -> TradeSheet:
    """Plan one trading day and store the result as a new version.

    Everything the plan writes carries the *trading date's* trace id, whoever
    called it and whenever. A sheet generated for Tuesday by a shell at
    00:06 on Wednesday is still Tuesday's record, and the trace id is what
    reassembles that day later.
    """
    trading_date = trading_date or today_ist()
    token = bind_trading_day(trading_date)
    try:
        return _generate(
            trading_date=trading_date,
            quotes=quotes,
            carry_in=carry_in,
            generated_by=generated_by,
            has_session=has_session,
            cash_available=cash_available,
        )
    finally:
        reset_trace_id(token)


def _generate(
    *,
    trading_date: date | None = None,
    quotes: dict[str, Quote],
    carry_in: Decimal | None = None,
    generated_by: str = "system",
    has_session: bool | None = None,
    cash_available: CashAvailable | None = None,
) -> TradeSheet:
    """Plan one trading day and store the result as a new version.

    When `cash_available` is given -- a live plan -- what the day may spend
    is capped at it: the engine spends only what the account holds (I4).

    When `carry_in` is not given, the day's amount is allocated in the journal
    and carry is the engine's balance there. `has_session`
    is read from the stored Kite session, and decides only whether the day is
    ready to run or parked waiting for a login -- never what it would buy.
    """
    trading_date = trading_date or today_ist()
    config = current_configuration()

    # D13: a sheet that has already produced a live order is frozen. The
    # snapshot on it is the explanation for something out in the market.
    require_unlocked(trading_date)

    paused = active_pause(on=trading_date)
    if paused is not None:
        return _write_paused_day(trading_date=trading_date, pause=paused, generated_by=generated_by)

    session = session_for(trading_date)
    if not session.is_open:
        return _write_closed_day(
            trading_date=trading_date, session=session, generated_by=generated_by
        )

    if carry_in is None:
        from journal.cash import allocate_daily

        allocate_daily(trading_date, book=config.order_mode, actor=generated_by)
        opening = opening_for(trading_date, book=config.order_mode)
        carry_in = opening.carry
        spendable_cash = opening.spendable
    else:
        # An explicit carry is floored the same way the journal's is: an
        # overspend is repaid by the next allocation, never spent (D7).
        spendable_cash = max(Decimal("0"), config.daily_amount + carry_in)

    cash_cap = None
    if cash_available is not None:
        held = max(Decimal("0"), cash_available.amount)
        cash_cap = {
            "source": cash_available.source,
            "available": str(held),
            "budget": str(spendable_cash),
            "capped": held < spendable_cash,
        }
        if held < spendable_cash:
            logger.warning(
                "the plan is capped at the cash zerodha holds",
                extra=fields(
                    trading_date=str(trading_date),
                    source=cash_available.source,
                    available=str(held),
                    budget=str(spendable_cash),
                ),
            )
            spendable_cash = held.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    inputs = gather(trading_date=trading_date, quotes=quotes, carry_in=carry_in)
    ranking = _rank(inputs, config=config)
    selection = select(ranking, spendable_cash=spendable_cash)

    earlier = previous_sheet(trading_date)
    reserve_state = reserve.advance(
        ranking=ranking,
        selection=selection,
        previous_symbol=earlier.reserve_symbol if earlier else "",
        previous_days=earlier.reserve_days if earlier else 0,
        threshold=config.reserve_threshold_days,
        spendable_cash=spendable_cash,
    )

    # An engaged reserve sets part of the day aside for the name it waits on
    # and walks the rest of the ranking with what is left (M15d). The targets
    # stay shares of the whole day, since the money set aside is still the
    # portfolio's.
    reserved = reserve.held_back(
        reserve_state,
        ranking=ranking,
        spendable_cash=spendable_cash,
        daily_amount=config.daily_amount,
        threshold=config.reserve_threshold_days,
    )
    if reserved > 0:
        selection = select(
            ranking, spendable_cash=spendable_cash - reserved, reachable_cash=spendable_cash
        )

    if has_session is None:
        from broker import session as kite_session

        has_session = bool(kite_session.status().valid)

    sheet = _write(
        trading_date=trading_date,
        inputs=inputs,
        ranking=ranking,
        selection=selection,
        reserve_state=reserve_state,
        spendable_cash=spendable_cash,
        generated_by=generated_by,
        has_session=has_session,
        reserved=reserved,
        cash_cap=cash_cap,
    )

    reserve.announce(
        reserve_state,
        trading_date=trading_date,
        previously_engaged=bool(earlier and earlier.reserve_engaged),
    )
    return sheet


def _rank(inputs: Inputs, *, config) -> Ranking:
    """Rank the model portfolio against what is held, leaving out what may
    not be bought today."""
    from rebalance.batches import instruments_with_a_live_sell, names_sold_today

    # Never buy what the day is selling or has sold (D-S2): a running batch's
    # names, and any name with a sell resting or filled today -- a cancelled
    # batch that sold still nets against a buy of its name.
    return rank(
        weights=inputs.portfolio.weights(),
        holdings=inputs.holdings,
        quotes=inputs.quotes,
        max_price_per_share=config.max_price_per_share,
        inactive=inputs.inactive,
        being_sold=(
            instruments_with_a_live_sell(inputs.trading_date)
            | names_sold_today(inputs.trading_date)
            | inputs.excluded
        ),
    )


def _write_paused_day(*, trading_date: date, pause, generated_by: str) -> TradeSheet:
    """A paused day plans nothing, and says who paused it and why.

    It still gets a sheet: "the system was paused" is a better answer to
    "what happened on the 14th?" than an absence somebody has to interpret.
    """
    existing = TradeSheet.objects.filter(trading_date=trading_date).order_by("-version").first()
    if existing is not None and existing.skip_reason == SkipReason.PAUSED:
        return existing

    sheet = TradeSheet.objects.create(
        trading_date=trading_date,
        version=(existing.version + 1) if existing else 1,
        generated_by=generated_by,
        trace_id=trace_id_for(trading_date),
        state=TradeSheetState.SKIPPED,
        skip_reason=SkipReason.PAUSED,
        ranking_snapshot={"paused": True, "reason": pause.reason},
    )

    record(
        "tradesheet.generated",
        actor=generated_by,
        entity=sheet,
        after={
            "trading_date": str(trading_date),
            "state": TradeSheetState.SKIPPED,
            "skip_reason": SkipReason.PAUSED,
            "paused_reason": pause.reason,
        },
    )
    logger.info(
        "the system is paused",
        extra=fields(trading_date=str(trading_date), reason=pause.reason),
    )
    return sheet


def _write_closed_day(*, trading_date: date, session, generated_by: str) -> TradeSheet:
    """Record that the market was shut, rather than recording nothing.

    An absent day and a day that bought nothing look identical in a table of
    tradesheets, and only one of them is a problem.
    """
    existing = TradeSheet.objects.filter(trading_date=trading_date).order_by("-version").first()
    if existing is not None and existing.skip_reason in (SkipReason.WEEKEND, SkipReason.HOLIDAY):
        return existing

    reason = SkipReason.WEEKEND if session.note == "weekend" else SkipReason.HOLIDAY
    version = (existing.version + 1) if existing else 1

    sheet = TradeSheet.objects.create(
        trading_date=trading_date,
        version=version,
        generated_by=generated_by,
        trace_id=trace_id_for(trading_date),
        state=TradeSheetState.SKIPPED,
        skip_reason=reason,
        ranking_snapshot={"closed": True, "note": session.note},
    )

    record(
        "tradesheet.generated",
        actor=generated_by,
        entity=sheet,
        after={
            "trading_date": str(trading_date),
            "version": version,
            "state": TradeSheetState.SKIPPED,
            "skip_reason": reason,
        },
    )
    logger.info(
        "market closed",
        extra=fields(trading_date=str(trading_date), reason=reason, note=session.note),
    )
    return sheet


def _write(
    *,
    trading_date: date,
    inputs: Inputs,
    ranking: Ranking,
    selection: Selection,
    reserve_state: reserve.ReserveState,
    spendable_cash: Decimal,
    generated_by: str,
    has_session: bool,
    reserved: Decimal = Decimal("0"),
    replan: dict | None = None,
    cash_cap: dict | None = None,
) -> TradeSheet:
    version = (
        TradeSheet.objects.select_for_update()
        .filter(trading_date=trading_date)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
        or 0
    ) + 1

    state = TradeSheetState.DRAFT
    skip_reason = ""
    instrument_id = None
    quantity = 0
    reference_price = None

    if selection.bought_something:
        # The lead name and the day's total summarise the lines, written below.
        instrument_id = selection.candidate.tradingsymbol
        quantity = selection.quantity
        reference_price = selection.candidate.ask
        if not has_session:
            # The plan stands; only the placing waits. A login before the
            # cutoff resumes it (M7, S6.4).
            state = TradeSheetState.AWAITING_SESSION
    elif reserve_state.engaged:
        # Everything the day had went toward the name it is waiting for.
        state = TradeSheetState.SKIPPED
        skip_reason = SkipReason.RESERVE_ENGAGED
    elif selection.reason == EMPTY_UNIVERSE:
        state = TradeSheetState.SKIPPED
        skip_reason = SkipReason.EMPTY_UNIVERSE
    else:
        state = TradeSheetState.SKIPPED
        skip_reason = SkipReason.NO_AFFORDABLE_INSTRUMENT

    sheet = TradeSheet.objects.create(
        trading_date=trading_date,
        version=version,
        generated_by=generated_by,
        trace_id=trace_id_for(trading_date),
        state=state,
        skip_reason=skip_reason,
        model_portfolio=inputs.portfolio,
        instrument_id=instrument_id,
        carry_in=inputs.carry_in,
        spendable_cash=spendable_cash,
        quantity=quantity,
        reference_price=reference_price,
        reserve_symbol=reserve_state.symbol,
        reserve_days=reserve_state.days,
        reserve_engaged=reserve_state.engaged,
        ranking_snapshot={
            **ranking.as_snapshot(),
            # Why each name above the chosen one was stepped over, so the page
            # can say it rather than guess it from prices years later.
            "passed_over": {
                "unaffordable": list(selection.unaffordable),
                "capped": list(selection.capped),
            },
            "reserved": str(reserved),
            # What a replanned version netted and why (see engine.replan).
            **({"replan": replan} if replan is not None else {}),
            # A live plan's cap at the cash Zerodha holds, and its source (I4).
            **({"cash_cap": cash_cap} if cash_cap is not None else {}),
        },
    )

    for rank_no, pick in enumerate(selection.picks, start=1):
        TradeSheetLine.objects.create(
            tradesheet=sheet,
            instrument_id=pick.candidate.tradingsymbol,
            rank=next(
                (
                    position
                    for position, candidate in enumerate(ranking.candidates, start=1)
                    if candidate.tradingsymbol == pick.candidate.tradingsymbol
                ),
                rank_no,
            ),
            quantity=pick.quantity,
            reference_price=pick.candidate.ask,
            price_source=pick.candidate.price_source,
        )

    record(
        "tradesheet.generated",
        actor=generated_by,
        entity=sheet,
        after={
            "trading_date": str(trading_date),
            "version": version,
            "state": state,
            "skip_reason": skip_reason,
            "symbol": instrument_id,
            "quantity": quantity,
            "lines": {p.candidate.tradingsymbol: p.quantity for p in selection.picks},
            "planned_cost": str(selection.cost),
            "reserved": str(reserved),
            "spendable_cash": str(spendable_cash),
            "reserve_symbol": reserve_state.symbol,
            "reserve_days": reserve_state.days,
        },
    )
    logger.info(
        "tradesheet generated",
        extra=fields(
            trading_date=str(trading_date),
            version=version,
            state=state,
            symbol=instrument_id,
            quantity=quantity,
            passed_over=selection.passed_over,
        ),
    )
    return sheet
