"""The cash ledger is append-only, enforced by the database.

This is the invariant the whole system is judged by: every rupee traces to a
day and an order. A ledger row that can be edited is a ledger that can be made
to balance, which is the same as one that cannot be trusted to.

A correction is an `adjustment` on a later row, never a rewrite of an earlier
one -- which is exactly why that column exists.
"""

from django.db import migrations

TABLE = "engine_cashledger"

TRIGGER = f"""
CREATE TRIGGER {TABLE}_append_only
    BEFORE UPDATE OR DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION steadystack_reject_mutation();
"""

DROP_TRIGGER = f"DROP TRIGGER IF EXISTS {TABLE}_append_only ON {TABLE};"

REVOKE = f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        REVOKE UPDATE, DELETE ON {TABLE} FROM steadystack_app;
    END IF;
END
$$;
"""

REGRANT = f"""
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
        ("engine", "0002_cashledger_lumpsumcredit"),
        ("audit", "0002_append_only"),
    ]

    operations = [
        migrations.RunSQL(TRIGGER, DROP_TRIGGER),
        migrations.RunSQL(REVOKE, REGRANT),
    ]
