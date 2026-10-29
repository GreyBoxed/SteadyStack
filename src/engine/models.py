"""The day's decision, recorded before anything is bought.

A tradesheet is the permanent explanation for a purchase: which names were
ranked, where each stood against its target, what the prices were, which one
was chosen and how much cash it was allowed. Stored in full (M16), so that a
decision made in 2026 is still explicable in 2046 without recomputing
anything and without the prices that produced it existing anywhere else.

Versioned rather than edited (D13). Regeneration produces a new version; the
old one stays exactly as it was, because it may already have explained an
order. Once placement has begun the operator's lever is an override, which is
recorded against the sheet rather than merged into it.
"""

import uuid
from decimal import Decimal

from django.db import models

from configuration.store import order_mode_now


class TradeSheetState(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    AWAITING_SESSION = "AWAITING_SESSION", "Awaiting session"
    RUNNING = "RUNNING", "Running"
    FILLED = "FILLED", "Filled"
    PARTIAL = "PARTIAL", "Partially filled"
    UNFILLED = "UNFILLED", "Unfilled"
    HALTED = "HALTED", "Halted"
    SKIPPED = "SKIPPED", "Skipped"


class SkipReason(models.TextChoices):
    """Why a day bought nothing. Never blank on a skipped day: an idle day
    that cannot explain itself reads as a failure."""

    WEEKEND = "weekend", "Weekend"
    HOLIDAY = "holiday", "Exchange holiday"
    NO_SESSION_BY_CUTOFF = "no_session_by_cutoff", "No Kite session by the cutoff"
    RESERVE_ENGAGED = "reserve_engaged", "Reserving for an unaffordable leader"
    NO_AFFORDABLE_INSTRUMENT = "no_affordable_instrument", "Nothing affordable today"
    EMPTY_UNIVERSE = "empty_universe", "No instrument in the universe"
    OPERATOR = "operator", "Skipped by the operator"
    PAUSED = "paused", "System paused"
    RECONCILIATION_BLOCKED = "reconciliation_blocked", "Blocked by an unresolved divergence"
    NOT_AUTHORIZED = "not_authorized", "Never authorized to trade"


class TradeSheet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trading_date = models.DateField(db_index=True)
    version = models.PositiveIntegerField()

    generated_at = models.DateTimeField(auto_now_add=True)
    generated_by = models.CharField(max_length=150, default="system")
    trace_id = models.CharField(max_length=64, db_index=True)

    state = models.CharField(
        max_length=24, choices=TradeSheetState.choices, default=TradeSheetState.DRAFT
    )
    skip_reason = models.CharField(max_length=32, choices=SkipReason.choices, blank=True)
    book = models.CharField(
        max_length=8,
        choices=[("live", "Live"), ("paper", "Paper")],
        default=order_mode_now,
        help_text=(
            "The book the day was planned and funded in, stored when it is planned: "
            "its ledger row is that book's, whatever the mode is when it settles (M1)."
        ),
    )

    model_portfolio = models.ForeignKey(
        "model_portfolio.ModelPortfolio",
        on_delete=models.PROTECT,
        related_name="tradesheets",
        null=True,
        blank=True,
        help_text="The exact weights that produced this sheet (D14).",
    )
    instrument = models.ForeignKey(
        "portfolio.Instrument",
        on_delete=models.PROTECT,
        related_name="tradesheets",
        null=True,
        blank=True,
    )

    # -- money -------------------------------------------------------------
    carry_in = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    spendable_cash = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    quantity = models.PositiveIntegerField(default=0)
    reference_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="The ask the quantity was sized against (M15).",
    )

    # -- the reserve (D5, M15d) --------------------------------------------
    reserve_symbol = models.CharField(
        max_length=64,
        blank=True,
        help_text="The unaffordable leader this day is waiting for, if any.",
    )
    reserve_days = models.PositiveSmallIntegerField(
        default=0,
        help_text="Consecutive days that name has ranked first and been unaffordable.",
    )
    reserve_engaged = models.BooleanField(default=False)

    ranking_snapshot = models.JSONField(
        default=dict,
        help_text="The full ranking, stored so the choice needs no recomputation (M16).",
    )

    # -- authorization (M15f) ---------------------------------------------
    # A plan is made automatically; placing it is not. Until a person
    # authorizes this version, the ladder places nothing and the fallback buys
    # nothing. A regenerated version starts unauthorized: what was approved
    # was the old plan, not this one.
    authorized_at = models.DateTimeField(null=True, blank=True)
    authorized_by = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-trading_date", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["trading_date", "version"], name="one_version_per_trading_date"
            )
        ]
        indexes = [models.Index(fields=["trading_date", "state"])]

    def __str__(self) -> str:
        return f"{self.trading_date} v{self.version} ({self.state})"

    @property
    def is_skipped(self) -> bool:
        return self.state == TradeSheetState.SKIPPED

    @property
    def is_authorized(self) -> bool:
        return self.authorized_at is not None

    @property
    def planned_cost(self) -> Decimal:
        """What the day's lines cost at the prices they were sized at.

        A sheet written before lines existed has only its one instrument, and
        is costed from that.
        """
        lines = list(self.lines.all()) if self.pk else []
        if lines:
            return sum((line.planned_cost for line in lines), Decimal("0"))
        if self.reference_price is None:
            return Decimal("0")
        return (self.reference_price * self.quantity).quantize(Decimal("0.01"))

    def bought_quantities(self) -> dict[str, int]:
        """Symbol to shares, for every name the day buys."""
        return {line.instrument_id: line.quantity for line in self.buy_lines()}

    @property
    def names_label(self) -> str:
        """The day's buy in a few words: "TCS", or "TCS, ITC +3 more"."""
        names = [line.instrument_id for line in self.buy_lines() if line.quantity]
        if not names:
            return ""
        shown = ", ".join(names[:2])
        return shown + (f" +{len(names) - 2} more" if len(names) > 2 else "")

    def buy_lines(self) -> list["TradeSheetLine"]:
        """The names this day buys, in ranking order.

        A sheet from before M15 bought many names has no line rows; its single
        instrument is presented as the one line it had, unsaved, so every
        reader sees the same shape.
        """
        lines = list(self.lines.select_related("instrument").order_by("rank"))
        if lines or not self.instrument_id or not self.quantity:
            return lines
        return [
            TradeSheetLine(
                tradesheet=self,
                instrument=self.instrument,
                rank=1,
                quantity=self.quantity,
                reference_price=self.reference_price or Decimal("0"),
                price_source=PriceSource.ASK,
            )
        ]

    @property
    def carry_out_if_unfilled(self) -> Decimal:
        """What carries if nothing fills: the whole amount (M17).

        Settlement replaces this with the real figure, computed from actual
        fills. It exists so a generated sheet can state its own arithmetic.
        """
        return self.spendable_cash


