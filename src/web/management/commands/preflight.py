"""Say what is true about this installation, and fail if it will not work.

Used on its own before a day is run, and by `runserver` on the way up.
"""

from django.core.management.base import BaseCommand

from web import preflight


class Command(BaseCommand):
    help = "Report the state of this installation; exit non-zero if a problem would stop a day."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Print only the problems.",
        )

    def handle(self, *args, **options) -> None:
        checks = preflight.run()
        self.stdout.write(render(checks, self.style, quiet=options["quiet"]))

        if preflight.blockers(checks):
            raise SystemExit(1)


def render(checks, style=None, *, quiet: bool = False, indent: str = "") -> str:
    """The report, as one block of text.

    Shared with `runserver`, which prints the same thing in the same shape --
    two renderings of one truth is one rendering too many.
    """
    lines = []
    width = max((len(check.label) for check in checks), default=0)

    for check in checks:
        if quiet and check.ok:
            continue
        mark = " " if check.ok else ("!" if check.blocking else "~")
        lines.append(f"{indent}{mark} {check.label.ljust(width)}  {check.value}")
        if check.problem:
            lines.append(f"{indent}  {' ' * width}  -> {check.problem}")

    if not lines:
        return f"{indent}nothing to report"
    return "\n".join(lines)
