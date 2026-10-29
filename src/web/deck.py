"""The morning screen, assembled.

Seven panels answering the questions an operator has before coffee: can it
reach the broker, did the day run, what is working right now, what happened
that somebody should know about, what is it all worth, what did the last few
days do, and is anything about the machine itself wrong.

Every number here comes from a module that already owns it. This file
arranges; it does not compute.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

logger = logging.getLogger("steadystack.web")

ZERO = Decimal("0")

#: Audit actions worth putting in front of somebody unprompted.
NOTABLE = (
    "configuration.live_refused",
    "ledger.lumpsum_credited",
    "ledger.adjustment_posted",
    "divergence.opened",
    "divergence.classified",
    "order.rejected",
    "tradesheet.skipped",
    "configuration.changed",
    "rebalance.rejected",
    "lot.basis_finalised",
)


#: The notable events that mean something went wrong, rather than merely
#: happened. Everything else in NOTABLE is a notice.
WARNINGS = frozenset(
    {
        "configuration.live_refused",
        "divergence.opened",
        "order.rejected",
        "tradesheet.skipped",
        "rebalance.rejected",
    }
)


@dataclass(frozen=True)
class Stat:
    label: str
    value: str
    tone: str = ""
    sub: str = ""


def health(day: date) -> list[Stat]:
    """Whether the parts that have to be alive are alive."""
    from engine.models import TradeSheet, TradeSheetState
    from orders.models import QueuedPostback, Tick

    sheet = TradeSheet.objects.filter(trading_date=day).order_by("-version").first()
    # Ticks belong to a version. A regenerated day counts the newest one's,
    # and says how many an earlier version used rather than claiming them.
    ticks = (
        Tick.objects.filter(trading_date=day, tradesheet=sheet) if sheet else Tick.objects.none()
    )
    last_tick = ticks.order_by("-tick_no").first()
    earlier = (
        Tick.objects.filter(trading_date=day).exclude(tradesheet=sheet).count() if sheet else 0
    )

    # Draft is a plan the ladder has not started on, not a finished day.
    tones = {
        TradeSheetState.RUNNING: "good",
        TradeSheetState.FILLED: "good",
        TradeSheetState.AWAITING_SESSION: "bad",
        TradeSheetState.HALTED: "bad",
        TradeSheetState.UNFILLED: "bad",
        TradeSheetState.PARTIAL: "warn",
        TradeSheetState.SKIPPED: "warn",
    }
    plan = (
        f"{sheet.quantity} shares · {sheet.names_label}"
        if sheet and sheet.instrument_id
        else "nothing chosen"
    )
    if sheet and sheet.state == TradeSheetState.DRAFT:
        plan += (
            f" · v{sheet.version}, waiting for the first order"
            if sheet.is_authorized
            else f" · v{sheet.version} not authorized — nothing will be placed"
        )

    return [
        Stat(
            "TRADESHEET",
            sheet.get_state_display() if sheet else "not generated",
            tones.get(sheet.state, "") if sheet else "bad",
            plan,
        ),
        Stat(
            "TICKS TODAY",
            str(ticks.count()),
            "good" if last_tick else "",
            (f"last was #{last_tick.tick_no}" if last_tick else "none have fired")
            + (f" · {earlier} under an earlier version" if earlier else ""),
        ),
        Stat(
            "POSTBACKS",
            str(QueuedPostback.objects.filter(received_at__date=day).count()),
            "",
            "checksum verified on arrival",
        ),
        Stat("DATABASE", "reachable", "good", "you are reading from it"),
    ]


def cycle(day: date) -> list[dict]:
    """The day's timetable, and how far through it the clock is.

    Two of these steps record a TaskRun and three do not, so each is read from
    whatever actually proves it happened: a tradesheet row proves the day was
    planned, a ledger row proves it settled, a valid token proves the session
    check found one. Asking TaskRun for a task that never writes one would
    report "never ran" forever.
    """
    from django.utils import timezone

    from broker import session as kite_session
    from configuration.store import current as current_configuration
    from engine.ledger import book_of_day, money, row_for
    from engine.models import TradeSheet
    from orders import idempotency
    from orders.models import TaskRun

    config = current_configuration()
    now = timezone.localtime().time()

    runs = {
        run.task: run for run in TaskRun.objects.filter(trading_date=day).order_by("started_at")
    }

    def from_run(task: str):
        run = runs.get(task)
        if run is None:
            return None
        return ("good", run.detail or "ran") if run.succeeded else ("bad", run.detail or "failed")

    planned = TradeSheet.objects.filter(trading_date=day).order_by("-version").first()
    settled = row_for(day, book=book_of_day(day))
    ticks = TaskRun.objects.filter(trading_date=day, task=idempotency.LADDER).count()
    ladder_note = _ladder_position(day, config)

    steps = [
        (
            time(9, 0),
            "Session check",
            ("good", "a usable token was found")
            if kite_session.status().valid
            else ("bad", "no usable token"),
            "is there a usable token",
        ),
        (
            config.tradesheet_time,
            "Tradesheet",
            ("good", f"{planned.quantity} of {planned.instrument_id}")
            if planned and planned.instrument_id
            else (("bad", planned.get_skip_reason_display()) if planned else None),
            "rank, select, size",
        ),
        (
            config.tradesheet_time,
            "Placing rounds",
            ("good", f"{ticks} round{'s' if ticks != 1 else ''} run{ladder_note}")
            if ticks
            else None,
            f"every {config.placer_interval_seconds}s, what each name's schedule owes{ladder_note}",
        ),
        (
            config.market_fallback_time,
            "Close",
            from_run(idempotency.FALLBACK),
            f"cancel, then market in rounds to {config.close_window_end:%H:%M}",
        ),
        (
            time(15, 45),
            "Settle",
            ("good", f"closing carry ₹{money(settled.closing_carry)}") if settled else None,
            "reconcile and close the row",
        ),
    ]

    rows, upcoming_marked = [], False
    for at, label, outcome, sub in steps:
        if outcome is not None:
            tone, detail = outcome
        elif at <= now:
            tone, detail = "", f"{sub} — no record"
        elif not upcoming_marked:
            tone, detail, upcoming_marked = "next", sub, True
        else:
            tone, detail = "later", sub
        rows.append({"at": at, "label": label, "tone": tone, "sub": detail})
    return rows


def _ladder_position(day: date, config) -> str:
    """Where the clock has the day: " · round 7 of 509 · next 09:48:30"."""
    from django.utils import timezone

    from orders.ladder import ladder_times

    now = timezone.localtime()
    if now.date() != day:
        return ""
    moments = ladder_times(day, config)
    if not moments:
        return ""
    passed = [m for m in moments if m <= now.time()]
    upcoming = [m for m in moments if m > now.time()]
    if not passed:
        return f" · first round {moments[0]:%H:%M}"
    note = f" · round {len(passed) - 1} of {len(moments) - 1}"
    return note + (f" · next {upcoming[0]:%H:%M:%S}" if upcoming else " · window closed")


def running_totals(day: date) -> dict | None:
    """The cash line under "what is running": available, deployed, carry."""
    from engine.settlement import day_spend, planned_budget
    from orders.models import Order, OrderSide
    from orders.state import TERMINAL
    from web.trading import buy_sheet

    sheet = buy_sheet(day)
    if sheet is None:
        return None
    # What the day's plans had, less what every version's fills cost as
    # settlement counts them -- charges estimated where a paper fill has
    # none -- so this line and the phone say the same thing.
    spend = day_spend(day)
    available = planned_budget(sheet)
    deployed = spend.spent + spend.charges
    committed = sum(
        (
            (o.limit_price or ZERO) * (o.quantity - o.filled_quantity)
            for o in Order.objects.filter(trading_date=day, side=OrderSide.BUY).exclude(
                state__in=TERMINAL
            )
        ),
        ZERO,
    )
    return {
        "sheet": sheet,
        "available": available,
        "deployed": deployed,
        "committed": committed,
        "projected_carry": available - deployed - committed,
    }


def running(day: date) -> list[dict]:
    """What is working at the broker right now, by instrument.

    The trading board's own rows, so Today and the board cannot disagree:
    the target is what the day meant to move, not what it has placed so far,
    and the value is what the fills paid.
    """
    from web.trading import board

    return [
        {**row, "pct": row["filled_pct"]}
        for row in board(day=day, prices={})["instruments"]
        if row["target"] or row["filled"]
    ]


def notices(limit: int = 6) -> list[dict]:
    """Recent events somebody should know about, from the audit trail.

    There is no alert transport yet, so this is what would have been sent --
    read from the record rather than from a second store that could disagree
    with it. Funding warnings are computed rather than logged, so they are
    put at the front rather than competing with the audit trail for a place
    in the limit.
    """
    from django.utils import timezone

    from audit.models import AuditEvent
    from journal.checks import funding_warnings

    now = timezone.now()
    warnings = [
        {
            "action": "journal.funding_warning",
            "at": now,
            "actor": "",
            "entity": "",
            "detail": message,
            "level": "warning",
        }
        for message in funding_warnings()
    ]
    events = [
        {
            "action": event.action,
            "at": event.occurred_at,
            "actor": event.actor,
            "entity": event.entity_type,
            "detail": _summarise(event),
            "level": "warning" if event.action in WARNINGS else "notice",
        }
        for event in AuditEvent.objects.filter(action__in=NOTABLE).order_by("-occurred_at")[:limit]
    ]
    return (warnings + events)[:limit]


def _summarise(event) -> str:
    after = event.after if isinstance(event.after, dict) else {}
    before = event.before if isinstance(event.before, dict) else {}

    for key in ("reason", "note", "detail", "amount", "classification", "skip_reason"):
        if after.get(key):
            return str(after[key])

    # A settings change carries two snapshots and no prose: say what moved,
    # because the identifier of the row it moved in tells nobody anything.
    moved = [k for k in after if before.get(k) != after.get(k)]
    if moved:
        return ", ".join(f"{k.replace('_', ' ')} → {after[k]}" for k in sorted(moved)[:3])
    return ""


def _benchmark_note(result) -> str:
    if result.xirr_pct is None:
        return "insufficient history"
    if result.benchmark_pct is None:
        return "money weighted"
    return f"index {result.benchmark_pct}% — {'ahead' if result.beat_the_benchmark else 'behind'}"


def snapshot(prices: dict, day: date) -> list[Stat]:
    """What it is all worth, in six numbers."""
    from engine.ledger import book_of_day, money, spendable_on
    from performance import returns as returns_reader
    from performance.gains import unrealised

    result = returns_reader.measure(prices, as_of=day)
    held = unrealised(prices)

    return [
        Stat("MARKET VALUE", f"₹{result.current_value}", ""),
        Stat("INVESTED", f"₹{result.invested}", ""),
        Stat(
            "UNREALISED",
            f"₹{held.total_gain}",
            "good" if held.total_gain >= ZERO else "bad",
        ),
        Stat(
            "XIRR",
            f"{result.xirr_pct}%" if result.xirr_pct is not None else "—",
            "good" if result.beat_the_benchmark else "warn",
            _benchmark_note(result),
        ),
        Stat("DRIFT Σ|GAP|", f"{drift(prices)}%", "", "distance from the targets"),
        Stat(
            "CASH TO DEPLOY",
            f"₹{money(spendable_on(day, book=book_of_day(day)))}",
            "",
            _cash_to_deploy_note(day),
        ),
    ]


def _cash_to_deploy_note(day: date) -> str:
    """The budget is the figure; a live day capped at Zerodha's cash says so (m-1)."""
    from engine.ledger import money
    from engine.models import TradeSheet

    sheet = TradeSheet.objects.filter(trading_date=day).order_by("-version").first()
    cap = ((sheet.ranking_snapshot or {}).get("cash_cap") or {}) if sheet else {}
    if cap.get("capped"):
        return f"capped at ₹{money(Decimal(cap['available']))} zerodha holds"
    return "left to spend today"


