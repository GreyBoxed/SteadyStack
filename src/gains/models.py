"""What a sale actually realised.

FIFO is kept because the broker matches that way, and two records that agree
are worth more than two that do not. It is not here because tax is being
modelled: nothing tax-specific exists in this system -- no rates, no holding
periods, no exemptions, no reporting.

A realised-gain row is immutable. It says which lot was consumed, how much of
it, what that quantity cost and what it sold for, and the difference. Undoing
one is a `Correction` that writes a reversing row (M36c), never an edit: a
number that was reported and then quietly changed is worse than one that was
wrong and then corrected in public.
"""

import uuid
from decimal import Decimal

from django.db import models


class RealisedGain(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    lot = models.ForeignKey("portfolio.Lot", on_delete=models.PROTECT, related_name="realisations")
    fill = models.ForeignKey(
        "orders.Fill",
        on_delete=models.PROTECT,
        related_name="realisations",
        null=True,
        blank=True,
        help_text="The sell fill that consumed it; null for an external sale.",
    )
    external_transaction = models.ForeignKey(
        "records.ExternalTransaction",
        on_delete=models.PROTECT,
        related_name="realisations",
        null=True,
        blank=True,
    )

    instrument = models.ForeignKey(
        "portfolio.Instrument", on_delete=models.PROTECT, related_name="realised_gains"
    )
    quantity = models.PositiveIntegerField()

    acquired_on = models.DateField(help_text="Copied from the lot, so the row stands alone.")
    disposed_on = models.DateField(db_index=True)

    cost = models.DecimalField(
        max_digits=16, decimal_places=4, help_text="What the consumed quantity cost (D6)."
    )
    net_proceeds = models.DecimalField(
        max_digits=16, decimal_places=4, help_text="Sale value after the charges on the sale."
    )
    gain = models.DecimalField(max_digits=16, decimal_places=4)

    basis_was_estimated = models.BooleanField(
        default=False,
        help_text="True if the lot's basis had not been settled against a contract note.",
    )
    reverses = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_by",
        help_text="Set on a reversing row written by a correction (M36c).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-disposed_on", "-created_at"]
        indexes = [
            models.Index(fields=["instrument", "disposed_on"]),
            models.Index(fields=["disposed_on"]),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} {self.instrument_id} realised {self.gain}"

    @property
    def is_reversal(self) -> bool:
        return self.reverses_id is not None

    @property
    def holding_days(self) -> int:
        """How long it was held. Reported, never used to classify anything."""
        return (self.disposed_on - self.acquired_on).days

    @property
    def proceeds_per_unit(self) -> Decimal:
        if self.quantity == 0:
            return Decimal("0")
        return self.net_proceeds / self.quantity
