# Double-Entry Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the daily cash ledger as the source of truth for money with a balanced, append-only double-entry journal that a chartered accountant can reconcile line by line against Zerodha's funds statement.

**Architecture:** A new `journal` app owns accounts, entries, lines, period closes, statement imports and balance checks, with every invariant (append-only, balanced, closed periods) enforced by Postgres triggers. The engine's carry becomes the balance of the *engine* cash account; fills, settlements, allocations, contributions and charges each post one entry through a single `journal.posting.post()`. `CashLedger` survives as a derived daily summary per book.

**Tech Stack:** Python 3.12, Django ≥ 5.1, PostgreSQL (plpgsql triggers), pytest + pytest-django, ruff.

**Spec:** `docs/superpowers/specs/2026-09-24-double-entry-journal-design.md` — read it before starting any task. Section numbers below (§4, §7.2 …) refer to it.

## Global Constraints

- Amounts in the journal are `DecimalField(max_digits=20, decimal_places=6)`; quantise with `journal.posting.q6()`. Screens show paise via `engine.ledger.money()`. Never use float.
- Debit is a positive `JournalLine.amount`, credit negative. Every entry sums to exactly zero and has at least two lines.
- Account codes are exactly those of spec §4: 1100, 1110, 1200, 1210, 1300, 3100, 3200, 3300, 3900, 4100, 4200, 5100. 3900 exists only in the paper book.
- Books are `live` and `paper`, the values of `orders.models.OrderMode`. A fill's book is its order's `mode`; an allocation's book is `configuration.store.current().order_mode`; contributions, withdrawals, account charges, dividends, external transactions and statement lines are always `live`.
- Every entry carries `trace_id=current_trace_id()` (from `audit.context`) and writes one audit event via `audit.events.record`.
- Append-only tables use the existing `steadystack_reject_mutation()` trigger plus `REVOKE UPDATE, DELETE ... FROM steadystack_app`, copied from `src/engine/migrations/0003_ledger_append_only.py`.
- The runtime role has UPDATE revoked on append-only tables, so **never** `select_for_update()` on them: Postgres requires UPDATE privilege for a row lock. Serialise with `pg_advisory_xact_lock`, as `configuration/store.py:_append_version` does.
- Nothing in this plan models tax.
- Lines ≤ 100 chars; ruff must pass (`uv run ruff check src && uv run ruff format --check src`).
- Commits: use `/atomic:atomic-commit` (the project rule); scope `journal` for the new app, otherwise the CLAUDE.md scopes. Never add Co-authored-by trailers.

**Running tests.** The worktree's `.env` points `DATABASE_URL` at the runtime role, which cannot create a test database. Always run:

```bash
DATABASE_URL=postgres://steadystack_owner:steadystack_owner_dev@localhost:5432/steadystack_journal \
  uv run pytest -p no:cacheprovider <paths> -q
```

Below this is abbreviated `PYTEST <paths>`.

**Tests that need a deferred trigger to fire.** pytest-django wraps each test in a transaction that never commits, so a `DEFERRABLE INITIALLY DEFERRED` trigger never runs. Force it inside the test with:

```python
from django.db import connection
with connection.cursor() as c:
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
```

## Review Focus

1. **Planning a day twice** (Generate, Regenerate, the 09:45 timer firing again) must post exactly one daily allocation per day and book — pinned in Task 8 `test_regenerating_a_day_does_not_allocate_twice`.
2. **A fill booked twice** (a replayed postback reaching `orders.booking.book` again) must not post a second fill entry — pinned in Task 6 `test_booking_a_fill_twice_posts_one_entry`.
3. **The same Console CSV uploaded twice, or two months that overlap**, must add no line twice and refuse a file whose opening balance does not continue the stored chain — pinned in Task 11 `test_an_overlapping_upload_adds_only_new_lines` and `test_a_broken_balance_chain_is_refused`.
4. **A lumpsum recorded in the app whose UPI transfer later appears on the statement** must link to the existing contribution, not post a second one — pinned in Task 12 `test_a_recorded_lumpsum_matches_its_upi_line`.
5. **A quarterly sweep that takes more than unallocated holds, followed by a contribution smaller than the carry owed**, must restore only what arrived and leave the rest owed — pinned in Task 5 `test_a_small_contribution_restores_carry_in_part`.

---

## File Structure

New app `src/journal/`:

| File | Responsibility |
|---|---|
| `models.py` | `Book`, `AccountType`, `Account`, `EntryKind`, `Component`, `JournalEntry`, `JournalLine`, `PeriodClose`, `StatementUpload`, `StatementLine`, `BalanceCheck` |
| `accounts.py` | Account-code constants and the chart (single source for the data migration) |
| `posting.py` | `Line`, `JournalError`, `q6`, `post()`, `reverse()` — the only writer of entries |
| `balances.py` | `balance()`, `engine_balance()`, `zerodha_cash()`, `trial_balance()`, `general_ledger()` |
| `cash.py` | contributions, withdrawals (incl. the quarterly sweep), allocations, lumpsums, carry owed |
| `trades.py` | fill entries, settlement entries, reinvested proceeds |
| `external.py` | entries for external transactions, dividends, the opening balance |
| `periods.py` | `close_year()`, `fy_end_for()` |
| `statement/parse.py` | Console CSV → rows, fingerprints, running-balance check |
| `statement/store.py` | keep an upload verbatim, add each line once, refuse a broken chain |
| `statement/classify.py` | proposals per row (§7.2 table) |
| `statement/reconcile.py` | match rows, confirm a row, settlement differences |
| `checks.py` | the daily balance check against Kite margins, funding warnings, journal-vs-lots |
| `performance.py` | XIRR cash flows and money contributed, per book |
| `views.py`, `urls.py`, `forms.py` | records screens: statement, queue, lumpsum, dividend, reverse, reports |
| `management/commands/open_books.py` | go-live opening entry |
| `management/commands/check_balance.py` | the morning balance check |
| `migrations/0001…` | schema, chart data, triggers |
| `tests/` | one test module per source module |

Modified: `engine/ledger.py` (rewritten), `engine/models.py` (`CashLedger` reshaped; `LumpsumCredit`, `LedgerAdjustment` removed), `engine/tradesheet.py`, `engine/settlement.py`, `engine/day.py`, `engine/views.py`, `engine/management/commands/seed_demo.py`, `orders/booking.py`, `rebalance/models.py`, `rebalance/ladder.py`, `records/models.py`, `records/external.py`, `portfolio/contract_note.py`, `portfolio/lots.py`, `performance/ledgers.py`, `performance/returns.py`, `performance/series.py`, `performance/valuation.py`, `web/views.py`, `web/deck.py`, templates under `src/templates/`, `config/settings/base.py`, `config/urls.py`, `CLAUDE.md`, the PRD, `OPERATIONS.md`.

---

### Task 1: The journal app, its models and the chart of accounts

**Files:**
- Create: `src/journal/__init__.py`, `src/journal/apps.py`, `src/journal/accounts.py`, `src/journal/models.py`, `src/journal/migrations/__init__.py`, `src/journal/migrations/0001_initial.py` (generated), `src/journal/migrations/0002_chart_of_accounts.py`, `src/journal/tests/__init__.py`, `src/journal/tests/test_models.py`
- Modify: `src/config/settings/base.py` (INSTALLED_APPS), `pyproject.toml` (`known-first-party` gains `"journal"`)

**Interfaces:**
- Produces: `journal.models.Book` (`LIVE="live"`, `PAPER="paper"`), `EntryKind`, `Component`, `Account`, `JournalEntry`, `JournalLine`, `PeriodClose`; `journal.accounts` constants `UNALLOCATED="1100"`, `ENGINE_CASH="1110"`, `SETTLE_ENGINE="1200"`, `SETTLE_OTHER="1210"`, `INVESTMENTS="1300"`, `CONTRIBUTIONS="3100"`, `WITHDRAWALS="3200"`, `OPENING="3300"`, `PAPER_FUNDING="3900"`, `REALISED="4100"`, `DIVIDENDS="4200"`, `ACCOUNT_CHARGES="5100"`, `ENGINE_ACCOUNTS=(ENGINE_CASH, SETTLE_ENGINE)`, `ZERODHA_ACCOUNTS=(UNALLOCATED, ENGINE_CASH, SETTLE_ENGINE, SETTLE_OTHER)`, `PAPER_ONLY={PAPER_FUNDING}`, `CHART` (tuple of `(code, name, type)`).

- [ ] **Step 1: Write the failing test**

`src/journal/tests/test_models.py`:

```python
"""The chart exists, and an entry is shaped the way the rest of the plan assumes."""

from datetime import date
from decimal import Decimal

import pytest

from journal import accounts
from journal.models import Account, AccountType, Book, EntryKind, JournalEntry, JournalLine


@pytest.mark.django_db
def test_the_chart_of_accounts_is_installed():
    codes = set(Account.objects.values_list("code", flat=True))
    assert codes == {code for code, _name, _type in accounts.CHART}
    assert Account.objects.get(code=accounts.ENGINE_CASH).type == AccountType.ASSET
    assert Account.objects.get(code=accounts.CONTRIBUTIONS).type == AccountType.EQUITY
    assert Account.objects.get(code=accounts.ACCOUNT_CHARGES).type == AccountType.EXPENSE


@pytest.mark.django_db
def test_an_entry_holds_signed_six_decimal_lines():
    entry = JournalEntry.objects.create(
        book=Book.LIVE,
        number=1,
        kind=EntryKind.CONTRIBUTION,
        occurred_on=date(2026, 9, 18),
        description="funds added",
        actor="operator",
    )
    JournalLine.objects.create(entry=entry, account_id=accounts.UNALLOCATED, amount=Decimal("1000.000220"))
    JournalLine.objects.create(entry=entry, account_id=accounts.CONTRIBUTIONS, amount=Decimal("-1000.000220"))

    amounts = sorted(line.amount for line in entry.lines.all())
    assert amounts == [Decimal("-1000.000220"), Decimal("1000.000220")]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTEST src/journal/tests/test_models.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'journal'`.

- [ ] **Step 3: Write the app**

`src/journal/__init__.py`: empty. `src/journal/tests/__init__.py`, `src/journal/migrations/__init__.py`: empty.

`src/journal/apps.py`:

```python
from django.apps import AppConfig


class JournalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "journal"
```

`src/journal/accounts.py`:

```python
"""The chart of accounts (spec §4).

Codes rather than names are what the rest of the system refers to, because a
name is copy and a code is an identifier. The chart is fixed: accounts are
installed by a data migration and never deleted, so a report joining to them
can never lose a row.
"""

UNALLOCATED = "1100"
ENGINE_CASH = "1110"
SETTLE_ENGINE = "1200"
SETTLE_OTHER = "1210"
INVESTMENTS = "1300"
CONTRIBUTIONS = "3100"
WITHDRAWALS = "3200"
OPENING = "3300"
PAPER_FUNDING = "3900"
REALISED = "4100"
DIVIDENDS = "4200"
ACCOUNT_CHARGES = "5100"

#: What the engine may spend is the balance of these two (spec §4).
ENGINE_ACCOUNTS = (ENGINE_CASH, SETTLE_ENGINE)

#: Zerodha's cash, as the CA reconciles it.
ZERODHA_ACCOUNTS = (UNALLOCATED, ENGINE_CASH, SETTLE_ENGINE, SETTLE_OTHER)

#: Accounts that may only appear in the paper book.
PAPER_ONLY = frozenset({PAPER_FUNDING})

CHART = (
    (UNALLOCATED, "Zerodha cash: unallocated", "asset"),
    (ENGINE_CASH, "Zerodha cash: engine", "asset"),
    (SETTLE_ENGINE, "Broker settlement: engine", "asset"),
    (SETTLE_OTHER, "Broker settlement: other", "asset"),
    (INVESTMENTS, "Investments", "asset"),
    (CONTRIBUTIONS, "Contributions", "equity"),
    (WITHDRAWALS, "Withdrawals", "equity"),
    (OPENING, "Opening balance", "equity"),
    (PAPER_FUNDING, "Paper funding", "equity"),
    (REALISED, "Realised gains", "income"),
    (DIVIDENDS, "Dividends", "income"),
    (ACCOUNT_CHARGES, "Account charges", "expense"),
)
```

`src/journal/models.py`:

```python
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
```

Add `"journal",` to `INSTALLED_APPS` in `src/config/settings/base.py` directly after `"gains",`, and `"journal"` to `known-first-party` in `pyproject.toml`.

- [ ] **Step 4: Generate the schema migration and write the chart migration**

Run: `DATABASE_URL=postgres://steadystack_owner:steadystack_owner_dev@localhost:5432/steadystack_journal uv run python manage.py makemigrations journal`
Expected: `src/journal/migrations/0001_initial.py` created. Check that it depends on the latest `portfolio` migration.

`src/journal/migrations/0002_chart_of_accounts.py`:

```python
from django.db import migrations

from journal.accounts import CHART


def install(apps, schema_editor):
    Account = apps.get_model("journal", "Account")
    for code, name, type_ in CHART:
        Account.objects.update_or_create(code=code, defaults={"name": name, "type": type_})


class Migration(migrations.Migration):
    dependencies = [("journal", "0001_initial")]

    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `PYTEST src/journal/tests/test_models.py`
Expected: 2 passed.

- [ ] **Step 6: Commit**

Stage `src/journal/`, `src/config/settings/base.py`, `pyproject.toml`; commit `feat(journal): add accounts, entries and lines`.

---

### Task 2: The database refuses edits, unbalanced entries and closed years

**Files:**
- Create: `src/journal/migrations/0003_enforcement.py`, `src/journal/tests/test_enforcement.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces: triggers `journal_journalentry_append_only`, `journal_journalline_append_only`, `journal_periodclose_append_only`, `journal_line_balanced` (deferred), `journal_entry_has_lines` (deferred), `journal_entry_period_open`. Later tasks rely on Postgres raising `django.db.utils.InternalError` (plpgsql `RAISE EXCEPTION`) for each.

- [ ] **Step 1: Write the failing tests**

`src/journal/tests/test_enforcement.py`:

```python
"""Each invariant is a property of the database, not of this code (spec §9)."""

from datetime import date
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.db.utils import InternalError

from journal import accounts
from journal.models import Book, EntryKind, JournalEntry, JournalLine, PeriodClose


def _entry(number=1, occurred_on=date(2026, 9, 18), book=Book.LIVE):
    return JournalEntry.objects.create(
        book=book,
        number=number,
        kind=EntryKind.CONTRIBUTION,
        occurred_on=occurred_on,
        description="test",
        actor="test",
    )


def _lines(entry, amount=Decimal("100")):
    JournalLine.objects.create(entry=entry, account_id=accounts.UNALLOCATED, amount=amount)
    JournalLine.objects.create(entry=entry, account_id=accounts.CONTRIBUTIONS, amount=-amount)


def _check_now():
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.django_db
def test_a_balanced_entry_commits():
    _lines(_entry())
    _check_now()


@pytest.mark.django_db
def test_an_unbalanced_entry_is_refused_at_commit():
    entry = _entry()
    JournalLine.objects.create(entry=entry, account_id=accounts.UNALLOCATED, amount=Decimal("100"))
    JournalLine.objects.create(entry=entry, account_id=accounts.CONTRIBUTIONS, amount=Decimal("-99"))
    with pytest.raises(InternalError, match="does not balance"):
        _check_now()


@pytest.mark.django_db
def test_an_entry_with_one_line_is_refused():
    entry = _entry()
    JournalLine.objects.create(entry=entry, account_id=accounts.UNALLOCATED, amount=Decimal("0"))
    with pytest.raises(InternalError, match="does not balance"):
        _check_now()


@pytest.mark.django_db
def test_an_entry_with_no_lines_is_refused():
    _entry()
    with pytest.raises(InternalError, match="has no lines"):
        _check_now()


@pytest.mark.django_db
def test_an_entry_cannot_be_edited():
    entry = _entry()
    _lines(entry)
    with pytest.raises(InternalError), transaction.atomic():
        JournalEntry.objects.filter(pk=entry.pk).update(description="changed")


@pytest.mark.django_db
def test_a_line_cannot_be_edited_or_deleted():
    entry = _entry()
    _lines(entry)
    line = entry.lines.first()
    with pytest.raises(InternalError), transaction.atomic():
        JournalLine.objects.filter(pk=line.pk).update(amount=Decimal("1"))
    with pytest.raises(InternalError), transaction.atomic():
        JournalLine.objects.filter(pk=line.pk).delete()


@pytest.mark.django_db
def test_a_closed_year_accepts_no_entry_dated_inside_it():
    PeriodClose.objects.create(book=Book.LIVE, fy_end=date(2026, 3, 31), closed_by="ca")
    with pytest.raises(InternalError, match="closed"), transaction.atomic():
        _entry(occurred_on=date(2026, 3, 31))


@pytest.mark.django_db
def test_a_closed_year_in_one_book_does_not_close_the_other():
    PeriodClose.objects.create(book=Book.LIVE, fy_end=date(2026, 3, 31), closed_by="ca")
    _lines(_entry(occurred_on=date(2026, 3, 31), book=Book.PAPER))
    _check_now()


@pytest.mark.django_db
def test_the_day_after_a_closed_year_is_open():
    PeriodClose.objects.create(book=Book.LIVE, fy_end=date(2026, 3, 31), closed_by="ca")
    _lines(_entry(occurred_on=date(2026, 4, 1)))
    _check_now()
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTEST src/journal/tests/test_enforcement.py`
Expected: the refusal tests FAIL with `DID NOT RAISE`; the two positive tests pass.

- [ ] **Step 3: Write the migration**

`src/journal/migrations/0003_enforcement.py`:

```python
"""The journal's invariants, enforced by the database (spec §9).

Triggers rather than grants alone, for the reason audit/0002 gives: migrations
and tests connect as the schema owner, and grants do not restrain an owner.

The balance check is a *deferred constraint trigger*: an entry is inserted
first and its lines after it, so the sum can only be judged at commit.
"""

from django.db import migrations

APPEND_ONLY = ["journal_journalentry", "journal_journalline", "journal_periodclose"]

FUNCTIONS = """
CREATE OR REPLACE FUNCTION journal_check_balanced() RETURNS trigger AS $$
DECLARE
    target uuid;
    total numeric;
    n integer;
BEGIN
    IF TG_TABLE_NAME = 'journal_journalentry' THEN
        target := NEW.id;
    ELSE
        target := NEW.entry_id;
    END IF;

    SELECT coalesce(sum(amount), 0), count(*) INTO total, n
      FROM journal_journalline WHERE entry_id = target;

    IF n = 0 THEN
        RAISE EXCEPTION 'journal entry % has no lines', target;
    END IF;
    IF total <> 0 OR n < 2 THEN
        RAISE EXCEPTION 'journal entry % does not balance (sum %, % lines)', target, total, n;
    END IF;
    RETURN NULL;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION journal_check_period_open() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM journal_periodclose
         WHERE book = NEW.book AND fy_end >= NEW.occurred_on
    ) THEN
        RAISE EXCEPTION 'journal entry dated % falls in a closed financial year of the % book',
            NEW.occurred_on, NEW.book;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTIONS = """
DROP FUNCTION IF EXISTS journal_check_balanced();
DROP FUNCTION IF EXISTS journal_check_period_open();
"""

CHECKS = """
CREATE CONSTRAINT TRIGGER journal_line_balanced
    AFTER INSERT ON journal_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION journal_check_balanced();

CREATE CONSTRAINT TRIGGER journal_entry_has_lines
    AFTER INSERT ON journal_journalentry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION journal_check_balanced();

CREATE TRIGGER journal_entry_period_open
    BEFORE INSERT ON journal_journalentry
    FOR EACH ROW EXECUTE FUNCTION journal_check_period_open();
"""

DROP_CHECKS = """
DROP TRIGGER IF EXISTS journal_line_balanced ON journal_journalline;
DROP TRIGGER IF EXISTS journal_entry_has_lines ON journal_journalentry;
DROP TRIGGER IF EXISTS journal_entry_period_open ON journal_journalentry;
"""


def _append_only(table: str) -> str:
    return f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION steadystack_reject_mutation();
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        REVOKE UPDATE, DELETE ON {table} FROM steadystack_app;
    END IF;
END
$$;
"""


def _undo_append_only(table: str) -> str:
    return f"""
DROP TRIGGER IF EXISTS {table}_append_only ON {table};
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        GRANT UPDATE, DELETE ON {table} TO steadystack_app;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0002_chart_of_accounts"),
        ("audit", "0002_append_only"),
    ]

    operations = [
        migrations.RunSQL(FUNCTIONS, DROP_FUNCTIONS),
        migrations.RunSQL(CHECKS, DROP_CHECKS),
        *[migrations.RunSQL(_append_only(t), _undo_append_only(t)) for t in APPEND_ONLY],
    ]
```

The `journal_account` table is not append-only: the chart migration uses `update_or_create`. It is protected instead by `on_delete=PROTECT` from every line.

- [ ] **Step 4: Run to verify they pass**

Run: `PYTEST src/journal/tests/`
Expected: all pass.

- [ ] **Step 5: Commit**

`feat(journal): enforce balance, append-only and closed years in the database`

---

### Task 3: Posting, reversing and reading balances

**Files:**
- Create: `src/journal/posting.py`, `src/journal/balances.py`, `src/journal/tests/test_posting.py`, `src/journal/tests/test_balances.py`

**Interfaces:**
- Consumes: Task 1 models and `accounts`.
- Produces:
  - `posting.q6(value) -> Decimal` (6 dp, ROUND_HALF_UP)
  - `@dataclass(frozen=True) posting.Line(account: str, amount: Decimal, instrument_id: str | None = None, component: str = "")`
  - `posting.JournalError(Exception)`
  - `posting.post(*, book: str, kind: str, occurred_on: date, lines: list[Line], description: str, actor: str = SYSTEM_ACTOR, source_type: str = "", source_id: str = "", idempotency_key: str | None = None, reverses: JournalEntry | None = None) -> JournalEntry`. It is idempotent on `idempotency_key` and returns the existing entry.
  - `posting.reverse(entry: JournalEntry, *, reason: str, actor: str, on: date | None = None) -> JournalEntry`
  - `balances.balance(accounts: str | Iterable[str], *, book: str, through: date | None = None, on: date | None = None, instrument_id: str | None = None, kinds: Iterable[str] | None = None, source_type: str | None = None) -> Decimal`. It returns the raw signed sum (debit positive).
  - `balances.engine_balance(book: str, *, through: date | None = None) -> Decimal`, the sum of `ENGINE_ACCOUNTS`
  - `balances.zerodha_cash(book: str, *, through: date | None = None) -> Decimal`, the sum of `ZERODHA_ACCOUNTS`

- [ ] **Step 1: Write the failing tests**

`src/journal/tests/test_posting.py`:

```python
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection

from audit.models import AuditEvent
from journal import accounts
from journal.models import Book, EntryKind, JournalEntry
from journal.posting import JournalError, Line, post, reverse

D = date(2026, 9, 18)


def _contribution(amount="1000", **kwargs):
    return post(
        book=kwargs.pop("book", Book.LIVE),
        kind=EntryKind.CONTRIBUTION,
        occurred_on=kwargs.pop("occurred_on", D),
        description="funds added",
        lines=[
            Line(accounts.UNALLOCATED, Decimal(amount)),
            Line(accounts.CONTRIBUTIONS, -Decimal(amount)),
        ],
        **kwargs,
    )


@pytest.mark.django_db
def test_post_writes_the_entry_its_lines_and_an_audit_event():
    entry = _contribution()
    assert entry.number == 1
    assert entry.lines.count() == 2
    assert entry.trace_id
    assert AuditEvent.objects.filter(action="journal.posted", entity_id=str(entry.id)).exists()


@pytest.mark.django_db
def test_numbers_are_gapless_per_book():
    assert _contribution().number == 1
    assert _contribution().number == 2
    assert _contribution(book=Book.PAPER, occurred_on=D).number == 1


@pytest.mark.django_db
def test_an_unbalanced_entry_is_refused_before_the_database_sees_it():
    with pytest.raises(JournalError, match="balance"):
        post(
            book=Book.LIVE,
            kind=EntryKind.CONTRIBUTION,
            occurred_on=D,
            description="x",
            lines=[Line(accounts.UNALLOCATED, Decimal("1")), Line(accounts.CONTRIBUTIONS, Decimal("-2"))],
        )
    assert JournalEntry.objects.count() == 0


@pytest.mark.django_db
def test_zero_lines_are_dropped_and_an_entry_left_with_fewer_than_two_is_refused():
    with pytest.raises(JournalError, match="two lines"):
        post(
            book=Book.LIVE,
            kind=EntryKind.CONTRIBUTION,
            occurred_on=D,
            description="x",
            lines=[Line(accounts.UNALLOCATED, Decimal("0")), Line(accounts.CONTRIBUTIONS, Decimal("0"))],
        )


@pytest.mark.django_db
def test_paper_funding_cannot_appear_in_the_live_book():
    with pytest.raises(JournalError, match="paper"):
        post(
            book=Book.LIVE,
            kind=EntryKind.ALLOCATION,
            occurred_on=D,
            description="x",
            lines=[Line(accounts.ENGINE_CASH, Decimal("1")), Line(accounts.PAPER_FUNDING, Decimal("-1"))],
        )


@pytest.mark.django_db
def test_an_idempotency_key_returns_the_first_entry():
    first = _contribution(idempotency_key="k1")
    second = _contribution(idempotency_key="k1")
    assert first.pk == second.pk
    assert JournalEntry.objects.count() == 1


@pytest.mark.django_db
def test_amounts_are_kept_to_six_decimals():
    entry = _contribution("248.221084")
    assert sorted(line.amount for line in entry.lines.all())[1] == Decimal("248.221084")


@pytest.mark.django_db
def test_a_reversal_mirrors_the_entry_and_points_at_it():
    entry = _contribution("500")
    undone = reverse(entry, reason="recorded twice", actor="operator")
    assert undone.kind == EntryKind.REVERSAL
    assert undone.reverses_id == entry.id
    assert sorted(line.amount for line in undone.lines.all()) == [Decimal("-500"), Decimal("500")]
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.django_db
def test_an_entry_is_reversed_at_most_once_and_a_reversal_never():
    entry = _contribution("500")
    undone = reverse(entry, reason="once", actor="operator")
    with pytest.raises(JournalError, match="already reversed"):
        reverse(entry, reason="twice", actor="operator")
    with pytest.raises(JournalError, match="reversal"):
        reverse(undone, reason="undo the undo", actor="operator")


@pytest.mark.django_db
def test_posting_takes_no_row_lock_on_an_append_only_table():
    """The runtime role cannot lock rows it may not update (see configuration/store.py)."""
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        _contribution()
    assert not [q for q in queries if "for update" in q["sql"].lower()]
```