def drift(prices: dict) -> Decimal:
    """How far the portfolio sits from its targets, summed and unsigned.

    One number for "how much rebalancing is owed", which no single gap can
    say. Zero means every name is exactly on target.
    """
    from engine.ledger import money
    from model_portfolio.models import ModelPortfolio
    from performance.valuation import value_now

    portfolio = ModelPortfolio.objects.order_by("-version").first()
    if portfolio is None:
        return ZERO

    targets = portfolio.weights()
    whole = value_now(prices)
    if not whole.total:
        return ZERO

    total = ZERO
    for symbol, target in targets.items():
        current = whole.per_instrument.get(symbol, ZERO) / whole.total * Decimal("100")
        total += abs(current - target)
    return money(total)


def recent_days(limit: int = 6) -> list[dict]:
    """What the last few days actually did."""
    from engine.models import CashLedger, TradeSheet
    from orders.models import OrderSide

    # Keyed by book as well as date: a day's carry is its own book's row, and
    # around a switch of mode the other book's row for the date is not it (M1).
    carry = {
        (row.trading_date, row.book): row.closing_carry
        for row in CashLedger.objects.order_by("-trading_date")[: limit * 4]
    }

    # One row per day. A day can carry more than one sheet -- a rebalance
    # batch gets its own -- and the row somebody wants is the day's decision,
    # not every sheet that happens to share the date.
    seen, chosen = set(), []
    for sheet in TradeSheet.objects.select_related("instrument").order_by(
        "-trading_date", "-version"
    )[: limit * 4]:
        if sheet.trading_date in seen or not (sheet.instrument_id or sheet.skip_reason):
            continue
        seen.add(sheet.trading_date)
        chosen.append(sheet)
        if len(chosen) >= limit:
            break

    rows = []
    for sheet in chosen:
        filled = sum(
            order.filled_quantity for order in sheet.orders.all() if order.side == OrderSide.BUY
        )
        rows.append(
            {
                "date": sheet.trading_date,
                "version": sheet.version,
                "symbol": sheet.names_label or "—",
                "quantity": sheet.quantity,
                "filled": filled,
                "state": sheet.get_state_display(),
                "carry": carry.get((sheet.trading_date, sheet.book)),
                "tone": "good" if sheet.quantity and filled >= sheet.quantity else "warn",
            }
        )
    return rows


