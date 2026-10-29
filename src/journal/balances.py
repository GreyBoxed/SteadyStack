"""Reading the journal: balances, and the reports built from them."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum

from journal.accounts import ENGINE_ACCOUNTS, ZERODHA_ACCOUNTS
from journal.models import Account, AccountType, JournalEntry, JournalLine
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


@dataclass(frozen=True)
class TrialRow:
    code: str
    name: str
    type: str
    debit: Decimal
    credit: Decimal


#: The trial-balance row carrying earlier years' income less expense (M11).
BROUGHT_FORWARD = "b/f"


def _row(code: str, name: str, type_: str, amount: Decimal) -> TrialRow:
    return TrialRow(
        code=code,
        name=name,
        type=type_,
        debit=amount if amount > ZERO else ZERO,
        credit=-amount if amount < ZERO else ZERO,
    )


def trial_balance(*, book: str, through: date, start: date | None = None) -> list[TrialRow]:
    """Every account's balance, debit or credit, as at *through*.

    With *start* -- the first day of a financial year -- income and expense
    accounts show only what moved from *start* to *through*, and what they
    held before it is one brought-forward line, so the totals still agree.
    Asset and equity accounts are balances, and are as at *through* either way.
    """
    rows = []
    brought_forward = ZERO
    before = start - timedelta(days=1) if start is not None else None
    for account in Account.objects.order_by("code"):
        amount = balance(account.code, book=book, through=through)
        if before is not None and account.type in (AccountType.INCOME, AccountType.EXPENSE):
            earlier = balance(account.code, book=book, through=before)
            brought_forward += earlier
            amount -= earlier
        if amount == ZERO:
            continue
        rows.append(_row(account.code, account.name, account.type, amount))
    if brought_forward != ZERO:
        rows.append(
            _row(
                BROUGHT_FORWARD,
                "Earlier years' income less expense, brought forward",
                AccountType.EQUITY,
                q6(brought_forward),
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


def general_ledger(
    code: str, *, book: str, start: date, end: date
) -> tuple[Decimal, list[LedgerRow]]:
    """One account's lines in a range, with the balance brought forward."""
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
