"""The demo seed keeps the ledger's promises.

It builds its days through the real code paths, so what it writes is held to
what a real day is held to: every row reconciles, and a day with a sale
carries that sale's proceeds on its own row -- the row is written once, so a
sale seeded after it would leave money the ledger can never be made to show.
"""

import pytest
from django.core.management import call_command

from engine.ledger import reconcile
from engine.models import CashLedger
from engine.settlement import day_proceeds

pytestmark = pytest.mark.django_db


def test_every_seeded_row_carries_its_days_sale_proceeds_and_balances(settings):
    settings.DEBUG = True

    call_command("seed_demo", "--days", "12")

    rows = list(CashLedger.objects.order_by("trading_date"))
    assert any(row.proceeds > 0 for row in rows)
    assert all(row.proceeds == day_proceeds(row.trading_date) for row in rows)
    assert reconcile()["ok"] is True


def test_the_seeded_sale_is_never_a_name_its_day_bought(settings):
    """D-S2 holds in the demo too: CNC would net a same-day buy and sell."""
    from django.utils import timezone

    from orders.models import Fill, OrderSide

    settings.DEBUG = True
    call_command("seed_demo", "--days", "12")

    sales = Fill.objects.filter(order__side=OrderSide.SELL).select_related("order")
    assert sales.exists()
    for sale in sales:
        day = sale.order.trading_date
        bought = Fill.objects.filter(
            order__trading_date=day,
            order__side=OrderSide.BUY,
            order__instrument_id=sale.order.instrument_id,
        )
        assert not bought.exists(), f"{sale.order.instrument_id} bought and sold on {day}"
        assert timezone.localdate(sale.executed_at) == day


def test_the_seeded_investments_agree_with_the_lots(settings):
    """m-4: a fill's charges are the rate card's, as in production, so the
    journal's investments and the lots' basis agree and preflight's
    investments check is clean on a seeded database."""
    from journal.checks import investments_disagree
    from orders.models import Fill, OrderSide

    settings.DEBUG = True
    call_command("seed_demo", "--days", "12")

    assert not Fill.objects.filter(order__side=OrderSide.BUY).exclude(charges=0).exists()
    assert not investments_disagree(book="paper")
    assert not investments_disagree(book="live")
