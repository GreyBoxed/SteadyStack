"""Settlement, tracked beside trading on the same row (M18b, D18).

One dated record, two questions: does it trade, does it settle. The exchange
publishes settlement holidays on which trading is ordinary and clearing is
shut, so a fourth session kind could not say it -- such a day is a normal
session in every way the ladder cares about, and differs only in when the
shares arrive.

Rows already loaded are trading rows, so they settle by default. The one
exception is a closed day, which has no trades to clear and is backfilled
before the constraint that says so is applied.
"""

from django.db import migrations, models


def closed_days_settle_nothing(apps, schema_editor):
    # Through the migration's own connection. The default alias is the
    # restricted runtime role on a second connection, which waits for the
    # schema owner's open transaction on this table for ever.
    SessionDay = apps.get_model("trading_calendar", "SessionDay")
    alias = schema_editor.connection.alias
    SessionDay.objects.using(alias).filter(kind="closed").update(settles=False)


def settle_again(apps, schema_editor):
    """Reversing leaves every row settling, which is the field's default and
    the state before it existed."""
    SessionDay = apps.get_model("trading_calendar", "SessionDay")
    SessionDay.objects.using(schema_editor.connection.alias).update(settles=True)


class Migration(migrations.Migration):
    dependencies = [("trading_calendar", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="sessionday",
            name="settles",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Whether clearing runs on this date. A settlement holiday trades "
                    "normally and settles nothing; a closed day does neither (M18b)."
                ),
            ),
        ),
        migrations.RunPython(closed_days_settle_nothing, settle_again),
        migrations.AddConstraint(
            model_name="sessionday",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "closed"), ("settles", False)),
                    models.Q(("kind", "closed"), _negated=True),
                    _connector="OR",
                ),
                name="a_closed_day_settles_nothing",
            ),
        ),
    ]
