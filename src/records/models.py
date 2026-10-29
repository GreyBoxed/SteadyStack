"""Things that happened to the portfolio which this system did not do.

Shares leave and arrive by routes the engine knows nothing about: a sale made
directly at the broker, a transfer in from another demat, a bonus issue, a
correction to a mistake. None of these is prevented -- the account belongs to
a person, not to this program -- so each one is *recorded* instead, in a form
the lot history can absorb.

A divergence is what the system notices when the broker's holdings and the
lots disagree. It is never inferred and never silently absorbed (M31a): the
operator says what it was, and until they do, the figures for that instrument
are withheld rather than computed from a holding known to be wrong.
"""

import uuid
from decimal import Decimal

from django.db import models


class ExternalSide(models.TextChoices):
    BUY = "BUY", "Bought outside this system"
    SELL = "SELL", "Sold outside this system"
    TRANSFER_IN = "TRANSFER_IN", "Transferred in"
    TRANSFER_OUT = "TRANSFER_OUT", "Transferred out"


class PaidFrom(models.TextChoices):
    ZERODHA = "zerodha", "Zerodha funds"
    BANK = "bank", "The owner's bank (an IPO through UPI)"


class ExternalTransaction(models.Model):
    """A movement of shares this system did not cause (M31a, M36d).

    A buy needs the acquisition date, quantity, price and charges, so the lot
    it creates carries the right basis and the right clock. A transfer in
    carries the transferor's date and basis, because the shares did not start
    existing when they arrived. A transfer out is a disposal at nil proceeds:
    the lots are consumed and the row is flagged for manual treatment rather
    than this system deciding what it was.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instrument = models.ForeignKey(
        "portfolio.Instrument", on_delete=models.PROTECT, related_name="external_transactions"
    )

    side = models.CharField(max_length=12, choices=ExternalSide.choices)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0"),
        help_text="Nil for a transfer out, which is a disposal at no proceeds.",
    )
    charges = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    paid_from = models.CharField(
        max_length=8,
        choices=PaidFrom.choices,
        default=PaidFrom.ZERODHA,
        help_text="A buy paid from the bank -- an IPO through UPI -- never touches Zerodha's "
        "funds.",
    )

    occurred_on = models.DateField(db_index=True)
    acquired_on = models.DateField(
        null=True,
        blank=True,
        help_text="For a buy or a transfer in: when the shares were originally acquired.",
    )

    reason = models.CharField(max_length=255)
    actor = models.CharField(max_length=150)
    recorded_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    needs_manual_treatment = models.BooleanField(
        default=False,
        help_text="A transfer out: what it was for is a decision this system does not make.",
    )

    class Meta:
        ordering = ["-occurred_on", "-recorded_at"]
        indexes = [models.Index(fields=["instrument", "occurred_on"])]

    def __str__(self) -> str:
        return f"{self.side} {self.quantity} {self.instrument_id} on {self.occurred_on}"

    @property
    def adds_shares(self) -> bool:
        return self.side in (ExternalSide.BUY, ExternalSide.TRANSFER_IN)


class DivergenceClassification(models.TextChoices):
    UNCLASSIFIED = "unclassified", "Not yet classified"
    EXTERNAL_BUY = "external_buy", "Bought outside this system"
    EXTERNAL_SELL = "external_sell", "Sold outside this system"
    TRANSFER = "transfer", "Transferred in or out"
    CORPORATE_ACTION = "corporate_action", "A corporate action"
    SYSTEM_ERROR = "system_error", "This system got it wrong"


class Divergence(models.Model):
    """The broker's holding and our lots disagree about one instrument.

    Recorded, not resolved. Until an operator says what it was, realised and
    unrealised figures for that instrument are withheld rather than computed
    from a holding known to be wrong -- and portfolio-level figures are still
    produced, flagged with the instrument and the quantity in doubt (M31b).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instrument = models.ForeignKey(
        "portfolio.Instrument", on_delete=models.PROTECT, related_name="divergences"
    )

    expected_quantity = models.IntegerField(help_text="What the lots say.")
    actual_quantity = models.IntegerField(help_text="What the broker says.")

    detected_on = models.DateField(db_index=True)
    classification = models.CharField(
        max_length=20,
        choices=DivergenceClassification.choices,
        default=DivergenceClassification.UNCLASSIFIED,
    )
    explained_by = models.ForeignKey(
        ExternalTransaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="divergences",
    )

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=150, blank=True)
    note = models.CharField(max_length=255, blank=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-detected_on"]
        indexes = [models.Index(fields=["instrument", "resolved_at"])]

    def __str__(self) -> str:
        return (
            f"{self.instrument_id}: lots say {self.expected_quantity}, "
            f"broker says {self.actual_quantity}"
        )

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    @property
    def difference(self) -> int:
        return self.actual_quantity - self.expected_quantity


class Correction(models.Model):
    """The only way to undo a bookkeeping error (M36c, D11).

    Appended and reasoned, never an edit. It carries a generic target rather
    than a foreign key because it has to be able to supersede a lot, a
    realised-gain row or a ledger row without three separate mechanisms.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target_table = models.CharField(max_length=100)
    target_id = models.CharField(max_length=64)

    reason = models.CharField(max_length=255)
    actor = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["target_table", "target_id"])]

    def __str__(self) -> str:
        return f"correction to {self.target_table} {self.target_id} by {self.actor}"
