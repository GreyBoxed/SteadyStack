"""A lot's basis cannot be rewritten, and the database is what says so.

Lots are not append-only in the way the audit tables are: consumption is the
whole point of a lot, so `remaining_quantity` has to move, and a correction
sets `superseded_by`. Everything else -- which instrument, when it was
acquired, how many shares, what they cost -- is the permanent record of a
purchase and must never change.

An UPDATE that touches any of those raises. A DELETE always raises: a lot that
was wrong is superseded by another lot, never removed, or the history stops
adding up.
"""

from django.db import migrations

TABLE = "portfolio_lot"

CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION steadystack_protect_lot_basis() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'lot % cannot be deleted: supersede it instead', OLD.id;
    END IF;

    IF NEW.instrument_id IS DISTINCT FROM OLD.instrument_id
        OR NEW.acquired_on IS DISTINCT FROM OLD.acquired_on
        OR NEW.quantity IS DISTINCT FROM OLD.quantity
        OR NEW.cost_per_unit IS DISTINCT FROM OLD.cost_per_unit
        OR NEW.opening_import_id IS DISTINCT FROM OLD.opening_import_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'lot % has an immutable basis: only remaining_quantity, basis_state and superseded_by may change',
            OLD.id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTION = "DROP FUNCTION IF EXISTS steadystack_protect_lot_basis() CASCADE;"

TRIGGER = f"""
CREATE TRIGGER {TABLE}_immutable_basis
    BEFORE UPDATE OR DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION steadystack_protect_lot_basis();
"""

DROP_TRIGGER = f"DROP TRIGGER IF EXISTS {TABLE}_immutable_basis ON {TABLE};"

REVOKE = f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        REVOKE DELETE ON {TABLE} FROM steadystack_app;
    END IF;
END
$$;
"""

REGRANT = f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'steadystack_app') THEN
        GRANT DELETE ON {TABLE} TO steadystack_app;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0003_openingimport_lot")]

    operations = [
        migrations.RunSQL(CREATE_FUNCTION, DROP_FUNCTION),
        migrations.RunSQL(TRIGGER, DROP_TRIGGER),
        migrations.RunSQL(REVOKE, REGRANT),
    ]
