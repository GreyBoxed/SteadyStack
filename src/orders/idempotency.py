"""Running a scheduled task exactly once.

Two guards, because they fail differently.

The **unique constraint** on `(trading_date, task, stream, tick_no)` is what
makes a repeat harmless: the second attempt loses the insert and does nothing.
It works across processes, across machines and across a restart, because the
database is the thing that remembers.

The **lock file** is what stops two order-touching tasks interleaving. The
09:45 ladder tick and the 15:00 fallback both fire at 15:00, and without a
shared lock a placement and a cancellation cross: the fallback sizes its
market order against a quantity the tick is in the middle of changing, and the
day over-buys. One lock file for every order-touching task, taken with
`flock -n` so a task that cannot get it gives up rather than queueing behind
the one that has it.

`tick_no` is never null. Postgres does not treat two NULLs as equal, so a
nullable column in a unique constraint protects nothing at all -- every run
would insert happily alongside every other.
"""

import fcntl
import logging
import time
from contextlib import contextmanager, nullcontext
from datetime import date
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.context import current_trace_id
from audit.logging import fields
from orders.models import DAILY_BUY_STREAM, TaskRun

logger = logging.getLogger("steadystack.orders")

DEFAULT_LOCK_PATH = "/tmp/steadystack-orders.lock"  # noqa: S108 -- replaced by settings in Phase 4
#: How often a waiting holder retries the lock.
LOCK_POLL_SECONDS = 0.1

#: The scheduled tasks that record a run. Named here rather than spelled out
#: at each call site: a screen that reports whether a task ran reads these,
#: and a literal in one place and a different literal in another is a panel
#: that quietly says "never ran" forever.
LADDER = "ladder"
FALLBACK = "fallback"
SETTLE = "settle"
#: A rebalance batch's rounds and its close, named apart from the buy's so
#: the buy's round counts and timeline stay the buy's.
REBALANCE = "rebalance"
REBALANCE_CLOSE = "rebalance_close"


class AlreadyRan(Exception):
    """This task, this tick, has already been claimed."""


class Busy(Exception):
    """Another order-touching task holds the lock."""


def lock_path() -> Path:
    return Path(getattr(settings, "ORDER_LOCK_PATH", DEFAULT_LOCK_PATH))


@contextmanager
def order_lock(*, wait_seconds: float = 0):
    """The one lock every order-touching task shares.

    Non-blocking by default, and on purpose: a tick that waits for the
    fallback to finish and then runs is worse than a tick that does not run
    at all, because it places against a day that has already closed. An
    operator's stop is the exception -- it passes `wait_seconds` so that a
    round in flight finishes and is then stopped, rather than the person
    being told to try again.
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    deadline = time.monotonic() + wait_seconds

    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise Busy("Another order-touching task is running.") from exc
                time.sleep(LOCK_POLL_SECONDS)

        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def claim(
    *,
    trading_date: date,
    task: str,
    tick_no: int = 0,
    stream: str = DAILY_BUY_STREAM,
) -> TaskRun:
    """Claim this run, or raise if it has already been claimed.

    The claim is committed in its own transaction before the work starts, so
    that a crash mid-task still leaves the claim behind. A task that ran half
    way and died must not be silently retried by the next tick -- that is what
    the reconciliation path is for.
    """
    try:
        with transaction.atomic():
            return TaskRun.objects.create(
                trading_date=trading_date,
                task=task,
                stream=stream,
                tick_no=tick_no,
                trace_id=current_trace_id(),
            )
    except IntegrityError as exc:
        raise AlreadyRan(
            f"{task} has already run for {trading_date} (stream {stream}, tick {tick_no})."
        ) from exc


def finish(run: TaskRun, *, succeeded: bool = True, detail: str = "") -> TaskRun:
    run.finished_at = timezone.now()
    run.succeeded = succeeded
    run.detail = detail[:255]
    run.save(update_fields=["finished_at", "succeeded", "detail"])
    return run


@contextmanager
def once(
    *,
    trading_date: date,
    task: str,
    tick_no: int = 0,
    stream: str = DAILY_BUY_STREAM,
    take_lock: bool = True,
    lock_wait: float = 0,
):
    """Run the body once, under the shared lock.

    Yields the `TaskRun`. Raises `AlreadyRan` if this tick has been claimed
    and `Busy` if another order-touching task is holding the lock -- both of
    which callers treat as "not my turn", not as failures. A caller that is
    never retried (the boot catch-up) passes *lock_wait* to wait its turn.

    The lock is taken before the claim is written and held until the claim
    is finished (m-2), so a run waiting its turn has claimed nothing, and a
    claim left unfinished while the lock is free belongs to a run that died.

    A failure is logged at ERROR, which alarms, unless the exception says
    it is expected (`log_level`, as `NothingToSettle` does on a fresh
    database). Busy is logged at ERROR too, as "scheduled task busy" -- no
    claim is written for it to be told apart from a claim that succeeded, so
    this line is what still lets a hung lock holder alarm as `task_failed`
    (Minor 1, wave H).
    """
    guard = order_lock(wait_seconds=lock_wait) if take_lock else nullcontext()
    try:
        with guard:
            run = claim(trading_date=trading_date, task=task, tick_no=tick_no, stream=stream)
            try:
                yield run
            except Exception as exc:
                finish(run, succeeded=False, detail=str(exc))
                logger.log(
                    getattr(exc, "log_level", logging.ERROR),
                    "scheduled task failed",
                    extra=fields(task=task, tick_no=tick_no, error=str(exc)),
                )
                raise
            else:
                finish(run, succeeded=True)
    except Busy:
        logger.error(
            "scheduled task busy",
            extra=fields(task=task, trading_date=str(trading_date), stream=stream, tick_no=tick_no),
        )
        raise