`src/journal/tests/test_balances.py`:

```python
from datetime import date
from decimal import Decimal

import pytest

from journal import accounts
from journal.balances import balance, engine_balance, zerodha_cash
from journal.models import Book, EntryKind
from journal.posting import Line, post

MON, TUE = date(2026, 9, 21), date(2026, 9, 22)


def _post(on, lines, book=Book.LIVE, kind=EntryKind.ALLOCATION):
    return post(book=book, kind=kind, occurred_on=on, description="t", lines=lines)


@pytest.mark.django_db
def test_balance_sums_signed_lines_through_a_date():
    _post(MON, [Line(accounts.ENGINE_CASH, Decimal("1000")), Line(accounts.UNALLOCATED, Decimal("-1000"))])
    _post(TUE, [Line(accounts.ENGINE_CASH, Decimal("500")), Line(accounts.UNALLOCATED, Decimal("-500"))])

    assert balance(accounts.ENGINE_CASH, book=Book.LIVE) == Decimal("1500")
    assert balance(accounts.ENGINE_CASH, book=Book.LIVE, through=MON) == Decimal("1000")
    assert balance(accounts.ENGINE_CASH, book=Book.LIVE, on=TUE) == Decimal("500")
    assert balance(accounts.ENGINE_CASH, book=Book.PAPER) == Decimal("0")


@pytest.mark.django_db
def test_engine_balance_nets_unsettled_engine_buys():
    _post(MON, [Line(accounts.ENGINE_CASH, Decimal("1000")), Line(accounts.UNALLOCATED, Decimal("-1000"))])
    _post(
        MON,
        [Line(accounts.INVESTMENTS, Decimal("400")), Line(accounts.SETTLE_ENGINE, Decimal("-400"))],
        kind=EntryKind.BUY_FILL,
    )
    assert engine_balance(Book.LIVE) == Decimal("600")
    assert zerodha_cash(Book.LIVE) == Decimal("-400")
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTEST src/journal/tests/test_posting.py src/journal/tests/test_balances.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'journal.posting'`.

- [ ] **Step 3: Implement**

`src/journal/posting.py`:

```python
"""The only way an entry is written.

`post()` checks in Python what the database will check again at commit --
balance, at least two lines, paper-only accounts -- so a mistake fails at the
call that made it rather than at a commit three frames away. The database
check stays, because code is not the only thing that can write a row.

Numbers are gapless per book, allocated under an advisory lock. Not
`SELECT ... FOR UPDATE`: the runtime role has UPDATE revoked on this table,
and Postgres refuses a row lock without it.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import connection, transaction
from django.db.models import Max

from audit.context import current_trace_id
from audit.events import SYSTEM_ACTOR, record
from audit.logging import fields
from journal.accounts import PAPER_ONLY
from journal.models import Book, EntryKind, JournalEntry, JournalLine

logger = logging.getLogger("steadystack.journal")

SIX = Decimal("0.000001")
ZERO = Decimal("0")

#: One advisory-lock key per book, so the books never wait on each other.
_NUMBER_LOCKS = {Book.LIVE: 7_310_442_901_115_001, Book.PAPER: 7_310_442_901_115_002}


class JournalError(Exception):
    """An entry that would break the journal's rules."""


@dataclass(frozen=True)
class Line:
    account: str
    amount: Decimal
    instrument_id: str | None = None
    component: str = ""


def q6(value) -> Decimal:
    return Decimal(value).quantize(SIX, rounding=ROUND_HALF_UP)


def _checked(book: str, lines: Iterable[Line]) -> list[Line]:
    kept = [
        Line(line.account, q6(line.amount), line.instrument_id, line.component)
        for line in lines
        if q6(line.amount) != ZERO
    ]
    if len(kept) < 2:
        raise JournalError("An entry needs at least two lines with an amount.")
    total = sum((line.amount for line in kept), ZERO)
    if total != ZERO:
        raise JournalError(f"The lines do not balance: they sum to {total}.")
    if book == Book.LIVE and any(line.account in PAPER_ONLY for line in kept):
        raise JournalError("Paper funding is a paper-book account.")
    return kept


@transaction.atomic
def post(
    *,
    book: str,
    kind: str,
    occurred_on: date,
    lines: list[Line],
    description: str,
    actor: str = SYSTEM_ACTOR,
    source_type: str = "",
    source_id: str = "",
    idempotency_key: str | None = None,
    reverses: JournalEntry | None = None,
) -> JournalEntry:
    """Append one balanced entry. Idempotent on `idempotency_key`."""
    if idempotency_key:
        existing = JournalEntry.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing

    kept = _checked(book, lines)

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_NUMBER_LOCKS[book]])
    last = JournalEntry.objects.filter(book=book).aggregate(n=Max("number"))["n"] or 0

    entry = JournalEntry.objects.create(
        book=book,
        number=last + 1,
        kind=kind,
        occurred_on=occurred_on,
        description=description[:255],
        source_type=source_type,
        source_id=str(source_id),
        idempotency_key=idempotency_key,
        reverses=reverses,
        actor=actor,
        trace_id=current_trace_id(),
    )
    JournalLine.objects.bulk_create(
        [
            JournalLine(
                entry=entry,
                account_id=line.account,
                amount=line.amount,
                instrument_id=line.instrument_id,
                component=line.component,
            )
            for line in kept
        ]
    )

    record(
        "journal.posted",
        actor=actor,
        entity=entry,
        after={
            "book": book,
            "number": entry.number,
            "kind": kind,
            "occurred_on": str(occurred_on),
            "lines": [[line.account, str(line.amount), line.instrument_id or ""] for line in kept],
        },
    )
    logger.info(
        "journal entry posted",
        extra=fields(book=book, number=entry.number, kind=kind, occurred_on=str(occurred_on)),
    )
    return entry


def reverse(
    entry: JournalEntry, *, reason: str, actor: str, on: date | None = None
) -> JournalEntry:
    """Undo an entry by appending its mirror (spec §6, `reversal`)."""
    from audit.context import today_ist

    if entry.kind == EntryKind.REVERSAL:
        raise JournalError("A reversal is not reversed; post the entry again instead.")
    if JournalEntry.objects.filter(reverses=entry).exists():
        raise JournalError("That entry is already reversed.")

    return post(
        book=entry.book,
        kind=EntryKind.REVERSAL,
        occurred_on=on or today_ist(),
        description=f"reverses #{entry.number}: {reason}",
        lines=[
            Line(line.account_id, -line.amount, line.instrument_id, line.component)
            for line in entry.lines.all()
        ],
        actor=actor,
        source_type="journal_entry",
        source_id=str(entry.id),
        idempotency_key=f"reversal:{entry.id}",
        reverses=entry,
    )
```

`src/journal/balances.py`:

```python
"""Reading the journal: balances, and the reports built from them."""

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from journal.accounts import ENGINE_ACCOUNTS, ZERODHA_ACCOUNTS
from journal.models import JournalLine
from journal.posting import q6

ZERO = Decimal("0")


def _lines(
    accounts: str | Iterable[str],
    *,
    book: str,
    through: date | None = None,
    on: date | None = None,
    instrument_id: str | None = None,
    kinds: Iterable[str] | None = None,
    source_type: str | None = None,
):
    codes = [accounts] if isinstance(accounts, str) else list(accounts)
    query = JournalLine.objects.filter(entry__book=book, account_id__in=codes)
    if through is not None:
        query = query.filter(entry__occurred_on__lte=through)
    if on is not None:
        query = query.filter(entry__occurred_on=on)
    if instrument_id is not None:
        query = query.filter(instrument_id=instrument_id)
    if kinds is not None:
        query = query.filter(entry__kind__in=list(kinds))
    if source_type is not None:
        query = query.filter(entry__source_type=source_type)
    return query


def balance(accounts, **filters) -> Decimal:
    """The signed sum of the lines: debit positive, credit negative."""
    total = _lines(accounts, **filters).aggregate(total=Sum("amount"))["total"]
    return q6(total or ZERO)


def engine_balance(book: str, *, through: date | None = None) -> Decimal:
    """Engine cash less what its own unsettled buys owe. May be negative (D7)."""
    return balance(ENGINE_ACCOUNTS, book=book, through=through)


def zerodha_cash(book: str, *, through: date | None = None) -> Decimal:
    """Zerodha's cash as the statement shows it, including what is in flight."""
    return balance(ZERODHA_ACCOUNTS, book=book, through=through)
```

`trial_balance` and `general_ledger` are added in Task 16.

- [ ] **Step 4: Run to verify they pass**

Run: `PYTEST src/journal/tests/`
Expected: all pass.

- [ ] **Step 5: Commit**

`feat(journal): post, reverse and read balances`

---

### Task 4: Closing a financial year

**Files:**
- Create: `src/journal/periods.py`, `src/journal/tests/test_periods.py`

**Interfaces:**
- Consumes: `PeriodClose`, the period trigger from Task 2.
- Produces: `periods.fy_end_for(day: date) -> date`, `periods.close_year(*, book: str, fy_end: date, actor: str) -> PeriodClose`, `periods.is_closed(book: str, day: date) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date

import pytest

from journal.models import Book
from journal.periods import JournalPeriodError, close_year, fy_end_for, is_closed


def test_the_financial_year_ends_on_31_march():
    assert fy_end_for(date(2026, 3, 31)) == date(2026, 3, 31)
    assert fy_end_for(date(2026, 4, 1)) == date(2027, 3, 31)
    assert fy_end_for(date(2026, 12, 31)) == date(2027, 3, 31)


@pytest.mark.django_db
def test_closing_a_year_records_who_and_closes_every_day_in_it():
    close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")
    assert is_closed(Book.LIVE, date(2025, 4, 1))
    assert not is_closed(Book.LIVE, date(2026, 4, 1))


@pytest.mark.django_db
def test_only_a_31_march_can_be_closed():
    with pytest.raises(JournalPeriodError, match="31 March"):
        close_year(book=Book.LIVE, fy_end=date(2026, 6, 30), actor="ca")


@pytest.mark.django_db
def test_a_year_cannot_be_closed_while_it_is_still_running(time_machine):
    time_machine.move_to("2026-03-15")
    with pytest.raises(JournalPeriodError, match="not over"):
        close_year(book=Book.LIVE, fy_end=date(2026, 3, 31), actor="ca")
```

- [ ] **Step 2: Run to verify they fail** — `PYTEST src/journal/tests/test_periods.py` → ModuleNotFoundError.

- [ ] **Step 3: Implement** `src/journal/periods.py`:

```python
"""Financial-year closes (spec J9). April to March, one close per year per book."""

from datetime import date

from audit.context import current_trace_id, today_ist
from audit.events import record
from journal.models import PeriodClose


class JournalPeriodError(Exception):
    pass


def fy_end_for(day: date) -> date:
    return date(day.year if day.month <= 3 else day.year + 1, 3, 31)


def is_closed(book: str, day: date) -> bool:
    return PeriodClose.objects.filter(book=book, fy_end__gte=day).exists()


def close_year(*, book: str, fy_end: date, actor: str) -> PeriodClose:
    if (fy_end.month, fy_end.day) != (3, 31):
        raise JournalPeriodError("A financial year ends on 31 March.")
    if fy_end >= today_ist():
        raise JournalPeriodError("That year is not over yet.")

    close, _ = PeriodClose.objects.get_or_create(
        book=book, fy_end=fy_end, defaults={"closed_by": actor, "trace_id": current_trace_id()}
    )
    record("journal.year_closed", actor=actor, entity=close, after={"book": book, "fy_end": str(fy_end)})
    return close
```

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal/tests/test_periods.py` → 4 passed.

- [ ] **Step 5: Commit** — `feat(journal): close a financial year`

---

### Task 5: Contributions, withdrawals, the quarterly sweep and allocations

**Files:**
- Create: `src/journal/cash.py`, `src/journal/tests/test_cash.py`, `src/journal/tests/conftest.py`

**Interfaces:**
- Consumes: `post`, `Line`, `q6`, `balance` (Task 3); `configuration.store.current()`, `configuration.pause.is_paused(on=)`, `trading_calendar.sessions.is_trading_day`.
- Produces (each returns a `JournalEntry` unless stated):
  - `allocate(amount, *, book, on, actor, description, source_type, source_id="", idempotency_key=None, source_account=None)`: Dr 1110, Cr `source_account` or, by default, 1100 (live) or 3900 (paper)
  - `allocate_daily(day, *, book=None, actor=SYSTEM_ACTOR) -> JournalEntry | None`: key `daily:{book}:{day}`, `source_type="daily"`; returns None on a non-trading day, a paused day, or a daily amount ≤ 0
  - `daily_allocation_for(day, *, book) -> Decimal`
  - `contribute(amount, *, on, actor, reference="", source_type="contribution", source_id="", idempotency_key=None)`: live book, then restores carry owed
  - `restored_by(contribution) -> Decimal`
  - `withdraw(amount, *, on, actor, reason, sweep=False, source_type="withdrawal", source_id="", idempotency_key=None)`: live; a sweep takes 1100 first, then 1110, with `source_type="quarterly_settlement"`
  - `carry_owed() -> Decimal`
  - `account_charge(amount, *, on, component, actor, description, source_id="", idempotency_key=None)`: Dr 5100 / Cr 1100, live
  - `@dataclass(frozen=True) Lumpsum(contribution, restored: Decimal, allocation: JournalEntry | None)`
  - `record_lumpsum(amount, *, received_on, actor, reference="") -> Lumpsum`
  - `class CashError(Exception)`
  - Constants `DAILY="daily"`, `SWEEP="quarterly_settlement"`, `RESTORE="carry_restore"`, `LUMPSUM="lumpsum"`, `REINVEST="reinvest"`
- Test fixture (in `conftest.py`): `live_mode`, which makes `configuration.store.current().order_mode` return `"live"`.

- [ ] **Step 1: Write the fixture and the failing tests**

`src/journal/tests/conftest.py` (more fixtures are added in Task 6):

```python
import pytest


@pytest.fixture
def live_mode(monkeypatch):
    """The engine in live mode, without the static-IP checks going live needs.

    Only `current().order_mode` is read by the code under test, so the
    configuration object is wrapped rather than a live version written.
    """
    from configuration import store

    real = store.current

    class Live:
        def __init__(self, config):
            self._config = config

        def __getattr__(self, name):
            return getattr(self._config, name)

        order_mode = "live"

    monkeypatch.setattr(store, "current", lambda: Live(real()))
```

`journal.cash` imports `current` inside each function (`from configuration.store import current`), so the patched attribute is what it sees.

`src/journal/tests/test_cash.py`:

```python
from datetime import date
from decimal import Decimal

import pytest

from journal import accounts, cash
from journal.balances import balance, engine_balance
from journal.models import Book

MON, TUE, SAT = date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 26)


def live(code):
    return balance(code, book=Book.LIVE)


def _swept_carry():
    """Unallocated 500, engine 2500; then a sweep of all 3000."""
    cash.contribute(Decimal("3000"), on=MON, actor="operator")
    cash.allocate(Decimal("2500"), book=Book.LIVE, on=MON, actor="t", description="t", source_type="t")
    cash.withdraw(Decimal("3000"), on=TUE, actor="system", reason="quarterly settlement", sweep=True)


@pytest.mark.django_db
def test_a_contribution_lands_in_unallocated():
    cash.contribute(Decimal("5000"), on=MON, actor="operator", reference="668124142266")
    assert live(accounts.UNALLOCATED) == Decimal("5000")
    assert live(accounts.CONTRIBUTIONS) == Decimal("-5000")


@pytest.mark.django_db
def test_the_daily_allocation_is_posted_once_per_day_and_book():
    first = cash.allocate_daily(MON, book=Book.PAPER)
    again = cash.allocate_daily(MON, book=Book.PAPER)
    assert first.pk == again.pk
    assert balance(accounts.ENGINE_CASH, book=Book.PAPER) == Decimal("1000")
    assert balance(accounts.PAPER_FUNDING, book=Book.PAPER) == Decimal("-1000")
    assert cash.daily_allocation_for(MON, book=Book.PAPER) == Decimal("1000")


@pytest.mark.django_db
def test_no_allocation_on_a_weekend():
    assert cash.allocate_daily(SAT, book=Book.PAPER) is None


@pytest.mark.django_db
def test_no_allocation_on_a_paused_day(monkeypatch):
    monkeypatch.setattr("configuration.pause.is_paused", lambda on=None: True)
    assert cash.allocate_daily(TUE, book=Book.PAPER) is None


@pytest.mark.django_db
def test_a_live_allocation_comes_out_of_unallocated():
    cash.contribute(Decimal("2000"), on=MON, actor="operator")
    cash.allocate_daily(MON, book=Book.LIVE)
    assert live(accounts.UNALLOCATED) == Decimal("1000")
    assert live(accounts.ENGINE_CASH) == Decimal("1000")


@pytest.mark.django_db
def test_a_lumpsum_in_paper_mode_is_real_money_and_paper_budget():
    result = cash.record_lumpsum(Decimal("25000"), received_on=SAT, actor="operator", reference="gift")
    assert result.contribution.book == Book.LIVE
    assert result.allocation.book == Book.PAPER
    assert live(accounts.UNALLOCATED) == Decimal("25000")
    assert engine_balance(Book.PAPER) == Decimal("25000")


@pytest.mark.django_db
def test_a_sweep_takes_unallocated_first_then_engine_carry():
    _swept_carry()
    assert live(accounts.UNALLOCATED) == Decimal("0")
    assert live(accounts.ENGINE_CASH) == Decimal("0")
    assert cash.carry_owed() == Decimal("2500")


@pytest.mark.django_db
def test_the_next_contribution_restores_swept_carry_first():
    _swept_carry()
    entry = cash.contribute(Decimal("4000"), on=TUE, actor="operator")
    assert cash.restored_by(entry) == Decimal("2500")
    assert cash.carry_owed() == Decimal("0")
    assert live(accounts.ENGINE_CASH) == Decimal("2500")
    assert live(accounts.UNALLOCATED) == Decimal("1500")


@pytest.mark.django_db
def test_a_small_contribution_restores_carry_in_part():
    _swept_carry()
    cash.contribute(Decimal("1000"), on=TUE, actor="operator")
    assert live(accounts.ENGINE_CASH) == Decimal("1000")
    assert live(accounts.UNALLOCATED) == Decimal("0")
    assert cash.carry_owed() == Decimal("1500")


@pytest.mark.django_db
def test_a_live_lumpsum_after_a_sweep_allocates_only_what_restoring_left(live_mode):
    _swept_carry()
    result = cash.record_lumpsum(Decimal("10000"), received_on=TUE, actor="operator")
    assert result.restored == Decimal("2500")
    assert live(accounts.ENGINE_CASH) == Decimal("10000")
    assert live(accounts.UNALLOCATED) == Decimal("0")


@pytest.mark.django_db
def test_an_account_charge_is_an_expense_paid_from_unallocated():
    cash.account_charge(Decimal("88.5"), on=MON, component="amc", actor="operator", description="AMC")
    assert live(accounts.ACCOUNT_CHARGES) == Decimal("88.5")
    assert live(accounts.UNALLOCATED) == Decimal("-88.5")


@pytest.mark.django_db
def test_an_allocation_can_name_its_source_account():
    cash.allocate(
        Decimal("5"), book=Book.PAPER, on=MON, actor="t", description="t",
        source_type="t", source_account=accounts.UNALLOCATED,
    )
    assert balance(accounts.UNALLOCATED, book=Book.PAPER) == Decimal("-5")


@pytest.mark.django_db
def test_amounts_must_be_positive():
    with pytest.raises(cash.CashError):
        cash.contribute(Decimal("0"), on=MON, actor="operator")
    with pytest.raises(cash.CashError):
        cash.withdraw(Decimal("-1"), on=MON, actor="operator", reason="x")
```

`test_no_allocation_on_a_paused_day` patches `configuration.pause.is_paused`. `cash.allocate_daily` must therefore look it up as `from configuration import pause` … `pause.is_paused(on=day)` at call time, not bind the name at import.

- [ ] **Step 2: Run to verify they fail** — `PYTEST src/journal/tests/test_cash.py` → `ModuleNotFoundError: journal.cash`.

- [ ] **Step 3: Implement** `src/journal/cash.py`:

```python
"""Money arriving, leaving, and being given to the engine (spec §3, §6, §7.4).

Two ideas carry this module:

  * **an allocation is the budget.** The daily amount, a lumpsum and
    reinvested sale proceeds all move money from *unallocated* to *engine*;
    carry is simply what the engine account holds;
  * **a sweep leaves carry owed.** SEBI's quarterly settlement empties the
    account, engine carry included. What it took from the engine is owed
    back, and the next contribution repays it before anything else.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from audit.events import SYSTEM_ACTOR
from journal.accounts import (
    ACCOUNT_CHARGES,
    CONTRIBUTIONS,
    ENGINE_CASH,
    PAPER_FUNDING,
    UNALLOCATED,
    WITHDRAWALS,
)
from journal.balances import balance
from journal.models import Book, EntryKind, JournalEntry
from journal.posting import Line, post, q6

ZERO = Decimal("0")

DAILY = "daily"
SWEEP = "quarterly_settlement"
RESTORE = "carry_restore"
LUMPSUM = "lumpsum"
REINVEST = "reinvest"


class CashError(Exception):
    pass


@dataclass(frozen=True)
class Lumpsum:
    contribution: JournalEntry
    restored: Decimal
    allocation: JournalEntry | None


def _positive(amount) -> Decimal:
    amount = q6(amount)
    if amount <= ZERO:
        raise CashError("An amount must be positive.")
    return amount


def allocate(
    amount,
    *,
    book: str,
    on: date,
    actor: str,
    description: str,
    source_type: str,
    source_id: str = "",
    idempotency_key: str | None = None,
    source_account: str | None = None,
) -> JournalEntry:
    amount = _positive(amount)
    source = source_account or (PAPER_FUNDING if book == Book.PAPER else UNALLOCATED)
    return post(
        book=book,
        kind=EntryKind.ALLOCATION,
        occurred_on=on,
        description=description,
        lines=[Line(ENGINE_CASH, amount), Line(source, -amount)],
        actor=actor,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=idempotency_key,
    )


def allocate_daily(
    day: date, *, book: str | None = None, actor: str = SYSTEM_ACTOR
) -> JournalEntry | None:
    """The day's budget, once per day and book. Nothing on a closed or paused day."""
    from configuration import pause
    from configuration.store import current
    from trading_calendar.sessions import is_trading_day

    config = current()
    book = book or config.order_mode
    if not is_trading_day(day) or pause.is_paused(on=day) or config.daily_amount <= ZERO:
        return None
    return allocate(
        config.daily_amount,
        book=book,
        on=day,
        actor=actor,
        description=f"daily amount for {day}",
        source_type=DAILY,
        source_id=day.isoformat(),
        idempotency_key=f"{DAILY}:{book}:{day.isoformat()}",
    )


def daily_allocation_for(day: date, *, book: str) -> Decimal:
    return balance(ENGINE_CASH, book=book, on=day, source_type=DAILY)


def carry_owed() -> Decimal:
    """Engine carry the sweeps took and contributions have not yet restored."""
    swept = -balance(ENGINE_CASH, book=Book.LIVE, source_type=SWEEP)
    restored = balance(ENGINE_CASH, book=Book.LIVE, source_type=RESTORE)
    return max(ZERO, q6(swept - restored))


def contribute(
    amount,
    *,
    on: date,
    actor: str,
    reference: str = "",
    source_type: str = "contribution",
    source_id: str = "",
    idempotency_key: str | None = None,
) -> JournalEntry:
    amount = _positive(amount)
    entry = post(
        book=Book.LIVE,
        kind=EntryKind.CONTRIBUTION,
        occurred_on=on,
        description=f"contribution {reference}".strip(),
        lines=[Line(UNALLOCATED, amount), Line(CONTRIBUTIONS, -amount)],
        actor=actor,
        source_type=source_type,
        source_id=source_id or reference,
        idempotency_key=idempotency_key,
    )
    take = min(carry_owed(), amount)
    if take > ZERO:
        allocate(
            take,
            book=Book.LIVE,
            on=on,
            actor=actor,
            description="carry restored after the quarterly settlement",
            source_type=RESTORE,
            source_id=str(entry.id),
            idempotency_key=f"{RESTORE}:{entry.id}",
        )
    return entry