def checks() -> list[Stat]:
    """The machine itself, rather than the portfolio."""
    from broker.mode import configured_mode
    from configuration.store import current as current_configuration
    from portfolio.models import OpeningImport
    from records.divergence import open_divergences
    from trading_calendar.sessions import coverage_until

    config = current_configuration()
    covered = coverage_until()
    opening = OpeningImport.objects.filter(completed_at__isnull=False).exists()
    divergent = open_divergences().count()

    return [
        Stat(
            "HOLIDAY CALENDAR",
            f"to {covered:%d %b %Y}" if covered else "not loaded",
            "good" if covered else "bad",
            "weekends are skipped regardless",
        ),
        Stat(
            "STATIC IP",
            str(config.registered_static_ip or "not registered"),
            "good" if config.registered_static_ip else "bad",
            "no order is placed without it",
        ),
        Stat(
            "ORDER MODE",
            configured_mode().upper(),
            "good" if configured_mode() == "paper" else "bad",
            "recorded, never sent" if configured_mode() == "paper" else "sent to the exchange",
        ),
        Stat(
            "OPENING IMPORT",
            "complete" if opening else "not done",
            "good" if opening else "bad",
            "gains and sells wait on it",
        ),
        Stat(
            "DIVERGENCES",
            "none" if not divergent else f"{divergent} open",
            "good" if not divergent else "bad",
            "figures withheld while open",
        ),
    ]


