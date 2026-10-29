# Phase 3 — Performance Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Report what the portfolio did and what it is worth — realised and
unrealised gains, XIRR against a benchmark, and the cash and share ledgers —
as tested functions first and dashboards over them.

**Architecture:** A new `performance` app holds every number: `gains.py`
reports from the lots Phase 2 already writes, `xirr.py` is a dependency-free
solver, `returns.py` builds flows from the cash ledger, `valuation.py` prices
the portfolio and snapshots it daily into the one new table (`DailyValuation`,
because `Lot` and `CashLedger` carry no price history), and `ledgers.py`
answers the two ledger surfaces. The `web` app renders them; no view computes
anything it could not also print.

**Tech Stack:** Django 6.1 (server-side rendered, no SPA), Postgres, Decimal
arithmetic throughout, pytest + pytest-django + hypothesis. **No new runtime
dependency** — the XIRR solver is written here rather than pulling in scipy.

**Spec:** `docs/superpowers/specs/2026-09-21-steadystack-prd.md` (M31b, M37, M38,
M39, M40, M40a, M40b, M41, M41a) and the Phase 3 section of
`docs/plans/2026-09-22-feat-daily-equity-accumulation-system-plan.md`.

## Global Constraints

Every task's requirements implicitly include this section.

- **Nothing tax-specific.** No classification, no holding-period threshold, no
  rates, no exemption, no financial-year framing. The report stops at the gain
  (M37, decision 16). A variable named `short_term` anywhere is a bug.
- **Money is `Decimal`.** Cash amounts quantize to 2dp via
  `engine.ledger.money`; per-unit amounts to 4dp via `gains.fifo.money4`.
  Never `float` except inside the XIRR solver, which is explicitly numeric.
- **`PriceTick` does not exist.** There is no price history in the database
  today. `DailyValuation` (Task 1) is the only place one gets written.
- **M31b withholding:** while an instrument carries an unresolved divergence,
  its realised and unrealised figures are *withheld*, not computed.
  Portfolio-level figures are still computed but carry a caveat naming the
  instrument and the quantity in doubt. Use
  `records.divergence.is_in_doubt(instrument_id)` and
  `records.divergence.portfolio_caveats()`.
- **Append-only holds.** `DailyValuation` is one row per trading day, never
  updated. Re-running the snapshot for a day that already has a row is a
  no-op that logs, not an overwrite.
- **Trace id.** Anything written during a trading day stamps
  `audit.context.current_trace_id()` into its `trace_id` column.
- **ruff:** line-length 100, target py312. Add `"performance"` to
  `known-first-party` in `pyproject.toml` (Task 1) or every import in the new
  app sorts wrongly.
- **Commits:** Conventional Commits, validated by `.githooks/commit-msg`.
  Scopes used here: `gains`, `engine`, `config`, `web`. Run
  `/atomic:atomic-commit` rather than raw `git commit`.
- **Tests live beside the app:** `src/performance/tests/test_*.py`. Run with
  `uv run pytest`. If a run was interrupted, the stale `test_steadystack` database
  makes the next run fail with a `django_content_type` FK violation — drop it
  and re-run, the suite is not broken.

---

## File Structure

**New app `src/performance/`**

| File | Responsibility |
|---|---|
| `__init__.py`, `apps.py` | app registration |
| `models.py` | `DailyValuation` — the daily portfolio value and benchmark level series |
| `migrations/0001_initial.py` | its table |
| `valuation.py` | price the portfolio now; write the day's snapshot |
| `gains.py` | realised (M37) and unrealised (M38) reports, with M31b withholding |
| `xirr.py` | the solver: Brent over a bounded bracket, bisection fallback |
| `returns.py` | portfolio flows, portfolio XIRR, benchmark XIRR (M41, M41a) |
| `ledgers.py` | cash ledger surface (M40a), share ledger surface (M40b) |
| `management/commands/snapshot_valuation.py` | the daily snapshot, scheduled in Phase 4 |
| `tests/test_valuation.py`, `test_gains.py`, `test_xirr.py`, `test_returns.py`, `test_ledgers.py` | |

**Modified**

| File | Change |
|---|---|
| `src/config/settings/base.py:27` | add `"performance"` to `INSTALLED_APPS` |
| `pyproject.toml` | add `"performance"` to `known-first-party` |
| `src/configuration/models.py` | `benchmark_symbol` field (Task 8) |
| `src/configuration/migrations/0003_benchmark_symbol.py` | new |
| `src/web/views.py`, `src/web/urls.py` | the dashboards |
| `src/templates/web/*.html` | holdings, performance, cash ledger, share ledger, orders |

Dashboards live in `web` because CLAUDE.md's scope table puts views, templates
and dashboards there. Every number they show comes from `performance`; a view
that computes a gain is in the wrong file.

---

### Task 1: The `performance` app and the daily valuation series

**Files:**
- Create: `src/performance/__init__.py`, `src/performance/apps.py`,
  `src/performance/models.py`, `src/performance/migrations/__init__.py`,
  `src/performance/tests/__init__.py`
- Modify: `src/config/settings/base.py:27-49`, `pyproject.toml` (`known-first-party`)
- Test: `src/performance/tests/test_valuation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `performance.models.DailyValuation` with fields `trading_date`
  (PK, `DateField`), `portfolio_value`, `invested_to_date`, `cash_carry`,
  `benchmark_level` (all `DecimalField(max_digits=16, decimal_places=4)`,
  `benchmark_level` nullable), `holdings_snapshot` (`JSONField`),
  `created_at`, `trace_id`. Ordering `["-trading_date"]`.

- [ ] **Step 1: Create the app package**

```bash
mkdir -p src/performance/migrations src/performance/tests src/performance/management/commands
touch src/performance/__init__.py src/performance/migrations/__init__.py \
      src/performance/tests/__init__.py src/performance/management/__init__.py \
      src/performance/management/commands/__init__.py
```

`src/performance/apps.py`:

```python
from django.apps import AppConfig


class PerformanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "performance"
```

Add `"performance",` to `INSTALLED_APPS` in `src/config/settings/base.py`
after `"gains",`, and `"performance"` to `known-first-party` in
`pyproject.toml`.

- [ ] **Step 2: Write the failing test**

`src/performance/tests/test_valuation.py`:

```python
"""The daily value series.

`Lot` records what a share cost and `CashLedger` what a day spent; neither
records what the portfolio was worth on a given evening. Every chart, the
benchmark comparison and any future backtest need that, and it cannot be
recovered after the fact -- a price not written down on the day is gone.
"""

from datetime import date
from decimal import Decimal

import pytest

from performance.models import DailyValuation

pytestmark = pytest.mark.django_db

SEP = date(2026, 9, 22)


def test_a_day_is_stored_once():
    DailyValuation.objects.create(
        trading_date=SEP,
        portfolio_value=Decimal("1000"),
        invested_to_date=Decimal("900"),
        cash_carry=Decimal("50"),
        benchmark_level=Decimal("24000"),
    )

    assert DailyValuation.objects.get(trading_date=SEP).portfolio_value == Decimal("1000.0000")


def test_a_day_cannot_be_stored_twice():
    from django.db import IntegrityError

    DailyValuation.objects.create(trading_date=SEP, portfolio_value=Decimal("1"))

    with pytest.raises(IntegrityError):
        DailyValuation.objects.create(trading_date=SEP, portfolio_value=Decimal("2"))


def test_the_benchmark_level_may_be_missing():
    """The index is on the same feed as everything else, and a feed can be
    down. A day with no level is recorded rather than skipped, because the
    portfolio value is still worth having."""
    row = DailyValuation.objects.create(trading_date=SEP, portfolio_value=Decimal("1"))

    assert row.benchmark_level is None
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest src/performance/tests/test_valuation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'performance.models'`

- [ ] **Step 4: Write the model**

`src/performance/models.py`:

```python
"""What the portfolio was worth, evening by evening.

One row per trading day, written once and never updated. The system's other
tables answer "what did it cost" and "what does it hold"; this one answers
"what was it worth then", which nothing else can reconstruct afterwards
because a price that was not written down on the day is gone.

The benchmark level rides along for the same reason: M41a judges the return
against holding the index over the same dates, and that needs the index level
*on those dates*, not today's.
"""

import uuid
from decimal import Decimal

from django.db import models


class DailyValuation(models.Model):
    trading_date = models.DateField(primary_key=True)

    portfolio_value = models.DecimalField(
        max_digits=16,
        decimal_places=4,
        help_text="Market value of every open lot at the day's last traded prices.",
    )
    invested_to_date = models.DecimalField(
        max_digits=16, decimal_places=4, default=Decimal("0")
    )
    cash_carry = models.DecimalField(max_digits=16, decimal_places=4, default=Decimal("0"))

    benchmark_level = models.DecimalField(
        max_digits=16,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="The index level, or null when the feed had nothing for it.",
    )

    holdings_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per instrument: quantity and the price it was valued at.",
    )

    id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-trading_date"]

    def __str__(self) -> str:
        return f"{self.trading_date}: {self.portfolio_value}"
```

- [ ] **Step 5: Make and run the migration**

```bash
uv run python manage.py makemigrations performance
uv run pytest src/performance/tests/test_valuation.py -v
```

Expected: PASS (3 tests). The generated file must be
`src/performance/migrations/0001_initial.py`.

- [ ] **Step 6: Commit**

```bash
git add src/performance src/config/settings/base.py pyproject.toml
# then: /atomic:atomic-commit
# message: feat(gains): add the daily portfolio value series
```

---

### Task 2: Price the portfolio, and snapshot the day

**Files:**
- Create: `src/performance/valuation.py`,
  `src/performance/management/commands/snapshot_valuation.py`
- Test: `src/performance/tests/test_valuation.py` (append)

**Interfaces:**
- Consumes: `DailyValuation` (Task 1); `portfolio.lots.open_lots(instrument_id)`,
  `portfolio.models.Lot`, `engine.quotes.fetch(symbols, broker=...) -> dict[str, Quote]`
  where `Quote` has `.last_price` and `.ask`; `engine.ledger.closing_carry_before(day)`.
- Produces:
  - `valuation.held_quantities() -> dict[str, int]` — open lot quantity by symbol.
  - `valuation.value_now(prices: dict[str, Decimal]) -> PortfolioValue` where
    `PortfolioValue` is a frozen dataclass with `total: Decimal`,
    `per_instrument: dict[str, Decimal]`, `priced: set[str]`, `unpriced: set[str]`.
  - `valuation.invested_to_date() -> Decimal` — total basis of open lots.
  - `valuation.snapshot(day, *, prices, benchmark_level=None) -> DailyValuation | None`
    — returns `None` when the day already has a row.

- [ ] **Step 1: Write the failing tests**

Append to `src/performance/tests/test_valuation.py`:

```python
# -- pricing what is held ------------------------------------------------


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(
        tradingsymbol="INFY", instrument_token=408065, name="Infosys"
    )


def a_lot(instrument, *, quantity, cost, acquired_on=SEP):
    from portfolio.models import Lot

    return Lot.objects.create(
        instrument=instrument,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=cost,
    )


def test_the_portfolio_is_worth_what_its_open_lots_are_worth(instrument):
    from performance import valuation

    a_lot(instrument, quantity=3, cost=Decimal("1000"))

    value = valuation.value_now({"INFY": Decimal("1100")})

    assert value.total == Decimal("3300.0000")
    assert value.per_instrument == {"INFY": Decimal("3300.0000")}


def test_a_consumed_lot_is_not_counted(instrument):
    from performance import valuation

    lot = a_lot(instrument, quantity=3, cost=Decimal("1000"))
    lot.remaining_quantity = 1
    lot.save(update_fields=["remaining_quantity"])

    assert valuation.value_now({"INFY": Decimal("1100")}).total == Decimal("1100.0000")


def test_an_unpriced_name_is_named_rather_than_valued_at_nothing(instrument):
    """Valuing a name at zero because the feed was silent understates the
    portfolio and would quietly poison XIRR. It is reported instead."""
    from performance import valuation

    a_lot(instrument, quantity=3, cost=Decimal("1000"))

    value = valuation.value_now({})

    assert value.total == Decimal("0.0000")
    assert value.unpriced == {"INFY"}


def test_invested_to_date_is_the_basis_of_what_is_still_held(instrument):
    from performance import valuation

    a_lot(instrument, quantity=2, cost=Decimal("1000.5000"))

    assert valuation.invested_to_date() == Decimal("2001.0000")


# -- the snapshot --------------------------------------------------------


def test_a_snapshot_records_the_day(instrument):
    from performance import valuation

    a_lot(instrument, quantity=2, cost=Decimal("1000"))

    row = valuation.snapshot(
        SEP, prices={"INFY": Decimal("1100")}, benchmark_level=Decimal("24000")
    )

    assert row.portfolio_value == Decimal("2200.0000")
    assert row.invested_to_date == Decimal("2000.0000")
    assert row.benchmark_level == Decimal("24000.0000")
    assert row.holdings_snapshot["INFY"] == {"quantity": 2, "price": "1100"}