def restored_by(contribution: JournalEntry) -> Decimal:
    """How much of a contribution went to repaying swept carry."""
    entry = JournalEntry.objects.filter(idempotency_key=f"{RESTORE}:{contribution.id}").first()
    if entry is None:
        return ZERO
    return q6(sum((line.amount for line in entry.lines.filter(account_id=ENGINE_CASH)), ZERO))


def record_lumpsum(amount, *, received_on: date, actor: str, reference: str = "") -> Lumpsum:
    """Money in, and all of it -- less any carry it restored -- budget (M8a).

    The contribution is real money and always live. The allocation goes to the
    engine's own book, so a lumpsum during paper trading funds the paper
    engine while the rupees sit in live *unallocated*, where they really are.
    """
    from configuration.store import current

    amount = _positive(amount)
    book = current().order_mode
    contribution = contribute(
        amount,
        on=received_on,
        actor=actor,
        reference=reference,
        source_type=LUMPSUM,
        source_id=reference,
    )
    restored = restored_by(contribution) if book == Book.LIVE else ZERO
    rest = q6(amount - restored)
    allocation = None
    if rest > ZERO:
        allocation = allocate(
            rest,
            book=book,
            on=received_on,
            actor=actor,
            description=f"lumpsum {reference}".strip(),
            source_type=LUMPSUM,
            source_id=str(contribution.id),
            idempotency_key=f"{LUMPSUM}:{contribution.id}",
        )
    return Lumpsum(contribution=contribution, restored=restored, allocation=allocation)


def withdraw(
    amount,
    *,
    on: date,
    actor: str,
    reason: str,
    sweep: bool = False,
    source_type: str = "withdrawal",
    source_id: str = "",
    idempotency_key: str | None = None,
) -> JournalEntry:
    """Money back to the owner. A sweep takes unallocated first, then carry."""
    amount = _positive(amount)
    lines = [Line(WITHDRAWALS, amount)]
    if sweep:
        source_type = SWEEP
        from_unallocated = min(amount, max(ZERO, balance(UNALLOCATED, book=Book.LIVE)))
        lines += [
            Line(UNALLOCATED, -from_unallocated),
            Line(ENGINE_CASH, -(amount - from_unallocated)),
        ]
    else:
        lines.append(Line(UNALLOCATED, -amount))
    return post(
        book=Book.LIVE,
        kind=EntryKind.WITHDRAWAL,
        occurred_on=on,
        description=reason,
        lines=lines,
        actor=actor,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=idempotency_key,
    )


def account_charge(
    amount,
    *,
    on: date,
    component: str,
    actor: str,
    description: str,
    source_id: str = "",
    idempotency_key: str | None = None,
) -> JournalEntry:
    amount = _positive(amount)
    return post(
        book=Book.LIVE,
        kind=EntryKind.ACCOUNT_CHARGE,
        occurred_on=on,
        description=description,
        lines=[Line(ACCOUNT_CHARGES, amount, component=component), Line(UNALLOCATED, -amount)],
        actor=actor,
        source_type="account_charge",
        source_id=source_id,
        idempotency_key=idempotency_key,
    )
```

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal/tests/test_cash.py` → all pass.

- [ ] **Step 5: Commit** — `feat(journal): record contributions, withdrawals, sweeps and allocations`

---

### Task 6: Every fill posts its entry, with each charge on its own line

**Files:**
- Create: `src/journal/trades.py`, `src/journal/tests/test_trades.py`
- Modify: `src/journal/tests/conftest.py` (fill fixtures), `src/orders/booking.py` (`book()`)

**Interfaces:**
- Consumes: `post`, `Line`, `q6`; `portfolio.charges.cost_of`; `orders.booking._dp_already_charged`; `gains.fifo.Consumption.cost`.
- Produces:
  - `trades.COMPONENTS: dict[str, str]`, mapping each `portfolio.charges.Charges` field to its `Component` value
  - `trades.settlement_account(order) -> str`: `SETTLE_ENGINE` for a BUY on `DAILY_BUY_STREAM`, otherwise `SETTLE_OTHER`
  - `trades.post_fill(fill, *, consumption=None) -> JournalEntry | None`, keyed `fill:{fill.id}`, `source_type="fill"`, `source_id=str(fill.id)`, book = `fill.order.mode`, `occurred_on = fill.order.trading_date` (the trade date the exchange settles on; `executed_at` is wall-clock and wrong in any test or late postback)
  - Fixtures `paper_buy_fill(quantity, price, charges=None, stream=DAILY_BUY_STREAM)` and `paper_sell_fill(quantity, price, stream="rebalance:test")`, both returning a saved `Fill`

- [ ] **Step 1: Write the fixtures and the failing tests**

Append to `src/journal/tests/conftest.py`:

```python
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

DAY = date(2026, 9, 22)
IST = ZoneInfo("Asia/Kolkata")


def _order_and_fill(*, side, quantity, price, charges, stream, seq):
    from engine.models import TradeSheet
    from orders.models import Fill, Order, OrderMode, OrderState, OrderType
    from portfolio.models import Instrument

    instrument, _ = Instrument.objects.get_or_create(
        tradingsymbol="INFY", defaults={"instrument_token": 408065, "name": "Infosys"}
    )
    sheet, _ = TradeSheet.objects.get_or_create(trading_date=DAY, version=1)
    order = Order.objects.create(
        tag=f"t{seq}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=DAY,
        stream=stream,
        tick_no=seq,
        side=side,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        filled_quantity=quantity,
        limit_price=price,
        state=OrderState.COMPLETE,
    )
    return Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-t{seq}",
        quantity=quantity,
        price=price,
        charges=charges or Decimal("0"),
        executed_at=datetime(2026, 9, 22, 10, seq % 60, tzinfo=IST),
    )


@pytest.fixture
def paper_buy_fill(db):
    counter = iter(range(1, 10_000))

    def make(quantity, price, charges=None, stream=None):
        from orders.models import DAILY_BUY_STREAM, OrderSide

        return _order_and_fill(
            side=OrderSide.BUY, quantity=quantity, price=Decimal(price), charges=charges,
            stream=stream or DAILY_BUY_STREAM, seq=next(counter),
        )

    return make


@pytest.fixture
def paper_sell_fill(db):
    counter = iter(range(5_000, 10_000))

    def make(quantity, price, stream="rebalance:test"):
        from orders.models import OrderSide

        return _order_and_fill(
            side=OrderSide.SELL, quantity=quantity, price=Decimal(price), charges=None,
            stream=stream, seq=next(counter),
        )

    return make
```

`TradeSheet` and `Order` may have more required fields than shown. Run the fixture once and add whatever `IntegrityError` names, copying values from `src/orders/tests/` factories (`grep -rn "Order.objects.create" src/orders/tests | head -3`).

`src/journal/tests/test_trades.py`:

```python
from decimal import Decimal

import pytest

from journal import accounts
from journal.balances import balance
from journal.models import Book, EntryKind, JournalEntry
from orders.booking import book


def _sum(lines, code):
    return sum((line.amount for line in lines if line.account_id == code), Decimal("0"))


@pytest.mark.django_db
def test_a_paper_buy_posts_gross_and_every_charge_to_investments(paper_buy_fill):
    fill = paper_buy_fill(quantity=2, price="1650.55")
    book(fill)
    fill.refresh_from_db()

    entry = JournalEntry.objects.get(kind=EntryKind.BUY_FILL)
    assert entry.book == Book.PAPER
    investment = {
        line.component: line.amount for line in entry.lines.filter(account_id=accounts.INVESTMENTS)
    }
    assert investment["gross"] == Decimal("3301.10")
    assert {"stt", "stamp_duty", "exchange_txn", "sebi", "gst"} <= set(investment)
    total = sum(investment.values())
    assert total == Decimal("3301.10") + fill.charges
    assert balance(accounts.SETTLE_ENGINE, book=Book.PAPER) == -total


@pytest.mark.django_db
def test_booking_a_fill_twice_posts_one_entry(paper_buy_fill):
    fill = paper_buy_fill(quantity=1, price="100")
    book(fill)
    book(fill)
    assert JournalEntry.objects.filter(kind=EntryKind.BUY_FILL).count() == 1


@pytest.mark.django_db
def test_a_sell_posts_net_proceeds_fifo_cost_gain_and_charges(paper_buy_fill, paper_sell_fill):
    book(paper_buy_fill(quantity=2, price="100"))
    sell = paper_sell_fill(quantity=2, price="120")
    book(sell)
    sell.refresh_from_db()

    lines = list(JournalEntry.objects.get(kind=EntryKind.SELL_FILL).lines.all())
    assert _sum(lines, accounts.SETTLE_OTHER) == Decimal("240") - sell.charges
    assert sum(line.amount for line in lines) == 0
    assert balance(accounts.INVESTMENTS, book=Book.PAPER) == 0


@pytest.mark.django_db
def test_a_stored_charge_that_differs_from_the_rate_card_keeps_its_total(paper_buy_fill):
    book(paper_buy_fill(quantity=1, price="100", charges=Decimal("0.5000")))
    entry = JournalEntry.objects.get(kind=EntryKind.BUY_FILL)
    charges = sum(
        line.amount
        for line in entry.lines.filter(account_id=accounts.INVESTMENTS).exclude(component="gross")
    )
    assert charges == Decimal("0.5")
```

- [ ] **Step 2: Run to verify they fail** — `PYTEST src/journal/tests/test_trades.py` → `ModuleNotFoundError: journal.trades`.

- [ ] **Step 3: Implement** `src/journal/trades.py`:

```python
"""Trades in the journal: fills and their settlement (spec §6).

A fill owes the broker until the trade date settles, so it posts against
*Broker settlement* rather than cash. Engine buys have their own settlement
account, which lets the engine's spendable balance net what its own unsettled
buys owe (spec §4).

Charges stay part of the shares' cost (D6), and each component is its own
line so a contract note can be checked against it line by line.
"""

import logging
from decimal import Decimal

from audit.logging import fields
from journal.accounts import INVESTMENTS, REALISED, SETTLE_ENGINE, SETTLE_OTHER
from journal.models import Component, EntryKind, JournalEntry
from journal.posting import Line, post, q6

logger = logging.getLogger("steadystack.journal")

ZERO = Decimal("0")

COMPONENTS = {
    "brokerage": Component.BROKERAGE,
    "securities_transaction_tax": Component.STT,
    "exchange_txn": Component.EXCHANGE_TXN,
    "sebi_turnover": Component.SEBI,
    "stamp_duty": Component.STAMP_DUTY,
    "gst": Component.GST,
    "dp_charge": Component.DP,
}


def settlement_account(order) -> str:
    from orders.models import DAILY_BUY_STREAM, OrderSide

    if order.side == OrderSide.BUY and order.stream == DAILY_BUY_STREAM:
        return SETTLE_ENGINE
    return SETTLE_OTHER


def _charge_lines(fill, *, account: str) -> tuple[list[Line], Decimal, Decimal]:
    """One line per component. A stored total the rate card disagrees with
    keeps that total through an `other` line. Returns (lines, gross, charges).
    """
    from orders.booking import _dp_already_charged
    from portfolio.charges import cost_of

    order = fill.order
    gross, charges = cost_of(
        quantity=fill.quantity,
        price=fill.price,
        side=order.side,
        exchange=order.instrument.exchange,
        dp_charged=_dp_already_charged(fill),
    )
    total = q6(fill.charges) if fill.charges else q6(charges.total)
    lines = [
        Line(account, q6(getattr(charges, field)), order.instrument_id, component)
        for field, component in COMPONENTS.items()
    ]
    difference = total - q6(charges.total)
    if difference != ZERO:
        lines.append(Line(account, difference, order.instrument_id, Component.OTHER))
    return lines, q6(gross), total


def post_fill(fill, *, consumption=None) -> JournalEntry | None:
    """The fill's entry. Idempotent: a replayed booking returns the first."""
    from orders.models import OrderSide

    key = f"fill:{fill.id}"
    existing = JournalEntry.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing

    order = fill.order
    symbol = order.instrument_id

    if order.side == OrderSide.BUY:
        charge_lines, gross, total = _charge_lines(fill, account=INVESTMENTS)
        lines = [
            Line(INVESTMENTS, gross, symbol, Component.GROSS),
            *charge_lines,
            Line(settlement_account(order), -(gross + total)),
        ]
        kind = EntryKind.BUY_FILL
    else:
        if consumption is None:
            # booking.book logged the shortfall as a divergence; the cash
            # will still arrive and the statement import will surface it.
            logger.error(
                "a sell with no lot consumption posts no entry", extra=fields(fill=str(fill.id))
            )
            return None
        charge_lines, gross, total = _charge_lines(fill, account=REALISED)
        cost = q6(consumption.cost)
        lines = [
            Line(settlement_account(order), gross - total),
            *charge_lines,
            Line(INVESTMENTS, -cost, symbol),
            Line(REALISED, -(gross - cost), symbol),
        ]
        kind = EntryKind.SELL_FILL

    return post(
        book=order.mode,
        kind=kind,
        occurred_on=order.trading_date,
        description=f"{order.side} {fill.quantity} {symbol} @ {fill.price}",
        lines=lines,
        source_type="fill",
        source_id=str(fill.id),
        idempotency_key=key,
    )
```

Check `Consumption.cost` (`src/gains/fifo.py:57`). It must be the lots' cost of the shares consumed. If it is not, sum `realisation.cost` over `consumption.realisations`.

Modify `src/orders/booking.py` `book()`, adding `from journal import trades as journal_trades` to its imports:

```python
    if order.side == OrderSide.BUY:
        lot = lots.from_fill(fill)
        journal_trades.post_fill(fill)
        return Booked(lot=lot)
```

After the successful `fifo.consume(...)`, before `return Booked(consumption=consumption)`, add:

```python
    journal_trades.post_fill(fill, consumption=consumption)
```

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal src/orders` → all pass.

- [ ] **Step 5: Commit** — `feat(journal): post every fill with its charges line by line`

---

### Task 7: Settling a trade date, and reinvesting sale proceeds

**Files:**
- Modify: `src/journal/trades.py`, `src/journal/tests/test_trades.py`, `src/rebalance/models.py`, `src/rebalance/batches.py` (`upload`, `_store`), `src/rebalance/views.py` (`_upload`), `src/templates/rebalance/index.html`, `src/rebalance/ladder.py` (`settle`), `src/rebalance/tests/test_batches.py`
- Create: `src/rebalance/migrations/00NN_rebalancesell_reinvest.py` (generated)

**Interfaces:**
- Consumes: `cash.allocate(..., source_account=)`, `cash.REINVEST` (Task 5); fill entries (Task 6).
- Produces:
  - `trades.settle_trades(trade_date, *, book, actor=SYSTEM_ACTOR) -> JournalEntry | None`, `source_type="trade_date"`, `source_id=trade_date.isoformat()`
  - `trades.reinvest(batch, *, book) -> list[JournalEntry]`
  - `RebalanceSell.reinvest: bool` (default `True`)
  - `batches.upload(..., reinvest: bool = True)`

- [ ] **Step 1: Write the failing tests.** Append to `src/journal/tests/test_trades.py`:

```python
from datetime import date

from journal.trades import settle_trades

DAY = date(2026, 9, 22)


