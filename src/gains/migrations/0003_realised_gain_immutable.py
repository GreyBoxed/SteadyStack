"""A realised gain, once reported, is never rewritten.

A number that was reported and then quietly changed is worse than one that
was wrong and then corrected in public. So the row is append-only, and undoing
one means writing a reversing row that points at it (M36c).
"""

from django.db import migrations

TABLE = "gains_realisedgain"

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
        ("gains", "0002_initial"),
        ("audit", "0002_append_only"),
    ]

    operations = [
        migrations.RunSQL(TRIGGER, DROP_TRIGGER),
        migrations.RunSQL(REVOKE, REGRANT),
    ]