def test_snapshotting_a_day_twice_does_not_rewrite_it(instrument):
    from performance import valuation

    a_lot(instrument, quantity=2, cost=Decimal("1000"))
    valuation.snapshot(SEP, prices={"INFY": Decimal("1100")})

    assert valuation.snapshot(SEP, prices={"INFY": Decimal("9999")}) is None
    assert DailyValuation.objects.get(trading_date=SEP).portfolio_value == Decimal("2200.0000")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest src/performance/tests/test_valuation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'performance.valuation'`

- [ ] **Step 3: Write `valuation.py`**

```python
"""What the portfolio is worth, now and on the evening of a given day.

Worth is always last traded price, never the ask: the ask is what the next
share would cost, which is a different question and the one `engine.selection`
asks. A name the feed has nothing for is reported as unpriced rather than
valued at zero -- a silent zero understates the portfolio and would poison
every number downstream of it.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from audit.context import current_trace_id
from audit.logging import fields
from engine.ledger import closing_carry_before
from gains.fifo import money4
from performance.models import DailyValuation
from portfolio.models import Lot

logger = logging.getLogger("steadystack.performance")

ZERO = Decimal("0")


@dataclass(frozen=True)
class PortfolioValue:
    total: Decimal
    per_instrument: dict[str, Decimal]
    priced: set[str] = field(default_factory=set)
    unpriced: set[str] = field(default_factory=set)


def _open_lots():
    return Lot.objects.filter(remaining_quantity__gt=0, superseded_by__isnull=True)


def held_quantities() -> dict[str, int]:
    """Open quantity by symbol, from the lots rather than from `Holding`.

    The lots are what the gains report and FIFO both read, so valuing against
    anything else would let two of our own numbers disagree.
    """
    held: dict[str, int] = {}
    for lot in _open_lots():
        held[lot.instrument_id] = held.get(lot.instrument_id, 0) + lot.remaining_quantity
    return held


def invested_to_date() -> Decimal:
    """The cost basis still sitting in open lots."""
    total = ZERO
    for lot in _open_lots():
        total += lot.cost_per_unit * lot.remaining_quantity
    return money4(total)


def value_now(prices: dict[str, Decimal]) -> PortfolioValue:
    per_instrument: dict[str, Decimal] = {}
    priced: set[str] = set()
    unpriced: set[str] = set()

    for symbol, quantity in held_quantities().items():
        price = prices.get(symbol)
        if price is None or price <= ZERO:
            unpriced.add(symbol)
            continue
        priced.add(symbol)
        per_instrument[symbol] = money4(price * quantity)

    return PortfolioValue(
        total=money4(sum(per_instrument.values(), ZERO)),
        per_instrument=per_instrument,
        priced=priced,
        unpriced=unpriced,
    )


def snapshot(
    day: date,
    *,
    prices: dict[str, Decimal],
    benchmark_level: Decimal | None = None,
) -> DailyValuation | None:
    """Write the day's value. Returns None if the day already has a row.

    The series is append-only: a day is valued once, on the day, and a second
    run is a no-op rather than a correction. A wrong snapshot is fixed by
    recording why, not by rewriting history.
    """
    if DailyValuation.objects.filter(trading_date=day).exists():
        logger.info("valuation already recorded", extra=fields(trading_date=str(day)))
        return None

    value = value_now(prices)
    held = held_quantities()

    row = DailyValuation.objects.create(
        trading_date=day,
        portfolio_value=value.total,
        invested_to_date=invested_to_date(),
        cash_carry=closing_carry_before(day),
        benchmark_level=money4(benchmark_level) if benchmark_level else None,
        holdings_snapshot={
            symbol: {"quantity": quantity, "price": str(prices.get(symbol, ""))}
            for symbol, quantity in sorted(held.items())
        },
        trace_id=current_trace_id(),
    )

    logger.info(
        "portfolio valued",
        extra=fields(
            trading_date=str(day),
            value=str(value.total),
            unpriced=sorted(value.unpriced),
        ),
    )
    return row
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest src/performance/tests/test_valuation.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Write the management command**

`src/performance/management/commands/snapshot_valuation.py`:

```python
"""Value the portfolio and store the day.

Runs after the close under a systemd timer in Phase 4. Reads only: quotes and
our own lots, then one insert. Safe to run twice -- the second run finds the
day already recorded and does nothing.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from audit.context import today_ist
from broker import get_broker
from broker.exceptions import BrokerError
from configuration.store import current as current_configuration
from engine import quotes as quote_reader
from performance import valuation
from trading_calendar.sessions import is_trading_day


class Command(BaseCommand):
    help = "Record what the portfolio was worth today."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", dest="trading_date", help="YYYY-MM-DD; defaults to today.")

    def handle(self, *args, **options) -> None:
        raw = options.get("trading_date")
        day = date.fromisoformat(raw) if raw else today_ist()

        if not is_trading_day(day):
            self.stdout.write(f"{day} is not a trading day; nothing to value.")
            return

        held = sorted(valuation.held_quantities())
        benchmark = current_configuration().benchmark_symbol
        broker = get_broker()

        try:
            prices = quote_reader.fetch(sorted({*held, benchmark}), broker=broker)
        except BrokerError as exc:
            raise CommandError(f"Could not read prices: {exc}") from exc

        benchmark_quote = prices.get(benchmark)
        row = valuation.snapshot(
            day,
            prices={symbol: quote.last_price for symbol, quote in prices.items()},
            benchmark_level=benchmark_quote.last_price if benchmark_quote else None,
        )

        if row is None:
            self.stdout.write(f"{day} was already valued.")
            return
        self.stdout.write(f"{day}: {row.portfolio_value}")
```

> This command imports `benchmark_symbol`, which Task 8 adds. Implement Task 8
> before running the command; the tests in this task do not touch it.

- [ ] **Step 6: Commit**

```bash
# /atomic:atomic-commit
# message: feat(gains): value the portfolio and record the day
```

---

### Task 3: Realised gains (M37)

**Files:**
- Create: `src/performance/gains.py`, `src/performance/tests/test_gains.py`

**Interfaces:**
- Consumes: `gains.models.RealisedGain` (fields `instrument_id`, `quantity`,
  `acquired_on`, `disposed_on`, `cost`, `net_proceeds`, `gain`,
  `basis_was_estimated`, `reverses`); `engine.ledger.money`.
- Produces:
  - `gains.realised(start=None, end=None, instrument_id=None) -> RealisedReport`
    where `RealisedReport` is a frozen dataclass with `rows: list[RealisedRow]`,
    `total_proceeds: Decimal`, `total_cost: Decimal`, `total_gain: Decimal`,
    `withheld: list[str]`, `any_estimated: bool`.
  - `RealisedRow` — frozen dataclass: `instrument_id`, `quantity`,
    `acquired_on`, `disposed_on`, `cost`, `proceeds`, `gain`, `estimated: bool`.

- [ ] **Step 1: Write the failing test**

`src/performance/tests/test_gains.py`:

```python
"""Realised and unrealised gains, reported as economics and nothing else.

The broker's tax P&L does classification, rates and financial years. This
report exists so those numbers can be checked against records the system can
prove -- which means proceeds, cost basis and gain, and stopping there (M37).
"""

from datetime import date
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

JAN = date(2026, 1, 15)
SEP = date(2026, 9, 22)


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(
        tradingsymbol="INFY", instrument_token=408065, name="Infosys"
    )


def a_realisation(instrument, *, quantity=1, cost="1000", proceeds="1200", disposed_on=SEP,
                  estimated=False):
    from gains.models import RealisedGain
    from portfolio.models import Lot

    lot = Lot.objects.create(
        instrument=instrument,
        acquired_on=JAN,
        quantity=quantity,
        remaining_quantity=0,
        cost_per_unit=Decimal(cost),
    )
    return RealisedGain.objects.create(
        lot=lot,
        instrument=instrument,
        quantity=quantity,
        acquired_on=JAN,
        disposed_on=disposed_on,
        cost=Decimal(cost) * quantity,
        net_proceeds=Decimal(proceeds) * quantity,
        gain=(Decimal(proceeds) - Decimal(cost)) * quantity,
        basis_was_estimated=estimated,
    )


def test_a_sale_is_reported_as_proceeds_cost_and_gain(instrument):
    from performance import gains

    a_realisation(instrument, quantity=2, cost="1000", proceeds="1200")

    report = gains.realised()

    assert report.total_proceeds == Decimal("2400.00")
    assert report.total_cost == Decimal("2000.00")
    assert report.total_gain == Decimal("400.00")
    assert report.rows[0].instrument_id == "INFY"


def test_a_loss_is_reported_as_a_negative_gain(instrument):
    from performance import gains

    a_realisation(instrument, quantity=1, cost="1200", proceeds="1000")

    assert gains.realised().total_gain == Decimal("-200.00")


def test_the_range_is_inclusive_at_both_ends(instrument):
    from performance import gains

    a_realisation(instrument, disposed_on=date(2026, 9, 1))
    a_realisation(instrument, disposed_on=date(2026, 9, 30))

    assert len(gains.realised(start=date(2026, 9, 1), end=date(2026, 9, 30)).rows) == 2
    assert len(gains.realised(start=date(2026, 9, 2), end=date(2026, 9, 29)).rows) == 0


def test_a_reversal_cancels_the_row_it_reverses(instrument):
    """A correction reverses a reported gain rather than deleting it, so the
    report must net the pair to zero instead of showing the sale twice."""
    from performance import gains

    original = a_realisation(instrument, quantity=1, cost="1000", proceeds="1200")
    reversal = a_realisation(instrument, quantity=1, cost="1000", proceeds="1200")
    reversal.gain = -original.gain
    reversal.net_proceeds = -original.net_proceeds
    reversal.cost = -original.cost
    reversal.reverses = original
    reversal.save()

    assert gains.realised().total_gain == Decimal("0.00")


def test_an_estimated_basis_is_flagged(instrument):
    """A lot whose contract note has not landed reports a gain that may move."""
    from performance import gains

    a_realisation(instrument, estimated=True)

    report = gains.realised()

    assert report.any_estimated is True
    assert report.rows[0].estimated is True


