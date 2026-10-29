"""A balance check is recorded once and never revised (spec §7.1, §9).

Re-running the morning check on a day already checked returns the existing
row rather than writing a second one -- `checks.check_balance` enforces that
in Python, and this is the database's half: the row it wrote cannot be
edited or removed out from under it.
"""

from django.db import migrations

TABLE = "journal_balancecheck"

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
    dependencies = [("journal", "0006_balancecheck")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
