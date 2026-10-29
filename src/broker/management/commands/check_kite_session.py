"""Assert there is a usable Kite session, and alert when there is not.

Scheduled 45 minutes before the tradesheet, deliberately: an alert is only
useful if there is time to act on it. At 09:00 a missing session is a login;
at 09:45 it is a missed trading day (M6).

It proves the session rather than trusting the stored expiry -- the token can
be invalidated at the broker's end by a login elsewhere, and a row that says
"valid until tomorrow" is not evidence.

The timer fires every weekday, so the command asks the calendar first. On an
exchange holiday nobody logs in and nothing trades; an alarm about the
session then would be one somebody learns to ignore. It is also the one
scheduled task that asks whether the calendar is running out (M18), which is
the other half of trusting it.
"""

import logging

from django.core.management.base import BaseCommand

from audit.events import record
from audit.logging import fields
from broker import get_broker, session
from broker.exceptions import BrokerError, SessionExpired

logger = logging.getLogger("steadystack.broker")


class Command(BaseCommand):
    help = "Check the Kite session and alert if the day cannot trade (M6)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--offline",
            action="store_true",
            help="Trust the stored expiry instead of proving the session against Kite.",
        )

    def handle(self, *args, **options) -> None:
        from django.utils import timezone

        from trading_calendar.sessions import check_coverage, is_trading_day

        check_coverage()

        today = timezone.localdate()
        if not is_trading_day(today):
            self.stdout.write(f"{today} is not a trading day; no session is needed.")
            return

        state = session.status()

        if not state.valid:
            self._alert(state.reason)
            return

        if options["offline"]:
            self.stdout.write(f"Session valid until {state.expires_at:%H:%M on %d %b}.")
            return

        broker = get_broker()
        session.attach(broker)

        try:
            profile = broker.profile()
        except SessionExpired as exc:
            # The stored row disagreed with the broker. The broker is right.
            session.revoke(actor="system", reason=f"rejected by Kite: {exc}")
            self._alert("Kite rejected the stored session")
            return
        except BrokerError as exc:
            # Could not be proven either way: not an alert about the session,
            # because the session may be perfectly fine.
            self.stdout.write(
                self.style.WARNING(f"Could not reach Kite to check the session: {exc}")
            )
            return

        record(
            "kite_session.checked",
            entity=state.session,
            after={"kite_user_id": profile.get("user_id", ""), "valid": True},
        )
        self.stdout.write(
            f"Session valid for {profile.get('user_id', 'unknown')} "
            f"until {state.expires_at:%H:%M on %d %b}."
        )

    def _alert(self, reason: str) -> None:
        record(
            "kite_session.missing",
            entity_type="broker.KiteSession",
            after={"reason": reason},
        )
        # The line the `no_session` alarm matches on. The stderr text below is
        # for a person at a terminal; the alarm reads the structured log, and
        # without this it had nothing to match and never fired.
        logger.error("no valid Kite session", extra=fields(reason=reason))
        self.stderr.write(
            self.style.ERROR(
                f"No usable Kite session: {reason}. "
                "Log in before the tradesheet time or the day is parked."
            )
        )