def test_the_report_says_nothing_about_tax(instrument):
    """Decision 16: no classification, no rates, no financial year."""
    from performance import gains

    a_realisation(instrument)
    row = gains.realised().rows[0]

    forbidden = {"short_term", "long_term", "tax", "rate", "financial_year", "exemption"}
    assert forbidden.isdisjoint(vars(row))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest src/performance/tests/test_gains.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'performance.gains'`

- [ ] **Step 3: Write `gains.py`**

```python
"""Realised and unrealised gains (M37, M38).

Economics only. The system knows what it paid, what it received and the
difference; it does not know, and deliberately does not guess, how any of that
is taxed. That is the broker's report to make -- this one exists so it can be
checked against records we can prove.

A reversal is a row whose `reverses` points at the row it undoes. Both stay in
the table, because a reported gain is corrected by reversing it rather than by
deleting it, and both are summed here so the pair nets to zero.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from engine.ledger import money
from gains.models import RealisedGain
from records.divergence import is_in_doubt

logger = logging.getLogger("steadystack.performance")

ZERO = Decimal("0")


@dataclass(frozen=True)
class RealisedRow:
    instrument_id: str
    quantity: int
    acquired_on: date
    disposed_on: date
    cost: Decimal
    proceeds: Decimal
    gain: Decimal
    estimated: bool


@dataclass(frozen=True)
class RealisedReport:
    rows: list[RealisedRow]
    total_proceeds: Decimal
    total_cost: Decimal
    total_gain: Decimal
    withheld: list[str]
    any_estimated: bool


def realised(
    start: date | None = None,
    end: date | None = None,
    instrument_id: str | None = None,
) -> RealisedReport:
    """Every disposal in the range, per sale and per lot consumed."""
    query = RealisedGain.objects.select_related("instrument").order_by(
        "disposed_on", "acquired_on"
    )
    if start is not None:
        query = query.filter(disposed_on__gte=start)
    if end is not None:
        query = query.filter(disposed_on__lte=end)
    if instrument_id is not None:
        query = query.filter(instrument_id=instrument_id)

    rows: list[RealisedRow] = []
    withheld: set[str] = set()
    proceeds = cost = gain = ZERO

    for record in query:
        if is_in_doubt(record.instrument_id):
            withheld.add(record.instrument_id)
            continue
        rows.append(
            RealisedRow(
                instrument_id=record.instrument_id,
                quantity=record.quantity,
                acquired_on=record.acquired_on,
                disposed_on=record.disposed_on,
                cost=money(record.cost),
                proceeds=money(record.net_proceeds),
                gain=money(record.gain),
                estimated=record.basis_was_estimated,
            )
        )
        proceeds += record.net_proceeds
        cost += record.cost
        gain += record.gain

    return RealisedReport(
        rows=rows,
        total_proceeds=money(proceeds),
        total_cost=money(cost),
        total_gain=money(gain),
        withheld=sorted(withheld),
        any_estimated=any(row.estimated for row in rows),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest src/performance/tests/test_gains.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
# /atomic:atomic-commit
# message: feat(gains): report realised gains as proceeds, cost and gain
```

---

### Task 4: Unrealised gains (M38)

**Files:**
- Modify: `src/performance/gains.py`
- Test: `src/performance/tests/test_gains.py` (append)

**Interfaces:**
- Consumes: `valuation.held_quantities`, `portfolio.models.Lot`,
  `portfolio.models.BasisState`.
- Produces:
  - `gains.unrealised(prices: dict[str, Decimal]) -> UnrealisedReport` with
    `rows: list[UnrealisedRow]`, `total_cost`, `total_value`, `total_gain`,
    `withheld: list[str]`, `unpriced: list[str]`, `any_estimated: bool`.
  - `UnrealisedRow` — `instrument_id`, `acquired_on`, `quantity`,
    `cost_per_unit`, `cost`, `price`, `value`, `gain`, `estimated: bool`.

- [ ] **Step 1: Write the failing test**

Append to `src/performance/tests/test_gains.py`:

```python
# -- unrealised (M38) ----------------------------------------------------


def an_open_lot(instrument, *, quantity=2, cost="1000", acquired_on=JAN, estimated=False):
    from portfolio.models import BasisState, Lot

    return Lot.objects.create(
        instrument=instrument,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal(cost),
        basis_state=BasisState.ESTIMATED if estimated else BasisState.FINAL,
    )


def test_an_open_lot_is_reported_per_lot(instrument):
    from performance import gains

    an_open_lot(instrument, quantity=2, cost="1000", acquired_on=JAN)

    report = gains.unrealised({"INFY": Decimal("1100")})

    row = report.rows[0]
    assert row.acquired_on == JAN
    assert row.cost == Decimal("2000.00")
    assert row.value == Decimal("2200.00")
    assert row.gain == Decimal("200.00")


def test_a_partly_consumed_lot_reports_only_what_remains(instrument):
    from performance import gains

    lot = an_open_lot(instrument, quantity=4, cost="1000")
    lot.remaining_quantity = 1
    lot.save(update_fields=["remaining_quantity"])

    assert gains.unrealised({"INFY": Decimal("1100")}).total_value == Decimal("1100.00")


def test_an_unpriced_instrument_is_named_not_valued(instrument):
    from performance import gains

    an_open_lot(instrument)

    report = gains.unrealised({})

    assert report.unpriced == ["INFY"]
    assert report.rows == []


def test_lots_are_reported_oldest_first(instrument):
    """The same order FIFO will consume them in, so the report reads as the
    queue it actually is."""
    from performance import gains

    an_open_lot(instrument, acquired_on=SEP)
    an_open_lot(instrument, acquired_on=JAN)

    report = gains.unrealised({"INFY": Decimal("1100")})

    assert [row.acquired_on for row in report.rows] == [JAN, SEP]


def test_an_estimated_basis_is_flagged_here_too(instrument):
    from performance import gains

    an_open_lot(instrument, estimated=True)

    assert gains.unrealised({"INFY": Decimal("1100")}).any_estimated is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest src/performance/tests/test_gains.py -k unrealised -v`
Expected: FAIL — `AttributeError: module 'performance.gains' has no attribute 'unrealised'`

- [ ] **Step 3: Append to `gains.py`**

```python
@dataclass(frozen=True)
class UnrealisedRow:
    instrument_id: str
    acquired_on: date
    quantity: int
    cost_per_unit: Decimal
    cost: Decimal
    price: Decimal
    value: Decimal
    gain: Decimal
    estimated: bool


@dataclass(frozen=True)
class UnrealisedReport:
    rows: list[UnrealisedRow]
    total_cost: Decimal
    total_value: Decimal
    total_gain: Decimal
    withheld: list[str]
    unpriced: list[str]
    any_estimated: bool


def unrealised(prices: dict[str, Decimal]) -> UnrealisedReport:
    """Every open lot at the prices given: acquisition date, basis, value, gain.

    Oldest first, which is the order FIFO will consume them in, so the report
    reads as the queue it actually is.
    """
    from portfolio.models import BasisState, Lot

    open_lots = (
        Lot.objects.filter(remaining_quantity__gt=0, superseded_by__isnull=True)
        .select_related("instrument")
        .order_by("acquired_on", "created_at")
    )

    rows: list[UnrealisedRow] = []
    withheld: set[str] = set()
    unpriced: set[str] = set()
    cost = value = ZERO

    for lot in open_lots:
        if is_in_doubt(lot.instrument_id):
            withheld.add(lot.instrument_id)
            continue

        price = prices.get(lot.instrument_id)
        if price is None or price <= ZERO:
            unpriced.add(lot.instrument_id)
            continue

        lot_cost = lot.cost_per_unit * lot.remaining_quantity
        lot_value = price * lot.remaining_quantity

        rows.append(
            UnrealisedRow(
                instrument_id=lot.instrument_id,
                acquired_on=lot.acquired_on,
                quantity=lot.remaining_quantity,
                cost_per_unit=lot.cost_per_unit,
                cost=money(lot_cost),
                price=price,
                value=money(lot_value),
                gain=money(lot_value - lot_cost),
                estimated=lot.basis_state == BasisState.ESTIMATED,
            )
        )
        cost += lot_cost
        value += lot_value

    return UnrealisedReport(
        rows=rows,
        total_cost=money(cost),
        total_value=money(value),
        total_gain=money(value - cost),
        withheld=sorted(withheld),
        unpriced=sorted(unpriced),
        any_estimated=any(row.estimated for row in rows),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest src/performance/tests/test_gains.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
# /atomic:atomic-commit
# message: feat(gains): report unrealised gains per lot
```

---

### Task 5: Withholding under an unresolved divergence (M31b)

The withholding branches were written in Tasks 3 and 4. This task proves they
hold and that portfolio figures are flagged rather than suppressed.

**Files:**
- Test: `src/performance/tests/test_gains.py` (append)

**Interfaces:**
- Consumes: `records.models.Divergence`, `records.divergence.is_in_doubt`,
  `records.divergence.portfolio_caveats() -> list[dict]`.
- Produces: nothing new.

- [ ] **Step 1: Read the divergence API before writing the test**

Run: `sed -n '160,210p' src/records/divergence.py`
Note the exact shape `portfolio_caveats()` returns and the fields needed to
construct an *unresolved* `Divergence`. Use those, not the ones guessed here.

- [ ] **Step 2: Write the failing test**

Append to `src/performance/tests/test_gains.py`:

```python
# -- withheld under divergence (M31b) ------------------------------------


@pytest.fixture
def in_doubt(instrument):
    """An unresolved divergence on INFY: the broker says one quantity, we say
    another, and nobody has said which is right yet."""
    from records.models import Divergence

    return Divergence.objects.create(
        instrument=instrument,
        detected_on=SEP,
        system_quantity=10,
        broker_quantity=8,
    )


def test_realised_gains_are_withheld_for_an_instrument_in_doubt(instrument, in_doubt):
    from performance import gains

    a_realisation(instrument)

    report = gains.realised()

    assert report.rows == []
    assert report.withheld == ["INFY"]
    assert report.total_gain == Decimal("0.00")


def test_unrealised_gains_are_withheld_too(instrument, in_doubt):
    from performance import gains

    an_open_lot(instrument)

    report = gains.unrealised({"INFY": Decimal("1100")})

    assert report.rows == []
    assert report.withheld == ["INFY"]


def test_a_resolved_divergence_stops_withholding(instrument, in_doubt):
    from records import divergence as divergence_reader
    from records.models import DivergenceClassification

    divergence_reader.classify(
        in_doubt,
        classification=DivergenceClassification.SYSTEM_ERROR,
        actor="operator",
        reason="miscount",
    )

    from performance import gains

    an_open_lot(instrument)
    assert gains.unrealised({"INFY": Decimal("1100")}).withheld == []
```

> `classify()`'s signature is in `src/records/divergence.py:117`. Read it and
> match it exactly — if it takes an id rather than an instance, adjust the call
> and nothing else.

- [ ] **Step 3: Run to verify**

Run: `uv run pytest src/performance/tests/test_gains.py -k doubt -v`
Expected: PASS — the branches already exist. If any fail, the bug is in Task 3
or 4's withholding, not in the test.

- [ ] **Step 4: Commit**

```bash
# /atomic:atomic-commit
# message: test(gains): prove figures are withheld under an open divergence
```

---

### Task 6: The XIRR solver

**Files:**
- Create: `src/performance/xirr.py`, `src/performance/tests/test_xirr.py`

**Interfaces:**
- Consumes: nothing — pure arithmetic, no Django import.
- Produces:
  - `xirr.Flow` — frozen dataclass: `on: date`, `amount: Decimal` (negative
    out, positive in).
  - `xirr.InsufficientHistory(Exception)`, `xirr.NoSolution(Exception)`.
  - `xirr.MINIMUM_HISTORY_DAYS = 30`
  - `xirr.solve(flows: list[Flow]) -> Decimal` — annualised rate as a Decimal
    fraction (`0.1234` is 12.34%), actual/365. Raises `InsufficientHistory`
    when fewer than two flows or the span is under `MINIMUM_HISTORY_DAYS`;
    raises `NoSolution` when the flows are all one sign or no root brackets.

- [ ] **Step 1: Write the failing test**

`src/performance/tests/test_xirr.py`:

```python
"""The money-weighted return, and its refusals.

Daily irregular contributions make a simple percentage return meaningless, so
XIRR is the headline metric (M41). It is also the metric most easily made to
print nonsense: one contribution annualises a one-day move into a number in
the thousands of percent, and same-sign flows have no solution at all. Both
are refusals here rather than numbers on a dashboard.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from performance.xirr import Flow, InsufficientHistory, NoSolution, solve

START = date(2025, 1, 1)


def test_doubling_over_a_year_is_about_one_hundred_percent():
    rate = solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("2000"))])

    assert abs(rate - Decimal("1")) < Decimal("0.001")


def test_no_growth_is_zero():
    rate = solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("1000"))])

    assert abs(rate) < Decimal("0.0001")


def test_a_loss_is_negative():
    rate = solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("900"))])

    assert Decimal("-0.11") < rate < Decimal("-0.09")


def test_irregular_contributions_are_weighted_by_time():
    """Money in later has less time to work, so it moves the rate less."""
    flows = [
        Flow(START, Decimal("-1000")),
        Flow(START + timedelta(days=180), Decimal("-1000")),
        Flow(START + timedelta(days=365), Decimal("2100")),
    ]

    rate = solve(flows)

    assert Decimal("0.04") < rate < Decimal("0.12")


def test_one_flow_is_refused():
    with pytest.raises(InsufficientHistory):
        solve([Flow(START, Decimal("-1000"))])


def test_a_short_history_is_refused():
    """A single day's move annualised is not a return, it is an artefact."""
    with pytest.raises(InsufficientHistory):
        solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=5), Decimal("1010"))])


def test_flows_of_one_sign_have_no_solution():
    with pytest.raises(NoSolution):
        solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("-500"))])


def test_a_total_loss_does_not_hang():
    """The bracket's floor is just above -100%, so a wipeout converges to it
    rather than dividing by zero."""
    rate = solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("1"))])

    assert rate < Decimal("-0.99")


