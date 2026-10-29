"""Plan today's buy.

Runs at the tradesheet time under a systemd timer in Phase 4. Everything it
does is a read plus a write to our own tables: nothing is placed here, in any
mode, which is why it is safe to run at any time while the system is being
built.
"""

import logging
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from audit.logging import fields
from engine.tradesheet import GenerationRefused

logger = logging.getLogger("steadystack.engine")


class Command(BaseCommand):
    help = "Generate the day's tradesheet from live prices and holdings."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--date",
            dest="trading_date",
            help="Trading date as YYYY-MM-DD. Defaults to today in IST.",
        )
        parser.add_argument(
            "--carry-in",
            default=None,
            help=(
                "Override the carry from prior days. Left out, it is read from the cash "
                "ledger with any lumpsum and adjustment -- which is what a real day wants."
            ),
        )
        parser.add_argument("--actor", default="system")
        parser.add_argument(
            "--allow-closed-market",
            action="store_true",
            help="Plan today even outside market hours, from whatever the book shows. For tests.",
        )
        parser.add_argument(
            "--skip-holdings",
            action="store_true",
            help="Rank against the stored holdings instead of re-reading the broker.",
        )

    def handle(self, *args, **options) -> None:
        from engine.lock import TradesheetLocked
        from engine.planning import PlanningFailed, plan_from_broker

        trading_date = (
            date.fromisoformat(options["trading_date"]) if options["trading_date"] else None
        )

        try:
            sheet = plan_from_broker(
                trading_date=trading_date,
                actor=options["actor"],
                # None means "read it from the ledger". A default of zero here
                # once planned every day without its carry or its lumpsums.
                carry_in=Decimal(options["carry_in"]) if options["carry_in"] is not None else None,
                skip_holdings=options["skip_holdings"],
                allow_closed_market=options["allow_closed_market"],
            )
        except (GenerationRefused, PlanningFailed, TradesheetLocked) as exc:
            # The line the `task_failed` alarm matches. A CommandError reaches
            # stderr and nothing else, and a day that never planned is a day
            # nothing buys -- worth an email, not a line in a file.
            logger.error(
                "tradesheet generation failed",
                extra=fields(
                    trading_date=options["trading_date"] or "today",
                    reason=exc.__class__.__name__,
                    error=str(exc),
                ),
            )
            raise CommandError(str(exc)) from exc

        if sheet.is_skipped:
            self.stdout.write(
                self.style.WARNING(
                    f"{sheet.trading_date} v{sheet.version}: nothing bought "
                    f"({sheet.get_skip_reason_display()})."
                )
            )
            return

        self.stdout.write(
            f"{sheet.trading_date} v{sheet.version}: {sheet.quantity} x {sheet.instrument_id} "
            f"at up to {sheet.reference_price} "
            f"({sheet.planned_cost} of {sheet.spendable_cash})."
        )
