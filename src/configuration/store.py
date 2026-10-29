"""Reading and changing configuration.

Two rules live here, and nothing else needs to know them:

  * reading always returns a row. A system with no configuration yet behaves
    as though it holds the defaults, so the first tradesheet does not have to
    wait for somebody to visit a settings page;
  * changing always appends, and always audits. There is no update path,
    because the previous values are how a past day is explained.

Switching to live is the one change with a precondition, and it is enforced
here rather than at the screen that offers it -- a gate that only exists in a
form is a gate that the next caller walks around.
"""

from decimal import Decimal
from typing import Any

from django.db import connection, transaction

from audit.events import record
from configuration.models import ConfigurationVersion, OrderMode

#: Fields a caller may set. Anything else is bookkeeping this module owns.
MUTABLE_FIELDS = frozenset(
    {
        "daily_amount",
        "max_price_per_share",
        "reserve_threshold_days",
        "tradesheet_time",
        "ladder_interval_minutes",
        "placer_interval_seconds",
        "catch_up_minutes",
        "order_ttl_minutes",
        "close_window_end",
        "placement_window_close",
        "market_fallback_time",
        "slice_quantity",
        "ladder_patience",
        "fallback_band_pct",
        "max_orders_per_window",
        "order_rate_window_seconds",
        "order_mode",
        "registered_static_ip",
        "benchmark_symbol",
        "market_data_monthly_cost",
    }
)


#: Serialises configuration appends. Any stable 64-bit number will do; this
#: one is arbitrary and belongs to this table alone.
_VERSION_LOCK = 8_142_037_115_900_311


class ConfigurationError(Exception):
    """A change that was refused."""


class _LiveRefused(Exception):
    """Internal: carries the reason out of the transaction before it is audited."""

    def __init__(self, why: str, registered: str | None) -> None:
        super().__init__(why)
        self.why = why
        self.registered = registered


def current() -> ConfigurationVersion:
    """The configuration in force.

    Returns an unsaved instance carrying the defaults when nothing has been
    configured yet: the system is usable from the first minute, and the first
    real change persists version 1.
    """
    latest = ConfigurationVersion.objects.order_by("-version").first()
    return latest or ConfigurationVersion(version=0, changed_by="system")


def order_mode_now() -> str:
    """The order mode in force: the default book of a row stamped with one
    (a tradesheet, an evening's valuation) when its writer names none."""
    return current().order_mode


def _snapshot(config: ConfigurationVersion) -> dict[str, Any]:
    return {field: str(getattr(config, field)) for field in sorted(MUTABLE_FIELDS)}


def update(*, changed_by: str, reason: str = "", **changes: Any) -> ConfigurationVersion:
    """Append a new configuration version and audit the change.

    Unknown field names raise rather than being ignored: a typo that silently
    changes nothing is the kind of failure that is discovered by a wrong
    trade.

    A refused switch to live is audited *after* its transaction has rolled
    back, not inside it. Recording the attempt in the same transaction that
    the refusal aborts would roll the record back too, and an attempt to trade
    with real money is exactly the event that must survive being refused.
    """
    unknown = set(changes) - MUTABLE_FIELDS
    if unknown:
        raise ConfigurationError(f"Unknown configuration fields: {', '.join(sorted(unknown))}")

    # The band is both the close's sanity check and the market order's
    # protection. At zero the first withholds on any move at all and the
    # second is a protection the exchange refuses, so the close never buys.
    if "fallback_band_pct" in changes and Decimal(str(changes["fallback_band_pct"])) <= 0:
        raise ConfigurationError("fallback_band_pct must be greater than zero.")

    try:
        return _append_version(changed_by=changed_by, reason=reason, changes=changes)
    except _LiveRefused as refusal:
        record(
            "configuration.live_refused",
            actor=changed_by,
            entity_type="configuration.ConfigurationVersion",
            after={"reason": refusal.why, "registered_static_ip": refusal.registered},
        )
        raise ConfigurationError(f"Refusing to switch to live orders: {refusal.why}.") from refusal


