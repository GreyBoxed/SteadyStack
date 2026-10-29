"""The target weights, as an immutable document.

A model portfolio is maintained the way it is actually maintained in
practice -- as a document that gets revised and re-issued -- rather than as
rows edited in a settings screen. Each upload is a new version and none is
ever edited (D14).

That choice pays for itself in one place: a tradesheet points at the exact
version that produced it, so "which weights were live the day this was
bought?" is a foreign key rather than a reconstruction from effective dates.
"""

import uuid
from decimal import Decimal

from django.db import models


class ModelPortfolio(models.Model):
    """One uploaded document. Immutable once written."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField(unique=True)
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    uploaded_by = models.CharField(max_length=150)

    source_filename = models.CharField(max_length=255, blank=True)
    content_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 of the normalised rows, so an identical re-upload is recognisable.",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-version"]
        get_latest_by = "version"

    def __str__(self) -> str:
        return f"model portfolio v{self.version}"

    @property
    def total_weight(self) -> Decimal:
        return sum((row.target_pct for row in self.rows.all()), Decimal("0"))

    def weights(self) -> dict[str, Decimal]:
        """Symbol to target percentage, for the engine to rank against."""
        return {row.tradingsymbol: row.target_pct for row in self.rows.all()}


class ModelPortfolioRow(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio = models.ForeignKey(ModelPortfolio, on_delete=models.PROTECT, related_name="rows")
    instrument = models.ForeignKey(
        "portfolio.Instrument",
        on_delete=models.PROTECT,
        related_name="model_portfolio_rows",
    )
    target_pct = models.DecimalField(max_digits=7, decimal_places=4)

    class Meta:
        ordering = ["instrument_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "instrument"],
                name="one_row_per_instrument_per_version",
            )
        ]

    def __str__(self) -> str:
        return f"{self.instrument_id} {self.target_pct}%"

    @property
    def tradingsymbol(self) -> str:
        return self.instrument_id