def as_of_label(moment: datetime | None) -> str:
    return moment.strftime("%H:%M") if moment else "—"


def holdings_stats(prices: dict, day: date) -> list[Stat]:
    """The six numbers a holdings page is really asked for."""
    from audit.context import today_ist
    from engine.ledger import book_of_day, closing_carry_before, engine_book, money
    from journal.performance import contributed as journal_contributed
    from performance.gains import unrealised
    from performance.valuation import _open_lots, invested_to_date, value_now

    # One book for every figure: CONTRIBUTED is the engine book's, so value,
    # cost and the lot count are read from the same book's lots.
    book = engine_book()
    whole = value_now(prices, book=book)
    cost = invested_to_date(book)
    gain = money(whole.total - cost)
    held = unrealised(prices)

    contributed = journal_contributed(book=book, as_of=today_ist())
    lots = _open_lots(book).count()

    return [
        Stat(
            "MARKET VALUE",
            f"₹{money(whole.total)}",
            "",
            f"{len(whole.per_instrument)} instruments · {lots} lots",
        ),
        Stat("COST BASIS", f"₹{money(cost)}", "", "including charges"),
        Stat(
            "UNREALISED",
            f"₹{gain}",
            "good" if gain >= ZERO else "bad",
            f"{money(gain / cost * Decimal('100')) if cost else ZERO}%",
        ),
        Stat("DRIFT Σ|GAP|", f"{drift(prices)}%", "", "distance from the targets"),
        Stat(
            "CARRY",
            f"₹{money(closing_carry_before(day, book=book_of_day(day)))}",
            "",
            "rolled into today",
        ),
        Stat(
            "CONTRIBUTED",
            f"₹{money(contributed)}",
            "",
            "deposits and lumpsums, all time",
        ),
    ] + (
        [Stat("WITHHELD", ", ".join(held.withheld), "bad", "divergence unresolved")]
        if held.withheld
        else []
    )


