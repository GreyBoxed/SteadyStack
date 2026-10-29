"""A transactional test flushes every table, the chart of accounts included.

pytest-django runs these last, so the suite used to pass by ordering alone:
the first transactional test to post an entry after another one had flushed
would fail with a foreign-key violation at commit. The root conftest puts the
chart back for every transactional test. Two tests, in this order, pin it.
"""

from datetime import date
from decimal import Decimal

import pytest

from journal.models import Account


@pytest.mark.django_db(transaction=True)
def test_a_transactional_test_that_flushes_on_the_way_out():
    assert Account.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_a_later_transactional_test_can_still_post():
    from journal import cash
    from journal.balances import balance
    from journal.models import Book

    cash.contribute(Decimal("5"), on=date(2026, 9, 22), actor="o")
    assert balance("1100", book=Book.LIVE) == Decimal("5")
