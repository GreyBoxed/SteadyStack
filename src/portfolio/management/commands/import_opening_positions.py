"""Seed the lots the account already held.

A one-time act, run from the console with the operator's own records. It is
deliberately not a web page: it happens once, it needs a file the operator has
assembled by hand, and it is the sort of thing that should require sitting
down with the contract notes.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from portfolio import opening


class Command(BaseCommand):
    help = "Import opening positions as immutable lots (M35a)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "path", nargs="?", help="CSV of symbol, quantity, acquired_on, cost_per_unit."
        )
        parser.add_argument("--actor", required=True, help="Who is importing.")
        parser.add_argument(
            "--source",
            required=True,
            help="Where the numbers came from -- a statement, contract notes, a spreadsheet.",
        )
        parser.add_argument("--note", default="")
        parser.add_argument(
            "--partial",
            action="store_true",
            help="Leave the import open because more files are coming.",
        )
        parser.add_argument(
            "--empty",
            action="store_true",
            help=(
                "The account held nothing before this system: complete the import with no "
                "lots. Refused once any opening lot exists."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate the file and report, without writing anything.",
        )

    def handle(self, *args, **options) -> None:
        if options["empty"]:
            self._empty(options)
            return
        if not options["path"]:
            raise CommandError("Name the CSV to import, or pass --empty if there is nothing.")

        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        raw = path.read_bytes()

        if options["dry_run"]:
            result = opening.parse(raw)
            if result.lots:
                opening.resolve(result)
            for error in result.errors:
                self.stdout.write(self.style.ERROR(error))
            if result.ok:
                self.stdout.write(f"{len(result.lots)} lots would be imported. Nothing written.")
            return

        try:
            imported = opening.run(
                raw,
                imported_by=options["actor"],
                source=options["source"],
                note=options["note"],
                complete=not options["partial"],
            )
        except opening.ImportRefused as refused:
            for error in refused.errors:
                self.stdout.write(self.style.ERROR(error))
            raise CommandError("Nothing was imported.") from refused

        self.stdout.write(
            f"Imported {imported.lot_count} lots from {imported.source}."
            + ("" if imported.is_complete else " The import is still open.")
        )
        if not imported.is_complete:
            self.stdout.write(
                self.style.WARNING(
                    "Gains and rebalance sells stay refused until the import is marked complete."
                )
            )

    def _empty(self, options) -> None:
        """No file at all, said on purpose -- never inferred from one without rows."""
        if options["path"] or options["partial"] or options["dry_run"]:
            raise CommandError("--empty takes no file, and is neither partial nor a dry run.")

        try:
            opening.record_empty(
                imported_by=options["actor"], source=options["source"], note=options["note"]
            )
        except opening.ImportRefused as refused:
            for error in refused.errors:
                self.stdout.write(self.style.ERROR(error))
            raise CommandError("Nothing was recorded.") from refused

        self.stdout.write("Recorded that the account held nothing. The import is complete.")
