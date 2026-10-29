"""Record a live order's exchange number by hand (Minor 3, wave H).

For the case Console's tradebook import refuses to guess: two or more of a
live day's orders are open, of the same symbol and side, and neither carries
an exchange number yet. Read Kite's order history or the contract note to
tell them apart, then:

    manage.py record_exchange_order_id --order tx-2026-09-22-3 \\
        --exchange-id 1100000012345678 --actor NAME

named by the tradebook refusal itself and by OPERATIONS.md §7.
"""

from django.core.management.base import BaseCommand, CommandError

from orders.exchange_id import RecordRefused, record_exchange_order_id


class Command(BaseCommand):
    help = "Record Order.exchange_order_id by hand, audited, for a live order that has none."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--order", required=True, dest="tag", help="The order's tag.")
        parser.add_argument(
            "--exchange-id",
            required=True,
            dest="exchange_id",
            help="The exchange's order number, from Kite's order history or the contract note.",
        )
        parser.add_argument("--actor", required=True, help="Who is recording this; audited.")

    def handle(self, *args, **options) -> None:
        try:
            order = record_exchange_order_id(
                options["tag"], options["exchange_id"], actor=options["actor"]
            )
        except RecordRefused as refused:
            raise CommandError(str(refused)) from refused

        self.stdout.write(f"{order.tag}: exchange number {order.exchange_order_id} recorded.")
