"""Value the portfolio and store the day.

Runs after the close under a systemd timer in Phase 4. Reads only: quotes and
our own lots, then one insert. Safe to run twice -- the second run finds the
day already recorded and does nothing.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from audit.context import today_ist
from broker import get_broker
from broker.exceptions import BrokerError
from configuration.store import current as current_configuration
from engine import quotes as quote_reader
from journal.performance import book_for_performance
from performance import valuation
from performance.models import DailyValuation
from trading_calendar.sessions import is_trading_day


class Command(BaseCommand):
    help = "Record what the portfolio was worth today."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", dest="trading_date", help="YYYY-MM-DD; defaults to today.")

    def handle(self, *args, **options) -> None:
        raw = options.get("trading_date")
        day = date.fromisoformat(raw) if raw else today_ist()

        if not is_trading_day(day):
            self.stdout.write(f"{day} is not a trading day; nothing to value.")
            return

        book = book_for_performance()
        if DailyValuation.objects.filter(trading_date=day, book=book).exists():
            # Before any quote is read (m-7): the retry timer runs this four
            # times an evening, and a valued day needs no Kite call to say so.
            self.stdout.write(f"{day} was already valued.")
            return
        held = sorted(valuation.held_quantities(book))
        benchmark = current_configuration().benchmark_symbol
        broker = get_broker()

        try:
            prices = quote_reader.fetch(sorted({*held, benchmark}), broker=broker)
        except BrokerError as exc:
            raise CommandError(f"Could not read prices: {exc}") from exc

        benchmark_quote = prices.get(benchmark)
        row = valuation.snapshot(
            day,
            prices={symbol: quote.last_price for symbol, quote in prices.items()},
            benchmark_level=benchmark_quote.last_price if benchmark_quote else None,
            book=book,
        )

        if row is None:
            self.stdout.write(f"{day} was already valued.")
            return
        self.stdout.write(f"{day}: {row.portfolio_value}")