@pytest.mark.django_db
def test_settlement_clears_the_day_into_engine_cash(paper_buy_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    entry = settle_trades(DAY, book=Book.PAPER)
    assert entry.kind == EntryKind.SETTLEMENT
    assert balance(accounts.SETTLE_ENGINE, book=Book.PAPER) == 0
    assert balance(accounts.ENGINE_CASH, book=Book.PAPER) < 0


@pytest.mark.django_db
def test_settling_twice_posts_nothing_the_second_time(paper_buy_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    settle_trades(DAY, book=Book.PAPER)
    assert settle_trades(DAY, book=Book.PAPER) is None


@pytest.mark.django_db
def test_a_late_fill_is_settled_by_a_second_entry(paper_buy_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    settle_trades(DAY, book=Book.PAPER)
    book(paper_buy_fill(quantity=1, price="101"))
    assert settle_trades(DAY, book=Book.PAPER) is not None
    assert balance(accounts.SETTLE_ENGINE, book=Book.PAPER) == 0


@pytest.mark.django_db
def test_sale_proceeds_settle_to_unallocated(paper_buy_fill, paper_sell_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    book(paper_sell_fill(quantity=1, price="120"))
    settle_trades(DAY, book=Book.PAPER)
    assert balance(accounts.SETTLE_OTHER, book=Book.PAPER) == 0
    assert balance(accounts.UNALLOCATED, book=Book.PAPER) > 0
```

And in the same file, the reinvest contract, which is independent of the rebalance UI:

```python
from types import SimpleNamespace

from journal.trades import reinvest


@pytest.mark.django_db
def test_a_reinvesting_batch_allocates_its_net_proceeds(paper_buy_fill, paper_sell_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    sell = paper_sell_fill(quantity=1, price="120", stream="rebalance:b1")
    book(sell)
    sell.refresh_from_db()

    batch = SimpleNamespace(id="b1", stream="rebalance:b1", reinvest=True)
    entries = reinvest(batch, book=Book.PAPER)

    assert len(entries) == 1
    assert balance(accounts.ENGINE_CASH, book=Book.PAPER, source_type="reinvest") == (
        Decimal("120") - sell.charges
    )
    assert balance(accounts.UNALLOCATED, book=Book.PAPER) == 0
    assert reinvest(batch, book=Book.PAPER)[0].pk == entries[0].pk


@pytest.mark.django_db
def test_a_batch_that_does_not_reinvest_allocates_nothing(paper_buy_fill, paper_sell_fill):
    book(paper_buy_fill(quantity=1, price="100"))
    book(paper_sell_fill(quantity=1, price="120", stream="rebalance:b2"))
    batch = SimpleNamespace(id="b2", stream="rebalance:b2", reinvest=False)
    assert reinvest(batch, book=Book.PAPER) == []
```

In `src/rebalance/tests/test_batches.py`, add one test that `batches.upload(..., reinvest=False)` stores `reinvest=False`, and that the default stores `True`. Use the file's existing upload helper and CSV (`grep -n "upload(" src/rebalance/tests/test_batches.py | head -3`).

- [ ] **Step 2: Run to verify they fail** — `PYTEST src/journal/tests/test_trades.py src/rebalance/tests/test_batches.py` → ImportError / TypeError.

- [ ] **Step 3: Implement.** Add these imports to the top of `src/journal/trades.py`: `from datetime import date`, `from django.db import connection, transaction`, `from audit.events import SYSTEM_ACTOR`, `from journal.accounts import ENGINE_CASH, UNALLOCATED` (merge into the existing accounts import), `from journal.balances import balance`. Then append:

```python
_SETTLE_LOCK = 7_310_442_901_115_010


@transaction.atomic
def settle_trades(trade_date: date, *, book: str, actor: str = SYSTEM_ACTOR) -> JournalEntry | None:
    """Clear what a trade date still owes, or is owed, into cash.

    The amount is whatever the settlement accounts still hold for that date,
    so a second run posts nothing and a fill that arrived late is settled by a
    second, smaller entry. The advisory lock stops two runs both seeing the
    same residual.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_SETTLE_LOCK])

    engine = balance(SETTLE_ENGINE, book=book, on=trade_date)
    other = balance(SETTLE_OTHER, book=book, on=trade_date)
    if engine == ZERO and other == ZERO:
        return None

    return post(
        book=book,
        kind=EntryKind.SETTLEMENT,
        occurred_on=trade_date,
        description=f"settlement of {trade_date} trades",
        lines=[
            Line(SETTLE_ENGINE, -engine),
            Line(ENGINE_CASH, engine),
            Line(SETTLE_OTHER, -other),
            Line(UNALLOCATED, other),
        ],
        actor=actor,
        source_type="trade_date",
        source_id=trade_date.isoformat(),
    )


def reinvest(batch, *, book: str) -> list[JournalEntry]:
    """Give a batch's net proceeds to the engine, one allocation per trade date.

    The proceeds are settled first, so the allocation moves money that is
    already in *unallocated*, in either book.
    """
    from journal.cash import REINVEST, allocate
    from journal.models import JournalLine
    from orders.models import Fill

    if not batch.reinvest:
        return []

    fill_ids = [
        str(pk) for pk in Fill.objects.filter(order__stream=batch.stream).values_list("pk", flat=True)
    ]
    proceeds_by_date: dict[date, Decimal] = {}
    for line in JournalLine.objects.filter(
        entry__kind=EntryKind.SELL_FILL,
        entry__book=book,
        entry__source_id__in=fill_ids,
        account_id=SETTLE_OTHER,
    ).select_related("entry"):
        day = line.entry.occurred_on
        proceeds_by_date[day] = proceeds_by_date.get(day, ZERO) + line.amount

    entries = []
    for trade_date, proceeds in sorted(proceeds_by_date.items()):
        settle_trades(trade_date, book=book)
        if proceeds > ZERO:
            entries.append(
                allocate(
                    proceeds,
                    book=book,
                    on=trade_date,
                    actor=SYSTEM_ACTOR,
                    description=f"reinvested proceeds of rebalance sell {batch.id}",
                    source_type=REINVEST,
                    source_id=str(batch.id),
                    idempotency_key=f"{REINVEST}:{book}:{batch.id}:{trade_date.isoformat()}",
                    source_account=UNALLOCATED,
                )
            )
    return entries
```

`src/rebalance/models.py`, `RebalanceSell`:

```python
    reinvest = models.BooleanField(
        default=True, help_text="Allocate the net proceeds to the engine once they settle."
    )
```

Run `DATABASE_URL=postgres://steadystack_owner:steadystack_owner_dev@localhost:5432/steadystack_journal uv run python manage.py makemigrations rebalance`.

`batches.upload` and `batches._store`: add a keyword `reinvest: bool = True`, pass it through, and set it on the `RebalanceSell` created in `_store`. `rebalance/views.py` `_upload`: pass `reinvest=request.POST.get("reinvest") == "on"`. In the upload form in `src/templates/rebalance/index.html`, add next to the file input:

```html
<label><input type="checkbox" name="reinvest" checked> Reinvest the proceeds</label>
```

Then run `npm run build`.

`rebalance/ladder.py` `settle()`: in the terminal branch, after `batch.save(update_fields=["state", "finished_at"])`, add:

```python
    from journal.trades import reinvest

    for mode in set(Order.objects.filter(stream=batch.stream).values_list("mode", flat=True)):
        reinvest(batch, book=mode)
```

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal src/rebalance src/orders` → all pass.

- [ ] **Step 5: Commit** — use `/atomic:atomic-commit` for two commits: `feat(journal): settle each trade date into cash` and `feat(rebalance): reinvest a batch's proceeds when it settles`.

---

### Task 8: The engine reads its budget from the journal

This is the switch-over. After it, carry is the engine account's balance, the daily amount is an allocation posted at planning, a lumpsum is a contribution plus an allocation, a contract-note difference is a charge correction, and `CashLedger` is a summary built from the journal. `LumpsumCredit` and `LedgerAdjustment` are gone. It is one task because the old `opening_for` feeds every one of those callers, and no intermediate state keeps the suite green.

**Files:**
- Rewrite: `src/engine/ledger.py`, `src/engine/tests/test_ledger.py`
- Modify: `src/engine/models.py` (`CashLedger`; delete `LumpsumCredit`, `AdjustmentSource`, `LedgerAdjustment`), `src/engine/tradesheet.py:185-192`, `src/engine/settlement.py` (`settle`, `Settlement`), `src/engine/day.py` (`lumpsums`), `src/engine/views.py:249-251`, `src/engine/management/commands/seed_demo.py`, `src/journal/trades.py` (add `post_charge_correction`), `src/portfolio/contract_note.py:110-125`, `src/performance/ledgers.py`, `src/performance/series.py` (`totals`, `_first_day`), `src/web/views.py` (`cash_ledger` CSV, `_credit_lumpsum`), `src/web/deck.py` (lines ~320-345, ~469-540, ~563, ~675), templates `src/templates/web/cash_ledger.html` and `src/templates/engine/detail.html` (column names, lumpsum rows)
- Create: `src/engine/migrations/0010_cash_ledger_from_journal.py` (hand-written)
- Update tests: `src/engine/tests/test_settlement.py`, `test_full_day.py`, `test_tradesheet.py`, `test_planning.py`, `test_day_view.py`; `src/rebalance/tests/test_batches.py:355-368`; `src/records/tests/test_records.py:744-777`, `test_views.py:194-212`; `src/performance/tests/test_ledgers.py`, `test_series.py`; `src/web/tests/test_dashboards.py:655-684`, `test_today.py`

**Interfaces:**
- Consumes: `journal.balances.engine_balance`, `journal.cash.{allocate_daily, daily_allocation_for, record_lumpsum, DAILY}`, `journal.trades.{settle_trades, settlement_account}`.
- Produces (`engine.ledger`):
  - `money(value) -> Decimal` (unchanged), `LedgerError` (unchanged)
  - `@dataclass(frozen=True) DayOpening(trading_date: date, carry: Decimal, daily_allocation: Decimal)` with properties `spendable` and `shortfall`
  - `engine_book(book: str | None = None) -> str`
  - `opening_for(day, *, book=None) -> DayOpening`, which writes nothing
  - `spendable_on(day, *, book=None) -> Decimal`
  - `closing_carry_before(day, *, book=None) -> Decimal`
  - `row_for(day, *, book=None)`, `is_settled(day, *, book=None) -> bool`
  - `settle_day(day, *, book=None, note="") -> CashLedger`
  - `reconcile(start=None, end=None, *, book=None) -> dict` with keys `rows`, `unbalanced`, `chain_breaks`, `ok`, `net_in`, `net_out`
- Produces (`engine.models.CashLedger`): fields `book`, `trading_date`, `created_at`, `trace_id`, `opening_carry`, `daily_allocation`, `extra_allocation`, `cash_spent`, `charges`, `other`, `closing_carry`, `note`; properties `available`, `reconciles`, `discrepancy`; unique `(book, trading_date)`.
- Produces (`journal.trades`): `post_charge_correction(lot, difference: Decimal, *, actor: str, reference: str, on: date) -> JournalEntry | None`, keyed `charge_correction:{lot.id}`.

- [ ] **Step 1: Rewrite the ledger tests first**

Replace `src/engine/tests/test_ledger.py` wholesale. Spending is simulated by posting a buy entry directly, so these tests stay about the ledger rather than about orders:

```python
"""The engine's cash, now read from the journal.

The property test at the bottom is the point of this file: money is conserved
over any run of days, however spending, lumpsums and overspends interleave.
"""

from datetime import date
from decimal import Decimal
from itertools import pairwise

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from configuration.store import update as configure
from engine.ledger import (
    LedgerError,
    closing_carry_before,
    opening_for,
    reconcile,
    settle_day,
    spendable_on,
)
from engine.models import CashLedger
from journal import accounts, cash
from journal.models import Book, Component, EntryKind
from journal.posting import Line, post

MON, TUE, WED, THU, FRI, SAT = (date(2026, 9, d) for d in (21, 22, 23, 24, 25, 26))
NEXT_MON = date(2026, 9, 28)

pytestmark = pytest.mark.django_db


def spend(day, gross, charges="0"):
    """A buy fill the engine paid for, as the journal records one."""
    gross, charges = Decimal(gross), Decimal(charges)
    lines = [Line(accounts.INVESTMENTS, gross, component=Component.GROSS)]
    if charges:
        lines.append(Line(accounts.INVESTMENTS, charges, component=Component.STT))
    lines.append(Line(accounts.SETTLE_ENGINE, -(gross + charges)))
    post(book=Book.PAPER, kind=EntryKind.BUY_FILL, occurred_on=day, description="buy", lines=lines)


def run_day(day, gross="0", charges="0"):
    cash.allocate_daily(day, book=Book.PAPER)
    if Decimal(gross):
        spend(day, gross, charges)
    return settle_day(day, book=Book.PAPER)


# -- opening a day ---------------------------------------------------------


def test_the_first_day_opens_with_the_daily_amount_alone():
    opening = opening_for(MON, book=Book.PAPER)
    assert opening.carry == Decimal("0")
    assert opening.daily_allocation == Decimal("1000.00")
    assert opening.spendable == Decimal("1000.00")


def test_opening_a_day_writes_nothing():
    opening_for(MON, book=Book.PAPER)
    from journal.models import JournalEntry

    assert JournalEntry.objects.count() == 0


def test_the_configured_amount_is_what_is_allocated():
    configure(changed_by="operator", reason="t", daily_amount=Decimal("2500"))
    assert opening_for(MON, book=Book.PAPER).daily_allocation == Decimal("2500.00")


def test_an_allocated_day_reports_the_allocation_not_the_configuration():
    cash.allocate_daily(MON, book=Book.PAPER)
    configure(changed_by="operator", reason="t", daily_amount=Decimal("2500"))
    assert opening_for(MON, book=Book.PAPER).daily_allocation == Decimal("1000")


def test_what_a_day_did_not_spend_carries_into_the_next():
    run_day(MON, gross="400")
    assert opening_for(TUE, book=Book.PAPER).carry == Decimal("600")
    assert spendable_on(TUE, book=Book.PAPER) == Decimal("1600.00")
    assert closing_carry_before(TUE, book=Book.PAPER) == Decimal("600.00")


def test_a_weekend_carries_to_monday():
    run_day(FRI, gross="250")
    assert opening_for(NEXT_MON, book=Book.PAPER).carry == Decimal("750")


def test_an_overspend_leaves_negative_carry_that_the_next_allocation_repays():
    run_day(MON, gross="1050")
    tuesday = opening_for(TUE, book=Book.PAPER)
    assert tuesday.carry == Decimal("-50")
    assert tuesday.spendable == Decimal("950.00")


def test_an_overspend_larger_than_a_day_floors_at_zero_and_is_still_owed():
    run_day(MON, gross="2500")
    tuesday = opening_for(TUE, book=Book.PAPER)
    assert tuesday.spendable == Decimal("0")
    assert tuesday.shortfall == Decimal("500.00")


def test_a_lumpsum_on_a_saturday_is_monday_money():
    cash.record_lumpsum(Decimal("50000"), received_on=SAT, actor="operator")
    assert spendable_on(NEXT_MON, book=Book.PAPER) == Decimal("51000.00")


def test_a_lumpsum_after_the_day_planned_is_seen_by_a_regeneration():
    cash.allocate_daily(MON, book=Book.PAPER)
    before = spendable_on(MON, book=Book.PAPER)
    cash.record_lumpsum(Decimal("5000"), received_on=MON, actor="operator")
    assert spendable_on(MON, book=Book.PAPER) == before + Decimal("5000")


# -- the daily summary -----------------------------------------------------


def test_settling_writes_a_row_that_balances_on_its_own():
    row = run_day(MON, gross="400", charges="2.50")
    assert row.opening_carry == 0
    assert row.daily_allocation == Decimal("1000")
    assert row.cash_spent == Decimal("400")
    assert row.charges == Decimal("2.50")
    assert row.closing_carry == Decimal("597.50")
    assert row.reconciles


def test_settling_is_idempotent():
    first = run_day(MON, gross="400")
    second = settle_day(MON, book=Book.PAPER)
    assert first.pk == second.pk
    assert CashLedger.objects.count() == 1


def test_no_row_is_due_on_a_weekend():
    with pytest.raises(LedgerError):
        settle_day(SAT, book=Book.PAPER)


def test_the_row_is_append_only():
    from django.db import DatabaseError, transaction

    row = run_day(MON)
    with pytest.raises(DatabaseError), transaction.atomic():
        CashLedger.objects.filter(pk=row.pk).update(note="changed")


def test_a_lumpsum_shows_as_an_extra_allocation():
    cash.record_lumpsum(Decimal("5000"), received_on=MON, actor="operator")
    row = run_day(MON, gross="1000")
    assert row.extra_allocation == Decimal("5000")
    assert row.closing_carry == Decimal("5000")


def test_an_entry_dated_into_a_settled_day_lands_in_the_next_row():
    run_day(MON)
    cash.allocate(Decimal("300"), book=Book.PAPER, on=MON, actor="t", description="late", source_type="t")
    tuesday = run_day(TUE)
    assert tuesday.extra_allocation == Decimal("300")
    assert tuesday.closing_carry == Decimal("2300")


def test_an_allocation_for_a_later_day_is_not_counted_early():
    cash.allocate_daily(TUE, book=Book.PAPER)
    monday = run_day(MON)
    assert monday.closing_carry == Decimal("1000")
    tuesday = run_day(TUE)
    assert tuesday.daily_allocation == Decimal("1000")


def test_the_books_do_not_mix():
    run_day(MON, gross="400")
    assert opening_for(TUE, book=Book.LIVE).carry == 0


def test_reconcile_checks_every_row_and_the_chain():
    run_day(MON, gross="900")
    run_day(TUE, gross="1050")
    run_day(WED)
    run_day(THU, gross="1900", charges="3.40")
    result = reconcile(book=Book.PAPER)
    assert result["ok"]
    assert result["rows"] == 4


# -- money is conserved ----------------------------------------------------

DAYS = [MON, TUE, WED, THU, FRI, NEXT_MON]

day_plan = st.tuples(
    st.decimals(min_value=0, max_value=3000, places=2),
    st.decimals(min_value=0, max_value=20, places=2),
    st.decimals(min_value=0, max_value=20000, places=2),
)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(plans=st.lists(day_plan, min_size=len(DAYS), max_size=len(DAYS)))
def test_money_is_conserved_over_any_run_of_days(plans):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SAVEPOINT prop")
    try:
        for day, (gross, charges, lumpsum) in zip(DAYS, plans, strict=True):
            if lumpsum:
                cash.record_lumpsum(lumpsum, received_on=day, actor="operator")
            run_day(day, gross=gross, charges=charges)

        rows = list(CashLedger.objects.filter(book=Book.PAPER).order_by("trading_date"))
        assert all(row.reconciles for row in rows)
        for previous, current in pairwise(rows):
            assert current.opening_carry == previous.closing_carry
        total_in = sum((r.daily_allocation + r.extra_allocation for r in rows), Decimal("0"))
        total_out = sum((r.cash_spent + r.charges for r in rows), Decimal("0"))
        assert total_in - total_out == rows[-1].closing_carry
        assert reconcile(book=Book.PAPER)["ok"]
    finally:
        with connection.cursor() as cursor:
            cursor.execute("ROLLBACK TO SAVEPOINT prop")
```

The old file used `configure(changed_by=..., daily_amount=...)` without `reason`. Match whatever signature `configuration.store.update` actually has.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTEST src/engine/tests/test_ledger.py`
Expected: FAIL — `ImportError: cannot import name 'closing_carry_before'`… or `TypeError` on `book=`.

- [ ] **Step 3: Reshape `CashLedger` and remove the old tables**

In `src/engine/models.py`, delete `LumpsumCredit`, `AdjustmentSource` and `LedgerAdjustment`, and replace `CashLedger` with:

```python
class CashLedger(models.Model):
    """One row per trading day per book: what the engine account did that day.

    A summary of the journal, not the books themselves (spec §8). It is kept
    because a day-by-day view of carry is what M40a shows and what a person
    checks first; the journal is what it is checked against.

        opening_carry + daily_allocation + extra_allocation
            - cash_spent - charges + other == closing_carry

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
        help_text="Lumpsums, reinvested proceeds and restored carry.",
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
        return self.opening_carry + self.daily_allocation + self.extra_allocation

    @property
    def discrepancy(self) -> Decimal:
        return (
            self.available - self.cash_spent - self.charges + self.other - self.closing_carry
        )

    @property
    def reconciles(self) -> bool:
        return self.discrepancy == 0
```

`src/engine/migrations/0010_cash_ledger_from_journal.py`. Check the latest engine migration name first with `ls src/engine/migrations`, and set the dependency to it:

```python
"""The cash ledger becomes a summary of the journal (spec §8, J10).

The old table's rows, the lumpsum credits and the carried-forward adjustments
are paper history and are not converted; `seed_demo --reset` rebuilds a demo
through the new code. Dropping a table is DDL and is not stopped by the
append-only trigger, which fires on UPDATE and DELETE of rows.
"""

from decimal import Decimal

from django.db import migrations, models

TABLE = "engine_cashledger"

APPEND_ONLY = f"""
CREATE TRIGGER {TABLE}_append_only
    BEFORE UPDATE OR DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION steadystack_reject_mutation();
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        REVOKE UPDATE, DELETE ON {TABLE} FROM steadystack_app;
    END IF;
END
$$;
"""


def money():
    return models.DecimalField(max_digits=20, decimal_places=6, default=Decimal("0"))


class Migration(migrations.Migration):
    dependencies = [("engine", "0009_override_stop_day")]

    operations = [
        migrations.DeleteModel("LumpsumCredit"),
        migrations.DeleteModel("LedgerAdjustment"),
        migrations.DeleteModel("CashLedger"),
        migrations.CreateModel(
            name="CashLedger",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("book", models.CharField(choices=[("live", "Live"), ("paper", "Paper")], max_length=8)),
                ("trading_date", models.DateField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("trace_id", models.CharField(blank=True, db_index=True, max_length=64)),
                ("opening_carry", money()),
                ("daily_allocation", money()),
                ("extra_allocation", money()),
                ("cash_spent", money()),
                ("charges", money()),
                ("other", money()),
                ("closing_carry", money()),
                ("note", models.CharField(blank=True, max_length=255)),
            ],
            options={"ordering": ["-trading_date"], "verbose_name_plural": "cash ledger"},
        ),
        migrations.AddConstraint(
            model_name="cashledger",
            constraint=models.UniqueConstraint(
                fields=("book", "trading_date"), name="one_ledger_row_per_day"
            ),
        ),
        migrations.RunSQL(APPEND_ONLY, f"DROP TRIGGER IF EXISTS {TABLE}_append_only ON {TABLE};"),
    ]
```

After writing it, run `makemigrations --check --dry-run engine`. It must report no changes (the `help_text` on `extra_allocation` counts; add it to the migration field if Django asks).

- [ ] **Step 4: Rewrite `src/engine/ledger.py`**

```python
"""The engine's cash, read from the journal.

Carry is no longer a column somebody writes: it is the balance of the engine's
accounts (spec §4), and every rupee in it arrived by an allocation and left by
a fill. Three rules follow:

  * **the day's budget is an allocation**, posted when the day is planned
    (`journal.cash.allocate_daily`). Asking what a day is worth writes
    nothing, so a screen can ask before the day is planned;
  * **carry may go negative** when a fill costs more than the day was sized
    at. What the engine may spend is floored at zero (D7) and the next
    allocation repays the rest -- there is no overhang column to carry it;
  * **the daily row is a summary.** `settle_day` reads what the engine
    account did since the previous row and writes it down once. Each row
    balances on its own and opens where the previous one closed.

One quantisation point for display, `money()`, to two decimal places. The
journal itself keeps six, because Zerodha's statement does.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from django.db import transaction
from django.db.models import Q, Sum

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from engine.models import CashLedger
from journal.accounts import ENGINE_ACCOUNTS, ENGINE_CASH, INVESTMENTS, SETTLE_ENGINE
from journal.balances import engine_balance
from journal.models import Component, EntryKind, JournalLine
from journal.posting import q6
from trading_calendar.sessions import is_trading_day

logger = logging.getLogger("steadystack.engine")

PAISE = Decimal("0.01")
ZERO = Decimal("0")


def money(value: Decimal) -> Decimal:
    """The single place a rupee amount is rounded for display."""
    return Decimal(value).quantize(PAISE, rounding=ROUND_HALF_UP)


class LedgerError(Exception):
    """A ledger operation that would break one of the rules."""


@dataclass(frozen=True)
class DayOpening:
    """What a trading day has to spend, and where it came from."""

    trading_date: date
    carry: Decimal
    daily_allocation: Decimal

    @property
    def spendable(self) -> Decimal:
        """`max(0, ...)` is D7's floor: an overspend is repaid, never spent twice."""
        return max(ZERO, money(self.carry + self.daily_allocation))

    @property
    def shortfall(self) -> Decimal:
        raw = money(self.carry + self.daily_allocation)
        return -raw if raw < ZERO else ZERO


def engine_book(book: str | None = None) -> str:
    from configuration.store import current

    return book or current().order_mode


def opening_for(day: date, *, book: str | None = None) -> DayOpening:
    """What *day* has to spend (M14). Pure: it writes nothing.

    Before the day is planned its allocation is the configured amount it will
    receive; once planned, it is what was actually allocated. A paused day
    receives nothing -- accruing while paused would bring a month away back as
    one large purchase at whatever the market was doing that morning.
    """
    from configuration.pause import is_paused
    from configuration.store import current
    from journal.cash import daily_allocation_for

    book = engine_book(book)
    posted = daily_allocation_for(day, book=book)
    carry = engine_balance(book, through=day) - posted

    if posted:
        daily = posted
    elif not is_trading_day(day) or is_paused(on=day):
        daily = ZERO
    else:
        daily = q6(current().daily_amount)

    return DayOpening(trading_date=day, carry=q6(carry), daily_allocation=q6(daily))


def spendable_on(day: date, *, book: str | None = None) -> Decimal:
    return opening_for(day, book=book).spendable


def closing_carry_before(day: date, *, book: str | None = None) -> Decimal:
    """The engine's balance at the end of the previous calendar day."""
    return money(engine_balance(engine_book(book), through=day - timedelta(days=1)))


def row_for(day: date, *, book: str | None = None) -> CashLedger | None:
    return CashLedger.objects.filter(book=engine_book(book), trading_date=day).first()


def is_settled(day: date, *, book: str | None = None) -> bool:
    return row_for(day, book=book) is not None


def _unsummarised(book: str, day: date, previous: CashLedger | None):
    """Engine-account lines no earlier row counted, dated on or before *day*.

    An entry dated into a day that already has its row -- a statement line
    confirmed a week late -- is counted by the next row rather than lost.
    """
    lines = JournalLine.objects.filter(
        entry__book=book, account_id__in=ENGINE_ACCOUNTS, entry__occurred_on__lte=day
    )
    if previous is not None:
        lines = lines.exclude(
            Q(entry__occurred_on__lte=previous.trading_date)
            & Q(entry__created_at__lte=previous.created_at)
        )
    return lines


def _total(lines) -> Decimal:
    return q6(lines.aggregate(total=Sum("amount"))["total"] or ZERO)


@transaction.atomic
def settle_day(day: date, *, book: str | None = None, note: str = "") -> CashLedger:
    """Write the day's summary row, once. A second call returns the first."""
    from journal.cash import DAILY

    if not is_trading_day(day):
        raise LedgerError(f"{day} is not a trading day; no ledger row is due.")

    book = engine_book(book)
    existing = row_for(day, book=book)
    if existing is not None:
        return existing

    previous = (
        CashLedger.objects.filter(book=book, trading_date__lt=day).order_by("-trading_date").first()
    )
    window = _unsummarised(book, day, previous)
    allocations = window.filter(entry__kind=EntryKind.ALLOCATION, account_id=ENGINE_CASH)
    daily = _total(allocations.filter(entry__source_type=DAILY))
    extra = _total(allocations.exclude(entry__source_type=DAILY))

    fill_entries = window.filter(
        entry__kind=EntryKind.BUY_FILL, account_id=SETTLE_ENGINE
    ).values("entry_id")
    fill_costs = JournalLine.objects.filter(entry_id__in=fill_entries, account_id=INVESTMENTS)
    spent = _total(fill_costs.filter(component=Component.GROSS))
    charges = _total(fill_costs.exclude(component=Component.GROSS)) - _total(
        window.filter(entry__kind=EntryKind.CHARGE_CORRECTION)
    )

    opening = previous.closing_carry if previous else ZERO
    closing = q6(opening + _total(window))
    other = q6(closing - (opening + daily + extra - spent - charges))

    row = CashLedger.objects.create(
        book=book,
        trading_date=day,
        trace_id=current_trace_id(),
        opening_carry=opening,
        daily_allocation=daily,
        extra_allocation=extra,
        cash_spent=spent,
        charges=charges,
        other=other,
        closing_carry=closing,
        note=note[:255],
    )

    record(
        "ledger.day_settled",
        entity=row,
        after={
            "book": book,
            "trading_date": str(day),
            "opening_carry": str(opening),
            "daily_allocation": str(daily),
            "extra_allocation": str(extra),
            "cash_spent": str(spent),
            "charges": str(charges),
            "other": str(other),
            "closing_carry": str(closing),
        },
    )
    if closing < ZERO:
        # Worth a warning every time: a fill went through above the price the
        # day was sized against, and the next allocation pays for it.
        logger.warning(
            "the day spent more than it held",
            extra=fields(trading_date=str(day), book=book, closing_carry=str(closing)),
        )
    else:
        logger.info(
            "ledger day settled",
            extra=fields(trading_date=str(day), book=book, closing_carry=str(closing)),
        )
    return row


def reconcile(start: date | None = None, end: date | None = None, *, book: str | None = None) -> dict:
    """Every row balances, and each opens where the previous one closed."""
    book = engine_book(book)
    rows = CashLedger.objects.filter(book=book).order_by("trading_date")
    if start:
        rows = rows.filter(trading_date__gte=start)
    if end:
        rows = rows.filter(trading_date__lte=end)
    rows = list(rows)

    breaks = [
        {
            "date": str(current.trading_date),
            "problem": "opening carry does not match the previous closing",
            "expected": str(previous.closing_carry),
            "found": str(current.opening_carry),
        }
        for previous, current in pairwise(rows)
        if current.opening_carry != previous.closing_carry
    ]
    unbalanced = [str(row.trading_date) for row in rows if not row.reconciles]
    return {
        "rows": len(rows),
        "unbalanced": unbalanced,
        "chain_breaks": breaks,
        "ok": not unbalanced and not breaks,
        "net_in": str(money(sum((r.daily_allocation + r.extra_allocation for r in rows), ZERO))),
        "net_out": str(money(sum((r.cash_spent + r.charges for r in rows), ZERO))),
    }
```

Note on `reconcile`'s chain check over a filtered range: the first row in range is not checked against the row before the range, the same as today.

- [ ] **Step 5: Run the ledger tests**

Run: `PYTEST src/engine/tests/test_ledger.py`
Expected: all pass. Other suites still fail; the next steps fix them.

- [ ] **Step 6: Plan the day through the journal**

`src/engine/tradesheet.py`, in the branch `if carry_in is None:` (currently lines ~185-188):

```python
    if carry_in is None:
        from journal.cash import allocate_daily

        allocate_daily(trading_date, book=config.order_mode, actor=generated_by)
        opening = opening_for(trading_date, book=config.order_mode)
        carry_in = opening.carry
        spendable_cash = opening.spendable
```

The `else` branch (an explicit `carry_in`) is unchanged. Keep the variable name `config` as it is in that function. If the function names it differently, use that name.

Add to `src/engine/tests/test_tradesheet.py` (replacing the three tests at ~505-531 that used `settle_day(MONDAY, cash_spent=...)` and `credit_lumpsum`):

```python
def _spend(day, gross):
    """A buy the engine paid for, as the journal records one."""
    from journal import accounts
    from journal.models import Book, Component, EntryKind
    from journal.posting import Line, post

    post(
        book=Book.PAPER,
        kind=EntryKind.BUY_FILL,
        occurred_on=day,
        description="buy",
        lines=[
            Line(accounts.INVESTMENTS, Decimal(gross), component=Component.GROSS),
            Line(accounts.SETTLE_ENGINE, -Decimal(gross)),
        ],
    )


@pytest.mark.django_db
def test_regenerating_a_day_does_not_allocate_twice(portfolio):
    from journal.cash import daily_allocation_for
    from journal.models import Book

    generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)
    generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert daily_allocation_for(TUESDAY, book=Book.PAPER) == Decimal("1000")


@pytest.mark.django_db
def test_carry_is_taken_from_the_journal_when_not_stated(portfolio):
    from engine.ledger import settle_day
    from journal.cash import allocate_daily
    from journal.models import Book

    allocate_daily(MONDAY, book=Book.PAPER)
    _spend(MONDAY, "400")
    settle_day(MONDAY, book=Book.PAPER)

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert sheet.carry_in == Decimal("600")
    assert sheet.spendable_cash == Decimal("1600.00")


@pytest.mark.django_db
def test_an_overspend_reduces_what_the_next_day_may_spend(portfolio):
    """D7 reaching the tradesheet: the day is sized after the debt."""
    from journal.cash import allocate_daily
    from journal.models import Book

    allocate_daily(MONDAY, book=Book.PAPER)
    _spend(MONDAY, "1200")

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert sheet.spendable_cash == Decimal("800.00")


@pytest.mark.django_db
def test_a_lumpsum_is_spendable_from_the_next_sheet(portfolio):
    from journal.cash import record_lumpsum

    record_lumpsum(Decimal("5000"), received_on=TUESDAY, actor="operator")

    sheet = generate(trading_date=TUESDAY, quotes=quotes(AAA=100, BBB=100), has_session=True)

    assert sheet.spendable_cash == Decimal("6000.00")
```

The last three replace the tests of the same meaning at ~505-541 (`test_carry_is_taken_from_the_ledger_when_not_stated`, `test_an_overhang_reduces_what_the_next_day_may_spend`, `test_a_lumpsum_is_spendable_from_the_next_sheet`); delete the old ones.

- [ ] **Step 7: Settle the day through the journal**

`src/engine/settlement.py` sums a day's spend across every version of it: `day_spend(trading_date)`, `spend_and_charges(trading_date)`, and `_note(...)` naming each version that bought. That comes from the replan work on `master`. Keep all of it. Change only these:

- In `settle()`, replace the `settle_day(trading_date, cash_spent=..., charges=..., note=...)` call with:

```python
    from engine.ledger import engine_book
    from journal.trades import settle_trades

    modes = set(
        Order.objects.filter(trading_date=trading_date).values_list("mode", flat=True)
    ) or {engine_book()}
    for mode in modes:
        settle_trades(trading_date, book=mode)

    ledger = settle_day(
        trading_date,
        book=engine_book(),
        note=_note(trading_date, latest=tradesheet, filled=filled, stream=stream),
    )
```

  `cash_spent, charges = spend_and_charges(trading_date, stream=stream)` stays above it for the `day.settled` audit payload and the returned `Settlement`. Remove `"overhang"` from that payload.
- `day_spend()` sets `opening=opening_for(trading_date).spendable` and `remaining = opening - spent - charges`. With the journal, `opening_for(day)` already nets the day's own fills (they are posted as they happen), so this subtracts them twice. Replace the `opening=` argument with:

```python
    return DaySpend(
        opening=money(opening_for(trading_date).spendable + money(gross) + money(charges)),
        spent=money(gross),
        charges=money(charges),
        fills=shares,
    )
```

  `remaining` then equals `opening_for(trading_date).spendable`, which is what a replan may still spend.

- The factory `a_fill` in `src/engine/tests/test_settlement.py` creates the lot with `lots.from_fill(fill)` directly, so the fill never reaches the journal and every ledger assertion in that file would see nothing spent. Add `post_fill(fill)` (from `journal.trades`) after `lots.from_fill(fill)` there. Do the same everywhere a test or command creates fills this way: `grep -rn "from_fill(" src --include=*.py | grep -v "def from_fill"`. This covers `test_full_day.py`, `test_replan.py`, the web tests and `seed_demo` (Step 10). Code paths that go through `orders.booking.book` already post.

  Add to `src/engine/tests/test_settlement.py`:

```python
@pytest.mark.django_db
def test_what_a_replan_may_spend_is_the_engine_balance_left(sheet, instrument):
    from engine.ledger import spendable_on
    from engine.settlement import day_spend
    from journal.cash import allocate_daily
    from journal.models import Book

    allocate_daily(TUE, book=Book.PAPER)
    a_fill(an_order(sheet, instrument, quantity=1), quantity=1, price=Decimal("100"), charges=Decimal("0.50"))

    assert spendable_on(TUE) == Decimal("899.50")
    assert day_spend(TUE).remaining == spendable_on(TUE)
```

  Also run `src/engine/tests/test_replan.py`. Its expectations about `remaining` must hold unchanged. If one differs, the fixture's fills were not booked through `orders.booking.book`, so they have no journal entry. Book them.
- In `test_settlement.py:179`, the assertion `opening_for(date(2026, 9, 23)).opening_carry == Decimal("499.00")` becomes `opening_for(date(2026, 9, 23)).carry == Decimal("499")`. Adjust the expected value if the fixture's fill now carries rate-card charges through the journal. Recompute it from the fixture and write down why in a comment.
- In `test_full_day.py:206`, use `.carry` and `result.ledger.closing_carry`. The three-day chain test asserts `reconcile()["ok"]`.

- [ ] **Step 8: The remaining callers**

`src/engine/views.py:249-251`: `opening_for(sheet.trading_date).spendable` is unchanged in meaning; leave it.

`src/engine/day.py`, replace `lumpsums()`:

```python
def lumpsums(day: date) -> dict:
    """Money given to the engine on this day beyond its daily amount, split by
    whether the day's latest sheet was planned after it arrived."""
    from engine.ledger import engine_book
    from engine.models import TradeSheet
    from journal.accounts import ENGINE_CASH
    from journal.cash import DAILY
    from journal.models import EntryKind, JournalLine

    sheet = TradeSheet.objects.filter(trading_date=day).order_by("-version").first()
    rows = [
        {
            "amount": line.amount,
            "reference": line.entry.description,
            "recorded_at": line.entry.created_at,
            "source": line.entry.source_type,
        }
        for line in JournalLine.objects.filter(
            entry__book=engine_book(),
            entry__kind=EntryKind.ALLOCATION,
            entry__occurred_on=day,
            account_id=ENGINE_CASH,
        )
        .exclude(entry__source_type=DAILY)
        .select_related("entry")
        .order_by("entry__created_at")
    ]
    planned = sheet.generated_at if sheet else None
    return {
        "in_sheet": [r for r in rows if planned and r["recorded_at"] <= planned],
        "after": [r for r in rows if not planned or r["recorded_at"] > planned],
    }
```

In `src/templates/engine/detail.html`, change the lumpsum rows to read `row.amount`, `row.reference` and `row.recorded_at` (`grep -n "lumpsum\|credit\." src/templates/engine/detail.html`). Rewrite `src/engine/tests/test_day_view.py:154-160` to create the lumpsums with `journal.cash.record_lumpsum`.

`src/web/views.py`, `_credit_lumpsum`:

```python
    from journal.cash import CashError, record_lumpsum

    # the LumpsumForm validation above this point is unchanged
    try:
        result = record_lumpsum(
            form.cleaned_data["amount"],
            received_on=form.cleaned_data["received_on"],
            actor=request.user.get_username(),
            reference=form.cleaned_data["reference"],
        )
    except CashError as exc:
        messages.error(request, str(exc))
    else:
        from audit.context import today_ist
        from engine.models import TradeSheet

        received = form.cleaned_data["received_on"]
        text = f"₹{money(result.contribution.lines.get(account_id='1100').amount)} recorded."
        if result.restored:
            text += f" ₹{money(result.restored)} restored carry the quarterly settlement took."
        if received == today_ist() and TradeSheet.objects.filter(trading_date=received).exists():
            text += " Today's tradesheet was built before it arrived — regenerate it to spend it today."
        else:
            text += " It is spendable from the next tradesheet."
        messages.success(request, text)
```

Import `money` from `engine.ledger`. The `cash_ledger` view's CSV header becomes `date, opening_carry, daily_allocation, extra_allocation, cash_spent, charges, other, closing_carry, symbol, quantity, outcome, trace_id`, with the row values to match. `src/web/tests/test_dashboards.py:655-684`: assert on `JournalEntry` (kind `contribution`, source_type `lumpsum`) instead of `LumpsumCredit`.

`src/web/deck.py`:
- ~480 and ~675: `contributed = sum((row.deposit + row.lumpsum ...))` becomes `sum((row.daily_allocation + row.extra_allocation for row in CashLedger.objects.filter(book=engine_book())), ZERO)`. Task 17 replaces this with the journal's contributions.
- ~528-540: the overhang notice becomes `if row.closing_carry < ZERO:` with the text `f"the day spent ₹{money(-row.closing_carry)} more than it held. The next day's allocation repays it."`.
- ~563: `reconcile()` is unchanged.

`src/performance/ledgers.py`: `CashLedgerRow` fields become `trading_date, opening_carry, daily_allocation, extra_allocation, cash_spent, charges, other, closing_carry`, and `available = opening + daily + extra`. Filter the query by `book=engine_book()`. Totals sum the flows (daily, extra, spent, charges, other) and span the positions, exactly as today. Update `src/templates/web/cash_ledger.html` column headings to match. `src/performance/tests/test_ledgers.py:67`: `totals.daily_allocation == Decimal("2000")`.

`src/performance/series.py`: `_first_day()` filters `CashLedger` by `book=engine_book()`. In `totals()`, `contributed = money(sum((row.daily_allocation + row.extra_allocation for row in rows), ZERO))`, with rows filtered by book. Task 17 replaces this.

`src/rebalance/tests/test_batches.py:355-368`: `opening_for(TUE).deposit` becomes `opening_for(TUE).daily_allocation`.

- [ ] **Step 9: Contract-note differences become charge corrections**

Append to `src/journal/trades.py`:

```python
def post_charge_correction(lot, difference, *, actor: str, reference: str, on) -> JournalEntry | None:
    """The contract note's charges replace the estimate (M35b).

    Positive *difference* means the broker charged more: cash leaves. The part
    that belongs to shares still held moves the Investments cost; the part
    that belongs to shares already sold adjusts their realised gain, because
    the lot's history cannot be re-costed after it was consumed.
    """
    from journal.accounts import ENGINE_CASH, UNALLOCATED
    from journal.models import Book

    difference = q6(difference)
    if difference == ZERO:
        return None

    fill = getattr(lot, "created_from_fill", None)
    book = fill.order.mode if fill is not None else Book.LIVE
    engine_bought = fill is not None and settlement_account(fill.order) == SETTLE_ENGINE
    cash_account = ENGINE_CASH if engine_bought else UNALLOCATED

    held = q6(difference * lot.remaining_quantity / lot.quantity)
    sold = difference - held
    return post(
        book=book,
        kind=EntryKind.CHARGE_CORRECTION,
        occurred_on=on,
        description=f"charges on {lot.acquired_on} settled against {reference}",
        lines=[
            Line(INVESTMENTS, held, lot.instrument_id, Component.OTHER),
            Line(REALISED, sold, lot.instrument_id, Component.OTHER),
            Line(cash_account, -difference),
        ],
        actor=actor,
        source_type="lot",
        source_id=str(lot.id),
        idempotency_key=f"charge_correction:{lot.id}",
    )
```

In `src/portfolio/contract_note.py`, delete the imports of `engine.ledger` and `AdjustmentSource`, and replace the `if difference != ZERO: ledger.post_adjustment(...)` block with:

```python
    from journal.trades import post_charge_correction

    post_charge_correction(
        lot,
        difference,
        actor=actor,
        reference=note_reference or "the contract note",
        on=timezone.localdate(),
    )
```

It must be called before the `Lot.objects.filter(pk=lot.pk).update(... remaining_quantity=0)` line, while `lot.remaining_quantity` is still the held quantity. The in-memory `lot` object is not refreshed by `.update()`, so calling it after that line also works, but put it before for clarity.

Rewrite `src/records/tests/test_records.py:744-777` and `src/records/tests/test_views.py:194-212` to assert the `CHARGE_CORRECTION` entry. The cash line equals `-difference`, so a note charging ₹18 more gives a cash line of `-18`, and a note equal to the estimate posts no entry.

- [ ] **Step 10: seed_demo through the new paths**

In `src/engine/management/commands/seed_demo.py`:
- `_lumpsum`: `from journal.cash import record_lumpsum`; `record_lumpsum(Decimal("25000.00"), received_on=day, actor="operator", reference="birthday, from the grandparents")`.
- `_run_days`: before `budget = spendable_on(day)`, add `allocate_daily(day, book=Book.PAPER)`. After each `lot_writer.from_fill(fill)`, add `post_fill(fill)`. Replace `settle_day(day, cash_spent=spent, charges=charges)` with `settle_trades(day, book=Book.PAPER)` followed by `settle_day(day, book=Book.PAPER)`. Replace the skipped-day `settle_day(day, note=...)` with `settle_day(day, book=Book.PAPER, note=...)`. Drop the now-unused `spent`/`charges` accumulators.
- If `_rebalance_sell` books sells without `orders.booking.book`, add `post_fill(fill, consumption=...)` after its `fifo.consume` the same way.

Run: `DATABASE_URL=postgres://steadystack_owner:steadystack_owner_dev@localhost:5432/steadystack_journal DJANGO_DEBUG=true uv run python manage.py seed_demo --reset`. It must finish, and afterwards `uv run python manage.py shell -c "from engine.ledger import reconcile; print(reconcile(book='paper'))"` must print `'ok': True`.

- [ ] **Step 11: Run everything**

Run: `PYTEST src`
Expected: all pass. Run `grep -rn "LumpsumCredit\|LedgerAdjustment\|credit_lumpsum\|post_adjustment\|\.overhang\|\.lumpsum\b\|\.deposit\b\|opening_carry + opening" src --include='*.py' --include='*.html' | grep -v migrations`. It must print nothing.

Run `uv run ruff check src && uv run ruff format --check src`. Run `npm run build` because templates changed.

- [ ] **Step 12: Commit**

With `/atomic:atomic-commit`, grouped roughly as: `feat(engine)!: read carry and the daily budget from the journal` (engine, tradesheet, settlement, migration, tests), `feat(portfolio): post contract-note differences as charge corrections`, `feat(web): record lumpsums through the journal`, `refactor(performance): read the daily summary's new columns`, `chore(engine): seed the demo through the journal`. The engine commit carries the footer `BREAKING CHANGE: LumpsumCredit and LedgerAdjustment are removed; paper cash history is not converted`.

---

### Task 9: External trades, IPOs, dividends and the opening balance

**Files:**
- Create: `src/journal/external.py`, `src/journal/management/__init__.py`, `src/journal/management/commands/__init__.py`, `src/journal/management/commands/open_books.py`, `src/journal/tests/test_external.py`
- Modify: `src/records/models.py` (`ExternalTransaction.paid_from`), `src/records/external.py` (`record_external`), `src/records/forms.py` (the external-transaction form), the view that calls `record_external` (`grep -rn "record_external(" src --include=*.py | grep -v tests`), and its template
- Create: `src/records/migrations/00NN_externaltransaction_paid_from.py` (generated)
- Modify: `src/portfolio/models.py` (`Lot.external_transaction`), `src/portfolio/contract_note.py` (carry the link to the replacement lot); create `src/portfolio/migrations/00NN_lot_external_transaction.py` (generated)

**Interfaces:**
- Consumes: `post`, `Line`, `q6`; `trades.settle_trades`; `gains.fifo.Consumption.cost`.
- Produces:
  - `records.models.PaidFrom` (`ZERODHA="zerodha"`, `BANK="bank"`); `ExternalTransaction.paid_from` (default `zerodha`)
  - `record_external(..., paid_from: str = PaidFrom.ZERODHA)`
  - `journal.external.post_external(tx, *, consumption=None) -> JournalEntry | None`, keyed `external:{tx.id}`
  - `journal.external.record_dividend(instrument_id: str, amount, *, paid_on: date, actor: str, reference: str = "") -> JournalEntry`
  - `journal.external.live_open_lots()`, a queryset of open, unsuperseded lots that did not come from a paper fill
  - `journal.external.open_books(*, cash, on: date, actor: str) -> JournalEntry`
  - `manage.py open_books --cash <amount> --on <YYYY-MM-DD> --actor <name>`

- [ ] **Step 1: Write the failing tests** — `src/journal/tests/test_external.py`:

```python
from datetime import date
from decimal import Decimal

import pytest

from journal import accounts
from journal.balances import balance
from journal.external import open_books, record_dividend
from journal.models import Book, EntryKind, JournalEntry
from records.external import record_external
from records.models import ExternalSide, PaidFrom

DAY = date(2026, 9, 22)


@pytest.fixture
def infy(db):
    from portfolio.models import Instrument

    return Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")


def live(code):
    return balance(code, book=Book.LIVE)


@pytest.mark.django_db
def test_a_direct_buy_at_zerodha_is_paid_from_unallocated_once_settled(infy):
    record_external(
        instrument=infy, side=ExternalSide.BUY, quantity=2, occurred_on=DAY, actor="operator",
        reason="bought in Kite", price=Decimal("100"), charges=Decimal("0.5"),
    )
    assert live(accounts.INVESTMENTS) == Decimal("200.5")
    assert live(accounts.UNALLOCATED) == Decimal("-200.5")
    assert live(accounts.SETTLE_OTHER) == 0


@pytest.mark.django_db
def test_an_ipo_paid_from_the_bank_is_a_contribution_in_shares(infy):
    record_external(
        instrument=infy, side=ExternalSide.BUY, quantity=10, occurred_on=DAY, actor="operator",
        reason="IPO allotment", price=Decimal("50"), paid_from=PaidFrom.BANK,
    )
    assert live(accounts.INVESTMENTS) == Decimal("500")
    assert live(accounts.CONTRIBUTIONS) == Decimal("-500")
    assert live(accounts.UNALLOCATED) == 0


@pytest.mark.django_db
def test_a_transfer_in_is_a_contribution_at_the_transferors_basis(infy):
    record_external(
        instrument=infy, side=ExternalSide.TRANSFER_IN, quantity=1, occurred_on=DAY,
        actor="operator", reason="gift", price=Decimal("900"), acquired_on=date(2020, 1, 1),
    )
    assert live(accounts.CONTRIBUTIONS) == Decimal("-900")


@pytest.mark.django_db
def test_a_sale_at_the_broker_books_proceeds_and_gain(infy):
    record_external(
        instrument=infy, side=ExternalSide.BUY, quantity=1, occurred_on=DAY, actor="o",
        reason="r", price=Decimal("100"),
    )
    record_external(
        instrument=infy, side=ExternalSide.SELL, quantity=1, occurred_on=date(2026, 9, 23),
        actor="o", reason="sold in Kite", price=Decimal("130"), charges=Decimal("1"),
    )
    assert live(accounts.INVESTMENTS) == 0
    assert live(accounts.REALISED) == Decimal("-29")  # 130 - 100 - 1
    assert live(accounts.UNALLOCATED) == Decimal("29")


@pytest.mark.django_db
def test_a_transfer_out_is_a_withdrawal_in_shares_at_cost(infy):
    record_external(
        instrument=infy, side=ExternalSide.BUY, quantity=1, occurred_on=DAY, actor="o",
        reason="r", price=Decimal("100"),
    )
    record_external(
        instrument=infy, side=ExternalSide.TRANSFER_OUT, quantity=1, occurred_on=DAY,
        actor="o", reason="moved",
    )
    assert live(accounts.INVESTMENTS) == 0
    assert live(accounts.WITHDRAWALS) == Decimal("100")


@pytest.mark.django_db
def test_a_dividend_is_income_withdrawn_to_the_owner(infy):
    record_dividend("INFY", Decimal("42"), paid_on=DAY, actor="operator")
    assert live(accounts.DIVIDENDS) == Decimal("-42")
    assert live(accounts.WITHDRAWALS) == Decimal("42")


@pytest.mark.django_db
def test_the_books_open_once_with_cash_and_the_live_lots(infy):
    record_external(
        instrument=infy, side=ExternalSide.TRANSFER_IN, quantity=1, occurred_on=DAY,
        actor="o", reason="r", price=Decimal("900"),
    )
    before = JournalEntry.objects.count()
    entry = open_books(cash=Decimal("-150.14"), on=date(2026, 9, 24), actor="operator")
    assert entry.kind == EntryKind.OPENING_BALANCE
    assert JournalEntry.objects.count() == before + 1
    with pytest.raises(ValueError, match="already"):
        open_books(cash=Decimal("0"), on=date(2026, 9, 24), actor="operator")
```

The opening-balance entry includes the lots that exist at go-live. In the test above, the transfer-in has already posted its own entry, so the opening entry is only correct for lots that have none. `open_books` must therefore include only live open lots **with no journal entry of their own** (no `external:` or `fill:` entry). Assert that as well:

```python
@pytest.mark.django_db
def test_opening_does_not_count_a_lot_the_journal_already_holds(infy):
    record_external(
        instrument=infy, side=ExternalSide.TRANSFER_IN, quantity=1, occurred_on=DAY,
        actor="o", reason="r", price=Decimal("900"),
    )
    open_books(cash=Decimal("100"), on=date(2026, 9, 24), actor="operator")
    assert live(accounts.INVESTMENTS) == Decimal("900")
    assert live(accounts.OPENING) == Decimal("-100")
```

- [ ] **Step 2: Run to verify they fail** — `PYTEST src/journal/tests/test_external.py` → ImportError.

- [ ] **Step 3: Implement**

`src/records/models.py`:

```python
class PaidFrom(models.TextChoices):
    ZERODHA = "zerodha", "Zerodha funds"
    BANK = "bank", "The owner's bank (an IPO through UPI)"
```

Add `paid_from = models.CharField(max_length=8, choices=PaidFrom.choices, default=PaidFrom.ZERODHA)` to `ExternalTransaction`. Run `makemigrations records`.

`src/journal/external.py`:

```python
"""Entries for what happened outside the engine (spec §6).

A direct buy at Zerodha settles like any trade. An IPO paid through UPI never
touches Zerodha's funds, so it is a contribution made in shares. Transfers are
contributions and withdrawals in kind, at cost: what a transfer out was *for*
is not this system's decision (M36d), so it books no gain.
"""

from datetime import date
from decimal import Decimal

from audit.events import SYSTEM_ACTOR
from journal.accounts import (
    CONTRIBUTIONS,
    DIVIDENDS,
    INVESTMENTS,
    OPENING,
    REALISED,
    SETTLE_OTHER,
    UNALLOCATED,
    WITHDRAWALS,
)
from journal.models import Book, Component, EntryKind, JournalEntry
from journal.posting import Line, post, q6

ZERO = Decimal("0")


def post_external(tx, *, consumption=None) -> JournalEntry | None:
    from journal.trades import settle_trades
    from records.models import ExternalSide, PaidFrom

    symbol = tx.instrument_id
    gross = q6(Decimal(tx.price) * tx.quantity)
    charges = q6(tx.charges)

    if tx.side in (ExternalSide.BUY, ExternalSide.TRANSFER_IN):
        if tx.side == ExternalSide.BUY and tx.paid_from == PaidFrom.ZERODHA:
            credit = SETTLE_OTHER
        else:
            credit = CONTRIBUTIONS
        lines = [
            Line(INVESTMENTS, gross, symbol, Component.GROSS),
            Line(INVESTMENTS, charges, symbol, Component.OTHER),
            Line(credit, -(gross + charges)),
        ]
        kind = EntryKind.EXTERNAL_ACQUISITION
    else:
        if consumption is None:
            return None
        cost = q6(consumption.cost)
        if tx.side == ExternalSide.SELL:
            lines = [
                Line(SETTLE_OTHER, gross - charges),
                Line(REALISED, charges, symbol, Component.OTHER),
                Line(INVESTMENTS, -cost, symbol),
                Line(REALISED, -(gross - cost), symbol),
            ]
        else:  # TRANSFER_OUT: a withdrawal of shares, at cost
            lines = [Line(WITHDRAWALS, cost, symbol), Line(INVESTMENTS, -cost, symbol)]
        kind = EntryKind.EXTERNAL_DISPOSAL

    entry = post(
        book=Book.LIVE,
        kind=kind,
        occurred_on=tx.occurred_on,
        description=f"{tx.side} {tx.quantity} {symbol}: {tx.reason}",
        lines=lines,
        actor=tx.actor,
        source_type="external_transaction",
        source_id=str(tx.id),
        idempotency_key=f"external:{tx.id}",
    )
    settle_trades(tx.occurred_on, book=Book.LIVE)
    return entry


def record_dividend(instrument_id: str, amount, *, paid_on: date, actor: str, reference: str = ""):
    """The portfolio's income, paid to the owner's bank: income and a withdrawal (J6)."""
    amount = q6(amount)
    if amount <= ZERO:
        raise ValueError("A dividend must be a positive amount.")
    return post(
        book=Book.LIVE,
        kind=EntryKind.DIVIDEND,
        occurred_on=paid_on,
        description=f"dividend {instrument_id} {reference}".strip(),
        lines=[Line(WITHDRAWALS, amount), Line(DIVIDENDS, -amount, instrument_id)],
        actor=actor,
        source_type="dividend",
        source_id=reference,
    )


def live_open_lots():
    from portfolio.models import Lot

    return Lot.objects.filter(superseded_by__isnull=True, remaining_quantity__gt=0).exclude(
        created_from_fill__order__mode=Book.PAPER
    )


def open_books(*, cash, on: date, actor: str = SYSTEM_ACTOR) -> JournalEntry:
    """The live book's first entry (J10): Zerodha's cash on go-live day, and
    the cost of every live lot the journal does not already hold."""
    if JournalEntry.objects.filter(book=Book.LIVE, kind=EntryKind.OPENING_BALANCE).exists():
        raise ValueError("The live book is already open.")

    journalled = set(
        JournalEntry.objects.filter(
            book=Book.LIVE, source_type__in=["external_transaction", "fill"]
        ).values_list("source_id", flat=True)
    )
    lines = [Line(UNALLOCATED, q6(cash))]
    for lot in live_open_lots().filter(acquired_on__lte=on):
        source = lot.created_from_fill_id or lot.external_transaction_id
        if source is not None and str(source) in journalled:
            continue
        lines.append(Line(INVESTMENTS, q6(lot.cost_per_unit * lot.remaining_quantity), lot.instrument_id))

    total = sum((line.amount for line in lines), ZERO)
    lines.append(Line(OPENING, -total))
    return post(
        book=Book.LIVE,
        kind=EntryKind.OPENING_BALANCE,
        occurred_on=on,
        description="opening balance at go-live",
        lines=lines,
        actor=actor,
        idempotency_key="opening_balance:live",
    )
```

`Lot` has no link to the `ExternalTransaction` that created it, and `open_books` needs one to skip lots the journal already holds. Add it to `src/portfolio/models.py` `Lot`:

```python
    external_transaction = models.OneToOneField(
        "records.ExternalTransaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lot",
        help_text="The external buy or transfer in that created this lot.",
    )
```

Run `makemigrations portfolio`. The new migration depends on the latest `records` migration, and `records` already depends on `portfolio`. That is not a cycle, because the dependency is on older portfolio migrations. In `records.external._create_lot`, pass `external_transaction=entry`. A lot superseded by contract-note finalisation (`contract_note.finalise`) must carry the link across: add `external_transaction=lot.external_transaction` to the replacement's `Lot.objects.create(...)`, and do the same for `created_from_fill` only if finalise already copies it (check; do not change fill behaviour otherwise). Lots are not an append-only table, so adding a nullable column is safe.

`src/records/external.py` `record_external`: add the `paid_from: str = PaidFrom.ZERODHA` keyword and store it on the created `ExternalTransaction`. At the end, once the lot is created or the consumption is done, call `post_external(entry, consumption=consumed)` (import `from journal.external import post_external`). `bonus_issue` posts nothing: a bonus has no cash and no cost.

In the external-transaction form in `src/records/forms.py`, add `paid_from = forms.ChoiceField(choices=PaidFrom.choices, initial=PaidFrom.ZERODHA)`. The view passes `paid_from=form.cleaned_data["paid_from"]`, and the template renders the field next to the side selector.

`src/journal/management/commands/open_books.py`:

```python
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from journal.external import open_books


class Command(BaseCommand):
    help = "Post the live book's opening balance from Zerodha's statement on go-live day."

    def add_arguments(self, parser):
        parser.add_argument("--cash", required=True, help="Zerodha's funds balance, as the statement shows it.")
        parser.add_argument("--on", required=True, help="YYYY-MM-DD")
        parser.add_argument("--actor", required=True)

    def handle(self, *args, **options):
        try:
            entry = open_books(
                cash=Decimal(options["cash"]),
                on=date.fromisoformat(options["on"]),
                actor=options["actor"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Opened the live book: entry #{entry.number}."))
```

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal src/records` → all pass.

- [ ] **Step 5: Commit** — `feat(journal): journal external trades, IPOs, dividends and the opening balance` (and `feat(records): record whether an external buy was paid from the bank` if the atomic grouping splits it).

---

### Task 10: Reading a Console funds statement

**Files:**
- Create: `src/journal/statement/__init__.py`, `src/journal/statement/parse.py`, `src/journal/tests/test_statement_parse.py`, `src/journal/tests/fixtures/statement_aug.csv`, `src/journal/tests/fixtures/statement_sep.csv`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) parse.Row(position: int, posting_date: date, particulars: str, cost_center: str, voucher_type: str, debit: Decimal, credit: Decimal, net_balance: Decimal, fingerprint: str)`
  - `@dataclass(frozen=True) parse.Statement(opening: Decimal, closing: Decimal, rows: list[Row])`
  - `parse.StatementError(Exception)`
  - `parse.parse(raw: bytes | str) -> Statement`

The fixtures are **synthetic** rows in the real file's shape, not the owner's statement. `statement_aug.csv`:

```csv
particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance
Opening Balance,,,,,,-150.140000
Delayed payment charges for July - 2026,2026-08-05,NSE-EQ - Z,Journal Entry,1.570000,0.000000,-151.710000
AMC for Demat Account for 01-04-2026 to 30-06-2026,2026-08-14,NSE-EQ - Z,Journal Entry,88.500000,0.000000,-240.210000
Funds added using UPI from ABC123 with reference number 111111111111,2026-08-14,NSE-EQ - Z,Bank Receipts,0.000000,500.000000,259.790000
Net settlement for Equity with settlement number: 2026153,2026-08-14,NSE-EQ - Z,Book Voucher,248.221084,0.000000,11.568916
Closing Balance,,,,,,11.568916
```

`statement_sep.csv` overlaps the last August line and continues:

```csv
particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance
Opening Balance,,,,,,259.790000
Net settlement for Equity with settlement number: 2026153,2026-08-14,NSE-EQ - Z,Book Voucher,248.221084,0.000000,11.568916
Funds added using UPI from ABC123 with reference number 222222222222,2026-09-18,NSE-EQ - Z,Bank Receipts,0.000000,1000.000000,1011.568916
Charges for enabling DDPI,2026-09-22,NSE-EQ - Z,Journal Entry,118.000000,0.000000,893.568916
Kite Connect API Charges (2026-09-23),2026-09-23,NSE-EQ - Z,Journal Entry,500.000220,0.000000,393.568696
Payout done for quarterly settlement,2026-10-02,NSE-EQ - Z,Bank Payments,393.568696,0.000000,0.000000
Closing Balance,,,,,,0.000000
```

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from journal.statement.parse import StatementError, parse

FIXTURES = Path(__file__).parent / "fixtures"


def test_a_statement_parses_to_rows_and_balances():
    statement = parse((FIXTURES / "statement_aug.csv").read_bytes())
    assert statement.opening == Decimal("-150.14")
    assert statement.closing == Decimal("11.568916")
    assert len(statement.rows) == 4
    first = statement.rows[0]
    assert first.posting_date == date(2026, 8, 5)
    assert first.debit == Decimal("1.57")
    assert first.voucher_type == "Journal Entry"


def test_the_same_row_has_the_same_fingerprint_in_two_files():
    aug = parse((FIXTURES / "statement_aug.csv").read_bytes())
    sep = parse((FIXTURES / "statement_sep.csv").read_bytes())
    assert aug.rows[-1].fingerprint == sep.rows[0].fingerprint


def test_identical_rows_on_one_day_get_different_fingerprints():
    raw = (
        "particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance\n"
        "Opening Balance,,,,,,0\n"
        "DP charges,2026-09-01,NSE-EQ - Z,Journal Entry,15.34,0,-15.34\n"
        "DP charges,2026-09-01,NSE-EQ - Z,Journal Entry,15.34,0,-30.68\n"
        "Closing Balance,,,,,,-30.68\n"
    )
    rows = parse(raw).rows
    assert rows[0].fingerprint != rows[1].fingerprint


def test_a_running_balance_that_does_not_add_up_is_refused():
    raw = (
        "particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance\n"
        "Opening Balance,,,,,,0\n"
        "AMC,2026-09-01,NSE-EQ - Z,Journal Entry,10,0,-11\n"
        "Closing Balance,,,,,,-11\n"
    )
    with pytest.raises(StatementError, match="line 1"):
        parse(raw)


def test_a_file_with_the_wrong_columns_is_refused():
    with pytest.raises(StatementError, match="columns"):
        parse("date,amount\n2026-09-01,10\n")


def test_a_file_without_its_balance_rows_is_refused():
    with pytest.raises(StatementError, match="Opening Balance"):
        parse("particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance\n")
```

- [ ] **Step 2: Run to verify they fail** — `PYTEST src/journal/tests/test_statement_parse.py` → ImportError.

- [ ] **Step 3: Implement** `src/journal/statement/parse.py`:

```python
"""Zerodha Console's funds statement, as the CSV it exports (spec §7.2).

The rows carry no identifier, and monthly exports overlap, so each row is
fingerprinted by what it says plus how many identical rows came before it on
the same day. The running balance is checked row by row: a file whose
arithmetic does not hold is not a statement this system can reconcile against.
"""

import csv
import hashlib
import io
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from journal.posting import q6

COLUMNS = ["particulars", "posting_date", "cost_center", "voucher_type", "debit", "credit", "net_balance"]


class StatementError(Exception):
    pass


@dataclass(frozen=True)
class Row:
    position: int
    posting_date: date
    particulars: str
    cost_center: str
    voucher_type: str
    debit: Decimal
    credit: Decimal
    net_balance: Decimal
    fingerprint: str


@dataclass(frozen=True)
class Statement:
    opening: Decimal
    closing: Decimal
    rows: list[Row]


def _amount(value: str, where: str) -> Decimal:
    try:
        return q6(Decimal(value.strip() or "0"))
    except InvalidOperation as exc:
        raise StatementError(f"{where}: {value!r} is not an amount.") from exc


def parse(raw: bytes | str) -> Statement:
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    reader = csv.DictReader(io.StringIO(text))
    if [c.strip() for c in (reader.fieldnames or [])] != COLUMNS:
        raise StatementError(f"Expected the columns {', '.join(COLUMNS)}.")

    opening = closing = None
    rows: list[Row] = []
    seen: Counter = Counter()

    for record in reader:
        particulars = record["particulars"].strip()
        if particulars == "Opening Balance":
            opening = _amount(record["net_balance"], "Opening Balance")
            continue
        if particulars == "Closing Balance":
            closing = _amount(record["net_balance"], "Closing Balance")
            continue

        position = len(rows) + 1
        where = f"line {position}"
        try:
            posting_date = date.fromisoformat(record["posting_date"].strip())
        except ValueError as exc:
            raise StatementError(f"{where}: {record['posting_date']!r} is not a date.") from exc
        debit = _amount(record["debit"], where)
        credit = _amount(record["credit"], where)

        identity = (posting_date.isoformat(), particulars, str(debit), str(credit))
        occurrence = seen[identity]
        seen[identity] += 1
        fingerprint = hashlib.sha256("|".join([*identity, str(occurrence)]).encode()).hexdigest()

        rows.append(
            Row(
                position=position,
                posting_date=posting_date,
                particulars=particulars,
                cost_center=record["cost_center"].strip(),
                voucher_type=record["voucher_type"].strip(),
                debit=debit,
                credit=credit,
                net_balance=_amount(record["net_balance"], where),
                fingerprint=fingerprint,
            )
        )

    if opening is None or closing is None:
        raise StatementError("The file has no Opening Balance or Closing Balance row.")

    running = opening
    for row in rows:
        running = q6(running - row.debit + row.credit)
        if running != row.net_balance:
            raise StatementError(
                f"line {row.position}: the balance should be {running}, the file says {row.net_balance}."
            )
    if running != closing:
        raise StatementError(f"The closing balance should be {running}, the file says {closing}.")

    return Statement(opening=opening, closing=closing, rows=rows)
```

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal/tests/test_statement_parse.py` → all pass.

- [ ] **Step 5: Commit** — `feat(journal): read a Console funds statement`

---

### Task 11: Storing an upload, safely repeatable

**Files:**
- Modify: `src/journal/models.py` (add `StatementUpload`, `StatementLine`)
- Create: `src/journal/migrations/0004_statement.py` (generated), `src/journal/migrations/0005_statement_enforcement.py`, `src/journal/statement/store.py`, `src/journal/tests/test_statement_store.py`

**Interfaces:**
- Consumes: `parse.parse`, `parse.StatementError`.
- Produces:
  - `StatementUpload(id, filename, sha256 unique, raw, period_from, period_to, opening_balance, closing_balance, uploaded_by, uploaded_at, trace_id)`
  - `StatementLine(id, upload FK, position, fingerprint unique, posting_date, particulars, cost_center, voucher_type, debit, credit, net_balance, matched_entry FK JournalEntry null)`. The only update ever allowed is `matched_entry` going from null to a value.
  - `store.@dataclass(frozen=True) Stored(upload: StatementUpload, added: int, already_held: int)`
  - `store.store_upload(raw: bytes, *, filename: str, actor: str) -> Stored`
  - `store.link(line: StatementLine, entry: JournalEntry) -> None`

- [ ] **Step 1: Write the failing tests** — `src/journal/tests/test_statement_store.py`:

```python
from pathlib import Path

import pytest
from django.db import transaction
from django.db.utils import InternalError

from journal.models import StatementLine
from journal.statement.parse import StatementError
from journal.statement.store import store_upload

FIXTURES = Path(__file__).parent / "fixtures"
AUG = (FIXTURES / "statement_aug.csv").read_bytes()
SEP = (FIXTURES / "statement_sep.csv").read_bytes()


@pytest.mark.django_db
def test_an_upload_stores_every_line():
    stored = store_upload(AUG, filename="aug.csv", actor="operator")
    assert stored.added == 4
    assert StatementLine.objects.count() == 4


@pytest.mark.django_db
def test_an_overlapping_upload_adds_only_new_lines():
    store_upload(AUG, filename="aug.csv", actor="operator")
    stored = store_upload(SEP, filename="sep.csv", actor="operator")
    assert stored.already_held == 1
    assert stored.added == 4
    assert StatementLine.objects.count() == 8


@pytest.mark.django_db
def test_the_same_file_twice_is_refused():
    store_upload(AUG, filename="aug.csv", actor="operator")
    with pytest.raises(StatementError, match="already"):
        store_upload(AUG, filename="aug-again.csv", actor="operator")
    assert StatementLine.objects.count() == 4


@pytest.mark.django_db
def test_a_file_that_starts_where_the_last_ended_is_accepted():
    store_upload(csv_of("0", [("AMC", "2026-08-01", "10", "0")]), filename="a.csv", actor="o")
    stored = store_upload(csv_of("-10", [("AMC", "2026-09-01", "10", "0")]), filename="b.csv", actor="o")
    assert stored.added == 1


@pytest.mark.django_db
def test_a_broken_balance_chain_is_refused():
    store_upload(csv_of("0", [("AMC", "2026-08-01", "10", "0")]), filename="a.csv", actor="o")
    with pytest.raises(StatementError, match="continue"):
        store_upload(csv_of("500", [("AMC", "2026-10-01", "10", "0")]), filename="c.csv", actor="o")


@pytest.mark.django_db
def test_a_statement_line_cannot_be_edited_except_to_match_it():
    store_upload(AUG, filename="aug.csv", actor="operator")
    line = StatementLine.objects.first()
    with pytest.raises(InternalError), transaction.atomic():
        StatementLine.objects.filter(pk=line.pk).update(particulars="changed")
    with pytest.raises(InternalError), transaction.atomic():
        StatementLine.objects.filter(pk=line.pk).delete()
```

`csv_of` builds a small, arithmetically valid statement. Add it to `src/journal/tests/conftest.py` as a plain function, since Task 13 uses it too, and import it with `from journal.tests.conftest import csv_of`:

```python
HEADER = "particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance\n"


def csv_of(opening, rows, voucher_type="Journal Entry"):
    """rows: (particulars, date, debit, credit); the running balance is computed."""
    balance = Decimal(opening)
    lines = [HEADER, f"Opening Balance,,,,,,{opening}\n"]
    for particulars, day, debit, credit in rows:
        balance = balance - Decimal(debit) + Decimal(credit)
        lines.append(f"{particulars},{day},NSE-EQ - Z,{voucher_type},{debit},{credit},{balance}\n")
    lines.append(f"Closing Balance,,,,,,{balance}\n")
    return "".join(lines).encode()
```

`Decimal` is already imported at the top of `conftest.py` (Task 6).

- [ ] **Step 2: Run to verify they fail** — `PYTEST src/journal/tests/test_statement_store.py` → ImportError.

- [ ] **Step 3: Implement**

Add to `src/journal/models.py`:

```python
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


class StatementLine(models.Model):
    """One row of Zerodha's statement. Matched to an entry once, and never changed."""

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
        JournalEntry, on_delete=models.PROTECT, null=True, blank=True, related_name="statement_lines"
    )

    class Meta:
        ordering = ["posting_date", "upload__uploaded_at", "position"]

    @property
    def amount(self):
        """Signed as Zerodha's cash moved: credit positive, debit negative."""
        return self.credit - self.debit
```

Run `makemigrations journal` (→ `0004_…`). Then `src/journal/migrations/0005_statement_enforcement.py`:

```python
from django.db import migrations

LINE = "journal_statementline"
UPLOAD = "journal_statementupload"

SQL = f"""
CREATE OR REPLACE FUNCTION journal_statementline_match_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'statement lines are never deleted';
    END IF;
    IF OLD.matched_entry_id IS NOT NULL
       OR NEW.matched_entry_id IS NULL
       OR (OLD.upload_id, OLD.position, OLD.fingerprint, OLD.posting_date, OLD.particulars,
           OLD.cost_center, OLD.voucher_type, OLD.debit, OLD.credit, OLD.net_balance)
          IS DISTINCT FROM
          (NEW.upload_id, NEW.position, NEW.fingerprint, NEW.posting_date, NEW.particulars,
           NEW.cost_center, NEW.voucher_type, NEW.debit, NEW.credit, NEW.net_balance)
    THEN
        RAISE EXCEPTION 'a statement line may only be matched, once';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE TRIGGER {LINE}_match_only
    BEFORE UPDATE OR DELETE ON {LINE}
    FOR EACH ROW EXECUTE FUNCTION journal_statementline_match_only();

CREATE TRIGGER {UPLOAD}_append_only
    BEFORE UPDATE OR DELETE ON {UPLOAD}
    FOR EACH ROW EXECUTE FUNCTION steadystack_reject_mutation();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        REVOKE UPDATE, DELETE ON {UPLOAD} FROM steadystack_app;
        REVOKE DELETE ON {LINE} FROM steadystack_app;
    END IF;
END
$$;
"""

REVERSE = f"""
DROP TRIGGER IF EXISTS {LINE}_match_only ON {LINE};
DROP TRIGGER IF EXISTS {UPLOAD}_append_only ON {UPLOAD};
DROP FUNCTION IF EXISTS journal_statementline_match_only();
"""


class Migration(migrations.Migration):
    dependencies = [("journal", "0004_statementupload_statementline")]  # use the generated name
    operations = [migrations.RunSQL(SQL, REVERSE)]
```

`src/journal/statement/store.py`:

```python
"""Keeping uploads: every file verbatim, every line once (spec §7.2)."""

import hashlib
from dataclasses import dataclass

from django.db import transaction

from audit.context import current_trace_id
from audit.events import record
from journal.models import JournalEntry, StatementLine, StatementUpload
from journal.statement.parse import StatementError, parse


@dataclass(frozen=True)
class Stored:
    upload: StatementUpload
    added: int
    already_held: int


def _last_held() -> StatementLine | None:
    return StatementLine.objects.order_by("-posting_date", "-upload__uploaded_at", "-position").first()


@transaction.atomic
def store_upload(raw: bytes, *, filename: str, actor: str) -> Stored:
    """Store a statement, refusing one that does not continue the chain.

    Overlap is expected (months are exported with ragged edges): the rows
    already held must be a prefix of the file and end exactly where the stored
    chain ends. With no overlap, the file's opening balance must equal the
    last stored balance. Anything else is a gap or a rewrite, and is refused.
    """
    digest = hashlib.sha256(raw).hexdigest()
    earlier = StatementUpload.objects.filter(sha256=digest).first()
    if earlier is not None:
        raise StatementError(f"This file was already uploaded on {earlier.uploaded_at:%d %b %Y}.")

    statement = parse(raw)
    held = set(
        StatementLine.objects.filter(
            fingerprint__in=[row.fingerprint for row in statement.rows]
        ).values_list("fingerprint", flat=True)
    )
    new_rows = [row for row in statement.rows if row.fingerprint not in held]
    if not new_rows:
        raise StatementError("Every line in this file is already held.")

    overlap = statement.rows[: statement.rows.index(new_rows[0])]
    if any(row.fingerprint not in held for row in overlap) or any(
        row.fingerprint in held for row in statement.rows[len(overlap):]
    ):
        raise StatementError("The lines already held are not a clean prefix of this file.")

    last = _last_held()
    if last is not None:
        if overlap:
            continues = overlap[-1].fingerprint == last.fingerprint
        else:
            continues = statement.opening == last.net_balance
        if not continues:
            raise StatementError(
                f"This file does not continue from the last statement, which ends on "
                f"{last.posting_date} at {last.net_balance}."
            )

    upload = StatementUpload.objects.create(
        filename=filename[:255],
        sha256=digest,
        raw=raw.decode("utf-8-sig"),
        period_from=statement.rows[0].posting_date if statement.rows else None,
        period_to=statement.rows[-1].posting_date if statement.rows else None,
        opening_balance=statement.opening,
        closing_balance=statement.closing,
        uploaded_by=actor,
        trace_id=current_trace_id(),
    )
    StatementLine.objects.bulk_create(
        [
            StatementLine(
                upload=upload,
                position=row.position,
                fingerprint=row.fingerprint,
                posting_date=row.posting_date,
                particulars=row.particulars[:500],
                cost_center=row.cost_center,
                voucher_type=row.voucher_type,
                debit=row.debit,
                credit=row.credit,
                net_balance=row.net_balance,
            )
            for row in new_rows
        ]
    )
    record(
        "journal.statement_uploaded",
        actor=actor,
        entity=upload,
        after={"filename": filename, "added": len(new_rows), "already_held": len(overlap)},
    )
    return Stored(upload=upload, added=len(new_rows), already_held=len(overlap))


def link(line: StatementLine, entry: JournalEntry) -> None:
    """Match a line to its entry: the one update the table allows."""
    StatementLine.objects.filter(pk=line.pk, matched_entry__isnull=True).update(matched_entry=entry)
    line.matched_entry = entry
```

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal` → all pass.

- [ ] **Step 5: Commit** — `feat(journal): keep statement uploads and refuse a broken chain`

---

### Task 12: Classifying, matching and confirming statement lines

**Files:**
- Create: `src/journal/statement/classify.py`, `src/journal/statement/reconcile.py`, `src/journal/tests/test_statement_reconcile.py`

**Interfaces:**
- Consumes: `store.store_upload`, `store.link`; `cash.contribute`, `cash.withdraw`, `cash.account_charge`; `trading_calendar.sessions.previous_trading_day`.
- Produces:
  - `classify.Proposal(kind: str, component: str = "", reference: str = "", note: str = "")`, where `kind` is one of `contribution`, `withdrawal`, `sweep`, `owner_expense`, `account_charge`, `settlement`, `dp_charge`, `unknown`
  - `classify.propose(line) -> Proposal`
  - `reconcile.auto_match(upload) -> int`, the number of lines linked
  - `reconcile.match_for(line) -> JournalEntry | None`
  - `reconcile.confirm(line, *, kind: str, actor: str, component: str = "") -> JournalEntry`
  - `reconcile.queue()`, a queryset of unmatched lines, oldest first
  - `reconcile.ReconcileError(Exception)`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from journal import accounts, cash
from journal.balances import balance
from journal.models import Book, JournalEntry, StatementLine
from journal.statement.classify import propose
from journal.statement.reconcile import auto_match, confirm, queue
from journal.statement.store import store_upload

FIXTURES = Path(__file__).parent / "fixtures"
AUG = (FIXTURES / "statement_aug.csv").read_bytes()
SEP = (FIXTURES / "statement_sep.csv").read_bytes()


def line(text):
    return StatementLine.objects.get(particulars__startswith=text)


@pytest.mark.django_db
def test_each_known_line_gets_the_proposal_the_spec_table_gives():
    store_upload(AUG, filename="aug.csv", actor="o")
    store_upload(SEP, filename="sep.csv", actor="o")
    assert propose(line("Funds added using UPI from ABC123 with reference number 111111111111")).kind == "contribution"
    assert propose(line("Funds added using UPI from ABC123 with reference number 111111111111")).reference == "111111111111"
    assert propose(line("Net settlement")).kind == "settlement"
    assert propose(line("AMC for Demat")).component == "amc"
    assert propose(line("Delayed payment")).component == "interest"
    assert propose(line("Charges for enabling DDPI")).component == "ddpi"
    assert propose(line("Kite Connect API")).kind == "owner_expense"
    assert propose(line("Payout done for quarterly settlement")).kind == "sweep"


@pytest.mark.django_db
def test_a_recorded_lumpsum_matches_its_upi_line():
    cash.record_lumpsum(Decimal("500"), received_on=date(2026, 8, 14), actor="operator",
                        reference="111111111111")
    before = JournalEntry.objects.count()

    stored = store_upload(AUG, filename="aug.csv", actor="o")
    auto_match(stored.upload)

    upi = line("Funds added using UPI")
    assert upi.matched_entry is not None
    assert upi.matched_entry.kind == "contribution"
    assert JournalEntry.objects.count() == before
    assert balance(accounts.CONTRIBUTIONS, book=Book.LIVE) == Decimal("-500")


@pytest.mark.django_db
def test_an_unrecorded_transfer_waits_in_the_queue_until_confirmed():
    stored = store_upload(AUG, filename="aug.csv", actor="o")
    auto_match(stored.upload)
    upi = line("Funds added using UPI")
    assert upi in list(queue())

    entry = confirm(upi, kind="contribution", actor="operator")
    upi.refresh_from_db()
    assert upi.matched_entry == entry
    assert upi not in list(queue())
    assert balance(accounts.CONTRIBUTIONS, book=Book.LIVE) == Decimal("-500")


@pytest.mark.django_db
def test_confirming_the_same_line_twice_posts_once():
    store_upload(AUG, filename="aug.csv", actor="o")
    amc = line("AMC for Demat")
    first = confirm(amc, kind="account_charge", component="amc", actor="operator")
    second = confirm(amc, kind="account_charge", component="amc", actor="operator")
    assert first.pk == second.pk


@pytest.mark.django_db
def test_the_kite_connect_fee_is_the_owners_cost():
    store_upload(AUG, filename="aug.csv", actor="o")
    store_upload(SEP, filename="sep.csv", actor="o")
    confirm(line("Kite Connect API"), kind="owner_expense", actor="operator")
    assert balance(accounts.WITHDRAWALS, book=Book.LIVE) == Decimal("500.00022")
    assert balance(accounts.ACCOUNT_CHARGES, book=Book.LIVE) == 0


@pytest.mark.django_db
def test_confirming_a_sweep_leaves_carry_owed(live_mode):
    cash.contribute(Decimal("400"), on=date(2026, 9, 1), actor="o")
    cash.allocate(Decimal("300"), book=Book.LIVE, on=date(2026, 9, 1), actor="o",
                  description="t", source_type="t")
    store_upload(AUG, filename="aug.csv", actor="o")
    store_upload(SEP, filename="sep.csv", actor="o")
    confirm(line("Payout done for quarterly settlement"), kind="sweep", actor="operator")
    assert cash.carry_owed() > 0


@pytest.mark.django_db
def test_a_debit_cannot_be_confirmed_as_a_contribution():
    from journal.statement.reconcile import ReconcileError

    store_upload(AUG, filename="aug.csv", actor="o")
    with pytest.raises(ReconcileError):
        confirm(line("AMC for Demat"), kind="contribution", actor="operator")
```

- [ ] **Step 2: Run to verify they fail** — `PYTEST src/journal/tests/test_statement_reconcile.py` → ImportError.

- [ ] **Step 3: Implement** `src/journal/statement/classify.py`:

```python
"""What each statement line probably is (spec §7.2).

A proposal, never a decision: nothing posts until a person confirms it.
Unknown text -- including the wording of an owner-requested payout, which
Zerodha does not document -- is proposed as nothing and classified by hand.
"""

import re
from dataclasses import dataclass

from journal.models import Component


@dataclass(frozen=True)
class Proposal:
    kind: str
    component: str = ""
    reference: str = ""
    note: str = ""


REFERENCE = re.compile(r"reference number[:\s]+(\w+)", re.IGNORECASE)
SETTLEMENT = re.compile(r"^Net settlement for Equity with settlement number:\s*(\d+)", re.IGNORECASE)

RULES = [
    (re.compile(r"^Funds auto-settled to the primary bank account", re.I), Proposal("sweep")),
    (re.compile(r"^Payout done for quarterly settlement", re.I), Proposal("sweep")),
    (re.compile(r"^AMC for Demat Account", re.I), Proposal("account_charge", Component.AMC)),
    (re.compile(r"^Delayed payment charges", re.I), Proposal("account_charge", Component.INTEREST)),
    (re.compile(r"^Charges for enabling DDPI", re.I), Proposal("account_charge", Component.DDPI)),
    (re.compile(r"^DP charges", re.I), Proposal("dp_charge", Component.DP)),
    (re.compile(r"^(Payment gateway|Call and trade) charges", re.I), Proposal("account_charge", Component.OTHER)),
    (re.compile(r"^Kite Connect API Charges", re.I), Proposal("owner_expense", note="the owner's cost of automation (J7)")),
]


def propose(line) -> Proposal:
    text = line.particulars
    reference = (REFERENCE.search(text) or [None, ""])[1]

    settlement = SETTLEMENT.search(text)
    if settlement:
        return Proposal("settlement", reference=settlement.group(1))

    for pattern, proposal in RULES:
        if pattern.search(text):
            return Proposal(proposal.kind, proposal.component, reference, proposal.note)

    if line.voucher_type == "Bank Receipts" and line.credit > 0:
        return Proposal("contribution", reference=reference)
    if line.voucher_type == "Bank Payments" and line.debit > 0:
        return Proposal("withdrawal", reference=reference, note="confirm this payout by hand")
    return Proposal("unknown")
```

`src/journal/statement/reconcile.py`:

```python
"""Matching statement lines to entries, and posting the ones nothing explains."""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from journal import cash
from journal.accounts import ENGINE_CASH, UNALLOCATED
from journal.models import Book, EntryKind, JournalEntry, StatementLine
from journal.statement.classify import propose
from journal.statement.store import link

ZERO = Decimal("0")
DEBIT_KINDS = {"withdrawal", "sweep", "owner_expense", "account_charge"}


class ReconcileError(Exception):
    pass


def queue():
    return StatementLine.objects.filter(matched_entry__isnull=True).order_by("posting_date", "position")


def _unclaimed(entries):
    return entries.exclude(statement_lines__isnull=False)


def _cash_effect(entry: JournalEntry) -> Decimal:
    return sum(
        (l.amount for l in entry.lines.filter(account_id__in=[UNALLOCATED, ENGINE_CASH])), ZERO
    )


def match_for(line: StatementLine) -> JournalEntry | None:
    """An existing entry that is this line, if there is exactly one."""
    from trading_calendar.sessions import previous_trading_day

    proposal = propose(line)
    live = JournalEntry.objects.filter(book=Book.LIVE)

    if proposal.kind == "contribution":
        candidates = _unclaimed(live.filter(kind=EntryKind.CONTRIBUTION))
        if proposal.reference:
            by_reference = candidates.filter(source_id=proposal.reference)
            if by_reference.count() == 1:
                return by_reference.first()
        nearby = [
            entry
            for entry in candidates.filter(
                occurred_on__range=(line.posting_date - timedelta(days=3), line.posting_date + timedelta(days=3))
            )
            if _cash_effect(entry) == line.credit
        ]
        return nearby[0] if len(nearby) == 1 else None

    if proposal.kind == "settlement":
        for day in (line.posting_date, previous_trading_day(line.posting_date)):
            if day is None:
                continue
            entry = live.filter(kind=EntryKind.SETTLEMENT, occurred_on=day).order_by("-number").first()
            if entry is not None:
                return entry
    return None


@transaction.atomic
def auto_match(upload) -> int:
    """Link every line of *upload* that an existing entry already explains.

    A settlement is linked whether or not the amounts agree: the difference is
    the charges check (Task 13), not a reason to leave the line unexplained.
    """
    linked = 0
    for line in upload.lines.filter(matched_entry__isnull=True):
        entry = match_for(line)
        if entry is not None:
            link(line, entry)
            linked += 1
    return linked


@transaction.atomic
def confirm(line: StatementLine, *, kind: str, actor: str, component: str = "") -> JournalEntry:
    """Post the entry a person says this line is, and link it."""
    line.refresh_from_db()
    if line.matched_entry_id is not None:
        return line.matched_entry

    key = f"statement:{line.fingerprint}"
    common = {"on": line.posting_date, "actor": actor, "idempotency_key": key}

    if kind == "contribution":
        if line.credit <= ZERO:
            raise ReconcileError("Only money arriving can be a contribution.")
        entry = cash.contribute(
            line.credit, reference=propose(line).reference, source_type="statement",
            source_id=line.fingerprint, **common,
        )
    elif kind in DEBIT_KINDS:
        if line.debit <= ZERO:
            raise ReconcileError(f"A {kind.replace('_', ' ')} is money leaving.")
        if kind == "account_charge":
            entry = cash.account_charge(
                line.debit, component=component or propose(line).component or "other",
                description=line.particulars, source_id=line.fingerprint, **common,
            )
        else:
            entry = cash.withdraw(
                line.debit,
                reason=line.particulars,
                sweep=kind == "sweep",
                source_type="owner_expense" if kind == "owner_expense" else "withdrawal",
                source_id=line.fingerprint,
                **common,
            )
    else:
        raise ReconcileError(f"{kind!r} lines are matched, not confirmed.")

    link(line, entry)
    return entry
```

`cash.contribute`, `cash.withdraw` and `cash.account_charge` already take `idempotency_key`. `confirm` passes `on`, `actor` and `idempotency_key` through `common`. `cash.contribute` takes `reference`, `source_type` and `source_id` too. Check that the keyword names match Task 5 exactly.

A `dp_charge` line is matched in Task 13, not confirmed. A settlement with no engine entry for its date, such as a direct trade recorded late, stays in the queue until the trade is recorded.

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal` → all pass.

- [ ] **Step 5: Commit** — `feat(journal): match statement lines and confirm the rest`

---

### Task 13: A settlement that disagrees corrects the charges

**Files:**
- Modify: `src/journal/statement/reconcile.py`, `src/journal/trades.py` (`post_charge_correction` gains a `key` parameter), `src/portfolio/contract_note.py` (`finalise` passes through a key), `src/journal/tests/test_statement_reconcile.py`

**Interfaces:**
- Consumes: `contract_note.finalise`, `FinalisationRefused`; `trades.post_charge_correction`.
- Produces:
  - `reconcile.settlement_difference(line) -> Decimal`, Zerodha's figure less ours, where negative means Zerodha took more
  - `reconcile.correct_settlement(line, *, actor) -> list[JournalEntry]`
  - `post_charge_correction(..., key: str | None = None)`, which defaults to `charge_correction:{lot.id}`

The rule (spec §7.3): Zerodha's net-settlement line is compared with our settlement entries for that date. Zerodha debits the DP charge as its own line rather than inside the settlement, while our sell entries include it, so our DP components for the date are added back before comparing. A difference is split across the date's live fills in proportion to gross turnover, with the last fill taking the rounding remainder. For a buy fill it goes to its lot through `contract_note.finalise`, so the lot is superseded at its final basis. If the lot is already final, it goes to `post_charge_correction` with key `settlement_correction:{fingerprint}:{lot.id}`. For a sell fill it is posted to 4100 against 1100. The DP line (`dp_charge`) is linked to that date's first sell entry carrying a DP component.

- [ ] **Step 1: Write the failing tests** (append to `test_statement_reconcile.py`). They need a live buy fill on 2026-08-14. Add a `live_buy_fill` fixture to `conftest.py`, identical to `paper_buy_fill` but with `mode=OrderMode.LIVE`, `DAY=date(2026, 8, 14)`, and booked via `orders.booking.book` then `settle_trades`:

```python
@pytest.mark.django_db
def test_a_settlement_that_agrees_posts_nothing(live_buy_fill):
    fill = live_buy_fill(quantity=1, price="240")  # our settlement is -(240 + charges)
    ours = -balance(accounts.ENGINE_CASH, book=Book.LIVE, kinds=["settlement"])
    raw = csv_of("0", [("Net settlement for Equity with settlement number: 2026153", "2026-08-14", str(ours), "0")])
    stored = store_upload(raw, filename="s.csv", actor="o")
    auto_match(stored.upload)
    line = StatementLine.objects.get()
    assert settlement_difference(line) == 0
    assert correct_settlement(line, actor="o") == []


@pytest.mark.django_db
def test_a_settlement_that_took_more_raises_the_lot_cost_and_finalises_it(live_buy_fill):
    fill = live_buy_fill(quantity=1, price="240")
    ours = -balance(accounts.ENGINE_CASH, book=Book.LIVE, kinds=["settlement"])
    theirs = ours + Decimal("0.21")
    raw = csv_of("0", [("Net settlement for Equity with settlement number: 2026153", "2026-08-14", str(theirs), "0")])
    stored = store_upload(raw, filename="s.csv", actor="o")
    auto_match(stored.upload)
    line = StatementLine.objects.get()

    assert settlement_difference(line) == Decimal("-0.21")
    correct_settlement(line, actor="o")

    from portfolio.models import BasisState, Lot
    final = Lot.objects.get(superseded_by__isnull=True)
    assert final.basis_state == BasisState.FINAL
    assert balance(accounts.ENGINE_CASH, book=Book.LIVE) == -theirs
    assert correct_settlement(line, actor="o") == []  # idempotent
```

Import `csv_of` from `journal.tests.conftest` (Task 11).

- [ ] **Step 2: Run to verify they fail** — ImportError on `settlement_difference`.

- [ ] **Step 3: Implement.** In `trades.post_charge_correction`, add `key: str | None = None` and use `idempotency_key=key or f"charge_correction:{lot.id}"`. In `contract_note.finalise`, add `correction_key: str | None = None` and pass it as `key=`.

Append to `reconcile.py`:

```python
from journal.accounts import REALISED
from journal.models import Component, JournalLine
from journal.posting import Line, post, q6


def _date_entries(line):
    entry = line.matched_entry
    return JournalEntry.objects.filter(
        book=Book.LIVE, kind=EntryKind.SETTLEMENT, occurred_on=entry.occurred_on
    ), entry.occurred_on


def _dp_on(day) -> Decimal:
    return q6(
        sum(
            JournalLine.objects.filter(
                entry__book=Book.LIVE, entry__kind=EntryKind.SELL_FILL,
                entry__occurred_on=day, component=Component.DP,
            ).values_list("amount", flat=True),
            ZERO,
        )
    )


def settlement_difference(line: StatementLine) -> Decimal:
    """Zerodha's settlement less ours, counting corrections already made for it."""
    if line.matched_entry is None or line.matched_entry.kind != EntryKind.SETTLEMENT:
        raise ReconcileError("Only a matched settlement line has a difference.")
    entries, day = _date_entries(line)
    corrections = JournalEntry.objects.filter(
        book=Book.LIVE, idempotency_key__startswith=f"settlement_correction:{line.fingerprint}:"
    )
    ours = q6(sum((_cash_effect(entry) for entry in [*entries, *corrections]), ZERO))
    return q6(line.amount - (ours + _dp_on(day)))


@transaction.atomic
def correct_settlement(line: StatementLine, *, actor: str) -> list[JournalEntry]:
    """Spread a settlement difference across the date's fills (spec §7.3)."""
    from orders.models import Fill
    from portfolio.contract_note import FinalisationRefused, finalise
    from journal.trades import post_charge_correction

    difference = settlement_difference(line)
    if difference == ZERO:
        return []
    extra = -difference  # positive: Zerodha charged more than we estimated
    _entries, day = _date_entries(line)

    fill_entries = list(
        JournalEntry.objects.filter(
            book=Book.LIVE, kind__in=[EntryKind.BUY_FILL, EntryKind.SELL_FILL], occurred_on=day
        ).order_by("number")
    )
    if not fill_entries:
        raise ReconcileError(f"No fills on {day} to carry a difference of {difference}.")

    fills = {str(f.id): f for f in Fill.objects.filter(pk__in=[e.source_id for e in fill_entries])}
    turnover = {e.source_id: q6(fills[e.source_id].price * fills[e.source_id].quantity) for e in fill_entries}
    whole = sum(turnover.values(), ZERO)

    posted, remaining = [], extra
    for index, entry in enumerate(fill_entries):
        share = remaining if index == len(fill_entries) - 1 else q6(extra * turnover[entry.source_id] / whole)
        remaining -= share
        if share == ZERO:
            continue
        fill = fills[entry.source_id]
        reference = f"settlement {line.fingerprint[:12]}"
        if entry.kind == EntryKind.BUY_FILL:
            lot = fill.lot  # Lot.created_from_fill's related_name
            while lot.superseded_by_id is not None:
                lot = lot.superseded_by
            try:
                result = finalise(
                    lot, actual_charges=_estimated_charges(lot) + share, actor=actor,
                    note_reference=reference,
                    correction_key=f"settlement_correction:{line.fingerprint}:{lot.id}",
                )
                posted.append(result)
            except FinalisationRefused:
                posted.append(post_charge_correction(
                    lot, share, actor=actor, reference=reference, on=day,
                    key=f"settlement_correction:{line.fingerprint}:{lot.id}",
                ))
        else:
            posted.append(post(
                book=Book.LIVE, kind=EntryKind.CHARGE_CORRECTION, occurred_on=day,
                description=f"sale charges settled against {reference}",
                lines=[Line(REALISED, share, fill.order.instrument_id, Component.OTHER),
                       Line(UNALLOCATED, -share)],
                actor=actor, source_type="fill", source_id=str(fill.id),
                idempotency_key=f"settlement_correction:{line.fingerprint}:{fill.id}",
            ))
    return [p for p in posted if p is not None]
```

Before running, settle these against the code:
- `_estimated_charges(lot)`: the lot's current charges, `lot.cost_per_unit * lot.quantity - contract_note._gross_of(lot)`, exactly as `finalise` computes `estimated_charges`. Import `_gross_of`, or move it to a public name `gross_of` in `contract_note.py` and use that.
- `finalise` returns a `Finalisation`, not an entry. Collect `JournalEntry.objects.get(idempotency_key=...)` instead, so `posted` holds entries. It is `None` when the difference rounds to zero.
- Idempotency: calling `correct_settlement` a second time must post nothing. Every correction it posts is keyed `settlement_correction:{fingerprint}:...`, and `settlement_difference` counts those, so after the first run the difference is zero and the second returns `[]`. That is what the test pins.

Corrections are never automatic. `auto_match` links the settlement line; the statement screen (Task 15) shows the difference beside it with a **Correct charges** button that calls `correct_settlement`.

For DP lines, `match_for` links a `dp_charge` line to the first `SELL_FILL` entry, on the same date or the previous trading day, that has a DP component of the same amount. Add this branch to `match_for`:

```python
    if proposal.kind == "dp_charge":
        for day in (line.posting_date, previous_trading_day(line.posting_date)):
            if day is None:
                continue
            for entry in live.filter(kind=EntryKind.SELL_FILL, occurred_on=day).order_by("number"):
                if q6(sum(l.amount for l in entry.lines.filter(component=Component.DP))) == line.debit:
                    return entry
```

Add a test that a DP line matches a live sell on the same date.

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal src/portfolio src/records` → all pass.

- [ ] **Step 5: Commit** — `feat(journal): correct charges from a settlement that disagrees`

---

### Task 14: The books a CA reads: journal, general ledger, trial balance

**Files:**
- Modify: `src/journal/balances.py` (add `TrialRow`, `trial_balance`, `LedgerRow`, `general_ledger`), `src/config/urls.py`
- Create: `src/journal/urls.py`, `src/journal/views.py`, `src/journal/forms.py`, `src/templates/journal/index.html`, `src/templates/journal/journal.html`, `src/templates/journal/ledger.html`, `src/templates/journal/entry.html`, `src/journal/tests/test_reports.py`, `src/journal/tests/test_views.py`
- Modify: `src/templates/app.html` (a "Books" link in the navigation, next to the existing records link)

**Interfaces:**
- Consumes: `posting.reverse`, `periods.close_year`, `periods.fy_end_for`.
- Produces:
  - `@dataclass(frozen=True) balances.TrialRow(code: str, name: str, type: str, debit: Decimal, credit: Decimal)`
  - `balances.trial_balance(*, book: str, through: date) -> list[TrialRow]`: each account's closing balance on the side it falls; the debit total equals the credit total
  - `@dataclass(frozen=True) balances.LedgerRow(entry: JournalEntry, amount: Decimal, running: Decimal, instrument_id: str | None, component: str)`
  - `balances.general_ledger(code: str, *, book: str, start: date, end: date) -> tuple[Decimal, list[LedgerRow]]`, returning the opening balance before `start` and the rows
  - URL names `journal:index`, `journal:entries`, `journal:ledger` (`<code>`), `journal:entry` (`<uuid:pk>`), `journal:reverse` (POST), `journal:close_year` (POST). Every list view accepts `?book=live|paper&from=YYYY-MM-DD&to=YYYY-MM-DD&format=csv`.

- [ ] **Step 1: Write the failing tests**

`src/journal/tests/test_reports.py`:

```python
from datetime import date
from decimal import Decimal

import pytest

from journal import accounts, cash
from journal.balances import general_ledger, trial_balance
from journal.models import Book

MON, TUE = date(2026, 9, 21), date(2026, 9, 22)


@pytest.mark.django_db
def test_the_trial_balance_balances():
    cash.contribute(Decimal("5000"), on=MON, actor="o")
    cash.allocate_daily(MON, book=Book.LIVE)
    cash.account_charge(Decimal("88.5"), on=TUE, component="amc", actor="o", description="AMC")

    rows = trial_balance(book=Book.LIVE, through=TUE)
    assert sum(r.debit for r in rows) == sum(r.credit for r in rows)
    by = {r.code: r for r in rows}
    assert by[accounts.CONTRIBUTIONS].credit == Decimal("5000")
    assert by[accounts.ENGINE_CASH].debit == Decimal("1000")
    assert by[accounts.ACCOUNT_CHARGES].debit == Decimal("88.5")


@pytest.mark.django_db
def test_the_general_ledger_runs_from_an_opening_balance():
    cash.contribute(Decimal("5000"), on=MON, actor="o")
    cash.contribute(Decimal("1000"), on=TUE, actor="o")

    opening, rows = general_ledger(accounts.UNALLOCATED, book=Book.LIVE, start=TUE, end=TUE)
    assert opening == Decimal("5000")
    assert [r.running for r in rows] == [Decimal("6000")]
```

`src/journal/tests/test_views.py` uses the project's `signed_in` fixture. Copy it into `src/journal/tests/conftest.py` from `src/engine/tests/conftest.py`: it is small, and the conftests are per-app in this project.

```python
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from journal import cash
from journal.models import EntryKind, JournalEntry


@pytest.mark.django_db
def test_the_books_page_shows_the_trial_balance(signed_in):
    cash.contribute(Decimal("5000"), on=date(2026, 9, 21), actor="o")
    response = signed_in.get(reverse("journal:index") + "?book=live")
    assert response.status_code == 200
    assert b"Contributions" in response.content


@pytest.mark.django_db
def test_the_journal_exports_csv(signed_in):
    cash.contribute(Decimal("5000"), on=date(2026, 9, 21), actor="o")
    response = signed_in.get(reverse("journal:entries") + "?book=live&format=csv")
    assert response["Content-Type"].startswith("text/csv")
    header = response.content.decode().splitlines()[0]
    assert header == "number,date,kind,description,account,account_name,instrument,component,debit,credit,trace_id"


@pytest.mark.django_db
def test_reversing_an_entry_needs_a_reason(signed_in):
    entry = cash.contribute(Decimal("5000"), on=date(2026, 9, 21), actor="o")
    url = reverse("journal:reverse", args=[entry.pk])
    signed_in.post(url, {"reason": ""})
    assert not JournalEntry.objects.filter(kind=EntryKind.REVERSAL).exists()
    signed_in.post(url, {"reason": "recorded twice"})
    assert JournalEntry.objects.filter(kind=EntryKind.REVERSAL, reverses=entry).exists()


@pytest.mark.django_db
def test_the_books_need_a_signed_in_operator(client):
    response = client.get(reverse("journal:index"))
    assert response.status_code in (302, 403)
```

- [ ] **Step 2: Run to verify they fail** — `PYTEST src/journal/tests/test_reports.py src/journal/tests/test_views.py` → ImportError / NoReverseMatch.

- [ ] **Step 3: Implement.** Append to `src/journal/balances.py`:

```python
from dataclasses import dataclass

from journal.models import Account, JournalEntry


@dataclass(frozen=True)
class TrialRow:
    code: str
    name: str
    type: str
    debit: Decimal
    credit: Decimal


def trial_balance(*, book: str, through: date) -> list[TrialRow]:
    """Every account's balance, debit or credit, as at *through*."""
    rows = []
    for account in Account.objects.order_by("code"):
        amount = balance(account.code, book=book, through=through)
        if amount == ZERO:
            continue
        rows.append(
            TrialRow(
                code=account.code,
                name=account.name,
                type=account.type,
                debit=amount if amount > ZERO else ZERO,
                credit=-amount if amount < ZERO else ZERO,
            )
        )
    return rows


@dataclass(frozen=True)
class LedgerRow:
    entry: JournalEntry
    amount: Decimal
    running: Decimal
    instrument_id: str | None
    component: str


def general_ledger(code: str, *, book: str, start: date, end: date) -> tuple[Decimal, list[LedgerRow]]:
    """One account's lines in a range, with the balance brought forward."""
    from datetime import timedelta

    opening = balance(code, book=book, through=start - timedelta(days=1))
    running = opening
    rows = []
    lines = (
        JournalLine.objects.filter(
            entry__book=book, account_id=code, entry__occurred_on__range=(start, end)
        )
        .select_related("entry")
        .order_by("entry__occurred_on", "entry__number", "id")
    )
    for line in lines:
        running = q6(running + line.amount)
        rows.append(LedgerRow(line.entry, line.amount, running, line.instrument_id, line.component))
    return opening, rows
```

Move the new imports to the module top. `src/journal/forms.py`:

```python
from django import forms


class ReverseForm(forms.Form):
    reason = forms.CharField(max_length=200)


class CloseYearForm(forms.Form):
    book = forms.ChoiceField(choices=[("live", "Live"), ("paper", "Paper")])
    fy_end = forms.DateField()
```

`src/journal/views.py` follows `records/views.py`: function views with `require_http_methods`, `messages` for outcomes, `render`, and a local `_csv(name, header, rows)` like `web/views.py` has (import and reuse `web.views._csv` if it is importable without a cycle; otherwise copy its six lines). Views:

- `index` (GET): `book` from the query (default `configuration.store.current().order_mode`), `through` from `?to=` (default today). Renders `journal/index.html` with `trial_balance(...)`, the debit and credit totals, the closed years (`PeriodClose.objects.filter(book=book)`), and links to each account's ledger. CSV columns: `code,name,type,debit,credit`.
- `entries` (GET): the entries in range for the book, newest first, paginated 100 per page with `django.core.paginator.Paginator`. CSV has one row per **line** with the header asserted in the test, `debit` and `credit` split from the signed amount, and `account_name` from `line.account.name`.
- `ledger(code)` (GET): `general_ledger(...)`. CSV columns: `date,number,kind,description,instrument,component,debit,credit,running`.
- `entry(pk)` (GET): the entry, its lines, what it reverses or is reversed by, the statement lines matched to it, and the reverse form.
- `reverse_entry(pk)` (POST): validates `ReverseForm`, calls `posting.reverse(entry, reason=..., actor=request.user.get_username())`, puts a `JournalError` into `messages.error`, and redirects to the entry.
- `close(request)` (POST): validates `CloseYearForm`, calls `periods.close_year`, puts a `JournalPeriodError` into `messages.error`, and redirects to the index.

`src/journal/urls.py`:

```python
from django.urls import path

from journal import views

app_name = "journal"

urlpatterns = [
    path("", views.index, name="index"),
    path("journal/", views.entries, name="entries"),
    path("ledger/<str:code>/", views.ledger, name="ledger"),
    path("entry/<uuid:pk>/", views.entry, name="entry"),
    path("entry/<uuid:pk>/reverse/", views.reverse_entry, name="reverse"),
    path("close/", views.close, name="close_year"),
]
```

In `src/config/urls.py`, add `path("books/", include("journal.urls", namespace="journal")),` before the `records/` line.

Templates extend `app.html` and mirror `src/templates/records/basis.html`: a `<p class="text-ink-2 max-w-[48rem]">` explanation, then a plain `<table>` (base styles make it read as a table), money through `|floatformat:2`, dates as `|date:"j M Y"`, and a CSV link `?{{ request.GET.urlencode }}&format=csv`. The index template's explanation reads: "Zerodha's cash is 1100 + 1110 + 1200 + 1210. The engine may spend 1110 + 1200. Every figure here is a sum of journal lines; nothing is typed in." Add a "Books" link to the navigation in `app.html` pointing at `{% url 'journal:index' %}`. Run `npm run build`.

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal` → all pass. Then run the server and look at `/books/`, `/books/journal/`, `/books/ledger/1110/` and one entry page in a browser, in both books.

- [ ] **Step 5: Commit** — `feat(journal): show the journal, ledgers and trial balance`

---

### Task 15: The statement screen, lumpsums, dividends and external buys

**Files:**
- Modify: `src/journal/views.py`, `src/journal/urls.py`, `src/journal/forms.py`
- Create: `src/templates/journal/statement.html`, `src/templates/journal/dividend.html`, `src/journal/tests/test_statement_views.py`
- Modify: the external-transaction template (from Task 9) to render `paid_from`

**Interfaces:**
- Consumes: `store.store_upload`, `reconcile.{auto_match, queue, confirm, settlement_difference, correct_settlement}`, `classify.propose`, `external.record_dividend`.
- Produces: URL names `journal:statement` (GET, POST upload), `journal:confirm` (`<int:pk>`, POST), `journal:correct` (`<int:pk>`, POST), `journal:dividend` (GET, POST).

- [ ] **Step 1: Write the failing tests** — `src/journal/tests/test_statement_views.py`:

```python
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from journal.models import StatementLine

AUG = (Path(__file__).parent / "fixtures" / "statement_aug.csv").read_bytes()


@pytest.mark.django_db
def test_uploading_a_statement_stores_it_and_lists_the_queue(signed_in):
    response = signed_in.post(
        reverse("journal:statement"),
        {"file": SimpleUploadedFile("aug.csv", AUG, content_type="text/csv")},
        follow=True,
    )
    assert StatementLine.objects.count() == 4
    assert b"Funds added using UPI" in response.content


@pytest.mark.django_db
def test_a_refused_upload_says_why(signed_in):
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})
    response = signed_in.post(
        reverse("journal:statement"), {"file": SimpleUploadedFile("b.csv", AUG)}, follow=True
    )
    assert b"already uploaded" in response.content


@pytest.mark.django_db
def test_confirming_a_line_from_the_queue_posts_it(signed_in):
    signed_in.post(reverse("journal:statement"), {"file": SimpleUploadedFile("a.csv", AUG)})
    amc = StatementLine.objects.get(particulars__startswith="AMC")
    signed_in.post(reverse("journal:confirm", args=[amc.pk]), {"kind": "account_charge", "component": "amc"})
    amc.refresh_from_db()
    assert amc.matched_entry is not None


@pytest.mark.django_db
def test_recording_a_dividend(signed_in):
    from portfolio.models import Instrument

    Instrument.objects.create(tradingsymbol="INFY", instrument_token=408065, name="Infosys")
    signed_in.post(
        reverse("journal:dividend"),
        {"instrument": "INFY", "amount": "42", "paid_on": "2026-09-22", "reference": "interim"},
    )
    from journal.models import EntryKind, JournalEntry

    assert JournalEntry.objects.filter(kind=EntryKind.DIVIDEND).count() == 1
```

- [ ] **Step 2: Run to verify they fail** — NoReverseMatch.

- [ ] **Step 3: Implement.** Forms:

```python
class StatementUploadForm(forms.Form):
    file = forms.FileField()


class ConfirmLineForm(forms.Form):
    kind = forms.ChoiceField(
        choices=[
            ("contribution", "Money in from the owner"),
            ("withdrawal", "Money out to the owner"),
            ("sweep", "Quarterly settlement (carry owed)"),
            ("owner_expense", "The owner's cost (Kite Connect)"),
            ("account_charge", "An account charge (the portfolio's cost)"),
        ]
    )
    component = forms.ChoiceField(
        required=False,
        choices=[("", "—"), ("amc", "AMC"), ("ddpi", "DDPI"), ("interest", "Interest"),
                 ("dp", "DP charge"), ("other", "Other")],
    )


class DividendForm(forms.Form):
    instrument = forms.ModelChoiceField(queryset=None, to_field_name="tradingsymbol")
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01)
    paid_on = forms.DateField()
    reference = forms.CharField(max_length=100, required=False)

    def __init__(self, *args, **kwargs):
        from portfolio.models import Instrument

        super().__init__(*args, **kwargs)
        self.fields["instrument"].queryset = Instrument.objects.order_by("tradingsymbol")
```

Views:
- `statement` (GET/POST). POST: `store_upload(request.FILES["file"].read(), filename=..., actor=...)`, then `auto_match(stored.upload)`. `messages.success(f"{added} new lines, {linked} matched to entries already in the books")`; a `StatementError` goes into `messages.error`. Redirect to GET. GET renders: the uploads (newest first), then the **queue** (`reconcile.queue()`), each line with `propose(line)` preselected in a `ConfirmLineForm`, then **settlement lines** matched but with a non-zero `settlement_difference(line)`, each with a "Correct charges" button. The page's explanation: "Every line of Zerodha's statement is either matched to an entry already in the books or waits here. Nothing posts until you confirm it. A settlement that differs from ours is the broker's rounding on the day's charges; correcting it spreads the difference across that day's fills and settles their lots."
- `confirm_line(pk)` (POST): `reconcile.confirm(line, kind=..., component=..., actor=...)`; a `ReconcileError` goes into `messages.error`. Redirect to `journal:statement`.
- `correct(pk)` (POST): `reconcile.correct_settlement(line, actor=...)`, with a message naming how many entries were posted.
- `dividend` (GET/POST): `DividendForm` → `record_dividend(form.cleaned_data["instrument"].tradingsymbol, ...)`, followed by `messages.success("Recorded as the portfolio's income, paid to your bank.")`.

Add the URL entries: `path("statement/", views.statement, name="statement")`, `path("statement/<int:pk>/confirm/", views.confirm_line, name="confirm")`, `path("statement/<int:pk>/correct/", views.correct, name="correct")`, `path("dividend/", views.dividend, name="dividend")`. Link the statement and dividend pages from `journal/index.html`. Run `npm run build`.

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal` → all pass. Then upload the synthetic `statement_aug.csv` through the browser and confirm one line.

- [ ] **Step 5: Commit** — `feat(journal): upload statements, confirm lines and record dividends`

---

### Task 16: Checking the balance against Zerodha every morning, and warning when it is short

**Files:**
- Modify: `src/journal/models.py` (add `BalanceCheck`)
- Create: `src/journal/migrations/0006_balancecheck.py` (generated), `src/journal/migrations/0007_balancecheck_append_only.py`, `src/journal/checks.py`, `src/journal/management/commands/check_balance.py`, `src/journal/tests/test_checks.py`, `infra/systemd/balance-check.service`, `infra/systemd/balance-check.timer`
- Modify: `src/web/deck.py` (Today's notices), `src/templates/journal/index.html` (latest checks), `OPERATIONS.md` (the new timer and what its alarm means)

**Interfaces:**
- Consumes: `balances.zerodha_cash`, `cash.carry_owed`, `broker.base.Broker.margins()`.
- Produces:
  - `BalanceCheck(id, checked_on: date unique, ours: Decimal, theirs: Decimal, difference: Decimal, trace_id, created_at)`, append-only
  - `checks.TOLERANCE = Decimal("1.00")`
  - `checks.zerodha_opening_balance(margins: dict) -> Decimal`
  - `checks.check_balance(*, broker, on: date) -> BalanceCheck`
  - `checks.funding_warnings() -> list[str]`
  - `checks.investments_disagree(*, book: str) -> dict[str, tuple[Decimal, Decimal]]`, keyed by symbol, as (journal, lots)
  - `manage.py check_balance`

The comparison (spec §7.1). Kite's `margins()["equity"]["available"]["opening_balance"]` is, as far as is known, the funds ledger balance at the start of the day. It is compared with our live `zerodha_cash(through=<previous calendar day>)`. **Verify the field against a stored response before relying on it:** `BrokerCall.objects.filter(endpoint__contains="margins").latest(...)`. Look at `response` and confirm that `equity.available.opening_balance` exists and equals the Console statement's balance for that morning. If the field that matches is a different one, use it and say so in the module docstring.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date
from decimal import Decimal

import pytest

from journal import accounts, cash
from journal.checks import TOLERANCE, check_balance, funding_warnings, investments_disagree
from journal.models import BalanceCheck


class FakeBroker:
    def __init__(self, opening):
        self.opening = opening

    def margins(self):
        return {"equity": {"available": {"opening_balance": float(self.opening)}}}


@pytest.mark.django_db
def test_a_balance_that_agrees_is_recorded_quietly(caplog):
    cash.contribute(Decimal("500"), on=date(2026, 9, 21), actor="o")
    row = check_balance(broker=FakeBroker(Decimal("500")), on=date(2026, 9, 22))
    assert row.difference == 0
    assert "disagrees" not in caplog.text


@pytest.mark.django_db
def test_a_balance_that_disagrees_beyond_a_rupee_alerts(caplog):
    cash.contribute(Decimal("500"), on=date(2026, 9, 21), actor="o")
    row = check_balance(broker=FakeBroker(Decimal("411.50")), on=date(2026, 9, 22))
    assert row.difference == Decimal("-88.50")
    assert "zerodha balance disagrees" in caplog.text


@pytest.mark.django_db
def test_a_morning_is_checked_once():
    check_balance(broker=FakeBroker(Decimal("0")), on=date(2026, 9, 22))
    check_balance(broker=FakeBroker(Decimal("5")), on=date(2026, 9, 22))
    assert BalanceCheck.objects.count() == 1


@pytest.mark.django_db
def test_unfunded_allocations_and_carry_owed_are_warned_about():
    cash.allocate_daily(date(2026, 9, 22), book="live")
    assert any("not transferred" in w for w in funding_warnings())

    cash.contribute(Decimal("3000"), on=date(2026, 9, 21), actor="o")
    cash.withdraw(Decimal("3000"), on=date(2026, 9, 23), actor="s", reason="sweep", sweep=True)
    assert any("quarterly settlement" in w for w in funding_warnings())


@pytest.mark.django_db
def test_investments_agree_with_the_lots_after_a_booked_fill(paper_buy_fill):
    from orders.booking import book

    book(paper_buy_fill(quantity=1, price="100"))
    assert investments_disagree(book="paper") == {}
```

`investments_disagree` compares each symbol's 1300 balance in the book with `sum(lot.cost_per_unit * lot.remaining_quantity)` over that book's open, unsuperseded lots. For paper, those are the lots whose `created_from_fill__order__mode="paper"`; for live, `journal.external.live_open_lots()`.

- [ ] **Step 2: Run to verify they fail** — ImportError.

- [ ] **Step 3: Implement.** `BalanceCheck` in `models.py`:

```python
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
```

Run `makemigrations journal`, then write `0007` using the same `_append_only` SQL as `0003` for `journal_balancecheck`.

`src/journal/checks.py`:

```python
"""Checks that need no person: Zerodha's balance, and whether the engine is funded.

A difference is recorded and alarmed, never balanced away (spec §7). The
tolerance is a rupee because the rate card and Zerodha round a day's charges
differently until the statement's settlement line corrects them.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from audit.context import current_trace_id
from audit.events import record
from audit.logging import fields
from journal import cash
from journal.accounts import INVESTMENTS, UNALLOCATED
from journal.balances import balance, zerodha_cash
from journal.models import BalanceCheck, Book
from journal.posting import q6

logger = logging.getLogger("steadystack.journal")

TOLERANCE = Decimal("1.00")
ZERO = Decimal("0")


def zerodha_opening_balance(margins: dict) -> Decimal:
    return q6(Decimal(str(margins["equity"]["available"]["opening_balance"])))


def check_balance(*, broker, on: date) -> BalanceCheck:
    existing = BalanceCheck.objects.filter(checked_on=on).first()
    if existing is not None:
        return existing

    theirs = zerodha_opening_balance(broker.margins())
    ours = zerodha_cash(Book.LIVE, through=on - timedelta(days=1))
    row = BalanceCheck.objects.create(
        checked_on=on, ours=ours, theirs=theirs, difference=q6(theirs - ours),
        trace_id=current_trace_id(),
    )
    record("journal.balance_checked", entity=row, after={"ours": str(ours), "theirs": str(theirs)})
    if abs(row.difference) > TOLERANCE:
        # The line an alarm is written against; see OPERATIONS.md.
        logger.error(
            "zerodha balance disagrees with the books",
            extra=fields(checked_on=str(on), ours=str(ours), theirs=str(theirs), difference=str(row.difference)),
        )
    return row


def funding_warnings() -> list[str]:
    warnings = []
    unallocated = balance(UNALLOCATED, book=Book.LIVE)
    if unallocated < ZERO:
        warnings.append(
            f"₹{-unallocated:.2f} has been allocated to the engine but not transferred to Zerodha."
        )
    owed = cash.carry_owed()
    if owed > ZERO:
        warnings.append(
            f"The quarterly settlement returned ₹{owed:.2f} of carry to your bank. "
            "Transfer it back before the next session."
        )
    return warnings


def investments_disagree(*, book: str) -> dict[str, tuple[Decimal, Decimal]]:
    from journal.external import live_open_lots
    from portfolio.models import Lot

    if book == Book.PAPER:
        lots = Lot.objects.filter(
            superseded_by__isnull=True, remaining_quantity__gt=0,
            created_from_fill__order__mode=Book.PAPER,
        )
    else:
        lots = live_open_lots()

    at_cost: dict[str, Decimal] = {}
    for lot in lots:
        at_cost[lot.instrument_id] = at_cost.get(lot.instrument_id, ZERO) + lot.cost_per_unit * lot.remaining_quantity

    shares: dict[str, int] = {}
    for lot in lots:
        shares[lot.instrument_id] = shares.get(lot.instrument_id, 0) + lot.remaining_quantity

    journalled = set(
        JournalLine.objects.filter(entry__book=book, account_id=INVESTMENTS).values_list(
            "instrument_id", flat=True
        )
    )
    out = {}
    for symbol in (set(at_cost) | journalled) - {None}:
        journal = balance(INVESTMENTS, book=book, instrument_id=symbol)
        lots_cost = q6(at_cost.get(symbol, ZERO))
        # A lot's cost per unit is stored to four decimals, so the lots can
        # sit a hundredth of a paisa per share away from what was paid.
        tolerance = Decimal("0.01") + Decimal("0.0001") * shares.get(symbol, 0)
        if abs(journal - lots_cost) > tolerance:
            out[symbol] = (journal, lots_cost)
    return out
```

Add `JournalLine` to the `journal.models` import at the top of `checks.py`.

`src/journal/management/commands/check_balance.py`: gets the broker the way `broker/management/commands/check_kite_session.py` does, calls `check_balance(broker=..., on=today_ist())`, and prints ours, theirs and the difference. With no session it prints why and exits 0: the session check already alarms on that.

`infra/systemd/balance-check.service`: a copy of `session-check.service` with `Description=steadystack: check Zerodha's funds balance against the books` and `ExecStart=... manage.py check_balance`, logging to `/var/log/steadystack/books.log`. `balance-check.timer`: `OnCalendar=Mon..Fri *-*-* 09:05 Asia/Kolkata`, after the session check. Add both to wherever the timers are enabled (`grep -rn "session-check" infra`).

`src/web/deck.py`: where Today builds its notices (the function that produced the overhang notice in Task 8), add each `funding_warnings()` string as a notice. Add the last five `BalanceCheck` rows to `journal/index.html`.

In `OPERATIONS.md`, add a section on the balance-check timer. It covers what "zerodha balance disagrees with the books" means, and the first thing to do: upload the latest Console statement and look at the queue.

- [ ] **Step 4: Run to verify they pass** — `PYTEST src/journal src/web` → all pass.

- [ ] **Step 5: Commit** — `feat(journal): check Zerodha's balance each morning and warn when unfunded` and `feat(infra): time the morning balance check`.

---

### Task 17: Performance reads the journal

**Files:**
- Modify: `src/performance/returns.py` (`contribution_flows`), `src/performance/series.py` (`totals`, `_first_day`), `src/performance/valuation.py` (`_open_lots`), `src/web/deck.py` (the two `contributed` sums from Task 8)
- Create: `src/journal/performance.py`, `src/journal/tests/test_performance.py`
- Update tests: `src/performance/tests/test_returns.py`, `test_series.py`, `test_valuation.py` as needed

**Interfaces:**
- Consumes: journal lines by kind and account.
- Produces:
  - `journal.performance.book_for_performance() -> str`, which is `current().order_mode`
  - `journal.performance.flows(*, book: str, as_of: date) -> list[tuple[date, Decimal]]`: outflows negative, inflows positive
  - `journal.performance.contributed(*, book: str, as_of: date) -> Decimal`
  - `performance.valuation._open_lots()`, filtered to the performance book's lots

The flows (spec §8 "Performance"). Every Investments debit from `buy_fill`, `external_acquisition` and `opening_balance` is an outflow of its amount on its date. Every `sell_fill` or `external_disposal` settlement-account debit (the net proceeds) is an inflow. Every `dividend` credit to 4200 is an inflow. Every 5100 debit (account charges) is an outflow. `charge_correction` lines on 1300 or 4100 are outflows (a positive correction is more money spent). Withdrawals in kind (`external_disposal` to 3200) are an inflow of their cost. Contributed is, for `live`, `-(balance(3100) + balance(3200))` through `as_of`; for `paper`, `-balance(3900)`.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date
from decimal import Decimal

import pytest

from journal import cash
from journal.external import record_dividend
from journal.models import Book
from journal.performance import contributed, flows
from records.external import record_external
from records.models import ExternalSide, PaidFrom

DAY = date(2026, 9, 22)


@pytest.mark.django_db
def test_an_ipo_is_money_invested(infy):
    record_external(
        instrument=infy, side=ExternalSide.BUY, quantity=10, occurred_on=DAY, actor="o",
        reason="IPO", price=Decimal("50"), paid_from=PaidFrom.BANK,
    )
    assert flows(book=Book.LIVE, as_of=DAY) == [(DAY, Decimal("-500"))]
    assert contributed(book=Book.LIVE, as_of=DAY) == Decimal("500")


@pytest.mark.django_db
def test_a_dividend_is_money_returned(infy):
    record_dividend("INFY", Decimal("42"), paid_on=DAY, actor="o")
    assert (DAY, Decimal("42")) in flows(book=Book.LIVE, as_of=DAY)


@pytest.mark.django_db
def test_the_kite_fee_is_not_a_cost_of_the_portfolio():
    cash.contribute(Decimal("1000"), on=DAY, actor="o")
    cash.withdraw(Decimal("500"), on=DAY, actor="o", reason="Kite Connect", source_type="owner_expense")
    assert flows(book=Book.LIVE, as_of=DAY) == []
    assert contributed(book=Book.LIVE, as_of=DAY) == Decimal("500")


@pytest.mark.django_db
def test_amc_is_a_cost_of_the_portfolio():
    cash.account_charge(Decimal("88.5"), on=DAY, component="amc", actor="o", description="AMC")
    assert flows(book=Book.LIVE, as_of=DAY) == [(DAY, Decimal("-88.5"))]
```

Move the `infy` fixture from `test_external.py` into `conftest.py`.

- [ ] **Step 2: Run to verify they fail** — ImportError.

- [ ] **Step 3: Implement** `src/journal/performance.py`:

```python
"""The cash flows XIRR is computed over, read from the journal (M41).

Money deployed into shares -- by the engine, directly, or as an IPO paid from
the owner's bank -- is an outflow; proceeds and dividends come back; account
charges are a cost of holding. Contributions and withdrawals of *cash* are
not flows of the portfolio: money sitting in Zerodha has not been put to work
(and the owner's own costs, J7, never touch it).
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from journal.accounts import (
    ACCOUNT_CHARGES,
    CONTRIBUTIONS,
    DIVIDENDS,
    INVESTMENTS,
    PAPER_FUNDING,
    REALISED,
    SETTLE_ENGINE,
    SETTLE_OTHER,
    WITHDRAWALS,
)
from journal.balances import balance
from journal.models import Book, EntryKind, JournalLine
from journal.posting import q6

ZERO = Decimal("0")


def book_for_performance() -> str:
    from configuration.store import current

    return current().order_mode


def flows(*, book: str, as_of: date) -> list[tuple[date, Decimal]]:
    by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    lines = JournalLine.objects.filter(entry__book=book, entry__occurred_on__lte=as_of).select_related("entry")

    for line in lines:
        kind, account, day = line.entry.kind, line.account_id, line.entry.occurred_on
        if account == INVESTMENTS and kind in (
            EntryKind.BUY_FILL, EntryKind.EXTERNAL_ACQUISITION, EntryKind.OPENING_BALANCE,
        ) and line.amount > ZERO:
            by_day[day] -= line.amount
        elif account in (SETTLE_ENGINE, SETTLE_OTHER) and kind in (
            EntryKind.SELL_FILL, EntryKind.EXTERNAL_DISPOSAL,
        ):
            by_day[day] += line.amount
        elif account == WITHDRAWALS and kind == EntryKind.EXTERNAL_DISPOSAL:
            by_day[day] += line.amount
        elif account == DIVIDENDS:
            by_day[day] -= line.amount  # a credit, so negative: an inflow
        elif account == ACCOUNT_CHARGES:
            by_day[day] -= line.amount
        elif kind == EntryKind.CHARGE_CORRECTION and account in (INVESTMENTS, REALISED):
            by_day[day] -= line.amount

    return [(day, q6(amount)) for day, amount in sorted(by_day.items()) if q6(amount) != ZERO]


def contributed(*, book: str, as_of: date) -> Decimal:
    if book == Book.PAPER:
        return -balance(PAPER_FUNDING, book=book, through=as_of)
    return -(balance(CONTRIBUTIONS, book=book, through=as_of) + balance(WITHDRAWALS, book=book, through=as_of))
```

`test_the_kite_fee_is_not_a_cost_of_the_portfolio` expects `contributed == 500` (1000 in, 500 back to the owner). Check that the sign logic gives that: 3100 = −1000 and 3200 = +500, so −(−1000 + 500) = 500. ✓.

`src/performance/returns.py` `contribution_flows(as_of, terminal_value)`: build the flows from `journal.performance.flows(book=book_for_performance(), as_of=as_of)` as `Flow(on=day, amount=money(amount))`, then append the terminal value as today. Remove the `CashLedger` and `RealisedGain` loops. Proceeds now come from the journal, which also carries external sales and IPOs: that is the fix for the overstated XIRR.

`src/performance/series.py` `totals()`: `contributed = money(journal.performance.contributed(book=book_for_performance(), as_of=as_of))`. `days_run` and `days_on_record` stay on `CashLedger` rows filtered by book.

`src/performance/valuation.py` `_open_lots()`: filter by the performance book. For paper, `created_from_fill__order__mode="paper"`; for live, `journal.external.live_open_lots()`. Hold off on this change if `performance/tests` pin today's behaviour of valuing every lot: then only add a `book` argument defaulting to "every lot", and pass the book from `measure()` and `snapshot()`. The rule: **the value and the flows must come from the same book**, or XIRR compares one portfolio's value with another's cash.

`src/web/deck.py`: replace Task 8's two `contributed` sums with `journal.performance.contributed(book=engine_book(), as_of=today_ist())`.

Add to `src/performance/tests/test_returns.py` a test where an IPO acquisition is recorded, the value equals its cost, and XIRR is therefore ~0. Previously the IPO's value counted with no outflow and XIRR came out very large.

- [ ] **Step 4: Run to verify they pass** — `PYTEST src` → all pass.

- [ ] **Step 5: Commit** — `fix(performance): compute XIRR from the journal, counting IPOs and dividends`

---

### Task 18: The documents say what the code now does

**Files:**
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-09-21-steadystack-prd.md`, `OPERATIONS.md`, `docs/superpowers/specs/2026-09-24-double-entry-journal-design.md` (status line)

- [ ] **Step 1: CLAUDE.md**
  - Core invariants: replace the bullet "**The cash ledger is append-only.** One row per trading day…" with: "**Every movement of money is a balanced, append-only journal entry.** Debits equal credits in every entry and the database refuses one that does not; nothing is edited, a mistake is reversed by a new entry, and a closed financial year accepts nothing dated inside it. Carry is the balance of the engine's cash accounts. The daily `CashLedger` row is a summary of the engine's book, not the books."
  - Commit scopes: add "`journal` — the double-entry journal, statement import, balance checks, the books screens".
  - Vocabulary: **carry** becomes "the engine cash balance (1110 + 1200): budget allocated and not yet spent. It may go negative after an overspend; the next allocation repays it". Add **allocation** ("moving money from unallocated to the engine: the daily amount, a lumpsum, reinvested proceeds, restored carry"), **book** ("live or paper; real money is always live"), and **sweep** ("SEBI's quarterly settlement, which returns the balance to the owner's bank and leaves carry owed").
  - Running it locally: one line on `manage.py open_books` at go-live and `/books/statement/` for the monthly Console upload.

- [ ] **Step 2: PRD.** Rewrite M8a (a lumpsum is a contribution and an allocation; the rest of its timing rule stands), M35b (the settlement line or a contract note corrects charges by a new entry and finalises the lot), and M40a (the daily summary per book; the journal, ledgers and trial balance are the books). Add M40c for the statement import, the balance check, the reports and year closes, pointing at the design spec. In the carry table in §6, the row "Lumpsum recorded" reads "a contribution to unallocated and an allocation to the engine, spendable from the next tradesheet". The data-model row for `CashLedger` becomes the summary, and new rows are added for `JournalEntry`, `JournalLine`, `StatementLine` and `BalanceCheck`.

- [ ] **Step 3: OPERATIONS.md.** A "Books" section covering: go-live (`open_books` with the statement's balance that morning), monthly (download the Console funds statement as CSV, upload it at `/books/statement/`, work through the queue, correct any settlement differences), quarterly (after the first-Friday settlement, transfer the carry owed back before Monday), and yearly (after the CA signs off, close the year at `/books/`).

- [ ] **Step 4: Spec status.** Set the design spec's `**Status:**` to `Implemented (see docs/plans/2026-09-24-feat-double-entry-journal-plan.md)`.

- [ ] **Step 5: Commit** — `docs: describe the journal in CLAUDE.md, the PRD and the runbook`
