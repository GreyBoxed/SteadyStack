"""The fill a lot came from is part of its identity.

`created_from_fill` is set once, when the fill creates the lot, and answers
"which trade bought these shares?" for as long as the lot exists. Repointing
it would make a lot claim a different purchase, so the immutability trigger
covers it alongside the basis fields.
"""

from django.db import migrations

FUNCTION = """
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
        OR NEW.created_from_fill_id IS DISTINCT FROM OLD.created_from_fill_id
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

PREVIOUS = """
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


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0005_lot_created_from_fill")]

    operations = [migrations.RunSQL(FUNCTION, PREVIOUS)]
