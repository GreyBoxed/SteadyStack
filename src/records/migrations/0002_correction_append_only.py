"""A correction is the one thing that undoes a bookkeeping error, so it is the
one thing that must never itself be quietly edited.

PRD §7 marks `Correction` append-only alongside the ledger, the audit trail,
the configuration versions and the realised gains -- and it was the only one
of them without the trigger that makes that true. A correction that can be
rewritten is a correction that proves nothing: the reason it states, and the
before/after it carries, are the entire evidence that an error was fixed
rather than covered up (M36c).
"""

from django.db import migrations

TABLE = "records_correction"

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
        ("records", "0001_initial"),
        ("audit", "0002_append_only"),
    ]

    operations = [
        migrations.RunSQL(TRIGGER, DROP_TRIGGER),
        migrations.RunSQL(REVOKE, REGRANT),
    ]
