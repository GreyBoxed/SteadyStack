"""Enforce append-only at the database.

A trigger rather than role grants alone: migrations and tests run as the schema
owner, and grants do not restrain an owner. A trigger fires for every role, so
the invariant holds no matter who holds the connection -- and can be tested.

Role grants are applied too, as defence in depth for the runtime connection.
"""

from django.db import migrations

CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION steadystack_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'append-only table: % on % is not permitted', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTION = "DROP FUNCTION IF EXISTS steadystack_reject_mutation() CASCADE;"


def _trigger(table: str) -> str:
    return f"""
    CREATE TRIGGER {table}_append_only
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION steadystack_reject_mutation();
    """


def _drop_trigger(table: str) -> str:
    return f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"


def _revoke(table: str) -> str:
    """Revoke from the runtime role if it exists; skip quietly if it does not,
    so the migration runs in environments that use a single role."""
    return f"""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
            REVOKE UPDATE, DELETE ON {table} FROM steadystack_app;
        END IF;
    END
    $$;
    """


def _regrant(table: str) -> str:
    return f"""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
            GRANT UPDATE, DELETE ON {table} TO steadystack_app;
        END IF;
    END
    $$;
    """


TABLES = ["audit_auditevent", "audit_brokercall"]


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [
        migrations.RunSQL(CREATE_FUNCTION, DROP_FUNCTION),
        *[
            op
            for table in TABLES
            for op in (
                migrations.RunSQL(_trigger(table), _drop_trigger(table)),
                migrations.RunSQL(_revoke(table), _regrant(table)),
            )
        ],
    ]
