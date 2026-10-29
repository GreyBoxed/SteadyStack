"""Hold the tick feed open, day after day.

A long-running process rather than a timer: the websocket is the point, and a
connection re-established every five minutes would miss most of what it is
there for. In Phase 4 this is a systemd unit; locally it is a terminal.

It fills open paper orders against real prices (M19c). It places nothing.

It follows the Kite session rather than holding the one it started with --
the token dies every morning -- and it waits for a login rather than exiting
without one. Every few seconds it writes its pulse to the database, which is
what the health endpoint reads to tell "the feed is down" from "the market is
shut".
"""

import signal
import time

from django.core.management.base import BaseCommand, CommandError

from orders.ticker import Supervisor

#: How often the feed writes its pulse for the health endpoint.
HEARTBEAT_SECONDS = 15
#: How often the feed re-checks which names have open paper orders.
RESYNC_SECONDS = 30
#: How often the feed re-reads which Kite session is current.
SESSION_SECONDS = 60


class Command(BaseCommand):
    help = "Connect to the Kite tick feed and fill open paper orders (M19c)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Connect, report, and exit -- for checking the feed works at all.",
        )

    def handle(self, *args, **options) -> None:
        from django.conf import settings

        feed = Supervisor(api_key=settings.KITE_API_KEY)

        if options["once"]:
            if feed.ensure_connected() == "waiting":
                raise CommandError(f"No usable Kite session: {feed.health.last_error}.")
            time.sleep(5)
            self.stdout.write(str(feed.health.as_dict()))
            feed.stop()
            return

        stopping = {"now": False}

        def stop(_signum, _frame):
            stopping["now"] = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        self.stdout.write(f"Tick feed: {feed.ensure_connected()}.")
        feed.beat()

        second = 0
        while not stopping["now"]:
            time.sleep(1)
            second += 1
            if second % SESSION_SECONDS == 0:
                outcome = feed.ensure_connected()
                if outcome != "unchanged":
                    self.stdout.write(f"Tick feed: {outcome}.")
            if second % RESYNC_SECONDS == 0:
                feed.resync()
            if second % HEARTBEAT_SECONDS == 0:
                feed.beat()

        feed.stop()
        self.stdout.write("Tick feed closed.")
