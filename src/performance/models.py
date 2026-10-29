"""What the portfolio was worth, evening by evening.

One row per trading day and book, written once and never updated. The paper
book before go-live and the live book after are two portfolios, so each keeps
its own series (M13), read by `journal.performance.book_for_performance`. The system's other
tables answer "what did it cost" and "what does it hold"; this one answers
"what was it worth then", which nothing else can reconstruct afterwards
because a price that was not written down on the day is gone.

The benchmark level rides along for the same reason: M41a judges the return
against holding the index over the same dates, and that needs the index level
*on those dates*, not today's.
"""

import uuid
from decimal import Decimal

from django.db import models

from configuration.store import order_mode_now
from journal.models import Book


class DailyValuation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trading_date = models.DateField(db_index=True)
    book = models.CharField(
        max_length=8,
        choices=Book.choices,
        default=order_mode_now,
        help_text="The book whose lots were valued: live or paper.",
    )

    portfolio_value = models.DecimalField(
        max_digits=16,
        decimal_places=4,
        help_text="Market value of every open lot at the day's last traded prices.",
    )
    invested_to_date = models.DecimalField(max_digits=16, decimal_places=4, default=Decimal("0"))
    cash_carry = models.DecimalField(max_digits=16, decimal_places=4, default=Decimal("0"))

    benchmark_level = models.DecimalField(
        max_digits=16,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="The index level, or null when the feed had nothing for it.",
    )

    holdings_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per instrument: quantity and the price it was valued at.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-trading_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["trading_date", "book"], name="one_valuation_per_day_and_book"
            )
        ]

    def __str__(self) -> str:
        return f"{self.trading_date}: {self.portfolio_value}"
