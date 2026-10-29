"""When the market is open, and for how long.

A session calendar rather than a holiday deny-list. The difference matters
once or twice a year: Muhurat trading is a real trading day with a one-hour
evening session, and a deny-list would say "not a holiday, therefore normal"
and ladder into a closed market all afternoon. A calendar that stores the
session itself cannot make that mistake.

Weekends need no rows. Saturday and Sunday are closed, always, everywhere in
this system's lifetime, and requiring a row for each would mean the calendar
runs out of coverage twice a week.
"""

from datetime import time

from django.db import models


class SessionKind(models.TextChoices):
    CLOSED = "closed", "Closed"
    NORMAL = "normal", "Normal session"
    SPECIAL = "special", "Special session"


#: The ordinary NSE equity session, in IST. A normal day needs no times of its
#: own; only a special session carries them.
NORMAL_OPEN = time(9, 15)
NORMAL_CLOSE = time(15, 30)


class SessionDay(models.Model):
    """One dated exception to the ordinary week.

    Only days that differ from the default need a row: a holiday, or a special
    session. An ordinary weekday with no row is a normal trading day, which is
    what keeps the table small enough to read.

    One row, two questions: does it trade, and does it settle (M18b, D18).
    They genuinely differ -- the exchange publishes settlement holidays on
    which trading is ordinary and clearing is shut -- and a fourth session
    kind could not express that, because such a day is a normal session in
    every way that the ladder cares about.
    """

    trading_date = models.DateField(primary_key=True)
    kind = models.CharField(max_length=8, choices=SessionKind.choices)

    settles = models.BooleanField(
        default=True,
        help_text=(
            "Whether clearing runs on this date. A settlement holiday trades "
            "normally and settles nothing; a closed day does neither (M18b)."
        ),
    )

    open_time = models.TimeField(null=True, blank=True, help_text="Set only for a special session.")
    close_time = models.TimeField(null=True, blank=True)

    note = models.CharField(max_length=255, blank=True)
    loaded_at = models.DateTimeField(auto_now_add=True)
    loaded_by = models.CharField(max_length=150, default="system")

    class Meta:
        ordering = ["trading_date"]
        indexes = [models.Index(fields=["kind"])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind="special", open_time__isnull=False, close_time__isnull=False)
                    | ~models.Q(kind="special")
                ),
                name="a_special_session_states_its_hours",
            ),
            models.CheckConstraint(
                # A day the market never opened cannot clear trades nobody
                # made. Stored rather than inferred so the two questions read
                # side by side, but they are not independent in this one case.
                condition=models.Q(kind="closed", settles=False) | ~models.Q(kind="closed"),
                name="a_closed_day_settles_nothing",
            ),
        ]

    def __str__(self) -> str:
        if self.kind == SessionKind.SPECIAL:
            return f"{self.trading_date} special {self.open_time:%H:%M}-{self.close_time:%H:%M}"
        return f"{self.trading_date} {self.kind}"
