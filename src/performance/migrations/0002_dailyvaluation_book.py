"""Key the value series by book (M13).

The paper book's evenings before go-live and the live book's after are two
portfolios; keyed by date alone they read as one that jumped on the day of
the switch. Each row gains `book`, and a date may carry one row per book, so
the date stops being the primary key and the row's existing `id` becomes it.

Rows already written are filed under the order mode in force when this runs
-- paper, unless the configuration already says live -- since that is the
book `snapshot_valuation` valued them in.

Reversible while no date carries two rows: the date becomes the key again.
"""

import uuid

from django.db import migrations, models

import configuration.store


def file_existing_evenings(apps, schema_editor):
    alias = schema_editor.connection.alias
    config = apps.get_model("configuration", "ConfigurationVersion")
    latest = config.objects.using(alias).order_by("-version").first()
    book = latest.order_mode if latest is not None else "paper"
    apps.get_model("performance", "DailyValuation").objects.using(alias).update(book=book)


TABLE = "performance_dailyvaluation"

KEY_BY_ID = f"""
ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_pkey;
ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_id_key;
ALTER TABLE {TABLE} ADD CONSTRAINT {TABLE}_pkey PRIMARY KEY (id);
CREATE INDEX {TABLE}_trading_date_idx ON {TABLE} (trading_date);
"""

KEY_BY_DATE = f"""
DROP INDEX {TABLE}_trading_date_idx;
ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_pkey;
ALTER TABLE {TABLE} ADD CONSTRAINT {TABLE}_id_key UNIQUE (id);
ALTER TABLE {TABLE} ADD CONSTRAINT {TABLE}_pkey PRIMARY KEY (trading_date);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("configuration", "0008_placing_rounds"),
        ("performance", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="dailyvaluation",
            name="book",
            field=models.CharField(
                choices=[("live", "Live"), ("paper", "Paper")],
                default="paper",
                help_text="The book whose lots were valued: live or paper.",
                max_length=8,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(file_existing_evenings, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="dailyvaluation",
            name="book",
            field=models.CharField(
                choices=[("live", "Live"), ("paper", "Paper")],
                default=configuration.store.order_mode_now,
                help_text="The book whose lots were valued: live or paper.",
                max_length=8,
            ),
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(KEY_BY_ID, KEY_BY_DATE)],
            state_operations=[
                migrations.AlterField(
                    model_name="dailyvaluation",
                    name="trading_date",
                    field=models.DateField(db_index=True),
                ),
                migrations.AlterField(
                    model_name="dailyvaluation",
                    name="id",
                    field=models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="dailyvaluation",
            constraint=models.UniqueConstraint(
                fields=("trading_date", "book"), name="one_valuation_per_day_and_book"
            ),
        ),
    ]
