"""Authorize today's latest tradesheet to trade (M15f).

The same act as the Authorize button, for a terminal: the plan was made
automatically, and this is the decision that lets the ladder place it.
"""

from django.core.management.base import BaseCommand, CommandError

from audit.context import today_ist
from engine.authorization import AuthorizationRefused, authorize
from engine.models import TradeSheet


class Command(BaseCommand):
    help = "Authorize today's latest tradesheet so the ladder may place it."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--actor", required=True, help="Who is authorizing; audited.")

    def handle(self, *args, **options) -> None:
        sheet = TradeSheet.objects.filter(trading_date=today_ist()).order_by("-version").first()
        if sheet is None:
            raise CommandError("Today has no tradesheet to authorize. Generate one first.")
        try:
            sheet = authorize(sheet, actor=options["actor"])
        except AuthorizationRefused as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Authorized {sheet.trading_date} v{sheet.version}: {sheet.names_label}, "
            f"₹{sheet.planned_cost:.2f}. The ladder places it from its next tick."
        )
