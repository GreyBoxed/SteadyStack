"""Replan what is left of today, after it has bought and been stopped.

The terminal's way to do what the tradesheet page's "Replan the rest" does.
Stop today first, from the tradesheet page or the phone, then:

    manage.py replan_tradesheet --reason "moved to the v3 model"
    manage.py authorize_tradesheet --actor NAME

The new version waits to be authorised, like any other (M15f).
"""

from django.core.management.base import BaseCommand, CommandError

from audit.context import today_ist
from broker.exceptions import BrokerError
from engine.lock import latest_sheet
from engine.replan import ReplanRefused, replan_from_broker
from orders.idempotency import Busy


class Command(BaseCommand):
    help = "Plan the rest of today, net of what it bought, as a new version."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--reason", required=True, help="Why. Audited with the new version.")
        parser.add_argument("--actor", default="operator", help="Who is replanning.")

    def handle(self, *args, **options) -> None:
        reason = options["reason"].strip()
        if not reason:
            raise CommandError("Say why the rest of the day is being replanned.")

        sheet = latest_sheet(today_ist())
        if sheet is None:
            raise CommandError("There is no tradesheet for today to replan.")

        try:
            new = replan_from_broker(sheet, actor=options["actor"], reason=reason)
        except (ReplanRefused, Busy, BrokerError) as exc:
            raise CommandError(f"Not replanned: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Planned the rest of {new.trading_date} as v{new.version}: "
                f"Rs {new.spendable_cash} to spend, {new.quantity} share(s). "
                "Authorize it before anything is placed."
            )
        )
