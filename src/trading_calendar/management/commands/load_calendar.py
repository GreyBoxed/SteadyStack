"""Load the exchange's session list for the year ahead.

Run when the exchange publishes its holiday list. Nothing in this system
guesses at a holiday: an invented one costs a trading day, and a missed one
puts orders into a closed market.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from trading_calendar.loader import load
from trading_calendar.sessions import check_coverage, coverage_until


class Command(BaseCommand):
    help = "Load trading sessions from a CSV of date, kind, open_time, close_time, note."

    def add_arguments(self, parser) -> None:
        parser.add_argument("path")
        parser.add_argument("--actor", default="system")

    def handle(self, *args, **options) -> None:
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        result = load(path.read_bytes(), actor=options["actor"])

        for error in result.errors:
            self.stderr.write(self.style.ERROR(error))
        for refusal in result.refused:
            self.stdout.write(self.style.WARNING(refusal))

        if not result.ok:
            raise CommandError(f"{result.loaded} rows loaded before the file was refused.")

        self.stdout.write(f"Loaded {result.loaded} sessions. Covered until {coverage_until()}.")
        check_coverage()