def test_flows_need_not_arrive_in_order():
    ordered = solve([Flow(START, Decimal("-1000")), Flow(START + timedelta(days=365), Decimal("1200"))])
    shuffled = solve([Flow(START + timedelta(days=365), Decimal("1200")), Flow(START, Decimal("-1000"))])

    assert ordered == shuffled
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest src/performance/tests/test_xirr.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'performance.xirr'`

- [ ] **Step 3: Write `xirr.py`**

```python
"""XIRR: the internal rate of return over irregularly dated flows.

Written here rather than imported because the alternative is scipy on a single
small instance, for one root-find over at most a few thousand points. Brent's
method over a bounded bracket, bisection as the fallback when Brent's inverse
quadratic step wanders outside it.

Actual/365. The bracket floors just above -100% -- at exactly -100% the
discount factor is zero and the function is undefined -- and ceilings at
1000%, which no real portfolio reaches and which keeps a pathological set of
flows from searching forever.

Floats are used deliberately inside the solver: this is numerical root-finding,
not bookkeeping, and Decimal's exactness buys nothing against an answer that is
converged to a tolerance anyway. The result comes back as a Decimal.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

DAYS_IN_YEAR = 365.0

#: A rate below this is indistinguishable from a wipeout, and at exactly
#: -1.0 the discount factor is zero.
RATE_FLOOR = -0.9999999
RATE_CEILING = 10.0

TOLERANCE = 1e-10
MAX_ITERATIONS = 200

#: Below this span, annualising is an artefact rather than a return.
MINIMUM_HISTORY_DAYS = 30


class InsufficientHistory(Exception):
    """Too few flows, or too short a span, for the answer to mean anything."""


class NoSolution(Exception):
    """The flows do not bracket a root -- usually because they share a sign."""


@dataclass(frozen=True)
class Flow:
    on: date
    amount: Decimal


def _net_present_value(flows: list[Flow], rate: float, origin: date) -> float:
    total = 0.0
    for flow in flows:
        years = (flow.on - origin).days / DAYS_IN_YEAR
        total += float(flow.amount) / (1.0 + rate) ** years
    return total


def solve(flows: list[Flow]) -> Decimal:
    """The annualised money-weighted return, as a fraction."""
    if len(flows) < 2:
        raise InsufficientHistory("XIRR needs at least two dated flows.")

    ordered = sorted(flows, key=lambda flow: flow.on)
    origin = ordered[0].on
    span = (ordered[-1].on - origin).days
    if span < MINIMUM_HISTORY_DAYS:
        raise InsufficientHistory(
            f"{span} days of history is too short to annualise; "
            f"{MINIMUM_HISTORY_DAYS} is the minimum."
        )

    signs = {flow.amount > 0 for flow in ordered if flow.amount != 0}
    if len(signs) < 2:
        raise NoSolution("Flows must include both an outflow and an inflow.")

    low, high = RATE_FLOOR, RATE_CEILING
    f_low = _net_present_value(ordered, low, origin)
    f_high = _net_present_value(ordered, high, origin)
    if f_low * f_high > 0:
        raise NoSolution("No rate between -100% and 1000% sets the present value to zero.")

    return Decimal(str(_brent(ordered, origin, low, high, f_low, f_high)))


def _brent(flows, origin, low, high, f_low, f_high) -> float:
    """Brent's method, falling back to bisection whenever the interpolated
    step leaves the bracket or fails to shrink it."""
    if abs(f_low) < abs(f_high):
        low, high = high, low
        f_low, f_high = f_high, f_low

    previous, f_previous = low, f_low
    step_before_last = high - low
    used_bisection = True

    for _ in range(MAX_ITERATIONS):
        if abs(f_high) < TOLERANCE or abs(high - low) < TOLERANCE:
            return high

        if f_low != f_previous and f_high != f_previous:
            candidate = _inverse_quadratic(low, high, previous, f_low, f_high, f_previous)
        else:
            candidate = _secant(low, high, f_low, f_high)

        midpoint = (3 * low + high) / 4
        outside = not (
            (midpoint < candidate < high) or (high < candidate < midpoint)
        )
        too_slow = used_bisection and abs(candidate - high) >= abs(high - previous) / 2

        if outside or too_slow:
            candidate = (low + high) / 2
            used_bisection = True
        else:
            used_bisection = False

        step_before_last = abs(high - previous)  # noqa: F841 -- kept for readability
        previous, f_previous = high, f_high

        f_candidate = _net_present_value(flows, candidate, origin)
        if f_low * f_candidate < 0:
            high, f_high = candidate, f_candidate
        else:
            low, f_low = candidate, f_candidate

        if abs(f_low) < abs(f_high):
            low, high = high, low
            f_low, f_high = f_high, f_low

    return high


def _inverse_quadratic(low, high, previous, f_low, f_high, f_previous) -> float:
    return (
        low * f_high * f_previous / ((f_low - f_high) * (f_low - f_previous))
        + high * f_low * f_previous / ((f_high - f_low) * (f_high - f_previous))
        + previous * f_low * f_high / ((f_previous - f_low) * (f_previous - f_high))
    )


def _secant(low, high, f_low, f_high) -> float:
    if f_high == f_low:
        return (low + high) / 2
    return high - f_high * (high - low) / (f_high - f_low)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest src/performance/tests/test_xirr.py -v`
Expected: PASS (9 tests)

If `test_irregular_contributions_are_weighted_by_time` fails, check the sign
convention before touching the solver: outflows are negative.

- [ ] **Step 5: Add a property test**

Append to `src/performance/tests/test_xirr.py`:

```python
# -- the solver's own invariant ------------------------------------------


def test_the_rate_it_returns_zeroes_the_present_value():
    """Whatever the solver returns, discounting the flows at that rate must
    come back to zero. This is the definition, and the only check that does
    not just restate the implementation."""
    from datetime import timedelta

    from performance.xirr import _net_present_value

    flows = [
        Flow(START, Decimal("-500")),
        Flow(START + timedelta(days=90), Decimal("-750")),
        Flow(START + timedelta(days=200), Decimal("-250")),
        Flow(START + timedelta(days=400), Decimal("1700")),
    ]

    rate = solve(flows)

    assert abs(_net_present_value(flows, float(rate), START)) < 0.01
```

Run: `uv run pytest src/performance/tests/test_xirr.py -v` → PASS (10 tests)

- [ ] **Step 6: Commit**

```bash
# /atomic:atomic-commit
# message: feat(gains): add an XIRR solver with no new dependency
```

---

### Task 7: Portfolio return (M41)

**Files:**
- Create: `src/performance/returns.py`, `src/performance/tests/test_returns.py`

**Interfaces:**
- Consumes: `engine.models.CashLedger` (`trading_date`, `cash_spent`,
  `charges`); `gains.models.RealisedGain` (`disposed_on`, `net_proceeds`);
  `performance.xirr.Flow`, `solve`, `InsufficientHistory`, `NoSolution`;
  `performance.valuation.value_now`, `invested_to_date`;
  `records.divergence.portfolio_caveats`.
- Produces:
  - `returns.contribution_flows(as_of: date, terminal_value: Decimal) -> list[Flow]`
  - `returns.Performance` — frozen dataclass: `invested: Decimal`,
    `current_value: Decimal`, `absolute_return: Decimal`,
    `percent_return: Decimal`, `xirr: Decimal | None`,
    `xirr_unavailable: str` (empty when available),
    `per_instrument: list[InstrumentContribution]`, `caveats: list[dict]`.
  - `InstrumentContribution` — `instrument_id`, `invested`, `value`, `gain`,
    `share_of_gain: Decimal` (percent of total gain, `0` when total gain is 0).
  - `returns.measure(prices: dict[str, Decimal], *, as_of: date) -> Performance`

- [ ] **Step 1: Write the failing test**

`src/performance/tests/test_returns.py`:

```python
"""Portfolio performance (M41).

The flows are the cash actually spent on shares, not notional deposits: money
sitting in carry has not been put to work and must not be counted as though it
had. Disposals come back as inflows, and what is still held is the terminal
inflow.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

START = date(2025, 9, 1)
TODAY = date(2026, 9, 22)


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(
        tradingsymbol="INFY", instrument_token=408065, name="Infosys"
    )


def a_spending_day(day, *, spent="1000", charges="2"):
    from engine.models import CashLedger

    return CashLedger.objects.create(
        trading_date=day,
        cash_spent=Decimal(spent),
        charges=Decimal(charges),
        closing_carry=Decimal("0"),
    )


def an_open_lot(instrument, *, quantity=2, cost="1000", acquired_on=START):
    from portfolio.models import Lot

    return Lot.objects.create(
        instrument=instrument,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal(cost),
    )


# -- flows ---------------------------------------------------------------


def test_each_spending_day_is_an_outflow():
    from performance import returns

    a_spending_day(START, spent="1000", charges="2")

    flows = returns.contribution_flows(TODAY, terminal_value=Decimal("1500"))

    assert flows[0].on == START
    assert flows[0].amount == Decimal("-1002.00")


def test_a_day_that_spent_nothing_is_not_a_flow():
    """A skipped day moved no money; a zero flow would drag the date range
    without changing the arithmetic."""
    from performance import returns

    a_spending_day(START, spent="0", charges="0")

    assert returns.contribution_flows(TODAY, terminal_value=Decimal("0")) == []


def test_what_is_still_held_is_the_terminal_inflow():
    from performance import returns

    a_spending_day(START)

    flows = returns.contribution_flows(TODAY, terminal_value=Decimal("1500"))

    assert flows[-1].on == TODAY
    assert flows[-1].amount == Decimal("1500.00")


def test_a_disposal_is_an_inflow_on_the_day_it_settled(instrument):
    from gains.models import RealisedGain
    from performance import returns
    from portfolio.models import Lot

    a_spending_day(START)
    lot = Lot.objects.create(
        instrument=instrument,
        acquired_on=START,
        quantity=1,
        remaining_quantity=0,
        cost_per_unit=Decimal("1000"),
    )
    RealisedGain.objects.create(
        lot=lot,
        instrument=instrument,
        quantity=1,
        acquired_on=START,
        disposed_on=date(2026, 3, 1),
        cost=Decimal("1000"),
        net_proceeds=Decimal("1200"),
        gain=Decimal("200"),
    )

    flows = returns.contribution_flows(TODAY, terminal_value=Decimal("500"))
    inflow = next(flow for flow in flows if flow.on == date(2026, 3, 1))

    assert inflow.amount == Decimal("1200.00")


# -- the headline numbers ------------------------------------------------


def test_performance_reports_invested_value_and_return(instrument):
    from performance import returns

    a_spending_day(START, spent="2000", charges="0")
    an_open_lot(instrument, quantity=2, cost="1000")

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.invested == Decimal("2000.00")
    assert result.current_value == Decimal("2400.00")
    assert result.absolute_return == Decimal("400.00")
    assert result.percent_return == Decimal("20.00")


def test_xirr_is_reported_over_a_long_enough_history(instrument):
    from performance import returns

    a_spending_day(START, spent="2000", charges="0")
    an_open_lot(instrument, quantity=2, cost="1000")

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.xirr is not None
    assert result.xirr_unavailable == ""


def test_a_short_history_says_so_instead_of_printing_a_number(instrument):
    """One contribution and a one-week move annualises into nonsense."""
    from performance import returns

    recent = TODAY - timedelta(days=7)
    a_spending_day(recent, spent="2000", charges="0")
    an_open_lot(instrument, quantity=2, cost="1000", acquired_on=recent)

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.xirr is None
    assert "history" in result.xirr_unavailable


def test_per_instrument_contribution_is_reported(instrument):
    from performance import returns

    a_spending_day(START, spent="2000", charges="0")
    an_open_lot(instrument, quantity=2, cost="1000")

    contribution = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY).per_instrument[0]

    assert contribution.instrument_id == "INFY"
    assert contribution.gain == Decimal("400.00")
    assert contribution.share_of_gain == Decimal("100.00")


