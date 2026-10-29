"""A statement line may be matched again once its entry has been reversed (I4).

0005 allowed exactly one transition, null to a value. A line confirmed as the
wrong kind was then stuck: reversing its entry took the money out of the
books, but the line stayed matched to the reversed entry and could not be
confirmed as what it really was. The function now also allows one entry to be
replaced by another -- only when the old entry has a reversal. Back to null,
or any other column changing, is still refused. No grant changes: the runtime
role already holds UPDATE on the table for the first match.
"""

from django.db import migrations

LINE_COLUMNS = """
       (OLD.upload_id, OLD.position, OLD.fingerprint, OLD.posting_date, OLD.particulars,
        OLD.cost_center, OLD.voucher_type, OLD.debit, OLD.credit, OLD.net_balance)
       IS DISTINCT FROM
       (NEW.upload_id, NEW.position, NEW.fingerprint, NEW.posting_date, NEW.particulars,
        NEW.cost_center, NEW.voucher_type, NEW.debit, NEW.credit, NEW.net_balance)
"""

SQL = f"""
CREATE OR REPLACE FUNCTION journal_statementline_match_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'statement lines are never deleted';
    END IF;
    IF NEW.matched_entry_id IS NULL OR {LINE_COLUMNS} THEN
        RAISE EXCEPTION 'a statement line may only be matched';
    END IF;
    IF OLD.matched_entry_id IS NOT NULL
       AND NEW.matched_entry_id IS DISTINCT FROM OLD.matched_entry_id
       AND NOT EXISTS (
           SELECT 1 FROM journal_journalentry WHERE reverses_id = OLD.matched_entry_id
       )
    THEN
        RAISE EXCEPTION 'a statement line is matched again only once its entry is reversed';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
"""

REVERSE = f"""
CREATE OR REPLACE FUNCTION journal_statementline_match_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'statement lines are never deleted';
    END IF;
    IF OLD.matched_entry_id IS NOT NULL
       OR NEW.matched_entry_id IS NULL
       OR {LINE_COLUMNS}
    THEN
        RAISE EXCEPTION 'a statement line may only be matched, once';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [("journal", "0007_balancecheck_append_only")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