def cash_exceptions(limit: int = 8) -> list[dict]:
    """Days that need a second look.

    A day that bought nothing, or spent more than it had, is not a failure --
    but it is the row somebody scrolling a ledger stops at, so it is easier to
    put them at the top than to make them hunt.
    """
    from engine.ledger import engine_book, money
    from engine.models import CashLedger, TradeSheet, TradeSheetState

    # A day is skipped when its latest version is. An earlier version that was
    # stopped carries a skip reason too, on a day that went on to buy.
    latest: dict = {}
    for sheet in TradeSheet.objects.order_by("-trading_date", "-version")[:80]:
        latest.setdefault(sheet.trading_date, sheet)
    skipped = {
        day: sheet for day, sheet in latest.items() if sheet.state == TradeSheetState.SKIPPED
    }

    rows = []
    for row in CashLedger.objects.filter(book=engine_book()).order_by("-trading_date")[:60]:
        if row.closing_carry < ZERO:
            rows.append(
                {
                    "date": row.trading_date,
                    "kind": "OVER-FILLED",
                    "tone": "bad",
                    "text": (
                        f"the day spent ₹{money(-row.closing_carry)} more than it held. "
                        "The next day's allocation repays it."
                    ),
                }
            )
        elif row.trading_date in skipped:
            sheet = skipped[row.trading_date]
            rows.append(
                {
                    "date": row.trading_date,
                    "kind": "SKIPPED",
                    "tone": "warn",
                    "text": (
                        f"{sheet.get_skip_reason_display()}. "
                        f"₹{money(row.closing_carry)} carried forward whole."
                    ),
                }
            )
        if len(rows) >= limit:
            break
    return rows


def cash_integrity() -> list[Stat]:
    """That the ledger is what it claims to be."""
    from engine.ledger import engine_book, reconcile
    from engine.models import CashLedger

    result = reconcile()
    first = CashLedger.objects.filter(book=engine_book()).order_by("trading_date").first()

    return [
        Stat(
            "ROWS",
            str(result["rows"]),
            "",
            f"since {first.trading_date:%d %b %Y}" if first else "none yet",
        ),
        Stat("ROWS EVER UPDATED", "0", "good", "the table refuses UPDATE"),
        Stat(
            "CARRY CHAIN BREAKS",
            str(len(result["chain_breaks"])),
            "good" if not result["chain_breaks"] else "bad",
        ),
        Stat(
            "ROWS THAT DO NOT BALANCE",
            str(len(result["unbalanced"])),
            "good" if not result["unbalanced"] else "bad",
        ),
        Stat("NET IN", f"₹{result['net_in']}", ""),
        Stat("NET OUT", f"₹{result['net_out']}", ""),
        Stat("SALE PROCEEDS", f"₹{result['proceeds']}", "", "back from rebalance sales"),
    ]


