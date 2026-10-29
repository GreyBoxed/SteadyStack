"""One placing round of the daily buy.

Runs every thirty seconds under a systemd timer. Claims its round, takes the
shared lock, reads every name's quote in one call and places what the schedule
says is due -- at most what the rate limit has room for, most-behind first.
Everything it declines to do is written down as a tick row before it returns.
"""

from django.core.management.base import BaseCommand, CommandError

from broker import get_broker, session
from broker.exceptions import BrokerError
from configuration.pause import PauseError, require_running
from engine.lock import latest_sheet
from engine.models import TradeSheet
from orders import idempotency, ladder, placement, reconcile
from orders.models import DAILY_BUY_STREAM
from trading_calendar.sessions import is_trading_day


class Command(BaseCommand):
    help = "Fire one ladder tick for today's tradesheet."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", dest="trading_date")
        parser.add_argument("--stream", default=DAILY_BUY_STREAM)

    def handle(self, *args, **options) -> None:
        from audit.context import today_ist

        trading_date = (
            __import__("datetime").date.fromisoformat(options["trading_date"])
            if options["trading_date"]
            else today_ist()
        )

        if not is_trading_day(trading_date):
            self.stdout.write(f"{trading_date} is not a trading day.")
            return

        try:
            require_running("Placing an order")
        except PauseError as paused:
            self.stdout.write(self.style.WARNING(str(paused)))
            return

        sheet = TradeSheet.objects.filter(trading_date=trading_date).order_by("-version").first()
        if sheet is None:
            self.stdout.write("No tradesheet for today; nothing to place.")
            return

        broker = get_broker()
        if not session.attach(broker):
            self.stdout.write(self.style.WARNING("No Kite session; the day stays parked."))
            return

        from django.utils import timezone

        from configuration.store import current as current_configuration

        config = current_configuration()
        now = timezone.localtime()

        # Before the tradesheet time the clock says tick 0, and a tick claimed
        # then -- a run at one in the morning -- used up the day's tick 0 for
        # good, so the real 09:45 tick never fired. Nothing is claimed early.
        if now.date() == trading_date and now.time() < config.tradesheet_time:
            self.stdout.write(
                f"Before {config.tradesheet_time:%H:%M}: the ladder has not opened, "
                "and no tick is used up."
            )
            return

        tick_no = ladder.round_number(now, config)

        try:
            with idempotency.once(
                trading_date=trading_date,
                task=idempotency.LADDER,
                tick_no=tick_no,
                stream=options["stream"],
            ):
                # A live day's orders change at the exchange, and a postback is
                # not guaranteed to say so. The book is read before anything is
                # expired or sized; a book that cannot be read is logged and
                # the round goes on from what it last knew.
                reconcile.refresh_live_orders(
                    trading_date=trading_date, broker=broker, stream=options["stream"]
                )
                # Read again under the lock: a stop or a replan may have landed
                # since the read above, and a round acts on the version that is
                # latest now, not the one it happened to load first.
                sheet = latest_sheet(trading_date)
                outcomes = ladder.run_round(
                    tradesheet=sheet,
                    quotes=self._quotes_for(sheet.buy_lines(), broker),
                    broker=broker,
                    stream=options["stream"],
                    rounds_run=self._rounds_run(trading_date, options["stream"]),
                )
        except idempotency.AlreadyRan as ran:
            self.stdout.write(str(ran))
            return
        except idempotency.Busy as busy:
            self.stdout.write(self.style.WARNING(str(busy)))
            return
        except placement.RateBreakerTripped as breaker:
            # Recorded on the tick and audited already; say it plainly and
            # stop, without a traceback in the way of the message.
            raise CommandError(f"Rate breaker tripped: {breaker}") from breaker
        except BrokerError as failure:
            raise CommandError(f"The tick could not run: {failure}") from failure

        placed = [o for o in outcomes if o.placed]
        for outcome in outcomes:
            who = outcome.symbol or "the day"
            if outcome.placed:
                self.stdout.write(f"round {tick_no}: placed {outcome.tick.slice_qty} {who}")
            elif outcome.tick is not None:
                self.stdout.write(f"round {tick_no}: {who} nothing ({outcome.reason})")
        if not placed and all(o.tick is None for o in outcomes):
            self.stdout.write(f"round {tick_no}: nothing due; every name as before")

    def _rounds_run(self, trading_date, stream) -> set[int]:
        """Today's rounds that ran, this one included; a failed one placed
        nothing and counts as missed, so the schedule re-spreads over it."""
        from orders.models import TaskRun

        return set(
            TaskRun.objects.filter(
                trading_date=trading_date, task=idempotency.LADDER, stream=stream
            )
            .exclude(succeeded=False)
            .values_list("tick_no", flat=True)
        )

    def _quotes_for(self, lines, broker) -> dict:
        """Every line's quote -- depth and last price -- in one call, by symbol."""
        if not lines:
            return {}
        keys = {line.instrument.instrument_key: line.instrument_id for line in lines}
        quotes = broker.quote(list(keys))
        return {symbol: quotes.get(key) or {} for key, symbol in keys.items()}