@transaction.atomic
def _append_version(
    *, changed_by: str, reason: str, changes: dict[str, Any]
) -> ConfigurationVersion:
    # Serialise version allocation so two concurrent changes cannot both claim
    # the same number, and so the precondition below is checked against the
    # row that will actually be superseded.
    #
    # An advisory lock rather than SELECT ... FOR UPDATE: this table is
    # append-only, which means the runtime role has UPDATE revoked on it, and
    # Postgres requires UPDATE privilege for a row lock. Locking a row we are
    # forbidden -- by design -- to update was never the right instrument. The
    # lock is transaction-scoped and released with it.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_VERSION_LOCK])

    previous = current()

    proposed = {field: getattr(previous, field) for field in MUTABLE_FIELDS}
    proposed.update(changes)

    going_live = proposed["order_mode"] == OrderMode.LIVE and previous.order_mode != OrderMode.LIVE
    if going_live:
        _refuse_unless_live_is_permitted(proposed)

    version = ConfigurationVersion.objects.create(
        version=previous.version + 1,
        changed_by=changed_by,
        reason=reason,
        **proposed,
    )

    record(
        "configuration.changed",
        actor=changed_by,
        entity=version,
        before=_snapshot(previous),
        after=_snapshot(version),
    )
    return version


def _refuse_unless_live_is_permitted(proposed: dict[str, Any]) -> None:
    """M19b: live is refused unless the registered address is recorded and is
    the one this system calls out from -- and, once it is, unless the books
    are ready for it (below).

    Only the switch *into* live is gated. Once live, an unrelated change --
    the daily amount, the reserve threshold -- is not re-gated, because a
    network blip should not lock the operator out of their own settings. The
    placement-time check in `broker.mode` is what protects the orders.
    """
    from broker import network  # imported late: reads settings at call time

    registered = proposed.get("registered_static_ip")
    if not registered:
        raise _LiveRefused("no static IP is recorded in configuration", registered)

    observed = network.outbound_ip()
    if observed is None:
        raise _LiveRefused(
            "the outbound address could not be determined, so it cannot be "
            "shown to match the registered one",
            registered,
        )

    if observed != registered:
        raise _LiveRefused(
            f"this system calls out from {observed}, not the registered {registered}",
            registered,
        )

    _refuse_unless_the_books_are_ready(registered)


def _refuse_unless_the_books_are_ready(registered: str) -> None:
    """B1: the go-live procedure (DEPLOY.md §8), enforced where it can be.

    The live book must hold its opening entry: without it the live book is a
    fragment of Zerodha's cash, the morning balance check and the funding
    warnings stay silent, and every live allocation overdraws.

    And the switch waits for a paper day in progress to settle. The day's
    budget is allocated in the book of the mode that planned it, and a mode
    is fixed on each order when it is placed: switching after the 09:45 paper
    plan -- or with a paper order still resting -- would leave the day's money
    in the paper book while live orders spend from a live engine that was
    never given it.
    """
    from audit.context import today_ist
    from engine.ledger import day_settled
    from engine.models import TradeSheet
    from journal.external import live_book_opened
    from journal.models import Book, EntryKind, JournalEntry
    from orders.models import Order
    from orders.state import TERMINAL

    if not live_book_opened():
        raise _LiveRefused(
            "the live book has no opening entry; run open_books first (DEPLOY.md §8)",
            registered,
        )

    # Any order of any date still open, before today's settle can excuse
    # anything (m-5): an order left open on an earlier day is not closed by
    # today's row.
    if Order.objects.exclude(state__in=list(TERMINAL)).exists():
        raise _LiveRefused(
            "a paper order is still open; switch after today's settle (DEPLOY.md §8)",
            registered,
        )

    today = today_ist()
    # Today's own book's row: the paper day it was planned as (M1).
    if day_settled(today):
        return
    planned = TradeSheet.objects.filter(trading_date=today).exists()
    allocated = JournalEntry.objects.filter(
        book=Book.PAPER, kind=EntryKind.ALLOCATION, occurred_on=today
    ).exists()
    if planned or allocated:
        raise _LiveRefused(
            "today was already planned or funded in paper; switch after today's settle "
            "and before the next 09:45 (DEPLOY.md §8)",
            registered,
        )
