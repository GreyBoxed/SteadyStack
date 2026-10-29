"""The journal's invariants, enforced by the database (spec §9).

Triggers rather than grants alone, for the reason audit/0002 gives: migrations
and tests connect as the schema owner, and grants do not restrain an owner.

The balance check is a *deferred constraint trigger*: an entry is inserted
first and its lines after it, so the sum can only be judged at commit.
"""

from django.db import migrations

APPEND_ONLY = ["journal_journalentry", "journal_journalline", "journal_periodclose"]

FUNCTIONS = """
CREATE OR REPLACE FUNCTION journal_check_balanced() RETURNS trigger AS $$
DECLARE
    target uuid;
    total numeric;
    n integer;
BEGIN
    IF TG_TABLE_NAME = 'journal_journalentry' THEN
        target := NEW.id;
    ELSE
        target := NEW.entry_id;
    END IF;

    SELECT coalesce(sum(amount), 0), count(*) INTO total, n
      FROM journal_journalline WHERE entry_id = target;

    IF n = 0 THEN
        RAISE EXCEPTION 'journal entry % has no lines', target;
    END IF;
    IF total <> 0 OR n < 2 THEN
        RAISE EXCEPTION 'journal entry % does not balance (sum %, % lines)', target, total, n;
    END IF;
    RETURN NULL;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION journal_check_period_open() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM journal_periodclose
         WHERE book = NEW.book AND fy_end >= NEW.occurred_on
    ) THEN
        RAISE EXCEPTION 'journal entry dated % falls in a closed financial year of the % book',
            NEW.occurred_on, NEW.book;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTIONS = """
DROP FUNCTION IF EXISTS journal_check_balanced();
DROP FUNCTION IF EXISTS journal_check_period_open();
"""

CHECKS = """
CREATE CONSTRAINT TRIGGER journal_line_balanced
    AFTER INSERT ON journal_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION journal_check_balanced();

CREATE CONSTRAINT TRIGGER journal_entry_has_lines
    AFTER INSERT ON journal_journalentry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION journal_check_balanced();

CREATE TRIGGER journal_entry_period_open
    BEFORE INSERT ON journal_journalentry
    FOR EACH ROW EXECUTE FUNCTION journal_check_period_open();
"""

DROP_CHECKS = """
DROP TRIGGER IF EXISTS journal_line_balanced ON journal_journalline;
DROP TRIGGER IF EXISTS journal_entry_has_lines ON journal_journalentry;
DROP TRIGGER IF EXISTS journal_entry_period_open ON journal_journalentry;
"""


def _append_only(table: str) -> str:
    return f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION steadystack_reject_mutation();
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        REVOKE UPDATE, DELETE ON {table} FROM steadystack_app;
    END IF;
END
$$;
"""


def _undo_append_only(table: str) -> str:
    return f"""
DROP TRIGGER IF EXISTS {table}_append_only ON {table};
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        GRANT UPDATE, DELETE ON {table} TO steadystack_app;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0002_chart_of_accounts"),
        ("audit", "0002_append_only"),
    ]

    operations = [
        migrations.RunSQL(FUNCTIONS, DROP_FUNCTIONS),
        migrations.RunSQL(CHECKS, DROP_CHECKS),
        *[migrations.RunSQL(_append_only(t), _undo_append_only(t)) for t in APPEND_ONLY],
    ]
