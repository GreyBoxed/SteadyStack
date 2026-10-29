"""Record a past live day's fills from Zerodha Console's tradebook CSV (B2).

For a live day that did not settle before midnight IST: after that, Kite's
own trade book no longer holds its trades. Download Console → Reports →
Tradebook for the day as CSV, run this, then `settle_day --date <day> --retry`.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from orders import tradebook


class Command(BaseCommand):
    help = (
        "Record a past live day's fills from Zerodha Console's tradebook CSV, then "
        "settle_day --date can write its row. Columns expected: "
        f"{', '.join(sorted(tradebook.COLUMNS))}. No real Console file has been checked "
        "against these yet: run the first one with --dry-run and compare by hand."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("path", help="Console tradebook CSV.")
        parser.add_argument("--actor", required=True, help="Who is importing.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Check every row and report what would be recorded, without writing.",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        try:
            trades = tradebook.parse(path.read_bytes())
            result = tradebook.import_trades(
                trades, actor=options["actor"], dry_run=options["dry_run"]
            )
        except tradebook.TradebookError as refused:
            raise CommandError(f"Nothing was imported. {refused}") from refused

        for matched in result.matched:
            console_id, tag = matched.split(" -> ")
            verb = "Would match" if options["dry_run"] else "Matched"
            self.stdout.write(
                f"{verb} Console order {console_id} to {tag}, the day's only live order of its "
                "symbol and side: no order here carries that id."
            )
        verb = "Would record" if options["dry_run"] else "Recorded"
        self.stdout.write(
            f"{verb} {len(result.recorded)} trade(s) against {len(result.orders)} order(s)"
            f"{': ' + ', '.join(result.recorded) if result.recorded else ''}."
        )
        if result.levelled:
            verb = "Would level" if options["dry_run"] else "Levelled"
            self.stdout.write(
                f"{verb} the day's other open live order(s) from their fills: "
                f"{', '.join(result.levelled)}."
            )
        if result.skipped:
            self.stdout.write(f"Already recorded, skipped: {', '.join(result.skipped)}.")
        if result.recorded and not options["dry_run"]:
            self.stdout.write(
                "Now run settle_day --date <that day> --retry to write its ledger row "
                "(--retry releases the failed settle's claim that usually brings you here)."
            )
