"""The day's rebalance sale money, on the day's ledger row (D-S1).

Added by DDL with a database default, so the append-only trigger on the table
never fires and every existing row reads 0. Removing the column is refused
once any row carries proceeds: the ledger is append-only, and dropping a
column of it would destroy what it recorded.
"""

from decimal import Decimal

from django.db import migrations, models


def refuse_to_drop_recorded_proceeds(apps, schema_editor):
    CashLedger = apps.get_model("engine", "CashLedger")
    rows = CashLedger.objects.using(schema_editor.connection.alias)
    if rows.exclude(proceeds=Decimal("0")).exists():
        raise RuntimeError(
            "Rows carry sale proceeds; removing the column would destroy them. "
            "The cash ledger is append-only."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0009_override_stop_day"),
    ]

    operations = [
        migrations.AddField(
            model_name="cashledger",
            name="proceeds",
            field=models.DecimalField(
                decimal_places=2,
                db_default=Decimal("0"),
                default=Decimal("0"),
                help_text="Rebalance sale money, net of the sells' charges (D-S1).",
                max_digits=14,
            ),
        ),
        # Listed after the field so that, reversed, it runs first.
        migrations.RunPython(migrations.RunPython.noop, refuse_to_drop_recorded_proceeds),
    ]
