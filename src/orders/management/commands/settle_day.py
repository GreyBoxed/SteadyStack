"""The evening's close.

Reconciles against the broker, sums the fills, writes the ledger row, settles
the tradesheet's state, and then checks the two records against each other --
holdings against lots, and lots against their realised rows. Both checks are
here rather than anywhere else because the evening is when a disagreement is
cheap to notice and expensive to miss.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from audit.context import today_ist
from broker import get_broker, session
from broker.exceptions import BrokerError
from engine import settlement
from engine.ledger import LedgerError
from gains import fifo
from orders import idempotency
from orders.models import DAILY_BUY_STREAM
from records import divergence
from trading_calendar.sessions import is_trading_day, now_ist, previous_trading_day, session_for

#: How long the boot catch-up waits for the order lock. Nothing retries it,
#: so a round holding the lock for a moment must not make it give up (Nm4).
CATCH_UP_LOCK_WAIT_SECONDS = 120


def unseen_refusal(trading_date, orders, *, claim_failed: bool = False) -> str:
    """What to do about a past day's live orders still open here (I-1).

    With *claim_failed* -- an earlier settle of the day failed and holds its
    claim -- the named route releases it too (`--retry`, m-3)."""
    tags = ", ".join(order.tag for order in orders)
    retry = " --retry" if claim_failed else ""
    return (
        f"{len(orders)} live order(s) of {trading_date} are still open ({tags}), and Kite "
        "no longer holds them to say whether they filled; nothing was settled. Import "
        f"{trading_date}'s tradebook first (import_tradebook, OPERATIONS.md §7), which levels "
        f"them with their fills -- or, once Console's tradebook shows none of them traded, "
        f"run settle_day --date {trading_date}{retry} --level-unseen --actor NAME."
    )


class Command(BaseCommand):
    help = "Settle the day: ledger, state, and the reconciliation checks."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", dest="trading_date")
        parser.add_argument(
            "--previous",
            action="store_true",
            help=(
                "Settle the latest trading day whose close has passed -- today after its "
                "close, else the trading day before -- the boot-time catch-up "
                "(settle-catchup.service), with --if-unsettled. A day with no tradesheet "
                "gives way to the trading day before when that one is planned and has no "
                "row; an older unsettled day is named and refused; else it is reported and "
                "left unclaimed."
            ),
        )
        parser.add_argument("--stream", default=DAILY_BUY_STREAM)
        parser.add_argument(
            "--retry",
            action="store_true",
            help="Release a failed settle's claim and try again. Refused once the row exists.",
        )
        parser.add_argument(
            "--if-unsettled",
            action="store_true",
            help=(
                "For the evening retry timer: settle unless the day's row exists, releasing "
                "a failed settle's claim first. A no-op once the day has settled. A live day "
                "must settle before midnight IST, while Kite's trade book still holds its "
                "trades; after that, import_tradebook."
            ),
        )

        parser.add_argument(
            "--level-unseen",
            action="store_true",
            help=(
                "With --date or --previous, for a past live day whose orders are still open "
                "here: level them from their recorded fills without asking Kite. Only once "
                "Console's tradebook shows none of them traded -- otherwise import_tradebook, "
                "which levels them with their fills. Audited with --actor."
            ),
        )
        parser.add_argument("--actor", help="Who checked Console, for --level-unseen.")
        parser.add_argument(
            "--trades-only",
            action="store_true",
            help=(
                "With --date, for a day a later day's row has already counted: post only "
                "its trades' settlement entry. Its own row can no longer be written."
            ),
        )

    def handle(self, *args, **options) -> None:
        if options["trades_only"] and not options["trading_date"]:
            raise CommandError("--trades-only settles a past day; name it with --date.")
        if options["previous"] and options["trading_date"]:
            raise CommandError("Give --date or --previous, not both.")
        if options["level_unseen"]:
            if not (options["trading_date"] or options["previous"]):
                raise CommandError(
                    "--level-unseen levels a past day; name it with --date or --previous."
                )
            if not options["actor"]:
                raise CommandError(
                    "--level-unseen is audited: say who checked Console with --actor."
                )
        if options["trading_date"]:
            trading_date = date.fromisoformat(options["trading_date"])
        elif options["previous"]:
            trading_date = self._latest_closed_day()
            if trading_date is None:
                self.stdout.write("No trading day in the last month; nothing to settle.")
                return
            trading_date = self._catch_up_day(trading_date)
        else:
            trading_date = today_ist()
            self._refuse_before_the_close(trading_date)

        if not is_trading_day(trading_date):
            self.stdout.write(f"{trading_date} is not a trading day.")
            return
        if options["level_unseen"] and trading_date >= today_ist():
            raise CommandError(
                f"--level-unseen is for a past day; Kite still holds {trading_date}'s orders, "
                "and the settle cancels them there."
            )

        # Everything this settle writes -- the sweep's cancels, the batches it
        # closes, the row -- is the settled day's, whenever it runs.
        from audit.context import bind_trading_day, reset_trace_id

        token = bind_trading_day(trading_date)
        try:
            self._settle(trading_date, options)
        finally:
            reset_trace_id(token)

    def _settle(self, trading_date, options) -> None:
        level_unseen = options["actor"] if options["level_unseen"] else None
        # The boot catch-up is never retried: it waits its turn for the lock.
        lock_wait = CATCH_UP_LOCK_WAIT_SECONDS if options["previous"] else 0
        if options["trades_only"]:
            self._trades_only(trading_date, level_unseen=level_unseen)
            return
        if options["previous"] and not self._planned(trading_date):
            # Down all that day, or a database just recreated: nothing to
            # settle, and no claim or failure for a day already alarmed.
            self.stdout.write(f"{trading_date} has no tradesheet; nothing to settle.")
            return
        if options["if_unsettled"]:
            if self._settled(trading_date):
                self.stdout.write(f"{trading_date} has already settled; nothing to do.")
                return
            self._release_failed_claim(trading_date, wait_seconds=lock_wait)
        elif options["retry"]:
            if self._settled(trading_date):
                raise CommandError(
                    f"{trading_date} has already settled; there is nothing to retry."
                )
            self._release_failed_claim(trading_date)

        broker = get_broker()
        has_session = session.attach(broker)

        self._sweep(trading_date, broker=broker, level_unseen=level_unseen, lock_wait=lock_wait)

        try:
            with idempotency.once(
                trading_date=trading_date,
                task=idempotency.SETTLE,
                tick_no=0,
                lock_wait=lock_wait,
            ):
                result = settlement.settle(
                    trading_date=trading_date,
                    broker=broker,
                    stream=options["stream"],
                    reconcile_first=has_session,
                )
        except idempotency.AlreadyRan as ran:
            held_by = self._claim_holder(trading_date)
            if held_by == "running":
                raise CommandError(
                    f"Not settled: {trading_date}'s settle claim is unfinished, and the order "
                    "lock is held by another task; try again shortly."
                ) from ran
            if held_by == "failed":
                raise CommandError(
                    f"{trading_date}: a failed settle holds the claim; run with --retry "
                    f"(settle_day --date {trading_date} --retry)."
                ) from ran
            self.stdout.write(str(ran))
            return
        except idempotency.Busy as busy:
            raise CommandError(f"Not settled: {busy} Try again in a moment.") from busy
        except settlement.NothingToSettle as nothing:
            # The claim is recorded failed, so a settle once the day is
            # planned -- the retry timer's, or --retry -- still has it.
            self.stdout.write(f"No tradesheet to settle. {nothing}")
            return
        except BrokerError as failure:
            raise CommandError(f"The day could not be settled: {failure}") from failure
        except LedgerError as refused:
            raise CommandError(
                f"The day could not be settled: {refused} If a later day's row already "
                f"counted it, run settle_day --date {trading_date} --trades-only to post "
                "its trades' settlement entry."
            ) from refused

        self.stdout.write(
            f"{trading_date}: {result.tradesheet.state}, "
            f"{result.filled_quantity} filled, spent {result.cash_spent}, "
            f"carry {result.carry_out}"
        )

        if has_session:
            self._resync_live_holdings(trading_date, broker=broker)

        scan = divergence.scan(detected_on=trading_date)
        for opened in scan.opened:
            self.stderr.write(self.style.ERROR(f"Divergence: {opened}"))

        drift = fifo.check_projection()
        if drift:
            self.stderr.write(
                self.style.ERROR(f"{len(drift)} lots disagree with their realised rows.")
            )

    @staticmethod
    def _latest_closed_day():
        """--previous: today once its close has passed, else the trading day
        before (m-1). A box back on D after the 22:30 retry settles D."""
        today = today_ist()
        if is_trading_day(today):
            closes_at = session_for(today).closes_at
            if closes_at is not None and now_ist().time() >= closes_at:
                return today
        return previous_trading_day(today)

    def _catch_up_day(self, day):
        """--previous: *day*, unless it was never planned and an older day is
        still unsettled (gap m2).

        The latest planned day before it, when it has no row, is the day the
        catch-up exists for. One trading day back is settled here; anything
        older is beyond the catch-up's reach, and the command is named and
        refused, so the unit fails and alarms. With no such day, *day* is
        returned and reported as having no tradesheet.
        """
        from engine.models import TradeSheet

        if self._planned(day):
            return day
        latest = (
            TradeSheet.objects.filter(trading_date__lt=day)
            .order_by("-trading_date")
            .values_list("trading_date", flat=True)
            .first()
        )
        if latest is None or self._settled(latest):
            return day
        if latest == previous_trading_day(day):
            self.stdout.write(f"{day} has no tradesheet; settling {latest}, which has no row.")
            return latest
        raise CommandError(
            f"{day} has no tradesheet, and {latest} is still unsettled -- more than one "
            f"trading day back, beyond the catch-up's reach. Run settle_day --date {latest} "
            "--retry (OPERATIONS.md §1)."
        )

    @staticmethod
    def _planned(trading_date) -> bool:
        from engine.models import TradeSheet

        return TradeSheet.objects.filter(trading_date=trading_date).exists()

    @staticmethod
    def _claim_holder(trading_date) -> str | None:
        """Who holds the day's settle claim, when it is not a run that wrote
        its row (Nm1): "failed" for one that failed or died part way, and
        "running" for an unfinished one whose lock is still held (m-2).

        `once` holds the order lock from before its claim until it records
        the outcome, so an unfinished claim is dead only while the lock can
        be taken without waiting -- as `_release_failed_claim` reasons.
        """
        from orders.models import TaskRun

        held = (
            TaskRun.objects.filter(trading_date=trading_date, task=idempotency.SETTLE, tick_no=0)
            .exclude(succeeded=True)
            .values_list("succeeded", flat=True)
        )
        if not held:
            return None
        if False in held:
            return "failed"
        try:
            with idempotency.order_lock():
                return "failed"
        except idempotency.Busy:
            return "running"

    @staticmethod
    def _claim_left(trading_date) -> bool:
        """Under the order lock: any settle claim of the day not marked
        succeeded is one a failed or dead run left (m-2)."""
        from orders.models import TaskRun

        return (
            TaskRun.objects.filter(trading_date=trading_date, task=idempotency.SETTLE, tick_no=0)
            .exclude(succeeded=True)
            .exists()
        )

    @staticmethod
    def _refuse_before_the_close(today) -> None:
        """Without --date the command settles today, which only means
        something once today's session has closed (NI3)."""
        closes_at = session_for(today).closes_at
        if closes_at is not None and now_ist().time() < closes_at:
            raise CommandError(
                f"It is before the close ({closes_at:%H:%M} IST): today has nothing to settle "
                "yet. Name a past day with --date, or --previous for the last trading day."
            )

    def _trades_only(self, trading_date, *, level_unseen: str | None = None) -> None:
        """NI1: the settlement entry of a day behind a later row, and nothing else.

        With *level_unseen*, the day's live orders still open are levelled
        first, as the full settle's sweep does (I-1)."""
        from orders import placement
        from orders.models import Order, OrderMode
        from orders.state import TERMINAL

        try:
            with idempotency.order_lock(wait_seconds=30):
                if level_unseen:
                    for order in (
                        Order.objects.filter(trading_date=trading_date, mode=OrderMode.LIVE)
                        .exclude(state__in=list(TERMINAL))
                        .order_by("pk")
                    ):
                        placement.expire_unseen(
                            order,
                            detail="the day ended; Console shows it untraded",
                            actor=level_unseen,
                        )
                posted = settlement.settle_trades_behind(trading_date)
        except idempotency.Busy as busy:
            raise CommandError(f"Not settled: {busy} Try again in a moment.") from busy
        except LedgerError as refused:
            raise CommandError(f"Nothing was posted: {refused}") from refused
        numbers = ", ".join(f"{entry.book} #{entry.number}" for entry in posted) or "none owed"
        self.stdout.write(f"{trading_date}: trades settled ({numbers}); no row is written.")

    def _sweep(
        self, trading_date, *, broker, level_unseen: str | None = None, lock_wait: float = 0
    ) -> None:
        """Cancel what still rests, in every stream, then close the day's batches.

        A live order of a past date cannot be cancelled: Kite no longer holds
        it, and its books cannot say whether it filled while nobody watched.
        The settle refuses (I-1) -- the day's tradebook import levels it with
        its fills -- unless *level_unseen* names who read Console's tradebook
        and found none of them traded; then each is levelled from its
        recorded fills (`placement.expire_unseen`), audited with that name.

        Outside the settle's own transaction and before anything is claimed:
        each cancel commits on its own (a paper cancel re-reads its order under
        a row lock, so nothing fills after it), and a cancel the broker will
        not confirm refuses the whole settle -- a ledger row written while a
        sale might still fill would be missing money it can never be corrected
        to include. Under the order lock, waiting for a round in flight.
        """
        from orders import placement
        from orders.models import Order, OrderMode
        from orders.overrides import STOP_WAIT_SECONDS
        from orders.state import TERMINAL
        from rebalance.ladder import close_batches

        unconfirmed = []
        try:
            with idempotency.order_lock(wait_seconds=max(STOP_WAIT_SECONDS, lock_wait)):
                self._reconcile_live(trading_date, broker=broker)
                past = trading_date < today_ist()
                still_open = Order.objects.filter(trading_date=trading_date).exclude(
                    state__in=list(TERMINAL)
                )
                unseen = list(still_open.filter(mode=OrderMode.LIVE)) if past else []
                if unseen and not level_unseen:
                    raise CommandError(
                        unseen_refusal(
                            trading_date, unseen, claim_failed=self._claim_left(trading_date)
                        )
                    )
                for order in still_open.order_by("pk"):
                    if past and order.mode == OrderMode.LIVE:
                        # Kite holds today's orders only; a past day's DAY
                        # order has ended there, and asking fails (NI1).
                        placement.expire_unseen(
                            order,
                            detail="the day ended; Console shows it untraded",
                            actor=level_unseen,
                        )
                        continue
                    try:
                        placement.cancel(order, broker=broker)
                    except BrokerError:
                        unconfirmed.append(order.tag)
                if not unconfirmed:
                    for batch in close_batches(trading_date):
                        self.stdout.write(f"Rebalance batch {batch.stream}: {batch.state}.")
        except idempotency.Busy as busy:
            raise CommandError(f"Not settled: {busy} Try again in a moment.") from busy

        if unconfirmed:
            raise CommandError(
                f"{len(unconfirmed)} order(s) could not be confirmed cancelled "
                f"({', '.join(unconfirmed)}); nothing was settled. Check the broker and run "
                "settle_day again."
            )

    def _resync_live_holdings(self, trading_date, *, broker) -> None:
        """Holdings as the broker has them tonight, for a day that traded live.

        The morning's sync is what the scan would otherwise judge against, and
        a sale made at the broker during the day would not be in it. A paper
        day is left alone: its holdings are the lots', applied at settle.
        """
        from orders.models import Order, OrderMode
        from portfolio import holdings

        if not Order.objects.filter(trading_date=trading_date, mode=OrderMode.LIVE).exists():
            return
        try:
            holdings.sync(broker.holdings())
        except BrokerError as failure:
            self.stderr.write(f"Holdings not re-read; the scan uses the morning's: {failure}")

    def _reconcile_live(self, trading_date, *, broker) -> None:
        """Read the order book before cancelling anything live.

        A postback lost in the afternoon leaves an order open here that Kite
        has completed, and cancelling it fails -- every evening, for good. The
        book says what each order became; only what is still open after it is
        swept. A book that cannot be read is logged and the sweep goes ahead:
        its cancels refuse the settle on their own if they must.
        """
        from django.db import transaction

        from orders import reconcile
        from orders.models import Order, OrderMode
        from orders.state import TERMINAL

        streams = (
            Order.objects.filter(trading_date=trading_date, mode=OrderMode.LIVE)
            .exclude(state__in=list(TERMINAL))
            .values_list("stream", flat=True)
            .distinct()
        )
        for stream in sorted(set(streams)):
            try:
                with transaction.atomic():
                    reconcile.from_broker(trading_date=trading_date, broker=broker, stream=stream)
            except BrokerError as failure:
                self.stderr.write(f"Could not read the order book for {stream}: {failure}")

    @staticmethod
    def _settled(trading_date) -> bool:
        from engine.ledger import day_settled

        return day_settled(trading_date)

    def _release_failed_claim(self, trading_date, *, wait_seconds: float = 0) -> None:
        """Let a settle that failed or died part way run again -- never one that
        wrote its row, and never one still running.

        A settle killed mid-transaction leaves its claim unfinished rather than
        failed. It is released only while this command can hold the order
        lock, which a settle still running would be holding.
        """
        from django.db.models import Q

        from orders.models import TaskRun

        try:
            with idempotency.order_lock(wait_seconds=wait_seconds):
                released, _ = (
                    TaskRun.objects.filter(trading_date=trading_date, task=idempotency.SETTLE)
                    .filter(Q(succeeded=False) | Q(succeeded__isnull=True))
                    .delete()
                )
        except idempotency.Busy as busy:
            raise CommandError(f"Not released: {busy} A settle may still be running.") from busy
        self.stdout.write(f"Released {released} unfinished settle claim(s) for {trading_date}.")
