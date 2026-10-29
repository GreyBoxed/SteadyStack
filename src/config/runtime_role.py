"""The runtime role's privileges, as the migrations set them (M14).

The `runtime_role` fixture (src/conftest.py), the append-only guard and
preflight's "runtime role" row (NB1) read the REVOKEs from here, so none of
them keeps its own list.
"""

import re

RUNTIME_ROLE = "steadystack_app"

#: A migration's revoke: a DO block revoking from the runtime role. Matched
#: whole, so a trigger in the same RunSQL is not run a second time.
_REVOKE_BLOCK = re.compile(r"DO \$\$.*?END\s*\$\$;", re.DOTALL)


def migration_revokes() -> list[str]:
    """Every forward REVOKE the project's migrations run, in migration order.

    Read from the migrations rather than listed, so a new append-only table
    is covered the day its migration lands.
    """
    from django.db.migrations import RunSQL
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    plan: list = []
    for leaf in loader.graph.leaf_nodes():
        plan += [key for key in loader.graph.forwards_plan(leaf) if key not in plan]
    blocks = []
    for key in plan:
        for operation in loader.graph.nodes[key].operations:
            if not isinstance(operation, RunSQL):
                continue
            sqls = operation.sql if isinstance(operation.sql, (list, tuple)) else [operation.sql]
            for sql in sqls:
                if isinstance(sql, str):
                    blocks += [b for b in _REVOKE_BLOCK.findall(sql) if "REVOKE" in b]
    return blocks


def revoked_privileges() -> dict[str, set[str]]:
    """Each table the migrations narrow, and the privileges revoked on it."""
    revoked: dict[str, set[str]] = {}
    for block in migration_revokes():
        for privileges, table in re.findall(r"REVOKE ([A-Z, ]+) ON (\w+)", block):
            revoked.setdefault(table, set()).update(
                name.strip() for name in privileges.split(",") if name.strip()
            )
    return revoked


def update_revoked_tables() -> set[str]:
    """Every table a migration revokes UPDATE on from the runtime role."""
    return {table for table, names in revoked_privileges().items() if "UPDATE" in names}
