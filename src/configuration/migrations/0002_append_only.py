"""Configuration versions are append-only too.

The store never issues an UPDATE, but "never" is a property of today's code.
A tradesheet from 2031 is explained by the configuration that was in force
that day, so the row recording it has to be as immutable as the ledger it
explains. The trigger function itself belongs to the audit app, which is why
this migration depends on it rather than defining a second one.
"""

from django.db import migrations

TABLE = "configuration_configurationversion"

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
        ("configuration", "0001_initial"),
        ("audit", "0002_append_only"),
    ]

    operations = [
        migrations.RunSQL(TRIGGER, DROP_TRIGGER),
        migrations.RunSQL(REVOKE, REGRANT),
    ]
