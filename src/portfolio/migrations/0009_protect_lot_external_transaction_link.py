"""The external transaction a lot came from is part of its identity.

`external_transaction` is set once, by a buy or a transfer in recorded
outside the engine, and answers "which external movement created these
shares?" for as long as the lot exists. Repointing it would make a lot claim
a different movement, so the immutability trigger covers it alongside the
basis fields and `created_from_fill_id` (0006) -- even though, unlike a fill,
it may now be copied *onto* a new row (the replacement a contract note
finalises), because a plain `ForeignKey` allows more than one lot to carry
the same external transaction.
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
        OR NEW.external_transaction_id IS DISTINCT FROM OLD.external_transaction_id
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


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0008_alter_lot_external_transaction")]

    operations = [migrations.RunSQL(FUNCTION, PREVIOUS)]
