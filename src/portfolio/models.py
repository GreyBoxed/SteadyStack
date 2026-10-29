"""The instrument master.

Every order price is rounded to the instrument's own tick size, and the tick
size comes from Kite rather than from a constant (M11b). A hardcoded five
paise is a rejected order the day an instrument disagrees, and a stale master
is a wrong price, so the refresh is a scheduled task with an alert rather than
a one-off load.

`tradingsymbol` is the primary key, following the plan's data model: the
foreign keys that reference it read as themselves -- a lot in INFY says INFY
-- and this account trades NSE equity, where a symbol identifies one
instrument. A symbol that is reassigned by a corporate action is handled the
way every other historical restatement is, by superseding rows rather than
editing them.
"""

import uuid
from decimal import Decimal

from django.db import models


class Instrument(models.Model):
    tradingsymbol = models.CharField(max_length=64, primary_key=True)
    exchange = models.CharField(max_length=16, default="NSE", db_index=True)
    instrument_token = models.BigIntegerField(
        unique=True,
        help_text="Kite's numeric id, and the key the tick websocket subscribes by.",
    )
    name = models.CharField(max_length=255, blank=True)

    tick_size = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("0.0500"),
        help_text="Minimum price increment. Order prices are rounded to it (M11b).",
    )
    lot_size = models.PositiveIntegerField(default=1)
    instrument_type = models.CharField(max_length=16, default="EQ")
    segment = models.CharField(max_length=32, blank=True)

    active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="False once the instrument stops appearing in the master.",
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)
    refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tradingsymbol"]
        indexes = [models.Index(fields=["exchange", "active"])]

    def __str__(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def instrument_key(self) -> str:
        """The form Kite's quote and depth calls expect."""
        return f"{self.exchange}:{self.tradingsymbol}"

    def round_to_tick(self, price: Decimal) -> Decimal:
        """Round *price* down to a multiple of this instrument's tick size.

        Down, not nearest: this system only ever places buy limits, and
        rounding a buy limit up would pay more than the price that was
        decided on. The 15:00 fallback is a market order and does not come
        through here.
        """
        if self.tick_size <= 0:
            return price
        ticks = (price / self.tick_size).to_integral_value(rounding="ROUND_FLOOR")
        return (ticks * self.tick_size).quantize(self.tick_size)


class Holding(models.Model):
    """What the account holds, as the broker last reported it.

    The broker's view, kept deliberately separate from our lots. Kite returns
    an average price and never lots, so this table can say *how much* is held
    but never *what it cost* in the sense the gains calculation needs. Lots
    answer that, and the two are reconciled against each other (M42) -- which
    only works while they remain two records rather than one.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instrument = models.OneToOneField(
        Instrument,
        on_delete=models.PROTECT,
        related_name="holding",
    )
    quantity = models.IntegerField(default=0)
    average_cost = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"))
    reconciled_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["instrument_id"]

    def __str__(self) -> str:
        return f"{self.instrument_id} x{self.quantity}"


class BasisState(models.TextChoices):
    """How exact the cost on a lot is.

    An estimate settles into a final figure when the contract note arrives
    (M35b). An imported lot is final on arrival: it is the operator's own
    record of what was actually paid.
    """

    ESTIMATED = "estimated", "Estimated from the rate card"
    FINAL = "final", "Reconciled against the contract note"
    IMPORTED = "imported", "Imported from the operator's records"


class OpeningImport(models.Model):
    """The one-time seeding of what the account already held.

    `kite.holdings()` returns an average price and never lots, so the lots
    that existed before this system did can only come from the operator's own
    records. Until this is marked complete, anything that depends on a whole
    lot history refuses to run: FIFO against an incomplete set is wrong in a
    way nobody would notice.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    imported_by = models.CharField(max_length=150)
    source = models.CharField(
        max_length=255,
        help_text="Where the numbers came from -- a contract note, a statement, a spreadsheet.",
    )
    lot_count = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        state = "complete" if self.completed_at else "in progress"
        return f"opening import ({state}, {self.lot_count} lots)"

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None


class Lot(models.Model):
    """One parcel of shares, at what it cost, on the day it was acquired.

    The basis fields are immutable and the database enforces it (see the
    lot_immutability migration). `remaining_quantity` is the single exception,
    because consumption is what a lot is for: a sell takes from the oldest
    lots first and decrements what is left. A correction to a lot supersedes
    it with a new row rather than editing it, so the original stays readable
    alongside the thing that replaced it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="lots")

    acquired_on = models.DateField(db_index=True)
    quantity = models.PositiveIntegerField()
    remaining_quantity = models.PositiveIntegerField()
    cost_per_unit = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        help_text="Everything the share cost, charges included (D6).",
    )

    basis_state = models.CharField(
        max_length=16, choices=BasisState.choices, default=BasisState.ESTIMATED
    )
    opening_import = models.ForeignKey(
        OpeningImport,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lots",
    )
    created_from_fill = models.OneToOneField(
        "orders.Fill",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lot",
        help_text="The fill that bought it. One lot per fill, never per order (D11).",
    )
    external_transaction = models.ForeignKey(
        "records.ExternalTransaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lots",
        help_text=(
            "The external buy or transfer in that created this lot. A lot finalised "
            "against a contract note carries it to the replacement, alongside the "
            "original (D11): both describe the same purchase."
        ),
    )
    superseded_by = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supersedes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        # FIFO order, and the order the consumption walk reads in.
        ordering = ["acquired_on", "created_at"]
        indexes = [models.Index(fields=["instrument", "acquired_on"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(remaining_quantity__lte=models.F("quantity")),
                name="remaining_never_exceeds_acquired",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.instrument_id} {self.remaining_quantity}/{self.quantity} @ {self.cost_per_unit}"
        )

    @property
    def is_open(self) -> bool:
        return self.remaining_quantity > 0 and self.superseded_by_id is None

    @property
    def cost_remaining(self) -> Decimal:
        return (self.cost_per_unit * self.remaining_quantity).quantize(Decimal("0.01"))
