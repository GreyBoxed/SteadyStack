from django.db import migrations

from journal.accounts import CHART


def install(apps, schema_editor):
    Account = apps.get_model("journal", "Account")
    for code, name, type_ in CHART:
        Account.objects.update_or_create(code=code, defaults={"name": name, "type": type_})


class Migration(migrations.Migration):
    dependencies = [("journal", "0001_initial")]

    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
