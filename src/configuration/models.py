"""Operational configuration, versioned and append-only.

A settings row that is updated in place answers "what is the daily amount?"
and loses the ability to answer "what was it in March?". Since a tradesheet
from 2031 has to be explicable in 2046, each change writes a new version and
the newest one is in force. S7's browsable history is then the table itself
rather than a reconstruction from audit events.

The defaults here are the PRD's: a thousand rupees a day, paper orders, and no
registered address, which is the state in which no live order can be placed.
"""

import uuid
from datetime import time
from decimal import Decimal

from django.db import models


class OrderMode(models.TextChoices):
    PAPER = "paper", "Paper"
    LIVE = "live", "Live"


class ConfigurationVersion(models.Model):
    """One row per change. The highest version is the one in force."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField(unique=True)
    effective_from = models.DateTimeField(auto_now_add=True, db_index=True)
    changed_by = models.CharField(max_length=150, help_text="username, or 'system'")
    reason = models.CharField(max_length=255, blank=True)

    # -- money and sizing -------------------------------------------------
    daily_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("1000.00"),
        help_text="Rupees invested per trading day (M8).",
    )
    max_price_per_share = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Standing ceiling: an instrument priced above this leaves the "
            "universe entirely (M11). Unset means no ceiling."
        ),
    )
    reserve_threshold_days = models.PositiveSmallIntegerField(
        default=5,
        help_text=(
            "Consecutive days a name may rank first and be unaffordable before "
            "the day starts reserving cash for it (M15d)."
        ),
    )

    # -- the clock (M10, M11a) --------------------------------------------
    tradesheet_time = models.TimeField(
        default=time(9, 45),
        help_text="When the day's decision is made.",
    )
    ladder_interval_minutes = models.PositiveSmallIntegerField(
        default=5, help_text="How often a rebalance sell's ladder tick fires (M33)."
    )
    placer_interval_seconds = models.PositiveSmallIntegerField(
        default=30,
        help_text=(
            "How often the daily buy's placing round runs. Each round places at "
            "most the rate limit's worth of orders (M11a)."
        ),
    )
    placement_window_close = models.TimeField(
        default=time(14, 0),
        help_text="After this, no new limit orders; the last slices still have time to fill.",
    )
    market_fallback_time = models.TimeField(
        default=time(15, 0),
        help_text=(
            "Everything still open is cancelled, and each name's remainder is "
            "bought at market in paced rounds from here (M22)."
        ),
    )
    close_window_end = models.TimeField(
        default=time(15, 25),
        help_text=(
            "The paced close's last round. Before the exchange's closing auction "
            "entry and the last half hour's VWAP both end (M22)."
        ),
    )

    # -- how the ladder prices and sizes (D10, M11a) -----------------------
    slice_quantity = models.PositiveSmallIntegerField(
        default=1,
        help_text="The floor on a tick's share quantity; the ladder spreads above it.",
    )
    ladder_patience = models.PositiveSmallIntegerField(
        default=3,
        help_text=(
            "Rebalance sells: ticks the sell ladder waits before stepping one "
            "price tick toward the bid (M33)."
        ),
    )
    catch_up_minutes = models.PositiveSmallIntegerField(
        default=30,
        help_text=(
            "How far behind its schedule a name must fall before its order pays "
            "the ask. On schedule it rests at the bid; between, in proportion (D10)."
        ),
    )
    order_ttl_minutes = models.PositiveSmallIntegerField(
        default=5,
        help_text=(
            "A buy resting unfilled this long is cancelled, and its shares go "
            "back into the schedule to be placed again at the current price (M21)."
        ),
    )
    fallback_band_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("5.00"),
        help_text=(
            "How far the last traded price may have moved from the tradesheet's "
            "reference before the market order is withheld entirely (M22a)."
        ),
    )

    # -- the rate breaker -------------------------------------------------
    #
    # Not a market rule and not a broker rule: a rail against this program.
    # Every other guard in the system assumes the code is doing roughly what
    # it means to. This one assumes it is not -- a loop that places instead of
    # returning, a tick that fires without its idempotency key, a retry that
    # retries the placement rather than the read. It counts orders and stops.
    max_orders_per_window = models.PositiveSmallIntegerField(
        default=5,
        help_text="Orders this system may place within the window before it refuses.",
    )
    order_rate_window_seconds = models.PositiveSmallIntegerField(
        default=30,
        help_text="The window the order count is measured over.",
    )

    # -- broker -----------------------------------------------------------
    order_mode = models.CharField(
        max_length=8,
        choices=OrderMode.choices,
        default=OrderMode.PAPER,
        help_text="Paper orders are recorded and never sent (M19a, M19b).",
    )
    registered_static_ip = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text=(
            "The address registered with Zerodha. No order is placed while it "
            "is unset, and live is refused unless it matches the address the "
            "system is calling from (M12a, D12)."
        ),
    )

    market_data_monthly_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("500.00"),
        help_text=(
            "What the Kite market-data subscription costs each month. Reported as "
            "drag on the return so the cost can never hide inside it."
        ),
    )

    benchmark_symbol = models.CharField(
        max_length=64,
        default="NIFTY 50",
        help_text=(
            "The index the return is judged against (M41a). An instrument on "
            "the existing tick feed, so this adds no second market-data "
            "dependency."
        ),
    )

    class Meta:
        ordering = ["-version"]
        get_latest_by = "version"

    def __str__(self) -> str:
        return f"configuration v{self.version} ({self.order_mode})"

    @property
    def is_live(self) -> bool:
        return self.order_mode == OrderMode.LIVE


class SystemPause(models.Model):
    """The system stopped, on purpose, with a record of why.

    Without this the only way to stop a system that places orders every
    weekday for decades is to withhold a login -- which does not stop it. It
    parks the day, accrues the carry, and then deploys the whole accumulated
    pile the moment somebody logs in, which is the opposite of stopping.

    Carry accrual suspends while paused: a paused day is not a day the
    portfolio was owed money for.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    paused_at = models.DateTimeField(auto_now_add=True)
    paused_by = models.CharField(max_length=150)
    reason = models.CharField(max_length=255)

    resume_on = models.DateField(
        null=True,
        blank=True,
        help_text="Resumes by itself on this date; open-ended when unset.",
    )

    resumed_at = models.DateTimeField(null=True, blank=True)
    resumed_by = models.CharField(max_length=150, blank=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-paused_at"]

    def __str__(self) -> str:
        if self.resumed_at:
            return f"paused {self.paused_at:%Y-%m-%d}, resumed {self.resumed_at:%Y-%m-%d}"
        return f"paused since {self.paused_at:%Y-%m-%d} ({self.reason})"

    @property
    def is_active(self) -> bool:
        return self.resumed_at is None
