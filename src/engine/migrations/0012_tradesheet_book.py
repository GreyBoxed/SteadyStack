"""Store the book a day was planned in on its tradesheet (M1).

A day's budget is allocated, and its ledger row written, in the book of the
mode that planned it. Read from the configuration at settle time instead, a
paper day settled after the switch to live wrote a live row.

Sheets already written are filed under the mode their orders were placed in,
and a sheet with no order under the order mode in force when this runs --
paper, unless the configuration already says live.
"""

from django.db import migrations, models

import configuration.store


def file_existing_sheets(apps, schema_editor):
    alias = schema_editor.connection.alias
    config = apps.get_model("configuration", "ConfigurationVersion")
    latest = config.objects.using(alias).order_by("-version").first()
    default = latest.order_mode if latest is not None else "paper"

    sheets = apps.get_model("engine", "TradeSheet").objects.using(alias)
    orders = apps.get_model("orders", "Order").objects.using(alias)
    sheets.update(book=default)
    for book in ("paper", "live"):
        placed = orders.filter(mode=book).values_list("tradesheet_id", flat=True)
        sheets.filter(pk__in=placed).update(book=book)


class Migration(migrations.Migration):
    dependencies = [
        ("configuration", "0008_placing_rounds"),
        ("engine", "0011_cash_ledger_from_journal"),
        ("orders", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="tradesheet",
            name="book",
            field=models.CharField(
                choices=[("live", "Live"), ("paper", "Paper")],
                default="paper",
                help_text=(
                    "The book the day was planned and funded in, stored when it is planned: "
                    "its ledger row is that book's, whatever the mode is when it settles (M1)."
                ),
                max_length=8,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(file_existing_sheets, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="tradesheet",
            name="book",
            field=models.CharField(
                choices=[("live", "Live"), ("paper", "Paper")],
                default=configuration.store.order_mode_now,
                help_text=(
                    "The book the day was planned and funded in, stored when it is planned: "
                    "its ledger row is that book's, whatever the mode is when it settles (M1)."
                ),
                max_length=8,
            ),
        ),
    ]
