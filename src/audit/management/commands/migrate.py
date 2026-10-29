"""`migrate`, pointed at the connection that is allowed to migrate.

This lives in the audit app because the audit tables are the reason there are
two roles at all: UPDATE and DELETE are revoked on them for the runtime
connection, which is what makes append-only a property of the database rather
than a promise made by the application. A role restricted that way cannot
create tables either, so migrations run as the schema owner.

Without this, the first `manage.py migrate` on a fresh checkout fails with a
permission error that says nothing about roles, and the fix is a flag nobody
knew to pass. Pass `--database` explicitly to override.
"""

from django.conf import settings
from django.core.management.commands.migrate import Command as MigrateCommand

MIGRATION_ALIAS = "migration"


class Command(MigrateCommand):
    def handle(self, *args, **options):
        if options.get("database") == "default" and MIGRATION_ALIAS in settings.DATABASES:
            options["database"] = MIGRATION_ALIAS
            if options.get("verbosity", 1) >= 1:
                self.stdout.write(
                    f"Migrating over the {MIGRATION_ALIAS!r} connection (the schema owner)."
                )
        super().handle(*args, **options)