def test_portfolio_figures_are_flagged_not_withheld_under_a_divergence(instrument):
    """M31b: the instrument's own figures go, the portfolio's stay and carry
    a caveat naming what is in doubt."""
    from performance import returns
    from records.models import Divergence

    a_spending_day(START, spent="2000", charges="0")
    an_open_lot(instrument, quantity=2, cost="1000")
    Divergence.objects.create(
        instrument=instrument, detected_on=TODAY, system_quantity=2, broker_quantity=1
    )

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.current_value == Decimal("2400.00")
    assert result.caveats != []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest src/performance/tests/test_returns.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'performance.returns'`

- [ ] **Step 3: Write `returns.py`**

```python
"""Portfolio performance (M41).

The flows are the cash actually spent on shares -- `cash_spent + charges` from
the day's ledger row -- not notional deposits. Money sitting in carry has not
been put to work, and counting it as though it had would understate the return
of the part that was.

Disposals return as inflows on the day they settled, and whatever is still
held is the terminal inflow at today's prices. The result is the money-weighted
return of the equity portfolio, which is the only return a daily irregular
contribution schedule has (M41).
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from engine.ledger import money
from engine.models import CashLedger
from gains.models import RealisedGain
from performance import valuation
from performance.xirr import Flow, InsufficientHistory, NoSolution, solve
from records.divergence import portfolio_caveats

logger = logging.getLogger("steadystack.performance")

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class InstrumentContribution:
    instrument_id: str
    invested: Decimal
    value: Decimal
    gain: Decimal
    share_of_gain: Decimal


@dataclass(frozen=True)
class Performance:
    invested: Decimal
    current_value: Decimal
    absolute_return: Decimal
    percent_return: Decimal
    xirr: Decimal | None
    xirr_unavailable: str
    per_instrument: list[InstrumentContribution]
    caveats: list[dict]


def contribution_flows(as_of: date, terminal_value: Decimal) -> list[Flow]:
    """Every rupee that left for shares, every rupee that came back."""
    flows: list[Flow] = []

    for row in CashLedger.objects.filter(trading_date__lte=as_of).order_by("trading_date"):
        deployed = row.cash_spent + row.charges
        if deployed > ZERO:
            flows.append(Flow(on=row.trading_date, amount=money(-deployed)))

    for record in RealisedGain.objects.filter(disposed_on__lte=as_of).order_by("disposed_on"):
        if record.net_proceeds != ZERO:
            flows.append(Flow(on=record.disposed_on, amount=money(record.net_proceeds)))

    if flows and terminal_value > ZERO:
        flows.append(Flow(on=as_of, amount=money(terminal_value)))

    return flows


def measure(prices: dict[str, Decimal], *, as_of: date) -> Performance:
    value = valuation.value_now(prices)
    invested = valuation.invested_to_date()
    absolute = money(value.total - invested)
    percent = money(absolute / invested * HUNDRED) if invested > ZERO else ZERO

    rate: Decimal | None = None
    unavailable = ""
    try:
        rate = solve(contribution_flows(as_of, value.total))
    except InsufficientHistory as exc:
        unavailable = str(exc)
    except NoSolution as exc:
        unavailable = str(exc)

    return Performance(
        invested=money(invested),
        current_value=money(value.total),
        absolute_return=absolute,
        percent_return=percent,
        xirr=rate,
        xirr_unavailable=unavailable,
        per_instrument=_per_instrument(value, absolute),
        caveats=portfolio_caveats(),
    )


def _per_instrument(value, total_gain: Decimal) -> list[InstrumentContribution]:
    """Where the gain came from, largest contributor first."""
    from portfolio.models import Lot

    invested_by: dict[str, Decimal] = {}
    for lot in Lot.objects.filter(remaining_quantity__gt=0, superseded_by__isnull=True):
        invested_by[lot.instrument_id] = invested_by.get(lot.instrument_id, ZERO) + (
            lot.cost_per_unit * lot.remaining_quantity
        )

    rows = []
    for symbol, held_value in value.per_instrument.items():
        invested = invested_by.get(symbol, ZERO)
        gain = money(held_value - invested)
        rows.append(
            InstrumentContribution(
                instrument_id=symbol,
                invested=money(invested),
                value=money(held_value),
                gain=gain,
                share_of_gain=money(gain / total_gain * HUNDRED) if total_gain else ZERO,
            )
        )

    return sorted(rows, key=lambda row: row.gain, reverse=True)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest src/performance/tests/test_returns.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
# /atomic:atomic-commit
# message: feat(gains): measure the portfolio's money-weighted return
```

---

### Task 8: Benchmark XIRR (M41a)

**Files:**
- Modify: `src/configuration/models.py` (add `benchmark_symbol`),
  `src/performance/returns.py`
- Create: `src/configuration/migrations/0003_benchmark_symbol.py` (generated)
- Test: `src/performance/tests/test_returns.py` (append)

**Interfaces:**
- Consumes: `performance.models.DailyValuation.benchmark_level`;
  `configuration.store.current().benchmark_symbol`.
- Produces:
  - `ConfigurationVersion.benchmark_symbol = CharField(max_length=64, default="NIFTY 50")`
  - `returns.benchmark_xirr(flows: list[Flow], *, as_of: date) -> tuple[Decimal | None, str]`
    — `(rate, unavailable_reason)`. Buys index units with every outflow at that
    day's stored level, sells units on every inflow, and values the remainder
    at the latest stored level.
  - `Performance` gains `benchmark_xirr: Decimal | None` and
    `benchmark_unavailable: str`.

- [ ] **Step 1: Add the configuration field**

In `src/configuration/models.py`, after `registered_static_ip`:

```python
    benchmark_symbol = models.CharField(
        max_length=64,
        default="NIFTY 50",
        help_text=(
            "The index the return is judged against (M41a). An instrument on "
            "the existing tick feed, so this adds no second market-data "
            "dependency."
        ),
    )
```

Then:

```bash
uv run python manage.py makemigrations configuration -n benchmark_symbol
```

Check `configuration.store._snapshot` picks the field up. If `_snapshot`
enumerates fields explicitly (`src/configuration/store.py:66`), add
`benchmark_symbol` there too, or a change to it will not be audited.

- [ ] **Step 2: Write the failing test**

Append to `src/performance/tests/test_returns.py`:

```python
# -- the benchmark (M41a) ------------------------------------------------


def a_benchmark_level(day, level):
    from performance.models import DailyValuation

    return DailyValuation.objects.create(
        trading_date=day,
        portfolio_value=Decimal("0"),
        benchmark_level=Decimal(level),
    )


def test_the_benchmark_buys_units_on_the_same_dates(instrument):
    """The index rose 20% over the period, so putting the same money in on the
    same days returns about 20% -- which is the number the portfolio has to
    beat to have been worth the trouble."""
    from performance import returns
    from performance.xirr import Flow

    a_benchmark_level(START, "20000")
    a_benchmark_level(TODAY, "24000")

    flows = [Flow(START, Decimal("-2000")), Flow(TODAY, Decimal("2400"))]
    rate, unavailable = returns.benchmark_xirr(flows, as_of=TODAY)

    assert unavailable == ""
    assert Decimal("0.15") < rate < Decimal("0.25")


def test_a_contribution_date_with_no_level_makes_the_benchmark_unavailable(instrument):
    """Guessing a level would invent the comparison the operator is relying on."""
    from performance import returns
    from performance.xirr import Flow

    a_benchmark_level(TODAY, "24000")

    flows = [Flow(START, Decimal("-2000")), Flow(TODAY, Decimal("2400"))]
    rate, unavailable = returns.benchmark_xirr(flows, as_of=TODAY)

    assert rate is None
    assert str(START) in unavailable


def test_the_benchmark_is_absent_before_any_history_exists(instrument):
    from performance import returns
    from performance.xirr import Flow

    rate, unavailable = returns.benchmark_xirr(
        [Flow(START, Decimal("-2000")), Flow(TODAY, Decimal("2400"))], as_of=TODAY
    )

    assert rate is None
    assert unavailable != ""


def test_measure_reports_the_benchmark_alongside_the_portfolio(instrument):
    from performance import returns

    a_spending_day(START, spent="2000", charges="0")
    an_open_lot(instrument, quantity=2, cost="1000")
    a_benchmark_level(START, "20000")
    a_benchmark_level(TODAY, "24000")

    result = returns.measure({"INFY": Decimal("1200")}, as_of=TODAY)

    assert result.benchmark_xirr is not None
```

> `a_benchmark_level(START, ...)` and `a_spending_day(START, ...)` both write
> rows keyed on the same date but in different tables, so they do not collide.

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest src/performance/tests/test_returns.py -k benchmark -v`
Expected: FAIL — `AttributeError: module 'performance.returns' has no attribute 'benchmark_xirr'`

- [ ] **Step 4: Implement the benchmark**

Append to `src/performance/returns.py`:

```python
def benchmark_xirr(flows: list[Flow], *, as_of: date) -> tuple[Decimal | None, str]:
    """What the same money, on the same days, would have returned in the index.

    Every outflow buys index units at that day's close; every inflow sells
    them; whatever remains is valued at the latest level we hold. Returning
    against the index rather than against zero is the point of M41a: a year
    that made 8% while the index made 14% is not a good year.

    A contribution date with no stored level makes the whole comparison
    unavailable rather than approximate. Interpolating would invent the number
    the operator is relying on.
    """
    from performance.models import DailyValuation

    if not flows:
        return None, "No contributions to compare."

    levels = {
        row.trading_date: row.benchmark_level
        for row in DailyValuation.objects.filter(
            trading_date__lte=as_of, benchmark_level__isnull=False
        )
    }
    if not levels:
        return None, "No benchmark history has been recorded yet."

    units = Decimal("0")
    mirrored: list[Flow] = []

    for flow in sorted(flows, key=lambda item: item.on):
        if flow.on == as_of:
            continue  # the terminal flow is replaced by the index's own value
        level = levels.get(flow.on)
        if level is None or level <= ZERO:
            return None, f"No benchmark level recorded for {flow.on}."
        units -= flow.amount / level  # an outflow is negative, so this buys
        mirrored.append(flow)

    latest = max(levels)
    terminal = money(units * levels[latest])
    if terminal <= ZERO:
        return None, "The mirrored index position is empty."

    mirrored.append(Flow(on=as_of, amount=terminal))

    try:
        return solve(mirrored), ""
    except (InsufficientHistory, NoSolution) as exc:
        return None, str(exc)
```

Then extend `Performance` and `measure`:

```python
# in the Performance dataclass, after xirr_unavailable:
    benchmark_xirr: Decimal | None
    benchmark_unavailable: str
```

```python
# in measure(), after computing `rate`:
    flows = contribution_flows(as_of, value.total)
    benchmark_rate, benchmark_unavailable = benchmark_xirr(flows, as_of=as_of)
```

and pass `benchmark_xirr=benchmark_rate,
benchmark_unavailable=benchmark_unavailable` into the returned `Performance`.
Compute `flows` **once** and reuse it for `solve(flows)` rather than calling
`contribution_flows` twice.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest src/performance/tests/ -v`
Expected: PASS (all tests in the app)

- [ ] **Step 6: Commit**

```bash
# /atomic:atomic-commit — two commits:
# feat(config): make the benchmark index configurable
# feat(gains): judge the return against holding the index
```

---

### Task 9: The cash ledger surface (M40a)

**Files:**
- Create: `src/performance/ledgers.py`, `src/performance/tests/test_ledgers.py`

**Interfaces:**
- Consumes: `engine.models.CashLedger`, `engine.models.LedgerAdjustment`,
  `engine.ledger.reconcile(start, end) -> dict` (keys `ok`, `chain_breaks`, and
  whatever else it returns — read `src/engine/ledger.py:366` before using it).
- Produces:
  - `ledgers.cash(start=None, end=None) -> CashLedgerView` with
    `rows: list[CashLedgerRow]`, `totals: CashLedgerRow`, `reconciliation: dict`,
    `identity_holds: bool`.
  - `CashLedgerRow` — `trading_date`, `opening_carry`, `deposit`, `lumpsum`,
    `adjustment`, `cash_spent`, `charges`, `closing_carry`.

- [ ] **Step 1: Read `reconcile()`'s return shape**

Run: `sed -n '366,430p' src/engine/ledger.py`
The view passes it straight through; match the keys exactly.

- [ ] **Step 2: Write the failing test**

`src/performance/tests/test_ledgers.py`:

```python
"""The two ledger surfaces (M40a, M40b).

The cash ledger shows the identity D7's property tests already assert --
contributions minus deployed minus cash held equals zero -- rather than asking
to be trusted on it. The share ledger does the same for quantities, by source,
so that when the system and the broker disagree the disagreement can be
diagnosed instead of merely alerted.
"""

from datetime import date
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

MON = date(2026, 9, 21)
TUE = date(2026, 9, 22)


def a_ledger_row(day, **kwargs):
    from engine.models import CashLedger

    defaults = {
        "opening_carry": Decimal("0"),
        "deposit": Decimal("1000"),
        "lumpsum": Decimal("0"),
        "adjustment": Decimal("0"),
        "cash_spent": Decimal("900"),
        "charges": Decimal("2"),
        "closing_carry": Decimal("98"),
    }
    return CashLedger.objects.create(trading_date=day, **{**defaults, **kwargs})


# -- cash (M40a) ---------------------------------------------------------


def test_every_day_since_inception_is_listed():
    from performance import ledgers

    a_ledger_row(MON)
    a_ledger_row(TUE, opening_carry=Decimal("98"), closing_carry=Decimal("196"))

    view = ledgers.cash()

    assert [row.trading_date for row in view.rows] == [MON, TUE]


def test_the_range_narrows_it():
    from performance import ledgers

    a_ledger_row(MON)
    a_ledger_row(TUE, opening_carry=Decimal("98"), closing_carry=Decimal("196"))

    assert [row.trading_date for row in ledgers.cash(start=TUE).rows] == [TUE]


def test_totals_are_summed_over_the_range():
    from performance import ledgers

    a_ledger_row(MON)
    a_ledger_row(TUE, opening_carry=Decimal("98"), closing_carry=Decimal("196"))

    totals = ledgers.cash().totals

    assert totals.deposit == Decimal("2000.00")
    assert totals.cash_spent == Decimal("1800.00")
    assert totals.charges == Decimal("4.00")


def test_the_surface_carries_the_reconciliation():
    """D7's identity, shown rather than trusted."""
    from performance import ledgers

    a_ledger_row(MON)

    view = ledgers.cash()

    assert view.identity_holds is True
    assert "chain_breaks" in view.reconciliation


