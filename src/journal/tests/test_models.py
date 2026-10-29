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
    JournalLine.objects.create(
        entry=entry, account_id=accounts.UNALLOCATED, amount=Decimal("1000.000220")
    )
    JournalLine.objects.create(
        entry=entry, account_id=accounts.CONTRIBUTIONS, amount=Decimal("-1000.000220")
    )

    amounts = sorted(line.amount for line in entry.lines.all())
    assert amounts == [Decimal("-1000.000220"), Decimal("1000.000220")]
