"""Post the live book's opening balance before go-live (spec J10).

Run once, as DEPLOY.md §8 step 6. `--on` is the last trading day before
go-live and `--cash` that day's closing funds balance from Console's
statement -- which is also go-live morning's opening balance, so the first
09:05 check agrees by construction (it checks nothing on or before `--on`).
Whatever the live book already records through `--on` is netted, so only
the difference is posted and the book's Zerodha cash equals `--cash`
afterwards. A second call is refused unless the earlier opening entry has
been reversed.
"""

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from journal.external import open_books


class Command(BaseCommand):
    help = (
        "Post the live book's opening balance: --on the last trading day before go-live, "
        "--cash that day's closing funds balance (DEPLOY.md §8)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--cash",
            required=True,
            help=(
                "Zerodha's closing funds balance on --on, as Console's statement shows it. "
                "The cash the live book already records through --on is netted; only the "
                "difference posts."
            ),
        )
        parser.add_argument(
            "--on", required=True, help="YYYY-MM-DD: the last trading day before go-live."
        )
        parser.add_argument("--actor", required=True)

    def handle(self, *args, **options):
        try:
            entry = open_books(
                cash=Decimal(options["cash"]),
                on=date.fromisoformat(options["on"]),
                actor=options["actor"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Opened the live book: entry #{entry.number}."))
