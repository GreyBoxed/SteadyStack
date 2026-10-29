"""Fixtures every app's tests share.

A transactional test (`django_db(transaction=True)`, or one that asks for
`transactional_db` or `live_server`) ends by flushing every table -- and the
chart of accounts is data, installed by a migration, so it goes too. The next
transactional test to post an entry would then fail at commit on a missing
account. pytest-django runs these tests last, which hid it; ordering is not a
guarantee. The chart is put back before each such test, and only those: an
ordinary test runs inside a transaction that is rolled back, never flushed.
"""

import pytest


def _is_transactional(request) -> bool:
    marker = request.node.get_closest_marker("django_db")
    if marker is not None and marker.kwargs.get("transaction"):
        return True
    return bool({"transactional_db", "live_server"} & set(request.fixturenames))


@pytest.fixture(autouse=True)
def _chart_of_accounts(request):
    if not _is_transactional(request):
        return
    request.getfixturevalue("transactional_db")

    from journal.accounts import CHART
    from journal.models import Account

    for code, name, kind in CHART:
        Account.objects.update_or_create(code=code, defaults={"name": name, "type": kind})


@pytest.fixture
def live_book_open(db):
    """The live book's opening entry, which switching to live requires (B1).

    Nought cash, dated well before any test's trading day: the gate asks only
    that the entry exists, and a test that cares about the opening's amounts
    posts its own.
    """
    from datetime import date
    from decimal import Decimal

    from journal.external import open_books

    return open_books(cash=Decimal("0"), on=date(2026, 1, 1), actor="operator")


@pytest.fixture
def already_live(db, monkeypatch):
    """Put the system in live mode from the registered address, however late
    in the day: a function writing the next configuration version directly.

    For tests of what a live day refuses once it *is* live. Switching through
    `configuration.store.update` is gated on the day not having been planned
    in paper (B1), and those tests plan the day before they switch.
    """
    from configuration.models import ConfigurationVersion, OrderMode
    from configuration.store import MUTABLE_FIELDS, current

    def switch(registered="203.0.113.7"):
        monkeypatch.setattr("broker.network.outbound_ip", lambda **_: registered)
        previous = current()
        fields = {field: getattr(previous, field) for field in MUTABLE_FIELDS}
        fields.update(order_mode=OrderMode.LIVE, registered_static_ip=registered)
        return ConfigurationVersion.objects.create(
            version=previous.version + 1, changed_by="operator", **fields
        )

    return switch


@pytest.fixture
def runtime_role(db):
    """Run the test body as `steadystack_app`, with the box's privileges (M14).

    The suite connects as the schema owner -- locally a superuser -- so an
    UPDATE on an append-only table, or a row lock on one, passes here and
    fails on the box. This grants what bootstrap's default privileges give
    the runtime role on every table and sequence, applies the migrations'
    own REVOKEs on top, then `SET ROLE steadystack_app` until the test ends.
    All inside the test's transaction, which is rolled back with it.

    Skipped where the role does not exist, or the owner may not become it.
    """
    from django.db import DatabaseError, connection, transaction

    from config.runtime_role import RUNTIME_ROLE, migration_revokes

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [RUNTIME_ROLE])
        if cursor.fetchone() is None:
            pytest.skip(f"no {RUNTIME_ROLE} role in this database cluster")
        cursor.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
        cursor.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}"
        )
        cursor.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}")
        for block in migration_revokes():
            try:
                with transaction.atomic():
                    cursor.execute(block)
            except DatabaseError as missing:
                # A table a later migration dropped; its revoke went with it.
                if "does not exist" not in str(missing):
                    raise
        try:
            with transaction.atomic():
                cursor.execute(f"SET ROLE {RUNTIME_ROLE}")
        except DatabaseError as refused:
            pytest.skip(f"cannot become {RUNTIME_ROLE}: {refused}")
        # A SET inside a savepoint that committed stays in force.
        cursor.execute("SELECT current_user")
        assert cursor.fetchone()[0] == RUNTIME_ROLE
    yield RUNTIME_ROLE
    try:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
    except DatabaseError:
        # An aborted transaction: its rollback resets the role anyway.
        pass
