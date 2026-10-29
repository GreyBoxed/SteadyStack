"""The deliberate way shares leave the portfolio.

The engine never sells on its own initiative (M34). A sale happens because a
person uploaded a file saying which names and how many, looked at what it
would do, and confirmed it. Everything in this module exists to make that
sequence hard to get wrong and impossible to do by accident.

The execute token is why the preview cannot drift into the execution. It is
bound to the exact content that was previewed, single use, and expires: a
stale tab cannot sell a portfolio the operator was looking at an hour ago,
and a double submit cannot sell twice.
"""

import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone


class BatchState(models.TextChoices):
    DRAFT = "draft", "Uploaded, not yet confirmed"
    RUNNING = "running", "Executing"
    COMPLETE = "complete", "Fully executed"
    PARTIAL = "partial", "Partially executed"
    CANCELLED = "cancelled", "Cancelled by the operator"
    REJECTED = "rejected", "Refused at validation"


#: How long a preview stays good for. Long enough to read, short enough that
#: the book it was priced against still resembles the one being sold into.
TOKEN_TTL = timedelta(minutes=15)


class RebalanceSell(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.CharField(max_length=150)
    source_filename = models.CharField(max_length=255, blank=True)
    content_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="Of the normalised rows: the token is bound to exactly what was previewed.",
    )

    state = models.CharField(max_length=12, choices=BatchState.choices, default=BatchState.DRAFT)
    trading_date = models.DateField(null=True, blank=True, db_index=True)
    stream = models.CharField(
        max_length=32,
        blank=True,
        help_text="This batch's own stream, so it cannot collide with the daily buy (M24).",
    )

    reinvest = models.BooleanField(
        default=True, help_text="Allocate the net proceeds to the engine once they settle."
    )

    execute_token = models.CharField(max_length=64, blank=True, db_index=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    token_used_at = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cancelled_reason = models.CharField(max_length=255, blank=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return f"rebalance sell {self.id} ({self.state})"

    @property
    def token_is_live(self) -> bool:
        if not self.execute_token or self.token_used_at is not None:
            return False
        return self.token_expires_at is not None and self.token_expires_at > timezone.now()

    @property
    def is_running(self) -> bool:
        return self.state == BatchState.RUNNING


class RowState(models.TextChoices):
    VALID = "valid", "Valid"
    INVALID = "invalid", "Refused"
    FILLED = "filled", "Sold in full"
    PARTIAL = "partial", "Sold in part"
    UNSOLD = "unsold", "Not sold"


class RebalanceSellRow(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(RebalanceSell, on_delete=models.PROTECT, related_name="rows")
    instrument = models.ForeignKey(
        "portfolio.Instrument", on_delete=models.PROTECT, related_name="rebalance_sell_rows"
    )

    quantity = models.PositiveIntegerField()
    sold_quantity = models.PositiveIntegerField(default=0)

    validation_state = models.CharField(
        max_length=8, choices=RowState.choices, default=RowState.VALID
    )
    validation_error = models.CharField(max_length=255, blank=True)
    line = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["instrument_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "instrument"], name="one_row_per_instrument_per_batch"
            )
        ]

    def __str__(self) -> str:
        return f"sell {self.quantity} {self.instrument_id}"

    @property
    def unsold_quantity(self) -> int:
        return max(0, self.quantity - self.sold_quantity)
