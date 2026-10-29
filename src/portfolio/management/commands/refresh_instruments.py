"""Fetch the instrument master from Kite and apply it.

Runs daily under a systemd timer in Phase 4, before the tradesheet. It reads,
so it works in every environment (M19c) -- including the laptop this is being
built on.
"""

from django.core.management.base import BaseCommand, CommandError

from broker import get_broker
from broker.exceptions import BrokerError
from portfolio.instruments import DEFAULT_EXCHANGE, refresh


class Command(BaseCommand):
    help = "Refresh the instrument master from Kite (M11b)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
        parser.add_argument(
            "--actor",
            default="system",
            help="Who to record the refresh against; defaults to the scheduler.",
        )

    def handle(self, *args, **options) -> None:
        exchange = options["exchange"]

        try:
            rows = get_broker().instruments(exchange)
        except BrokerError as exc:
            # A stale master is a wrong price, so this failing is worth an
            # alert rather than a silent skip.
            raise CommandError(f"Could not fetch the instrument master: {exc}") from exc

        result = refresh(rows, exchange=exchange, actor=options["actor"])

        self.stdout.write(
            f"{exchange}: {result.created} new, {result.updated} updated, "
            f"{result.deactivated} deactivated, from {result.considered} rows."
        )
        if result.total == 0:
            self.stdout.write(
                self.style.WARNING("No tradable equity found. The master may have changed shape.")
            )
