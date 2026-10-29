"""One round of the close.

The first round cancels everything the ladder left resting, waits for every
confirmation and re-reads the fills. Each round then buys a share of every
name's remainder at market, paced across the close and within the rate limit
-- unless a name's price has left the sanity band, in which case it is
withheld and said so loudly (M22, M22a). Runs every thirty seconds from
`market_fallback_time` to `close_window_end`.
"""

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from audit.context import today_ist
from broker import get_broker, session
from broker.exceptions import BrokerError
from configuration.pause import PauseError, require_running
from configuration.store import current as current_configuration
from engine.lock import latest_sheet
from engine.models import TradeSheet
from orders import fallback, idempotency, placement, reconcile
from orders.models import DAILY_BUY_STREAM
from trading_calendar.sessions import is_trading_day


class Command(BaseCommand):
    help = "Cancel the day's open orders and buy the remainder at market (M22)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", dest="trading_date")
        parser.add_argument("--stream", default=DAILY_BUY_STREAM)

    def handle(self, *args, **options) -> None:
        trading_date = (
            date.fromisoformat(options["trading_date"]) if options["trading_date"] else today_ist()
        )

        if not is_trading_day(trading_date):
            return

        try:
            require_running("The market fallback")
        except PauseError as paused:
            self.stdout.write(self.style.WARNING(str(paused)))
            return

        sheet = TradeSheet.objects.filter(trading_date=trading_date).order_by("-version").first()
        if sheet is None or sheet.instrument_id is None:
            self.stdout.write("Nothing to close out.")
            return

        broker = get_broker()
        if not session.attach(broker):
            raise CommandError("No Kite session: the day's open orders cannot be cancelled.")

        try:
            with idempotency.once(
                trading_date=trading_date,
                task=idempotency.FALLBACK,
                tick_no=fallback.round_number(timezone.localtime(), current_configuration()),
                stream=options["stream"],
            ):
                # The book first, as the ladder does. A live order that filled
                # without a postback cannot be cancelled, and without this the
                # close stops at its first cancel instead of recording the fill.
                reconcile.refresh_live_orders(
                    trading_date=trading_date, broker=broker, stream=options["stream"]
                )
                # Read again under the lock, as the ladder does: the close acts
                # on the version that is latest now.
                sheet = latest_sheet(trading_date)
                outcome = fallback.run(
                    tradesheet=sheet,
                    broker=broker,
                    last_prices=self._last_prices(sheet, broker),
                    stream=options["stream"],
                )
        except idempotency.AlreadyRan as ran:
            self.stdout.write(str(ran))
            return
        except idempotency.Busy as busy:
            self.stdout.write(self.style.WARNING(str(busy)))
            return
        except fallback.CancellationsUnconfirmed as unconfirmed:
            raise CommandError(str(unconfirmed)) from unconfirmed
        except placement.RateBreakerTripped as breaker:
            raise CommandError(f"Rate breaker tripped: {breaker}") from breaker
        except BrokerError as failure:
            raise CommandError(f"The fallback could not run: {failure}") from failure

        if outcome.withheld:
            self.stdout.write(self.style.ERROR(f"Market order withheld: {outcome.withheld_reason}"))
        if outcome.placed:
            self.stdout.write(
                f"Cancelled {outcome.cancelled}; bought {outcome.quantity} at market."
            )
        elif not outcome.withheld:
            self.stdout.write(f"Cancelled {outcome.cancelled}; nothing more to buy this round.")
        if outcome.waiting:
            self.stdout.write(f"{outcome.waiting} name(s) wait for the next round's rate limit.")

    def _last_prices(self, sheet, broker) -> dict[str, Decimal]:
        """Every line's last traded price, in one quote call."""
        keys = {line.instrument.instrument_key: line.instrument_id for line in sheet.buy_lines()}
        quotes = broker.quote(list(keys)) if keys else {}
        return {
            symbol: Decimal(str((quotes.get(key) or {}).get("last_price", 0)))
            for key, symbol in keys.items()
        }
