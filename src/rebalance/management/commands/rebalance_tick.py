"""One round of every running rebalance batch: the ladder, or the close.

Runs each round *before* the buy's (`ladder_tick`, `market_fallback`) and
places at most one order: the buy round's own count of the rate window then
leaves it the rest, so a sale is never starved and the two together never
exceed the limit -- with no knowledge of sales in the buy ladder.

In the placement window it runs the sell ladder; from the fallback time to
the close window's end it runs the close, which sells each name's remainder at
market. Between the two it does nothing, as the buy does nothing. Each round
of each batch is claimed once, under task names of its own, and nothing is
placed once the day's ledger row exists: a sale after it could never be shown.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from audit.context import bind_trading_day, reset_trace_id, today_ist
from broker import get_broker, session
from broker.exceptions import BrokerError
from configuration.pause import PauseError, require_running
from configuration.store import current as current_configuration
from engine.ledger import day_settled
from orders import fallback, idempotency, placement
from orders import ladder as buy_ladder
from rebalance import ladder as sell_ladder
from rebalance.models import BatchState, RebalanceSell
from trading_calendar.sessions import is_trading_day

#: Sell orders a round may place across every running batch. The buy round
#: that follows counts the rate window itself and has the rest.
SELLS_PER_ROUND = 1


class Command(BaseCommand):
    help = "Run one sell round (or close round) for every running rebalance batch."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", dest="trading_date")

    def handle(self, *args, **options) -> None:
        trading_date = (
            date.fromisoformat(options["trading_date"]) if options["trading_date"] else today_ist()
        )
        if not is_trading_day(trading_date):
            return

        try:
            require_running("Placing a rebalance sell")
        except PauseError as paused:
            self.stdout.write(self.style.WARNING(str(paused)))
            return

        running = RebalanceSell.objects.filter(
            state=BatchState.RUNNING, trading_date=trading_date
        ).order_by("started_at")
        if not running.exists():
            return
        if day_settled(trading_date):
            self.stdout.write("The day has settled; nothing more sells today.")
            return

        config = current_configuration()
        if config.is_live:
            # Checked every round, not only at execute: a live sale's fills are
            # not yet recorded, so its money would never reach the ledger.
            self.stdout.write(self.style.WARNING("Rebalance sells run in paper only."))
            return
        now = timezone.localtime()
        closing = config.market_fallback_time <= now.time() < config.close_window_end
        placing = config.tradesheet_time <= now.time() < config.placement_window_close
        if not (closing or placing):
            return

        broker = get_broker()
        if not session.attach(broker):
            self.stdout.write(self.style.WARNING("No Kite session; the sale waits."))
            return

        # One sell order a round across every batch, so the buy round after
        # this one always has the rest of the rate window.
        budget = SELLS_PER_ROUND
        token = bind_trading_day(trading_date)
        try:
            for batch in running:
                budget -= self._one(
                    batch, broker=broker, config=config, now=now, closing=closing, budget=budget
                )
        finally:
            reset_trace_id(token)

    def _one(self, batch: RebalanceSell, *, broker, config, now, closing: bool, budget: int) -> int:
        """One round of one batch; returns how many orders it placed."""
        if closing:
            task, round_no, run = (
                idempotency.REBALANCE_CLOSE,
                fallback.round_number(now, config),
                sell_ladder.close_round,
            )
        else:
            task, round_no, run = (
                idempotency.REBALANCE,
                buy_ladder.round_number(now, config),
                sell_ladder.run_tick,
            )

        try:
            with idempotency.once(
                trading_date=batch.trading_date, task=task, tick_no=round_no, stream=batch.stream
            ):
                # Read again under the lock: a cancel or a stop may have landed.
                batch.refresh_from_db()
                if batch.state != BatchState.RUNNING:
                    return 0
                if day_settled(batch.trading_date):
                    return 0
                room = placement.room(config)
                outcome = run(
                    batch=batch,
                    # The close sells at market and needs no book.
                    quotes={} if closing else self._quotes_for(batch, broker),
                    broker=broker,
                    moment=now,
                    room=max(0, budget if room is None else min(budget, room)),
                )
        except idempotency.AlreadyRan as ran:
            self.stdout.write(str(ran))
            return 0
        except idempotency.Busy as busy:
            self.stdout.write(self.style.WARNING(str(busy)))
            return 0
        except placement.RateBreakerTripped as breaker:
            raise CommandError(f"Rate breaker tripped: {breaker}") from breaker
        except BrokerError as failure:
            # This batch loses its round; the others still get theirs.
            self.stdout.write(
                self.style.WARNING(f"{batch.stream} round {round_no} could not run: {failure}")
            )
            return 0

        for symbol in outcome.placed:
            self.stdout.write(f"{batch.stream} round {round_no}: sold {symbol}")
        for symbol, reason in outcome.skipped.items():
            self.stdout.write(f"{batch.stream} round {round_no}: {symbol} nothing ({reason})")
        return len(outcome.placed)

    def _quotes_for(self, batch: RebalanceSell, broker) -> dict:
        """Every name's quote -- depth and last price -- in one call, by symbol."""
        rows = list(batch.rows.select_related("instrument"))
        if not rows:
            return {}
        keys = {row.instrument.instrument_key: row.instrument_id for row in rows}
        quotes = broker.quote(list(keys))
        return {symbol: quotes.get(key) or {} for key, symbol in keys.items()}