def test_a_broken_chain_is_surfaced_not_hidden():
    from performance import ledgers

    a_ledger_row(MON, closing_carry=Decimal("98"))
    a_ledger_row(TUE, opening_carry=Decimal("5"), closing_carry=Decimal("103"))

    assert ledgers.cash().identity_holds is False
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest src/performance/tests/test_ledgers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'performance.ledgers'`

- [ ] **Step 4: Write the cash half of `ledgers.py`**

```python
"""The cash and share ledgers, as surfaces (M40a, M40b).

Both exist to be read by a person who is trying to work out where a number
came from. Neither computes anything the underlying tables do not already
hold -- they arrange, total, and show the reconciliation rather than asserting
it.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from engine.ledger import money, reconcile
from engine.models import CashLedger

logger = logging.getLogger("steadystack.performance")

ZERO = Decimal("0")


@dataclass(frozen=True)
class CashLedgerRow:
    trading_date: date | None
    opening_carry: Decimal
    deposit: Decimal
    lumpsum: Decimal
    adjustment: Decimal
    cash_spent: Decimal
    charges: Decimal
    closing_carry: Decimal


@dataclass(frozen=True)
class CashLedgerView:
    rows: list[CashLedgerRow]
    totals: CashLedgerRow
    reconciliation: dict
    identity_holds: bool


def cash(start: date | None = None, end: date | None = None) -> CashLedgerView:
    query = CashLedger.objects.all().order_by("trading_date")
    if start is not None:
        query = query.filter(trading_date__gte=start)
    if end is not None:
        query = query.filter(trading_date__lte=end)

    rows = [
        CashLedgerRow(
            trading_date=row.trading_date,
            opening_carry=money(row.opening_carry),
            deposit=money(row.deposit),
            lumpsum=money(row.lumpsum),
            adjustment=money(row.adjustment),
            cash_spent=money(row.cash_spent),
            charges=money(row.charges),
            closing_carry=money(row.closing_carry),
        )
        for row in query
    ]

    result = reconcile(start, end)

    return CashLedgerView(
        rows=rows,
        totals=CashLedgerRow(
            trading_date=None,
            opening_carry=rows[0].opening_carry if rows else ZERO,
            deposit=money(sum((row.deposit for row in rows), ZERO)),
            lumpsum=money(sum((row.lumpsum for row in rows), ZERO)),
            adjustment=money(sum((row.adjustment for row in rows), ZERO)),
            cash_spent=money(sum((row.cash_spent for row in rows), ZERO)),
            charges=money(sum((row.charges for row in rows), ZERO)),
            closing_carry=rows[-1].closing_carry if rows else ZERO,
        ),
        reconciliation=result,
        identity_holds=bool(result.get("ok")),
    )
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest src/performance/tests/test_ledgers.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
# /atomic:atomic-commit
# message: feat(gains): surface the cash ledger with its reconciliation
```

---

### Task 10: The share ledger surface (M40b)

**Files:**
- Modify: `src/performance/ledgers.py`
- Test: `src/performance/tests/test_ledgers.py` (append)

**Interfaces:**
- Consumes: `orders.models.Fill` (engine buys and rebalance sells, via
  `fill.order.side` and `fill.order.stream`), `records.models.ExternalTransaction`
  (`side`, `quantity`, `occurred_on`), corporate-action restructures (lots with
  `superseded_by` set by `records.corporate_actions`), `portfolio.models.Holding`.
- Produces:
  - `ledgers.shares(instrument_id=None, start=None, end=None) -> ShareLedgerView`
    with `movements: list[ShareMovement]`, `by_instrument: dict[str, int]`,
    `divergences: list[dict]`.
  - `ShareMovement` — `on: date`, `instrument_id`, `quantity: int` (signed),
    `source: str` (one of `engine_buy`, `rebalance_sell`, `external`,
    `corporate_action`), `reference: str`.

- [ ] **Step 1: Confirm how a rebalance sell is distinguished from a buy**

Run: `grep -n "stream\|OrderSide" src/rebalance/*.py | head -20`
The `source` classification must match what `rebalance` actually writes. Use
`Order.side` first and `Order.stream` only to name the batch.

- [ ] **Step 2: Write the failing test**

Append to `src/performance/tests/test_ledgers.py`:

```python
# -- shares (M40b) -------------------------------------------------------


@pytest.fixture
def instrument(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(
        tradingsymbol="INFY", instrument_token=408065, name="Infosys"
    )


def a_fill(instrument, *, quantity=2, side=None, day=TUE):
    from django.utils import timezone

    from orders.models import Fill, Order, OrderMode, OrderSide, OrderType

    order = Order.objects.create(
        tag=f"tx-{quantity}-{side or 'buy'}-{day}",
        instrument=instrument,
        trading_date=day,
        tick_no=0,
        side=side or OrderSide.BUY,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=Decimal("1000"),
    )
    return Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-{order.tag}",
        quantity=quantity,
        price=Decimal("1000"),
        executed_at=timezone.now(),
    )


def test_an_engine_buy_is_a_positive_movement(instrument):
    from performance import ledgers

    a_fill(instrument, quantity=2)

    movement = ledgers.shares().movements[0]

    assert movement.quantity == 2
    assert movement.source == "engine_buy"
    assert movement.instrument_id == "INFY"


def test_a_sell_is_a_negative_movement(instrument):
    from orders.models import OrderSide
    from performance import ledgers

    a_fill(instrument, quantity=3, side=OrderSide.SELL)

    movement = ledgers.shares().movements[0]

    assert movement.quantity == -3
    assert movement.source == "rebalance_sell"


def test_an_external_transaction_is_its_own_source(instrument):
    from performance import ledgers
    from records.models import ExternalSide, ExternalTransaction

    ExternalTransaction.objects.create(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=5,
        price=Decimal("1000"),
        occurred_on=TUE,
        acquired_on=TUE,
        reason="bought at the broker",
        actor="operator",
    )

    movement = ledgers.shares().movements[0]

    assert movement.source == "external"
    assert movement.quantity == 5


def test_movements_net_to_the_quantity_held(instrument):
    from orders.models import OrderSide
    from performance import ledgers

    a_fill(instrument, quantity=5)
    a_fill(instrument, quantity=2, side=OrderSide.SELL)

    assert ledgers.shares().by_instrument["INFY"] == 3


def test_the_ledger_names_a_disagreement_with_the_broker(instrument):
    """Between the two ledgers a divergence is diagnosed, not merely alerted."""
    from performance import ledgers
    from portfolio.models import Holding

    a_fill(instrument, quantity=5)
    Holding.objects.create(instrument=instrument, quantity=4, average_cost=Decimal("1000"))

    divergence = ledgers.shares().divergences[0]

    assert divergence["instrument_id"] == "INFY"
    assert divergence["ours"] == 5
    assert divergence["broker"] == 4


def test_a_single_instrument_can_be_asked_about(instrument):
    from performance import ledgers

    a_fill(instrument, quantity=5)

    assert len(ledgers.shares(instrument_id="INFY").movements) == 1
    assert ledgers.shares(instrument_id="TCS").movements == []
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest src/performance/tests/test_ledgers.py -k shares -v`
Expected: FAIL — `AttributeError: module 'performance.ledgers' has no attribute 'shares'`

- [ ] **Step 4: Implement the share ledger**

Append to `src/performance/ledgers.py`:

```python
ENGINE_BUY = "engine_buy"
REBALANCE_SELL = "rebalance_sell"
EXTERNAL = "external"
CORPORATE_ACTION = "corporate_action"


@dataclass(frozen=True)
class ShareMovement:
    on: date
    instrument_id: str
    quantity: int
    source: str
    reference: str


@dataclass(frozen=True)
class ShareLedgerView:
    movements: list[ShareMovement]
    by_instrument: dict[str, int]
    divergences: list[dict]


def shares(
    instrument_id: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> ShareLedgerView:
    """Every quantity in and out, by instrument and by source.

    The point is not the list but the netting: what the movements add up to
    has to equal what the broker says is held, and where it does not the
    difference is named. Two records that agree are worth more than two that
    do not.
    """
    from orders.models import Fill, OrderSide
    from portfolio.models import Holding
    from records.models import ExternalTransaction

    movements: list[ShareMovement] = []

    fills = Fill.objects.select_related("order").order_by("executed_at")
    if instrument_id is not None:
        fills = fills.filter(order__instrument_id=instrument_id)
    if start is not None:
        fills = fills.filter(order__trading_date__gte=start)
    if end is not None:
        fills = fills.filter(order__trading_date__lte=end)

    for fill in fills:
        is_sell = fill.order.side == OrderSide.SELL
        movements.append(
            ShareMovement(
                on=fill.order.trading_date,
                instrument_id=fill.order.instrument_id,
                quantity=-fill.quantity if is_sell else fill.quantity,
                source=REBALANCE_SELL if is_sell else ENGINE_BUY,
                reference=fill.order.tag,
            )
        )

    external = ExternalTransaction.objects.order_by("occurred_on")
    if instrument_id is not None:
        external = external.filter(instrument_id=instrument_id)
    if start is not None:
        external = external.filter(occurred_on__gte=start)
    if end is not None:
        external = external.filter(occurred_on__lte=end)

    for transaction in external:
        movements.append(
            ShareMovement(
                on=transaction.occurred_on,
                instrument_id=transaction.instrument_id,
                quantity=transaction.quantity if transaction.adds_shares else -transaction.quantity,
                source=EXTERNAL,
                reference=transaction.reason,
            )
        )

    movements.sort(key=lambda movement: (movement.on, movement.instrument_id))

    net: dict[str, int] = {}
    for movement in movements:
        net[movement.instrument_id] = net.get(movement.instrument_id, 0) + movement.quantity

    return ShareLedgerView(
        movements=movements,
        by_instrument=net,
        divergences=_disagreements(net, Holding.objects.all()),
    )


def _disagreements(net: dict[str, int], holdings) -> list[dict]:
    """Where our arithmetic and the broker's holding do not meet."""
    broker = {holding.instrument_id: holding.quantity for holding in holdings}
    found = []
    for symbol in sorted({*net, *broker}):
        ours, theirs = net.get(symbol, 0), broker.get(symbol, 0)
        if ours != theirs:
            found.append({"instrument_id": symbol, "ours": ours, "broker": theirs})
    return found
```

> A corporate action supersedes lots rather than writing a fill, so it
> produces no `Fill` row and would be invisible here. Add a fourth source
> reading superseded lots **only if** `test_movements_net_to_the_quantity_held`
> fails once a split exists — check `records/corporate_actions.py:72` for what
> `apply_ratio` writes before inventing a source for it.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest src/performance/tests/test_ledgers.py -v`
Expected: PASS (11 tests)

- [ ] **Step 6: Commit**

```bash
# /atomic:atomic-commit
# message: feat(gains): surface the share ledger and name its divergences
```

---

### Task 11: The holdings dashboard (M40)

**Files:**
- Modify: `src/web/views.py`, `src/web/urls.py`
- Create: `src/templates/web/holdings.html`
- Test: `src/web/tests/test_dashboards.py`

**Interfaces:**
- Consumes: `engine.selection.rank(...)` — **read its signature at
  `src/engine/selection.py:166` before calling it**; `engine.quotes.fetch`;
  `model_portfolio.models.ModelPortfolio`; `engine.reserve.ReserveState`;
  `performance.gains.unrealised`.
- Produces: URL name `web:holdings` at `/holdings/`.

- [ ] **Step 1: Write the failing test**

`src/web/tests/test_dashboards.py`:

```python
"""The dashboards.

Every number on these pages comes from `performance`; a view that computes a
gain is in the wrong file. What the tests check here is that the page renders,
that it says what it does not know, and that a withheld figure is visibly
withheld rather than quietly missing.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(client, django_user_model):
    """A verified session. Follow whatever `src/web/tests/test_pages.py`
    already does -- copy that fixture rather than inventing a second one."""
    raise NotImplementedError("Copy the verified-session fixture from test_pages.py")


def test_holdings_is_not_reachable_anonymously(client):
    response = client.get(reverse("web:holdings"))

    assert response.status_code in (302, 403)
```

> **Before writing the rest:** open `src/web/tests/test_pages.py` and copy its
> existing authenticated-client fixture verbatim into this file (or promote it
> into a shared `conftest.py` under `src/web/tests/`). Do not invent a second
> way to sign a test client in — the TOTP middleware makes that a trap.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest src/web/tests/test_dashboards.py -v`
Expected: FAIL — `NoReverseMatch: 'holdings' is not a valid view function or pattern name`

- [ ] **Step 3: Add the URL and the view**

In `src/web/urls.py`:

```python
urlpatterns = [
    path("", views.home, name="home"),
    path("holdings/", views.holdings, name="holdings"),
]
```

In `src/web/views.py`:

```python
@require_GET
def holdings(request: HttpRequest) -> HttpResponse:
    """What is held, against what was meant to be held.

    The gap column is the whole point: it is what the engine ranks on, so the
    page shows the operator the same number the machine used rather than a
    prettier one.
    """
    from broker import get_broker
    from broker.exceptions import BrokerError
    from engine import quotes as quote_reader
    from model_portfolio.models import ModelPortfolio
    from performance import gains as gains_reader

    portfolio = ModelPortfolio.objects.order_by("-version").first()
    targets = portfolio.weights() if portfolio else {}

    prices, price_error = {}, ""
    try:
        quotes = quote_reader.fetch(sorted(targets), broker=get_broker())
        prices = {symbol: quote.last_price for symbol, quote in quotes.items()}
    except BrokerError as exc:
        price_error = str(exc)

    report = gains_reader.unrealised(prices)
    total = report.total_value

    rows = []
    for symbol, target_pct in sorted(targets.items()):
        value = sum(
            (row.value for row in report.rows if row.instrument_id == symbol), Decimal("0")
        )
        quantity = sum(
            (row.quantity for row in report.rows if row.instrument_id == symbol), 0
        )
        current_pct = (value / total * Decimal("100")) if total else Decimal("0")
        rows.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "price": prices.get(symbol),
                "value": value,
                "target_pct": target_pct,
                "current_pct": current_pct,
                "gap": current_pct - target_pct,
                "withheld": symbol in report.withheld,
                "unpriced": symbol in report.unpriced,
            }
        )

    return render(
        request,
        "web/holdings.html",
        {
            "rows": sorted(rows, key=lambda row: row["gap"]),
            "report": report,
            "total": total,
            "price_error": price_error,
            "portfolio": portfolio,
        },
    )