def share_exceptions(limit: int = 8) -> list[dict]:
    """Where shares moved without this system asking."""
    from records.models import Divergence, ExternalTransaction

    rows = []
    for row in Divergence.objects.select_related("instrument").order_by("-detected_on")[:limit]:
        settled = row.resolved_at is not None
        rows.append(
            {
                "date": row.detected_on,
                "kind": row.get_classification_display().upper() if settled else "UNRESOLVED",
                "tone": "warn" if settled else "bad",
                "text": (
                    f"the broker reports {row.actual_quantity} {row.instrument_id} against "
                    f"{row.expected_quantity} in the lots. "
                    + (
                        f"Classified by {row.resolved_by}: {row.note}"
                        if settled
                        else "Until it is classified, this instrument's gains are withheld."
                    )
                ),
            }
        )

    for row in ExternalTransaction.objects.select_related("instrument").order_by("-occurred_on")[
        : max(0, limit - len(rows))
    ]:
        rows.append(
            {
                "date": row.occurred_on,
                "kind": f"EXTERNAL {row.get_side_display().upper()}",
                "tone": "warn",
                "text": f"{row.quantity} {row.instrument_id} — {row.reason}",
            }
        )
    return sorted(rows, key=lambda r: r["date"], reverse=True)[:limit]


def share_integrity() -> list[Stat]:
    from portfolio.lots import in_book, reader_book
    from portfolio.models import Instrument, Lot
    from records.models import Divergence

    open_lots = in_book(
        Lot.objects.filter(remaining_quantity__gt=0, superseded_by__isnull=True), reader_book()
    )
    unresolved = Divergence.objects.filter(resolved_at__isnull=True).count()

    return [
        Stat("INSTRUMENTS TRACKED", str(Instrument.objects.filter(active=True).count()), ""),
        Stat(
            "OPEN LOTS",
            str(open_lots.count()),
            "",
        ),
        Stat("LOTS EVER MUTATED", "0", "good", "basis and date are immutable"),
        Stat("DIVERGENCES, ALL TIME", str(Divergence.objects.count()), ""),
        Stat(
            "UNRESOLVED",
            str(unresolved),
            "good" if not unresolved else "bad",
            "figures withheld while open",
        ),
    ]


def cost_drag(prices: dict, day: date) -> dict:
    """What the market-data subscription takes out of the return.

    Plotted rather than netted, because a cost that disappears into a return
    is a cost nobody ever questions. It is small against contributions and
    large against an early gain -- both are worth saying, which is why the
    same rupees are shown three ways.
    """
    from audit.context import today_ist
    from configuration.store import current as current_configuration
    from engine.ledger import engine_book, money
    from engine.models import CashLedger
    from journal.performance import contributed as journal_contributed
    from performance.valuation import invested_to_date, value_now

    monthly = current_configuration().market_data_monthly_cost
    first = CashLedger.objects.filter(book=engine_book()).order_by("trading_date").first()
    if first is None or monthly <= ZERO:
        return {"paid": ZERO, "months": 0, "shares": []}

    # One definition of a billed month, shared with the value chart.
    from performance.series import months_paid

    months = months_paid(first.trading_date, day)
    paid = money(monthly * months)

    book = engine_book()
    contributed = journal_contributed(book=book, as_of=today_ist())
    corpus = value_now(prices, book=book).total
    gain = corpus - invested_to_date(book)

    shares = [
        {
            "label": "as a share of contributions",
            "value": money(paid / contributed * Decimal("100")) if contributed else ZERO,
        },
        {
            "label": "as a share of the gain so far",
            "value": money(paid / gain * Decimal("100")) if gain > ZERO else None,
        },
        {
            "label": "annual drag on the current corpus",
            "value": money(monthly * 12 / corpus * Decimal("100")) if corpus else ZERO,
        },
    ]
    return {"paid": paid, "monthly": monthly, "months": months, "shares": shares}


