"""A person's explanation of a settlement difference, appended only (m-4).

The table and its enforcement land together: the same trigger and REVOKE as
the journal's own tables, so an explanation, once written, cannot be edited
or removed -- a second one is a new row. Reversed, the grant comes back
before the table goes.
"""

import django.db.models.deletion
from django.db import migrations, models

TABLE = "journal_settlementexplanation"

SQL = f"""
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

REVERSE = f"""
DROP TRIGGER IF EXISTS {TABLE}_append_only ON {TABLE};

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        GRANT UPDATE, DELETE ON {TABLE} TO steadystack_app;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0009_journalline_lot"),
    ]

    operations = [
        migrations.CreateModel(
            name="SettlementExplanation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("difference", models.DecimalField(decimal_places=6, max_digits=20)),
                ("reason", models.CharField(max_length=500)),
                ("explained_by", models.CharField(max_length=150)),
                ("explained_at", models.DateTimeField(auto_now_add=True)),
                ("trace_id", models.CharField(blank=True, max_length=64)),
                (
                    "line",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="explanations",
                        to="journal.statementline",
                    ),
                ),
            ],
            options={
                "ordering": ["-explained_at"],
            },
        ),
        migrations.RunSQL(SQL, REVERSE),
    ]
