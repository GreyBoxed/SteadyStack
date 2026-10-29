"""Structural guards on the migrations themselves.

These exist because of a failure the test suite cannot reproduce. This project
runs two roles against one database -- `steadystack_app` for the runtime and
`steadystack_owner` for the schema -- and `manage.py migrate` redirects itself to
the owner's alias. The test settings pop that alias, so every test runs with a
single connection and the two-role topology is invisible here.

Which means a migration that writes on the *default* alias hangs the real
deploy and passes every test: the owner's connection is holding an
ACCESS EXCLUSIVE lock from its own ALTER TABLE, in a transaction that will not
commit until the migration finishes, and the migration is waiting on a second
connection for that lock. Nothing times out. It simply stops.

So the guard is on the source rather than the behaviour. It is crude on
purpose -- a false positive costs one line in ALLOWED_WITHOUT_ALIAS, and a
false negative costs a deploy that never finishes.
"""

import inspect

import pytest
from django.db.migrations import RunPython, RunSQL
from django.db.migrations.loader import MigrationLoader

#: Callables that genuinely need no alias, named rather than pattern-matched
#: so that adding one is a decision somebody made.
ALLOWED_WITHOUT_ALIAS: set[str] = set()

ALIAS = "schema_editor.connection.alias"

#: This project's own apps. Django's and django-otp's migrations are not ours
#: to police -- `auth.0011` writes on the default manager too -- and they run
#: before any of this schema exists, so they are not the ones that hang.
OURS = frozenset(
    {
        "accounts",
        "audit",
        "broker",
        "configuration",
        "engine",
        "gains",
        "model_portfolio",
        "orders",
        "performance",
        "portfolio",
        "rebalance",
        "records",
        "trading_calendar",
        "web",
    }
)


def run_python_callables():
    """Every function one of *our* RunPython operations would call."""
    loader = MigrationLoader(None, ignore_no_migrations=True)

    for (app_label, name), migration in sorted(loader.disk_migrations.items()):
        if app_label not in OURS:
            continue
        for operation in migration.operations:
            if not isinstance(operation, RunPython):
                continue
            for callable_ in (operation.code, operation.reverse_code):
                if callable_ is None or callable_ is RunPython.noop:
                    continue
                yield f"{app_label}/{name}", callable_


def test_every_run_python_writes_on_the_migration_connection():
    """A RunPython that uses the default manager writes as the runtime role,
    on a second connection, and waits forever on the schema owner's lock."""
    offenders = []

    for where, callable_ in run_python_callables():
        if callable_.__qualname__ in ALLOWED_WITHOUT_ALIAS:
            continue
        source = inspect.getsource(callable_)
        if ALIAS not in source:
            offenders.append(f"{where}:{callable_.__qualname__}")

    assert not offenders, (
        "These migration functions do not name the migration connection, so they "
        f"would write as the runtime role and deadlock a real migrate: {offenders}. "
        f"Use `.using({ALIAS})` on the queryset."
    )


def test_the_guard_can_actually_see_a_run_python():
    """The guard above passes trivially if the loader finds nothing.

    Asserted separately so that a refactor which stops the loader seeing
    migrations fails here, loudly, rather than turning the real check into a
    test that always passes.
    """
    found = list(run_python_callables())

    assert found, (
        "No RunPython callables were found at all. Either every data migration "
        "has gone, or the loader is no longer reading them -- and if it is the "
        "second, the guard above is asserting nothing."
    )


@pytest.mark.parametrize("operation_type", [RunPython, RunSQL])
def test_raw_operations_are_rare_enough_to_read(operation_type):
    """Not a rule, a tripwire on volume.

    Every RunSQL here is a trigger or a grant, and every RunPython is a
    backfill. If either count climbs, the migrations have started doing
    something this project does not do, and somebody should look rather than
    find out on a deploy.
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    count = sum(
        1
        for (app_label, _name), migration in loader.disk_migrations.items()
        if app_label in OURS
        for operation in migration.operations
        if isinstance(operation, operation_type)
    )

    assert count <= 40, f"{count} {operation_type.__name__} operations is more than expected."