class PriceSource(models.TextChoices):
    """Which price a line was sized at (M15e)."""

    ASK = "ask", "Best ask"
    CLOSE = "close", "Previous close"


class TradeSheetLine(models.Model):
    """One name the day buys, and how many (M15).

    A day walks the ranking and buys every name its cash reaches, each up to
    its target. The sheet's own `instrument` and `quantity` summarise these:
    the lead name and the day's total. The quantity here is the engine's,
    reduced only by an audited override (M15a).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tradesheet = models.ForeignKey(TradeSheet, on_delete=models.PROTECT, related_name="lines")
    instrument = models.ForeignKey(
        "portfolio.Instrument", on_delete=models.PROTECT, related_name="tradesheet_lines"
    )
    rank = models.PositiveSmallIntegerField(help_text="Its place in the day's ranking.")
    quantity = models.PositiveIntegerField()
    reference_price = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        help_text="The price the quantity was sized against: the ask, else the previous close.",
    )
    price_source = models.CharField(
        max_length=8, choices=PriceSource.choices, default=PriceSource.ASK
    )

    class Meta:
        ordering = ["tradesheet", "rank"]
        constraints = [
            models.UniqueConstraint(
                fields=["tradesheet", "instrument"], name="one_line_per_name_per_sheet"
            )
        ]

    def __str__(self) -> str:
        return f"{self.tradesheet} #{self.rank}: {self.quantity} x {self.instrument_id}"

    @property
    def planned_cost(self) -> Decimal:
        return (self.reference_price * self.quantity).quantize(Decimal("0.01"))


class CashLedger(models.Model):
    """One row per trading day per book: what the engine account did that day.

    A summary of the journal, not the books themselves (spec §8). It is kept
    because a day-by-day view of carry is what M40a shows and what a person
    checks first; the journal is what it is checked against.

        opening_carry + daily_allocation + extra_allocation + proceeds
            - cash_spent - charges + other == closing_carry

    `proceeds` is the day's rebalance sale money, net of the sells' own
    charges (D-S1): the reinvest allocations a settled rebalance batch gave
    the engine, split out of `extra_allocation` so the row says which money
    came back from a sale rather than in from outside. It is not part of
    `available` -- what the day had before it spent -- and it is not a
    contribution: it is money the portfolio already had, coming back to be
    spent again.

    `other` is what moved the engine account that was neither budget nor
    spend -- a quarterly-settlement sweep, carry restored after one. Carry may
    be negative: an overspend is repaid by the next allocation (D7).
    """

    book = models.CharField(max_length=8, choices=[("live", "Live"), ("paper", "Paper")])
    trading_date = models.DateField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    opening_carry = models.DecimalField(max_digits=20, decimal_places=6, default=Decimal("0"))
    daily_allocation = models.DecimalField(max_digits=20, decimal_places=6, default=Decimal("0"))
    extra_allocation = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        default=Decimal("0"),
        help_text="Lumpsums and restored carry.",
    )
    proceeds = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        default=Decimal("0"),
        help_text="Reinvested rebalance sale money, net of the sells' charges (D-S1).",
    )
    cash_spent = models.DecimalField(max_digits=20, decimal_places=6, default=Decimal("0"))
    charges = models.DecimalField(max_digits=20, decimal_places=6, default=Decimal("0"))
    other = models.DecimalField(max_digits=20, decimal_places=6, default=Decimal("0"))
    closing_carry = models.DecimalField(max_digits=20, decimal_places=6, default=Decimal("0"))

    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-trading_date"]
        verbose_name_plural = "cash ledger"
        constraints = [
            models.UniqueConstraint(fields=["book", "trading_date"], name="one_ledger_row_per_day")
        ]

    def __str__(self) -> str:
        return f"{self.book} {self.trading_date}: {self.opening_carry} -> {self.closing_carry}"

    @property
    def available(self) -> Decimal:
        """What the day had to spend before it spent any of it."""
        return self.opening_carry + self.daily_allocation + self.extra_allocation

    @property
    def discrepancy(self) -> Decimal:
        return (
            self.available
            + self.proceeds
            - self.cash_spent
            - self.charges
            + self.other
            - self.closing_carry
        )

    @property
    def reconciles(self) -> bool:
        return self.discrepancy == 0


class OverrideKind(models.TextChoices):
    """The three decisions an operator may take on a running day (M15a)."""

    REDUCE_QUANTITY = "reduce_quantity", "Reduce the day's quantity"
    EXCLUDE_TICK = "exclude_tick", "Exclude a tick from placing"
    SKIP_DAY = "skip_day", "Skip the day"
    ADJUST_LINE = "adjust_line", "Change one name's quantity"
    SKIP_LINE = "skip_line", "Skip one name for the day"
    STOP_DAY = "stop_day", "Stop the day after a fill"


class TradesheetOverride(models.Model):
    """An operator decision, recorded against a tradesheet and never merged in.

    The ranking snapshot is not rewritten. An override is a separate,
    immutable row saying what a person decided and why -- which is exactly the
    property the versioning was protecting: the sheet still explains what the
    engine chose, and this explains what was done about it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tradesheet = models.ForeignKey(TradeSheet, on_delete=models.PROTECT, related_name="overrides")

    kind = models.CharField(max_length=20, choices=OverrideKind.choices)
    tick_no = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Set only for a tick exclusion."
    )

    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)

    reason = models.CharField(max_length=255)
    actor = models.CharField(max_length=150)
    decided_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-decided_at"]
        indexes = [models.Index(fields=["tradesheet", "kind"])]

    def __str__(self) -> str:
        return f"{self.kind} on {self.tradesheet_id} by {self.actor}"
