"""The journal: the single record of what money did (spec §5).

Every movement is one entry whose lines sum to zero. Entries are appended and
never changed -- a mistake is undone by a reversal, which is itself an entry.
The database enforces all of that (migration 0003); this module only states
the shape.
"""

import uuid

from django.db import models


class Book(models.TextChoices):
    LIVE = "live", "Live"
    PAPER = "paper", "Paper"


class AccountType(models.TextChoices):
    ASSET = "asset", "Asset"
    EQUITY = "equity", "Equity"
    INCOME = "income", "Income"
    EXPENSE = "expense", "Expense"


class Account(models.Model):
    code = models.CharField(max_length=8, primary_key=True)
    name = models.CharField(max_length=100)
    type = models.CharField(max_length=8, choices=AccountType.choices)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    @property
    def debit_normal(self) -> bool:
        return self.type in (AccountType.ASSET, AccountType.EXPENSE)


class EntryKind(models.TextChoices):
    CONTRIBUTION = "contribution", "Contribution"
    WITHDRAWAL = "withdrawal", "Withdrawal"
    ALLOCATION = "allocation", "Allocation"
    BUY_FILL = "buy_fill", "Buy fill"
    SELL_FILL = "sell_fill", "Sell fill"
    SETTLEMENT = "settlement", "Settlement"
    CHARGE_CORRECTION = "charge_correction", "Charge correction"
    ACCOUNT_CHARGE = "account_charge", "Account charge"
    DIVIDEND = "dividend", "Dividend"
    EXTERNAL_ACQUISITION = "external_acquisition", "External acquisition"
    EXTERNAL_DISPOSAL = "external_disposal", "External disposal"
    REVERSAL = "reversal", "Reversal"
    OPENING_BALANCE = "opening_balance", "Opening balance"


class Component(models.TextChoices):
    NONE = "", "—"
    GROSS = "gross", "Gross"
    STT = "stt", "Securities transaction tax"
    STAMP_DUTY = "stamp_duty", "Stamp duty"
    EXCHANGE_TXN = "exchange_txn", "Exchange transaction charges"
    SEBI = "sebi", "SEBI turnover fee"
    GST = "gst", "GST"
    BROKERAGE = "brokerage", "Brokerage"
    DP = "dp", "DP charge"
    AMC = "amc", "AMC"
    DDPI = "ddpi", "DDPI"
    INTEREST = "interest", "Delayed payment interest"
    OTHER = "other", "Other"


class JournalEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.CharField(max_length=8, choices=Book.choices, db_index=True)
    number = models.PositiveIntegerField(help_text="Gapless within a book.")
    kind = models.CharField(max_length=24, choices=EntryKind.choices, db_index=True)
    occurred_on = models.DateField(db_index=True)
    description = models.CharField(max_length=255)

    source_type = models.CharField(max_length=32, blank=True, db_index=True)
    source_id = models.CharField(max_length=64, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, unique=True)
    reverses = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversed_by"
    )

    actor = models.CharField(max_length=150)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["book", "number"]
        constraints = [
            models.UniqueConstraint(fields=["book", "number"], name="journal_number_per_book")
        ]
        indexes = [models.Index(fields=["book", "occurred_on"])]

    def __str__(self) -> str:
        return f"{self.book} #{self.number} {self.kind} {self.occurred_on}"


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    amount = models.DecimalField(
        max_digits=20, decimal_places=6, help_text="Signed: debit positive, credit negative."
    )
    instrument = models.ForeignKey(
        "portfolio.Instrument",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="journal_lines",
    )
    component = models.CharField(max_length=16, choices=Component.choices, blank=True)
    lot = models.ForeignKey(
        "portfolio.Lot",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="journal_lines",
        help_text=(
            "The lot an opening entry's Investments line is the cost of, so its "
            "flow is dated when the lot was bought (I10). Empty on every other line."
        ),
    )

    class Meta:
        ordering = ["entry", "id"]
        indexes = [models.Index(fields=["account", "instrument"])]

    def __str__(self) -> str:
        return f"{self.account_id} {self.amount}"


class PeriodClose(models.Model):
    """A financial year the CA has signed off (spec J9)."""

    book = models.CharField(max_length=8, choices=Book.choices)
    fy_end = models.DateField(help_text="31 March of the year closed.")
    closed_by = models.CharField(max_length=150)
    closed_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["book", "fy_end"]
        constraints = [
            models.UniqueConstraint(fields=["book", "fy_end"], name="journal_one_close_per_year")
        ]

    def __str__(self) -> str:
        return f"{self.book} closed to {self.fy_end}"


class StatementUpload(models.Model):
    """One Console funds-statement file, kept verbatim (spec §7.2)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, unique=True)
    raw = models.TextField()
    period_from = models.DateField(null=True, blank=True)
    period_to = models.DateField(null=True, blank=True)
    opening_balance = models.DecimalField(max_digits=20, decimal_places=6)
    closing_balance = models.DecimalField(max_digits=20, decimal_places=6)
    uploaded_by = models.CharField(max_length=150)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return f"{self.filename} ({self.uploaded_at:%d %b %Y})"


class StatementLine(models.Model):
    """One row of Zerodha's statement, never changed except to match it.

    Matched to an entry once; matched again only if that entry is reversed
    (migration 0008), so a line confirmed as the wrong kind can be put right.
    """

    upload = models.ForeignKey(StatementUpload, on_delete=models.PROTECT, related_name="lines")
    position = models.PositiveIntegerField()
    fingerprint = models.CharField(max_length=64, unique=True)
    posting_date = models.DateField(db_index=True)
    particulars = models.CharField(max_length=500)
    cost_center = models.CharField(max_length=64, blank=True)
    voucher_type = models.CharField(max_length=64)
    debit = models.DecimalField(max_digits=20, decimal_places=6)
    credit = models.DecimalField(max_digits=20, decimal_places=6)
    net_balance = models.DecimalField(max_digits=20, decimal_places=6)
    matched_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="statement_lines",
    )

    class Meta:
        ordering = ["posting_date", "upload__uploaded_at", "position"]

    def __str__(self) -> str:
        return f"{self.posting_date} {self.particulars}"

    @property
    def amount(self):
        """Signed as Zerodha's cash moved: credit positive, debit negative."""
        return self.credit - self.debit


class BalanceCheck(models.Model):
    """Zerodha's funds balance against ours, each morning (spec §7.1)."""

    checked_on = models.DateField(unique=True)
    ours = models.DecimalField(max_digits=20, decimal_places=6)
    theirs = models.DecimalField(max_digits=20, decimal_places=6)
    difference = models.DecimalField(max_digits=20, decimal_places=6)
    trace_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-checked_on"]

    def __str__(self) -> str:
        return f"{self.checked_on}: ours {self.ours}, theirs {self.theirs}"


class SettlementExplanation(models.Model):
    """A person's account of a settlement difference no correction can take (m-4).

    A difference beyond what rounding on the day's charges explains cannot be
    booked as charges, and a settled date cannot take an imported trade, so
    it is reconciled by hand -- and the result written here, never as an
    edit of the line. Appended only: a second explanation is a new row. It
    holds for the difference it saw; if that moves, the line disagrees again.
    """

    line = models.ForeignKey(StatementLine, on_delete=models.PROTECT, related_name="explanations")
    difference = models.DecimalField(max_digits=20, decimal_places=6)
    reason = models.CharField(max_length=500)
    explained_by = models.CharField(max_length=150)
    explained_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-explained_at"]

    def __str__(self) -> str:
        return f"{self.line.posting_date} {self.difference}: {self.reason[:40]}"