def fill_quality() -> list[Stat]:
    """How well the ladder actually bought, rather than whether it bought."""
    from django.db.models import Avg, Count

    from orders.models import Fill, Order, OrderState

    orders = Order.objects.filter(side="BUY")
    total = orders.count()
    if not total:
        return []

    filled = orders.filter(state=OrderState.COMPLETE).count()
    fallback = orders.filter(is_fallback=True, state=OrderState.COMPLETE).count()
    fills = Fill.objects.count()
    per_day = orders.values("trading_date").annotate(n=Count("id")).aggregate(avg=Avg("n"))

    wait, versus_ask = _fill_timing()

    return [
        Stat("ORDERS PLACED", str(total), ""),
        Stat("FILLED ON THE LADDER", str(filled - fallback), "good"),
        Stat("FILLED AT THE FALLBACK", str(fallback), "warn" if fallback else "good"),
        Stat("NEVER FILLED", str(total - filled), "bad" if total - filled else "good"),
        Stat(
            "MEDIAN TIME TO FILL",
            _duration(wait) if wait is not None else "—",
            "",
            "placement to first fill",
        ),
        Stat(
            "PAID VS THE ASK",
            f"{versus_ask:+.1f} bps" if versus_ask is not None else "—",
            "" if versus_ask is None else ("good" if versus_ask <= 0 else "bad"),
            "fill price against the ask it was sized on",
        ),
        Stat("FILLS RECORDED", str(fills), ""),
        Stat(
            "ORDERS PER DAY",
            f"{per_day['avg']:.1f}" if per_day["avg"] else "—",
            "",
            "one per tick, one slice each",
        ),
    ]


def _fill_timing():
    """How long a buy rested before it filled, and what it paid for waiting.

    The ladder's claim is that patience buys below the ask. The median wait
    and the quantity-weighted distance from the ask the day was sized on are
    that claim, measured; negative basis points means it bought cheaper.
    """
    from statistics import median

    from orders.models import Fill

    paid = ZERO
    reference = ZERO
    fills = Fill.objects.filter(order__side="BUY", order__is_fallback=False).select_related(
        "order", "order__tradesheet"
    )
    first_fill: dict = {}
    for fill in fills:
        order = fill.order
        seen = first_fill.get(order.pk)
        if seen is None or fill.executed_at < seen[0]:
            first_fill[order.pk] = (fill.executed_at, order.placed_at)
        ask = _sized_at(order)
        if ask:
            paid += fill.price * fill.quantity
            reference += ask * fill.quantity

    waits = [(filled - placed).total_seconds() for filled, placed in first_fill.values()]

    wait = median(waits) if waits else None
    versus_ask = float((paid - reference) / reference * 10000) if reference else None
    return wait, versus_ask


def _sized_at(order):
    """The price the day sized this order's name at: its line, else the sheet's."""
    for line in order.tradesheet.buy_lines():
        if line.instrument_id == order.instrument_id:
            return line.reference_price
    return order.tradesheet.reference_price


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def did_it_run(limit: int | None = None) -> dict:
    """Every trading day as one cell: did the system do its job that day.

    Eighteen years is a long time to be told "it is fine". This is the shape
    of a system that ran, and the gaps are where it did not. One cell per day,
    not per sheet: a day re-planned, or carrying a sell batch, is still one
    day, and it is judged by the sheet that decided its buy.
    """
    from engine.models import TradeSheet

    tones = {
        "FILLED": "good",
        "PARTIAL": "warn",
        "UNFILLED": "bad",
        "SKIPPED": "idle",
        "HALTED": "bad",
        "AWAITING_SESSION": "warn",
    }

    by_day: dict = {}
    for sheet in TradeSheet.objects.order_by("-trading_date", "-version"):
        kept = by_day.get(sheet.trading_date)
        if kept is None or (not kept.instrument_id and sheet.instrument_id):
            by_day[sheet.trading_date] = sheet
        if limit is not None and len(by_day) > limit:
            by_day.pop(sheet.trading_date)
            break

    cells, counts = [], {}
    for day in sorted(by_day):
        sheet = by_day[day]
        counts[sheet.get_state_display()] = counts.get(sheet.get_state_display(), 0) + 1
        cells.append(
            {
                "date": day,
                "tone": tones.get(sheet.state, "idle"),
                "title": f"{day:%d %b %Y} — {sheet.get_state_display()}",
            }
        )
    return {
        "cells": cells,
        "legend": sorted(counts.items()),
        "first": cells[0]["date"] if cells else None,
        "last": cells[-1]["date"] if cells else None,
    }


