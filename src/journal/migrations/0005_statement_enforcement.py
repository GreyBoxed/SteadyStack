"""An upload is append-only; a line may only be matched, once (spec §7.2, §9).

`StatementLine` needs its own trigger rather than the shared
`steadystack_reject_mutation()`: one update is allowed -- `matched_entry` going
from null to a value, with every other column unchanged -- so the function
has to inspect OLD and NEW rather than simply refuse.
"""

from django.db import migrations

LINE = "journal_statementline"
UPLOAD = "journal_statementupload"

SQL = f"""
CREATE OR REPLACE FUNCTION journal_statementline_match_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'statement lines are never deleted';
    END IF;
    IF OLD.matched_entry_id IS NOT NULL
       OR NEW.matched_entry_id IS NULL
       OR (OLD.upload_id, OLD.position, OLD.fingerprint, OLD.posting_date, OLD.particulars,
           OLD.cost_center, OLD.voucher_type, OLD.debit, OLD.credit, OLD.net_balance)
          IS DISTINCT FROM
          (NEW.upload_id, NEW.position, NEW.fingerprint, NEW.posting_date, NEW.particulars,
           NEW.cost_center, NEW.voucher_type, NEW.debit, NEW.credit, NEW.net_balance)
    THEN
        RAISE EXCEPTION 'a statement line may only be matched, once';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE TRIGGER {LINE}_match_only
    BEFORE UPDATE OR DELETE ON {LINE}
    FOR EACH ROW EXECUTE FUNCTION journal_statementline_match_only();

CREATE TRIGGER {UPLOAD}_append_only
    BEFORE UPDATE OR DELETE ON {UPLOAD}
    FOR EACH ROW EXECUTE FUNCTION steadystack_reject_mutation();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        REVOKE UPDATE, DELETE ON {UPLOAD} FROM steadystack_app;
        REVOKE DELETE ON {LINE} FROM steadystack_app;
    END IF;
END
$$;
"""

REVERSE = f"""
DROP TRIGGER IF EXISTS {LINE}_match_only ON {LINE};
DROP TRIGGER IF EXISTS {UPLOAD}_append_only ON {UPLOAD};
DROP FUNCTION IF EXISTS journal_statementline_match_only();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        GRANT UPDATE, DELETE ON {UPLOAD} TO steadystack_app;
        GRANT DELETE ON {LINE} TO steadystack_app;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("journal", "0004_statementupload_statementline")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