```

Add `from decimal import Decimal` and `HttpResponse` to the imports at the top
of `src/web/views.py`.

> `ModelPortfolio.weights()` is used by `generate_tradesheet.py:53`. Confirm it
> returns `{symbol: target_pct}` before relying on it here.

- [ ] **Step 4: Write the template**

`src/templates/web/holdings.html`:

```html
{% extends "base.html" %}
{% block title %}Holdings — steadystack{% endblock %}
{% block content %}
  <h1>Holdings</h1>

  {% if price_error %}
    <p class="errors">Prices could not be read — {{ price_error }}. Values below are incomplete.</p>
  {% endif %}
  {% if report.withheld %}
    <p class="errors">
      Figures withheld for {{ report.withheld|join:", " }} — the quantity held is in doubt
      until the divergence is classified.
    </p>
  {% endif %}

  {% if rows %}
    <table>
      <thead>
        <tr><th>Symbol</th><th>Qty</th><th>Price</th><th>Value</th>
            <th>Now</th><th>Target</th><th>Gap</th></tr>
      </thead>
      <tbody>
        {% for row in rows %}
          <tr>
            <td>{{ row.symbol }}</td>
            {% if row.withheld %}
              <td colspan="6" class="muted">withheld — divergence unresolved</td>
            {% elif row.unpriced %}
              <td>{{ row.quantity }}</td>
              <td colspan="5" class="muted">no price on the feed</td>
            {% else %}
              <td>{{ row.quantity }}</td>
              <td>₹{{ row.price }}</td>
              <td>₹{{ row.value }}</td>
              <td>{{ row.current_pct|floatformat:2 }}%</td>
              <td>{{ row.target_pct|floatformat:2 }}%</td>
              <td>{{ row.gap|floatformat:2 }}%</td>
            {% endif %}
          </tr>
        {% endfor %}
      </tbody>
    </table>
    <p class="muted">
      Most underweight first — the order the engine ranks in.
      Total ₹{{ total }}{% if portfolio %}, against model portfolio v{{ portfolio.version }}{% endif %}.
    </p>
  {% else %}
    <p class="muted">No model portfolio has been uploaded yet.</p>
  {% endif %}
{% endblock %}
```

- [ ] **Step 5: Finish the test and run it**

Replace the `NotImplementedError` fixture with the copied one, then add:

```python
def test_holdings_renders_for_a_verified_operator(signed_in):
    response = signed_in.get(reverse("web:holdings"))

    assert response.status_code == 200


def test_a_withheld_instrument_says_so_on_the_page(signed_in, instrument_in_doubt):
    response = signed_in.get(reverse("web:holdings"))

    assert b"withheld" in response.content
```

Build `instrument_in_doubt` the way `src/performance/tests/test_gains.py`'s
`in_doubt` fixture does.

Run: `uv run pytest src/web/tests/ -v` → PASS

- [ ] **Step 6: Commit**

```bash
# /atomic:atomic-commit
# message: feat(web): show holdings against their targets and the gap
```

---

### Task 12: The performance dashboard (M41, M41a)

**Files:**
- Modify: `src/web/views.py`, `src/web/urls.py`
- Create: `src/templates/web/performance.html`
- Test: `src/web/tests/test_dashboards.py` (append)

**Interfaces:**
- Consumes: `performance.returns.measure(prices, as_of=...) -> Performance`;
  `performance.gains.realised()`.
- Produces: URL name `web:performance` at `/performance/`.

- [ ] **Step 1: Write the failing test**

Append to `src/web/tests/test_dashboards.py`:

```python
# -- performance (M41, M41a) ---------------------------------------------


def test_performance_renders(signed_in):
    response = signed_in.get(reverse("web:performance"))

    assert response.status_code == 200


def test_a_short_history_prints_a_sentence_not_a_number(signed_in):
    """M41: one contribution annualises a one-day move into nonsense. The page
    says so rather than printing it."""
    response = signed_in.get(reverse("web:performance"))

    assert b"insufficient history" in response.content.lower()


def test_the_benchmark_absence_is_explained_rather_than_blank(signed_in):
    response = signed_in.get(reverse("web:performance"))

    assert b"benchmark" in response.content.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest src/web/tests/test_dashboards.py -k performance -v`
Expected: FAIL — `NoReverseMatch`

- [ ] **Step 3: Add the URL, the view and the template**

`src/web/urls.py`: `path("performance/", views.performance, name="performance"),`

`src/web/views.py`:

```python
@require_GET
def performance(request: HttpRequest) -> HttpResponse:
    """Invested, worth, and whether either was worth doing.

    XIRR is the headline because daily irregular contributions make a simple
    percentage meaningless, and the benchmark is beside it because a return is
    only good relative to the index it could have been (M41, M41a).
    """
    from audit.context import today_ist
    from broker import get_broker
    from broker.exceptions import BrokerError
    from engine import quotes as quote_reader
    from performance import gains as gains_reader
    from performance import returns as returns_reader
    from performance.valuation import held_quantities

    prices, price_error = {}, ""
    try:
        quotes = quote_reader.fetch(sorted(held_quantities()), broker=get_broker())
        prices = {symbol: quote.last_price for symbol, quote in quotes.items()}
    except BrokerError as exc:
        price_error = str(exc)

    return render(
        request,
        "web/performance.html",
        {
            "result": returns_reader.measure(prices, as_of=today_ist()),
            "realised": gains_reader.realised(),
            "price_error": price_error,
        },
    )
```

`src/templates/web/performance.html`:

```html
{% extends "base.html" %}
{% block title %}Performance — steadystack{% endblock %}
{% block content %}
  <h1>Performance</h1>

  {% if price_error %}
    <p class="errors">Prices could not be read — {{ price_error }}.</p>
  {% endif %}
  {% for caveat in result.caveats %}
    <p class="errors">
      Portfolio figures include {{ caveat.instrument_id }}, whose quantity is in doubt
      by {{ caveat.quantity }}.
    </p>
  {% endfor %}

  <h2>Return</h2>
  <table>
    <tbody>
      <tr><th>Invested</th><td>₹{{ result.invested }}</td></tr>
      <tr><th>Current value</th><td>₹{{ result.current_value }}</td></tr>
      <tr><th>Absolute return</th><td>₹{{ result.absolute_return }} ({{ result.percent_return }}%)</td></tr>
      <tr>
        <th>XIRR</th>
        <td>
          {% if result.xirr is not None %}{{ result.xirr|floatformat:4 }}
          {% else %}<span class="muted">insufficient history — {{ result.xirr_unavailable }}</span>
          {% endif %}
        </td>
      </tr>
      <tr>
        <th>Benchmark XIRR</th>
        <td>
          {% if result.benchmark_xirr is not None %}{{ result.benchmark_xirr|floatformat:4 }}
          {% else %}<span class="muted">benchmark unavailable — {{ result.benchmark_unavailable }}</span>
          {% endif %}
        </td>
      </tr>
    </tbody>
  </table>

  <h2>Where the gain came from</h2>
  {% if result.per_instrument %}
    <table>
      <thead><tr><th>Symbol</th><th>Invested</th><th>Value</th><th>Gain</th><th>Share</th></tr></thead>
      <tbody>
        {% for row in result.per_instrument %}
          <tr>
            <td>{{ row.instrument_id }}</td>
            <td>₹{{ row.invested }}</td>
            <td>₹{{ row.value }}</td>
            <td>₹{{ row.gain }}</td>
            <td>{{ row.share_of_gain }}%</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="muted">Nothing is held yet.</p>
  {% endif %}

  <h2>Realised</h2>
  <p>
    Proceeds ₹{{ realised.total_proceeds }}, cost ₹{{ realised.total_cost }},
    gain ₹{{ realised.total_gain }}.
    {% if realised.any_estimated %}
      <span class="muted">Some basis is still estimated — a contract note may move these.</span>
    {% endif %}
  </p>
  <p class="muted">
    Economics only: no classification, no rates, no financial year. The broker's
    tax P&amp;L does that; this is what those numbers can be checked against.
  </p>
{% endblock %}
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest src/web/tests/ -v` → PASS

- [ ] **Step 5: Commit**

```bash
# /atomic:atomic-commit
# message: feat(web): show the return against the benchmark
```

---

### Task 13: The two ledger pages (M40a, M40b)

**Files:**
- Modify: `src/web/views.py`, `src/web/urls.py`
- Create: `src/templates/web/cash_ledger.html`, `src/templates/web/share_ledger.html`
- Test: `src/web/tests/test_dashboards.py` (append)

**Interfaces:**
- Consumes: `performance.ledgers.cash(start, end)`, `performance.ledgers.shares(...)`.
- Produces: URL names `web:cash_ledger` (`/ledger/cash/`) and
  `web:share_ledger` (`/ledger/shares/`), both accepting `?start=` and `?end=`
  as `YYYY-MM-DD`.

- [ ] **Step 1: Write the failing test**

```python
# -- the ledgers (M40a, M40b) --------------------------------------------


def test_the_cash_ledger_renders(signed_in):
    response = signed_in.get(reverse("web:cash_ledger"))

    assert response.status_code == 200


def test_the_cash_ledger_shows_whether_the_identity_holds(signed_in):
    response = signed_in.get(reverse("web:cash_ledger"))

    assert b"reconcile" in response.content.lower()


def test_the_cash_ledger_accepts_a_range(signed_in):
    response = signed_in.get(reverse("web:cash_ledger"), {"start": "2026-09-01"})

    assert response.status_code == 200


def test_a_bad_range_does_not_five_hundred(signed_in):
    response = signed_in.get(reverse("web:cash_ledger"), {"start": "not-a-date"})

    assert response.status_code == 200


def test_the_share_ledger_renders(signed_in):
    response = signed_in.get(reverse("web:share_ledger"))

    assert response.status_code == 200
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest src/web/tests/test_dashboards.py -k ledger -v`
Expected: FAIL — `NoReverseMatch`

- [ ] **Step 3: Add a date parser, the URLs, the views**

In `src/web/views.py`:

```python
def _date_arg(request: HttpRequest, name: str):
    """A range the URL asked for, or None.

    A malformed date is treated as absent rather than as an error: this is a
    reading surface, and a typo in a query string should not take the page
    away from the person trying to use it.
    """
    from datetime import date

    raw = request.GET.get(name, "")
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


@require_GET
def cash_ledger(request: HttpRequest) -> HttpResponse:
    """Every trading day since inception, and the identity it must satisfy."""
    from performance import ledgers

    return render(
        request,
        "web/cash_ledger.html",
        {
            "view": ledgers.cash(_date_arg(request, "start"), _date_arg(request, "end")),
            "start": request.GET.get("start", ""),
            "end": request.GET.get("end", ""),
        },
    )


@require_GET
def share_ledger(request: HttpRequest) -> HttpResponse:
    """Every quantity in and out, and where it disagrees with the broker."""
    from performance import ledgers

    return render(
        request,
        "web/share_ledger.html",
        {
            "view": ledgers.shares(
                instrument_id=request.GET.get("instrument") or None,
                start=_date_arg(request, "start"),
                end=_date_arg(request, "end"),
            ),
        },
    )
```

`src/web/urls.py`:

```python
    path("ledger/cash/", views.cash_ledger, name="cash_ledger"),
    path("ledger/shares/", views.share_ledger, name="share_ledger"),
