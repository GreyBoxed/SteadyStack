"""`runserver`, with the state of the installation printed on the way up.

The questions a person has when they start the server -- is the database
there, is there a Kite session, is this paper, what time will it act -- are
answered before they can ask. Discovering at 09:46 that the session expired
is a trading day lost to something a line of output would have prevented.

Django's own runserver is subclassed rather than replaced, so every flag it
takes still works.
"""

import os

from django.contrib.staticfiles.management.commands.runserver import (
    Command as StaticfilesRunserver,
)

from web import preflight
from web.management.commands.preflight import render


class Command(StaticfilesRunserver):
    def inner_run(self, *args, **options) -> None:
        # The autoreloader runs this twice: once in the parent to spawn, once
        # in the child that serves. Only the child sets RUN_MAIN, so the
        # banner prints once rather than every reload.
        if os.environ.get("RUN_MAIN") or not options.get("use_reloader", True):
            self._report()
        super().inner_run(*args, **options)

    def _report(self) -> None:
        try:
            checks = preflight.run()
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"preflight could not run: {exc}"))
            return

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("This installation"))
        self.stdout.write(render(checks, indent="  "))

        blocking = len(preflight.blockers(checks))
        warned = len(preflight.warnings(checks))

        if blocking:
            self.stdout.write("")
            self.stdout.write(
                self.style.ERROR(
                    f"  ! {blocking} thing(s) above will stop a trading day. The "
                    f"server still starts -- reading the app needs none of them."
                )
            )
        elif warned:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(f"  ~ {warned} thing(s) worth knowing, none fatal.")
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Running a day"))
        for line in preflight.next_steps(checks, base_url=self._base_url()):
            self.stdout.write(f"  {line}")
        self.stdout.write("")

    def _base_url(self) -> str:
        """The address this server actually bound to, not a guess at it."""
        host = getattr(self, "addr", "") or "127.0.0.1"
        if host in ("0", "0.0.0.0"):  # noqa: S104 -- displayed, not bound here
            host = "127.0.0.1"
        if ":" in host:  # IPv6
            host = f"[{host}]"
        return f"http://{host}:{getattr(self, 'port', '8000')}"
