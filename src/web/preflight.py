"""What is true about this installation, before anybody asks.

Every one of these has cost somebody an hour at least once: a database that
was not up, migrations nobody applied, a Kite session that expired at 06:00,
a mode nobody checked. They are cheap to look at and expensive to discover,
so the server says them on the way up and the day-runner refuses without them.

Severity is the point. A `blocks` problem will stop a day dead; a `warns` one
is worth knowing and will not. Treating the two alike is how a warning trains
somebody to ignore the line that mattered.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger("steadystack.web")


BLOCKS = "blocks"
WARNS = "warns"


@dataclass(frozen=True)
class Check:
    label: str
    value: str
    problem: str = ""
    severity: str = BLOCKS

    @property
    def ok(self) -> bool:
        return not self.problem

    @property
    def blocking(self) -> bool:
        return bool(self.problem) and self.severity == BLOCKS


def run() -> list[Check]:
    """Every check, in the order somebody would want to read them."""
    checks: list[Check] = []

    database = _database()
    checks.append(database)
    if not database.ok:
        # Nothing below can be answered without it, and guessing would be
        # worse than saying so once.
        return checks

    runtime = _runtime_role()
    checks.extend([_migrations(), runtime])
    if _cannot_read(runtime):
        # Every read below would be refused the same way.
        return checks

    checks.extend([_mode(), _session(), _universe(), _calendar()])
    checks.extend(_books())
    checks.append(_settlement_history())
    checks.extend(_schedule())
    return checks


def blockers(checks: list[Check]) -> list[str]:
    """Only what will stop a day. A warning is not one of them."""
    return [check.problem for check in checks if check.blocking]


def warnings(checks: list[Check]) -> list[str]:
    return [check.problem for check in checks if check.problem and not check.blocking]


def _database() -> Check:
    from django.db import connections

    try:
        connections["default"].ensure_connection()
    except Exception as exc:
        return Check(
            "database",
            "unreachable",
            f"{exc.__class__.__name__}. Try: docker compose up -d postgres",
        )
    return Check("database", "reachable")


def _migrations() -> Check:
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor

    try:
        executor = MigrationExecutor(connections["default"])
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as exc:
        return Check("migrations", "unknown", str(exc))

    if plan:
        return Check(
            "migrations",
            f"{len(plan)} unapplied",
            "the schema is behind the code. Try: manage.py migrate",
        )
    return Check("migrations", "up to date")


#: What the runtime role must hold on every append-only table: it reads
#: them and appends to them. What a migration revokes it must not hold.
_RUNTIME_NEEDS = ("SELECT", "INSERT")


def _runtime_role() -> Check:
    """Whether this connection has the runtime role's privileges (NB1).

    Asked on the connection the app and every timer use, of the tables the
    migrations narrow: SELECT and INSERT held, and each revoked privilege
    refused. A database recreated without bootstrap's default grants fails
    the first half -- every page and timer is refused -- and a DATABASE_URL
    pointed at the owner to get past that fails the second: the append-only
    tables would take an UPDATE and nothing else would say so. The test
    suite, connecting as the owner, cannot show either.
    """
    from django.db import connections

    from config.runtime_role import revoked_privileges

    wrong: dict[str, list[str]] = {}
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT current_user")
        user = cursor.fetchone()[0]
        for table, revoked in sorted(revoked_privileges().items()):
            cursor.execute("SELECT to_regclass(%s)", [table])
            if cursor.fetchone()[0] is None:
                # A table a later migration dropped, its revoke with it.
                continue
            for privilege in (*_RUNTIME_NEEDS, *sorted(revoked)):
                cursor.execute("SELECT has_table_privilege(%s, %s)", [table, privilege])
                held = cursor.fetchone()[0]
                if held == (privilege in revoked):
                    verdict = f"may {privilege}" if held else f"may not {privilege}"
                    wrong.setdefault(verdict, []).append(table)

    if wrong:
        said = "; ".join(f"{verdict} {', '.join(tables)}" for verdict, tables in wrong.items())
        return Check(
            "runtime role",
            f"{user}: wrong privileges",
            f"{user} {said}. The runtime connection must be steadystack_app with bootstrap's "
            "grants, applied before the migrations' REVOKEs (DEPLOY.md §8 step 2).",
        )
    return Check("runtime role", f"{user}: reads and appends; append-only tables refuse rewrites")


def _cannot_read(check: Check) -> bool:
    return "may not SELECT" in check.problem


def _mode() -> Check:
    """The mode, and -- when it is live -- the same gate every placement passes.

    A recorded address is not a matching one: an elastic IP lost and
    reassigned (D12) refuses every order, so LIVE is said only when the
    placement gate itself would let an order through.
    """
    from broker.mode import configured_mode, live_permission
    from configuration.store import current

    if configured_mode() == "paper":
        return Check("order mode", "paper — recorded, never sent")

    ip = current().registered_static_ip
    permission = live_permission()
    if not permission:
        return Check(
            "order mode",
            "live, with no registered address" if not ip else "live, but refused",
            f"live is configured but every order will be refused: {permission.reason}.",
        )
    return Check("order mode", f"LIVE — orders go to the broker from {ip}")


def _session() -> Check:
    from broker import session as kite_session

    status = kite_session.status()
    if status.valid:
        return Check("kite session", f"valid until {status.expires_at:%H:%M on %a}")
    return Check(
        "kite session",
        "none",
        f"{status.reason or 'no session'} — the tradesheet needs a live price "
        f"to size against the ask. Sign in at /broker/kite/login/.",
    )


def _universe() -> Check:
    from model_portfolio.models import ModelPortfolio
    from portfolio.models import Instrument

    portfolio = ModelPortfolio.objects.order_by("-version").first()
    if portfolio is None:
        return Check(
            "model portfolio",
            "none uploaded",
            "there is nothing to rank. Upload one at /model-portfolio/.",
        )
    known = Instrument.objects.filter(active=True).count()
    return Check(
        "model portfolio",
        f"v{portfolio.version}, {len(portfolio.weights())} names ({known} instruments known)",
    )


def _calendar() -> Check:
    from trading_calendar.sessions import coverage_until

    covered = coverage_until()
    if covered is None:
        return Check(
            "holiday calendar",
            "not loaded",
            "weekends are still skipped, but a holiday the calendar does not "
            "know about will be traded. Load one with manage.py load_calendar.",
            WARNS,
        )
    return Check("holiday calendar", f"to {covered:%d %b %Y}")


def _books() -> list[Check]:
    """Whether the journal and the lots tell the same story, and -- in live
    mode -- whether the live book has been opened, the one blocker here.

    Paper lots written before the journal existed have no `fill` entry: the
    engine 0010 migration dropped the old cash ledger they were paid from, so
    paper carry restarted at nought and paper performance counts their value
    with none of their cost. And the journal's Investments per name should
    match the open lots at cost; the live book is compared only once opened,
    since before go-live it holds only what was recorded so far.
    """
    from broker.mode import configured_mode
    from journal.checks import investments_disagree
    from journal.external import live_book_opened
    from journal.models import JournalEntry
    from orders.models import OrderMode
    from portfolio.models import Lot

    opened = live_book_opened()
    if configured_mode() != OrderMode.LIVE:
        live_book = Check("live book", "opened" if opened else "not opened (paper)")
    elif opened:
        live_book = Check("live book", "opened")
    else:
        # The one blocker here (B1): live orders would spend from a book with
        # no opening cash, and the checks that would say so wait for it.
        live_book = Check(
            "live book",
            "not opened",
            "live is configured but the live book has no opening entry, so the balance "
            "check and funding warnings are silent. Run manage.py open_books "
            "(DEPLOY.md §8).",
        )

    journalled = JournalEntry.objects.filter(source_type="fill").values_list("source_id", flat=True)
    stale = (
        Lot.objects.filter(created_from_fill__order__mode=OrderMode.PAPER)
        .exclude(created_from_fill_id__in=list(journalled))
        .count()
    )
    plural = "s" if stale != 1 else ""
    if stale:
        history = Check(
            "paper history",
            f"{stale} lot{plural} before the journal",
            f"{stale} paper lot{plural} predate the journal. Run manage.py seed_demo "
            "--reset, or accept that paper performance is meaningless.",
            WARNS,
        )
    else:
        history = Check("paper history", "every paper lot journalled")

    books = [OrderMode.PAPER] + ([OrderMode.LIVE] if opened else [])
    differing = sorted(
        f"{symbol} ({book})" for book in books for symbol in investments_disagree(book=book)
    )
    if differing:
        investments = Check(
            "investments",
            f"{len(differing)} name{'s' if len(differing) != 1 else ''} differ",
            "the journal's Investments and the open lots disagree at cost for "
            f"{', '.join(differing)}. See Books.",
            WARNS,
        )
    else:
        investments = Check("investments", "journal agrees with the lots")
    return [live_book, history, investments]


def _settlement_history() -> Check:
    """Any tradesheet dated before today whose own book never got a
    `CashLedger` row (gap m2, Minor 4).

    The boot catch-up (`settle_day --previous`) reaches one trading day back
    on its own; anything older -- a paper-era sheet left over from before
    go-live, most likely -- sits unsettled until somebody runs `settle_day
    --date <day> --retry` by hand, and this is what says so before it is
    missed (DEPLOY.md §8 step 7).
    """
    from audit.context import today_ist
    from engine.ledger import book_of_day, day_settled
    from engine.models import TradeSheet

    today = today_ist()
    days = sorted(
        TradeSheet.objects.filter(trading_date__lt=today)
        .values_list("trading_date", flat=True)
        .distinct()
    )
    unsettled = [(day, book_of_day(day)) for day in days if not day_settled(day)]
    if not unsettled:
        return Check("settlement history", "every past tradesheet settled")

    plural = "s" if len(unsettled) != 1 else ""
    named = ", ".join(f"{day} ({book})" for day, book in unsettled)
    return Check(
        "settlement history",
        f"{len(unsettled)} unsettled day{plural}",
        f"tradesheet{plural} dated before today with no ledger row in its book: {named}. "
        "Run settle_day --date <day> --retry for each (OPERATIONS.md §1).",
        WARNS,
    )


def _schedule() -> list[Check]:
    """The times the day will act on, since nothing here enforces them."""
    from configuration.store import current

    config = current()
    return [
        Check("tradesheet at", str(config.tradesheet_time)),
        Check(
            "placing rounds",
            f"every {config.placer_interval_seconds}s until {config.placement_window_close}, "
            f"at most {config.max_orders_per_window} orders per "
            f"{config.order_rate_window_seconds}s",
        ),
        Check(
            "close",
            f"market in rounds {config.market_fallback_time} to {config.close_window_end}",
        ),
        Check("a day's budget", f"Rs {config.daily_amount}"),
    ]


def next_steps(checks: list[Check], base_url: str = "") -> list[str]:
    """What to actually do, given what is true.

    The state of the installation is only half of what somebody starting the
    server wants; the other half is the command they were about to go looking
    for. Shown together because that is where they are standing.

    Columns are measured, not guessed: a URL longer than a hardcoded pad runs
    into its own description.
    """
    blocking = {check.label for check in checks if check.blocking}

    if "database" in blocking:
        return ["docker compose up -d postgres", "manage.py migrate"]

    steps: list[tuple[str, str]] = []

    if "migrations" in blocking:
        steps.append(("manage.py migrate", "apply the schema first"))
    if "model portfolio" in blocking:
        steps.append((f"{base_url}/model-portfolio/", "upload target weights"))
    if "kite session" in blocking:
        steps.append((f"{base_url}/broker/kite/login/", "sign in — a day needs this"))

    steps += [
        ("./ops/local/run_day.sh", "plan today, fire one tick"),
        ("./ops/local/run_day.sh --watch", "tick to the close, then settle"),
        ("./ops/local/run_day.sh --settle", "close a day that already ran"),
    ]

    width = max(len(command) for command, _ in steps)
    return [f"{command.ljust(width)}  {note}" for command, note in steps]
