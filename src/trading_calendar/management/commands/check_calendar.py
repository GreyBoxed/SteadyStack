"""Warn before the calendar runs out.

A stale calendar does not announce itself: every uncovered weekday reads as a
normal session, which is right for a maintained calendar and wrong for an
abandoned one. Scheduled monthly in Phase 4.
"""

from django.core.management.base import BaseCommand

from trading_calendar.sessions import check_coverage, coverage_until


class Command(BaseCommand):
    help = "Report how far ahead the trading calendar is loaded (M18)."

    def handle(self, *args, **options) -> None:
        remaining = check_coverage()

        if remaining is None:
            self.stderr.write(
                self.style.ERROR(
                    "The trading calendar is empty. Every weekday will be treated as a "
                    "normal session until the exchange's list is loaded."
                )
            )
            return

        self.stdout.write(f"Covered until {coverage_until()} ({remaining.days} days ahead).")
