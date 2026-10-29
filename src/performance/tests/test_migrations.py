"""Keying the value series by book (M13), and taking that back."""

from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = ("performance", "0001_initial")
AFTER = ("performance", "0002_dailyvaluation_book")


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


@pytest.mark.django_db(transaction=True)
def test_0002_files_existing_evenings_under_the_mode_in_force_and_reverses():
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        apps = _migrate(BEFORE)
        apps.get_model("performance", "DailyValuation").objects.create(
            trading_date=date(2026, 9, 21), portfolio_value=Decimal("100")
        )
        # Only this app is migrated back; the configuration is as it stands.
        from configuration.models import ConfigurationVersion

        ConfigurationVersion.objects.create(version=1, changed_by="operator", order_mode="live")

        apps = _migrate(AFTER)
        valuation = apps.get_model("performance", "DailyValuation")
        assert list(valuation.objects.values_list("book", flat=True)) == ["live"]
        # A second book may value the same evening; the same book may not.
        valuation.objects.create(
            trading_date=date(2026, 9, 21), book="paper", portfolio_value=Decimal("1")
        )
        assert valuation.objects.count() == 2
        valuation.objects.filter(book="paper").delete()

        apps = _migrate(BEFORE)
        valuation = apps.get_model("performance", "DailyValuation")
        assert valuation.objects.get().trading_date == date(2026, 9, 21)
    finally:
        MigrationExecutor(connection).migrate(leaves)


@pytest.mark.django_db(transaction=True)
def test_0002_files_evenings_as_paper_when_nothing_is_configured():
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        apps = _migrate(BEFORE)
        apps.get_model("performance", "DailyValuation").objects.create(
            trading_date=date(2026, 9, 21), portfolio_value=Decimal("100")
        )
        apps = _migrate(AFTER)
        valuation = apps.get_model("performance", "DailyValuation")
        assert list(valuation.objects.values_list("book", flat=True)) == ["paper"]
    finally:
        MigrationExecutor(connection).migrate(leaves)
