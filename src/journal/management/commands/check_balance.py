"""Check Zerodha's funds balance against the books each morning (spec §7.1).

Scheduled five minutes after the session check, deliberately: a missing
session is that check's alarm to raise, not this one's. Without a session
there is nothing to check the balance against, so this prints why and exits
quietly rather than raising a second alarm for the same cause.
"""

import logging

from django.core.management.base import BaseCommand

from audit.context import today_ist
from audit.logging import fields
from broker import get_broker, session
from broker.exceptions import BrokerError
from journal.checks import check_balance

logger = logging.getLogger("steadystack.journal")


class Command(BaseCommand):
    help = "Check Zerodha's funds balance against the books, and alert if it disagrees."

    def handle(self, *args, **options) -> None:
        state = session.status()
        if not state.valid:
            self.stdout.write(
                f"No usable Kite session ({state.reason}); the balance was not checked. "
                "The session check already alarms on that."
            )
            return

        broker = get_broker()
        session.attach(broker)

        try:
            row = check_balance(broker=broker, on=today_ist())
        except BrokerError as exc:
            # With a valid session, a failed read is this check failing: an
            # ERROR on steadystack.* is what the task_failed alarm reads (M-4).
            logger.error(
                "could not read zerodha's balance to check it",
                extra=fields(checked_on=str(today_ist()), error=str(exc)),
            )
            self.stdout.write(
                self.style.WARNING(f"Could not reach Kite to check the balance: {exc}")
            )
            return

        if row is None:
            self.stdout.write(
                "Balance not checked: live book not opened yet (open_books runs at go-live)."
            )
            return

        self.stdout.write(
            f"{row.checked_on}: ours ₹{row.ours:.2f}, theirs ₹{row.theirs:.2f}, "
            f"difference ₹{row.difference:.2f}."
        )