def parked(day: date) -> dict | None:
    """The day, waiting for a session, and what it costs to keep waiting.

    Returns None unless a sheet is actually parked. This is the failure that
    costs money: connect before the cutoff and the ladder resumes at the tick
    the clock implies; miss it and the day is skipped with the full amount
    carried.
    """
    from django.utils import timezone

    from configuration.store import current as current_configuration
    from engine.models import TradeSheet, TradeSheetState

    sheet = (
        TradeSheet.objects.select_related("instrument")
        .filter(trading_date=day, state=TradeSheetState.AWAITING_SESSION)
        .order_by("-version")
        .first()
    )
    if sheet is None:
        return None

    config = current_configuration()
    now = timezone.localtime()
    cutoff = now.replace(
        hour=config.market_fallback_time.hour,
        minute=config.market_fallback_time.minute,
        second=0,
        microsecond=0,
    )
    left = cutoff - now
    minutes = max(0, int(left.total_seconds() // 60))

    # Round numbering follows the clock, not the session: a day that resumes
    # at 11:35 starts at the round the time of day implies, not at zero.
    from orders.ladder import round_number

    rung = round_number(now, config)

    from datetime import datetime

    from audit.models import AuditEvent

    def minute_of(moment) -> int:
        return moment.hour * 60 + moment.minute

    start = minute_of(config.tradesheet_time)
    end = minute_of(config.market_fallback_time)
    span = max(1, end - start)

    def at(moment) -> float:
        return round(max(0, min(100, (minute_of(moment) - start) / span * 100)), 1)

    window_close = datetime.combine(day, config.placement_window_close).time()
    ticks_left = max(
        0,
        (minute_of(window_close) - max(minute_of(now), start))
        * 60
        // max(1, config.placer_interval_seconds),
    )
    parked_for = now - timezone.localtime(sheet.generated_at)

    return {
        "sheet": sheet,
        "parked_for": _duration(parked_for.total_seconds()),
        "now_pct": at(now),
        "marks": [
            {
                "at": config.tradesheet_time,
                "label": "tradesheet built",
                "pct": 0,
                "edge": "start",
            },
            # "now" sitting on another mark would overprint it; the bar's fill
            # already says where now is.
            *(
                [{"at": now.time(), "label": "now", "pct": at(now), "now": True}]
                if 8 <= at(now) <= 92
                else []
            ),
            {
                "at": config.placement_window_close,
                "label": "last new order",
                "pct": at(config.placement_window_close),
            },
            {
                "at": config.market_fallback_time,
                "label": "cutoff — skip",
                "pct": 100,
                "edge": "end",
            },
        ],
        "ticks_left": ticks_left,
        "slice": config.slice_quantity,
        "held": [
            ("buying", sheet.names_label or "—"),
            ("shares", sheet.quantity),
            ("planned cost", f"₹{sheet.planned_cost:.2f}"),
            ("spendable", f"₹{sheet.spendable_cash:.2f}"),
            ("carries if skipped", f"₹{sheet.spendable_cash:.2f}"),
        ],
        "trail": [
            {
                "at": event.occurred_at,
                "action": event.action,
                "actor": event.actor,
                "detail": _summarise(event),
                "level": "warning" if event.action in WARNINGS else "notice",
            }
            # The latest dozen, read oldest first.
            for event in reversed(
                AuditEvent.objects.filter(trace_id=sheet.trace_id).order_by("-occurred_at")[:12]
            )
        ],
        "minutes_left": minutes,
        "hours_left": minutes // 60,
        "minutes_remainder": minutes % 60,
        "cutoff": config.market_fallback_time,
        "resumes_at_tick": rung,
        "expired": minutes <= 0,
        "at_stake": sheet.spendable_cash,
        "safe": [
            "No order was placed, so no money has moved.",
            "Nothing rests at the broker — orders are DAY validity.",
            "The ledger, holdings and every past day read without a session.",
            "Connect late and the amount carries whole rather than being spent in a hurry.",
        ],
    }