```

- [ ] **Step 4: Write the templates**

`src/templates/web/cash_ledger.html`:

```html
{% extends "base.html" %}
{% block title %}Cash ledger — steadystack{% endblock %}
{% block content %}
  <h1>Cash ledger</h1>

  {% if view.identity_holds %}
    <p>Every rupee reconciles over this range: contributions − deployed − cash held = 0.</p>
  {% else %}
    <p class="errors">
      This range does not reconcile. {{ view.reconciliation.chain_breaks|length }} break(s):
    </p>
    <ul class="errors">
      {% for problem in view.reconciliation.chain_breaks %}
        <li>{{ problem.date }} — {{ problem.problem }}: expected {{ problem.expected }},
            found {{ problem.found }}</li>
      {% endfor %}
    </ul>
  {% endif %}

  <table>
    <thead>
      <tr><th>Date</th><th>Opening</th><th>Deposit</th><th>Lumpsum</th>
          <th>Adjustment</th><th>Spent</th><th>Charges</th><th>Closing</th></tr>
    </thead>
    <tbody>
      {% for row in view.rows %}
        <tr>
          <td>{{ row.trading_date }}</td>
          <td>₹{{ row.opening_carry }}</td>
          <td>₹{{ row.deposit }}</td>
          <td>₹{{ row.lumpsum }}</td>
          <td>₹{{ row.adjustment }}</td>
          <td>₹{{ row.cash_spent }}</td>
          <td>₹{{ row.charges }}</td>
          <td>₹{{ row.closing_carry }}</td>
        </tr>
      {% empty %}
        <tr><td colspan="8" class="muted">No day has settled yet.</td></tr>
      {% endfor %}
    </tbody>
    <tfoot>
      <tr>
        <th>Total</th>
        <td>₹{{ view.totals.opening_carry }}</td>
        <td>₹{{ view.totals.deposit }}</td>
        <td>₹{{ view.totals.lumpsum }}</td>
        <td>₹{{ view.totals.adjustment }}</td>
        <td>₹{{ view.totals.cash_spent }}</td>
        <td>₹{{ view.totals.charges }}</td>
        <td>₹{{ view.totals.closing_carry }}</td>
      </tr>
    </tfoot>
  </table>
{% endblock %}
```

`src/templates/web/share_ledger.html`:

```html
{% extends "base.html" %}
{% block title %}Share ledger — steadystack{% endblock %}
{% block content %}
  <h1>Share ledger</h1>

  {% if view.divergences %}
    <p class="errors">Our arithmetic and the broker's holding disagree:</p>
    <ul class="errors">
      {% for problem in view.divergences %}
        <li>{{ problem.instrument_id }} — we make it {{ problem.ours }},
            the broker says {{ problem.broker }}</li>
      {% endfor %}
    </ul>
  {% else %}
    <p>Every instrument's movements net to the quantity the broker holds.</p>
  {% endif %}

  <table>
    <thead><tr><th>Date</th><th>Symbol</th><th>Qty</th><th>Source</th><th>Reference</th></tr></thead>
    <tbody>
      {% for movement in view.movements %}
        <tr>
          <td>{{ movement.on }}</td>
          <td>{{ movement.instrument_id }}</td>
          <td>{{ movement.quantity }}</td>
          <td>{{ movement.source }}</td>
          <td class="muted">{{ movement.reference }}</td>
        </tr>
      {% empty %}
        <tr><td colspan="5" class="muted">No shares have moved yet.</td></tr>
      {% endfor %}
    </tbody>
  </table>
{% endblock %}
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest src/web/tests/ -v` → PASS

- [ ] **Step 6: Commit**

```bash
# /atomic:atomic-commit
# message: feat(web): add the cash and share ledger surfaces
```

---

### Task 14: Today's orders (M39) and navigation

**Files:**
- Modify: `src/web/views.py`, `src/web/urls.py`, `src/templates/base.html`
- Create: `src/templates/web/orders.html`
- Test: `src/web/tests/test_dashboards.py` (append)

**Interfaces:**
- Consumes: `orders.models.Order`, `orders.models.Tick`, `engine.models.TradeSheet`.
- Produces: URL name `web:orders` at `/orders/today/`, and a nav bar linking
  every dashboard.

- [ ] **Step 1: Write the failing test**

```python
# -- today's orders (M39) ------------------------------------------------


def test_todays_orders_renders(signed_in):
    response = signed_in.get(reverse("web:orders"))

    assert response.status_code == 200


def test_a_tick_that_placed_nothing_is_shown_with_its_reason(signed_in, a_quiet_tick):
    """Every tick writes a row, including the ones that place nothing. A page
    that showed only the orders would make a day look emptier than it was."""
    response = signed_in.get(reverse("web:orders"))

    assert b"no_place" in response.content or b"placed nothing" in response.content


def test_every_dashboard_is_reachable_from_the_nav(signed_in):
    response = signed_in.get(reverse("web:home"))

    for name in ("holdings", "performance", "cash_ledger", "share_ledger"):
        assert reverse(f"web:{name}").encode() in response.content
```

Build `a_quiet_tick` from `orders.models.Tick` with a `no_place_reason` set —
see `src/orders/models.py:141` for the field list and
`src/orders/tests/` for how existing tests construct one.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest src/web/tests/test_dashboards.py -k orders -v`
Expected: FAIL — `NoReverseMatch`

- [ ] **Step 3: Add the view, template and nav**

`src/web/urls.py`: `path("orders/today/", views.orders, name="orders"),`

`src/web/views.py`:

```python
@require_GET
def orders(request: HttpRequest) -> HttpResponse:
    """The day's ladder, including the ticks that placed nothing.

    A tick that placed nothing is a decision the system made and had a reason
    for. Showing only the orders would make the day look emptier than it was
    and hide the reason (M39).
    """
    from audit.context import today_ist
    from engine.models import TradeSheet
    from orders.models import Order, Tick

    day = _date_arg(request, "date") or today_ist()

    return render(
        request,
        "web/orders.html",
        {
            "day": day,
            "sheet": TradeSheet.objects.filter(trading_date=day)
            .select_related("instrument")
            .first(),
            "orders": Order.objects.filter(trading_date=day)
            .select_related("instrument")
            .order_by("tick_no"),
            "ticks": Tick.objects.filter(trading_date=day).order_by("tick_no"),
        },
    )
```

`src/templates/web/orders.html`:

```html
{% extends "base.html" %}
{% block title %}Orders — steadystack{% endblock %}
{% block content %}
  <h1>Orders — {{ day }}</h1>

  {% if sheet %}
    <p>
      {{ sheet.get_state_display }}:
      {% if sheet.instrument_id %}{{ sheet.quantity }} × {{ sheet.instrument_id }}
      {% else %}nothing chosen{% endif %},
      ₹{{ sheet.spendable_cash }} to spend.
    </p>
  {% else %}
    <p class="muted">No tradesheet for this day.</p>
  {% endif %}

  <h2>Orders</h2>
  <table>
    <thead><tr><th>Tick</th><th>Symbol</th><th>Qty</th><th>Filled</th>
               <th>Limit</th><th>State</th></tr></thead>
    <tbody>
      {% for order in orders %}
        <tr>
          <td>{{ order.tick_no }}</td>
          <td>{{ order.instrument_id }}</td>
          <td>{{ order.quantity }}</td>
          <td>{{ order.filled_quantity }}</td>
          <td>{% if order.limit_price %}₹{{ order.limit_price }}{% else %}market{% endif %}</td>
          <td>{{ order.get_state_display }}</td>
        </tr>
      {% empty %}
        <tr><td colspan="6" class="muted">Nothing was placed.</td></tr>
      {% endfor %}
    </tbody>
  </table>

  <h2>Ticks</h2>
  <table>
    <thead><tr><th>#</th><th>Bid</th><th>Ask</th><th>Slice</th><th>Outcome</th></tr></thead>
    <tbody>
      {% for tick in ticks %}
        <tr>
          <td>{{ tick.tick_no }}</td>
          <td>{% if tick.best_bid %}₹{{ tick.best_bid }}{% else %}—{% endif %}</td>
          <td>{% if tick.best_ask %}₹{{ tick.best_ask }}{% else %}—{% endif %}</td>
          <td>{{ tick.slice_qty }}</td>
          <td>
            {% if tick.order_id %}placed
            {% else %}<span class="muted">placed nothing — {{ tick.get_no_place_reason_display }}
              {% if tick.detail %}({{ tick.detail }}){% endif %}</span>
            {% endif %}
          </td>
        </tr>
      {% empty %}
        <tr><td colspan="5" class="muted">No tick has fired.</td></tr>
      {% endfor %}
    </tbody>
  </table>
{% endblock %}
```

In `src/templates/base.html`, inside the existing `<nav><div>`, after the
brand link:

```html
        {% if user.is_authenticated %}
          <a href="{% url 'web:holdings' %}">Holdings</a>
          <a href="{% url 'web:performance' %}">Performance</a>
          <a href="{% url 'web:orders' %}">Orders</a>
          <a href="{% url 'web:cash_ledger' %}">Cash</a>
          <a href="{% url 'web:share_ledger' %}">Shares</a>
          <a href="{% url 'engine:index' %}">Tradesheets</a>
        {% endif %}
```

Move the existing `{% if user.is_authenticated %}<span class="muted">…` block
so the username stays last rather than duplicating the condition.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all tests pass, including the 845 that passed before this plan began.

- [ ] **Step 5: Commit**

```bash
# /atomic:atomic-commit
# message: feat(web): show today's ladder and link the dashboards
```

---

### Task 15: Close Phase 3 in the plan and the docs

**Files:**
- Modify: `docs/plans/2026-09-22-feat-daily-equity-accumulation-system-plan.md`,
  `CLAUDE.md`

- [ ] **Step 1: Verify before claiming**

```bash
uv run ruff check src/ && uv run ruff format --check src/ && uv run pytest
```

All three must pass. Record the test count.

- [ ] **Step 2: Prove a past day renders from stored data alone**

Phase 3's stated verification is that a past day's detail renders with no
broker calls. Check it by hand:

```bash
uv run python manage.py shell -c "
from django.test import Client
# sign in as your verified test operator, then:
# c.get('/orders/today/?date=<a past trading date>')
"
```

Confirm no `BrokerCall` row is written by the request:

```bash
uv run python manage.py shell -c "
from broker.models import BrokerCall
print(BrokerCall.objects.count())
"
```

before and after. The counts must match. (`src/broker/models.py` holds the
table; confirm the model name there.)

- [ ] **Step 3: Tick the acceptance criteria**

In the Phase 3 plan's **Acceptance criteria → Functional** list, tick:

- "Realised and unrealised gains are exact and include every charge, reported
  as proceeds, cost basis and gain…"
- "Performance reports XIRR against the benchmark index over the same
  contribution dates"
- "Any past trading day is fully reconstructable from stored data alone,
  including the ticks that placed nothing"

Also tick the already-true-but-stale **"No day's buy carries a name past its
target weight"** — the cap is implemented in `src/engine/selection.py` and
tested at `src/engine/tests/test_tradesheet.py:419`.

Change the Phase 3 heading to `### Phase 3 — Local: performance — **built**`.

Leave "Configuration changes and model-portfolio versions are browsable in the
UI with actor, timestamp and before/after" **unticked** — no task here builds
it. It is a Phase 4 item or a follow-on.

- [ ] **Step 4: Update `CLAUDE.md`**

Change the Status section's first line from "Phases 1 and 2 of `docs/plans/`
are built" to name Phase 3, and add one sentence: the `performance` app holds
every reported number and the `web` app only renders them.

- [ ] **Step 5: Commit**

```bash
# /atomic:atomic-commit
# message: docs: mark phase 3 built
```

---

## Self-Review

**Spec coverage**

| Requirement | Task |
|---|---|
| M31b — figures withheld, portfolio flagged | 3, 4, 5, 7, 11, 12 |
| M37 — realised gains, no tax framing | 3 |
| M38 — unrealised gains per lot | 4 |
| M39 — today's orders, including ticks that placed nothing | 14 |
| M40 — holdings with weight, target, gap | 11 |
| M40a — cash ledger with reconciliation | 9, 13 |
| M40b — share ledger by source, reconciled to the broker | 10, 13 |
| M41 — XIRR, invested, value, return, per-instrument | 6, 7, 12 |
| M41a — benchmark XIRR over the same dates | 8, 12 |
| Daily portfolio-value series | 1, 2 |
| "Insufficient history" rather than a number | 6, 7, 12 |
| Past day renders from stored data, no broker calls | 15 |

**Known gaps, stated rather than hidden**

- **Reserve state is not on the holdings page.** The Phase 3 spec asks for
  "holdings with weight, target, gap *and reserve state*". Task 11 shows the
  first three. `engine.reserve.ReserveState` is constructed during tradesheet
  generation and is not persisted per instrument in a form a dashboard can
  read; wiring that is a small piece of engine work that belongs in its own
  task. **Add it as Task 11b if `ReserveState` turns out to be readable from
  the tradesheet's `ranking_snapshot`** — check
  `src/engine/tradesheet.py` first.
- **Corporate actions in the share ledger.** Task 10 covers fills and external
  transactions. A split supersedes lots without writing a fill, so it needs a
  fourth source; the task says to add it only after confirming what
  `records/corporate_actions.apply_ratio` writes, rather than guessing.
- **Configuration and model-portfolio version browsing** is in the acceptance
  criteria but has no task here. It is a configuration surface, not a
  performance one.

**Placeholder scan:** the three `NotImplementedError` / "read this first"
markers are deliberate — they point at existing code whose exact signature the
implementer must copy rather than at content this plan failed to supply. Every
other step carries its code.

**Type consistency:** `money()` (2dp) is used for every reported rupee figure;
`money4()` (4dp) only inside `valuation.py`, where it matches
`DailyValuation`'s column. `Flow.amount` is always `Decimal`, negative for
outflows, in both `returns.contribution_flows` and `returns.benchmark_xirr`.
`Performance.xirr` and `Performance.benchmark_xirr` are both
`Decimal | None` with a paired `*_unavailable: str`.
