"""The cash ledger becomes a summary of the journal (spec §8, J10).

The old table's rows (the sale proceeds 0010 added included), the lumpsum
credits and the carried-forward adjustments are paper history and are not
converted; `seed_demo --reset` rebuilds a demo
through the new code (preflight warns while paper lots predate the journal).
The new table keeps a `proceeds` column: the day's reinvest allocations,
split out of the extra allocations.
Dropping a table is DDL and is not stopped by the append-only trigger, which
fires on UPDATE and DELETE of rows.

Reversible: going back recreates the old table from migration state, which
knows nothing of 0003's trigger, so the reverse puts it and the revoke back.

Refused outright if any live order exists (M-8): the old rows are dropped
because they are paper history, and a database that has placed a live
order holds real money in them.
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


def refuse_after_a_live_order(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    if Order.objects.using(schema_editor.connection.alias).filter(mode="live").exists():
        raise RuntimeError(
            "A live order exists, so the old cash ledger holds real money; this migration "
            "drops it as paper history and must not run here. Stop and convert the ledger "
            "by hand."
        )


def money():
    return models.DecimalField(max_digits=20, decimal_places=6, default=Decimal("0"))


class Migration(migrations.Migration):
    dependencies = [("engine", "0010_cashledger_proceeds"), ("orders", "0001_initial")]

    operations = [
        migrations.RunPython(refuse_after_a_live_order, migrations.RunPython.noop),
        migrations.DeleteModel("LumpsumCredit"),
        migrations.DeleteModel("LedgerAdjustment"),
        # Nothing forward; in reverse it runs right after the old table is
        # recreated, and gives it back its append-only trigger (0003).
        migrations.RunSQL(migrations.RunSQL.noop, APPEND_ONLY),
        migrations.DeleteModel("CashLedger"),
        migrations.CreateModel(
            name="CashLedger",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "book",
                    models.CharField(choices=[("live", "Live"), ("paper", "Paper")], max_length=8),
                ),
                ("trading_date", models.DateField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("trace_id", models.CharField(blank=True, db_index=True, max_length=64)),
                ("opening_carry", money()),
                ("daily_allocation", money()),
                (
                    "extra_allocation",
                    models.DecimalField(
                        decimal_places=6,
                        default=Decimal("0"),
                        help_text="Lumpsums and restored carry.",
                        max_digits=20,
                    ),
                ),
                (
                    "proceeds",
                    models.DecimalField(
                        decimal_places=6,
                        default=Decimal("0"),
                        help_text=(
                            "Reinvested rebalance sale money, net of the sells' charges (D-S1)."
                        ),
                        max_digits=20,
                    ),
                ),
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
