"""Stop trading and cancel everything live.

The emergency handle, deliberately available from a shell as well as from the
browser: the moment somebody most wants this is the moment the web tier might
be the thing that is wrong. Over SSM it is

    manage.py stop_trading --reason "a circuit move in the index"

It exits non-zero when an order could not be confirmed cancelled, so a script
or a person can tell the difference between "stopped" and "stopped, and
something is still out there".
"""

from django.core.management.base import BaseCommand, CommandError

from broker import get_broker, session
from orders import halt


class Command(BaseCommand):
    help = "Pause the system and cancel every open order (the emergency stop)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--reason",
            required=True,
            help="Why. Stored with the pause and the audit event; it cannot be edited after.",
        )
        parser.add_argument(
            "--actor",
            default="operator",
            help="Who pressed it.",
        )

    def handle(self, *args, **options) -> None:
        broker = get_broker()

        # A stop without a session still pauses, and pausing is the half that
        # keeps the next tick from placing. Live orders it could not reach
        # come back in `unconfirmed`, so the missing session reports itself as
        # what it actually cost rather than as a separate warning.
        session.attach(broker)

        result = halt.stop_trading(
            actor=options["actor"],
            reason=options["reason"],
            broker=broker,
        )

        self.stdout.write(self.style.WARNING(result.summary))

        if not result.clean:
            raise CommandError(
                f"{len(result.unconfirmed)} order(s) may still be live at the broker: "
                f"{', '.join(result.unconfirmed)}"
            )

        self.stdout.write("Resuming is a separate, audited decision.")
