"""End every session that existed before the intranet moved to intern.gahk.dk.

Those sessions were issued under a ".gahk.dk" cookie, which browsers go on sending to gahk.dk — now
separate, less trusted webhosting — until they next visit intern.gahk.dk. Renaming the cookie does
not end them: the key is still a valid session on this database, so anyone who read it on the
webhost could present it under the new name and be logged in. Deleting them makes the move's
"everyone logs in once more" true. It runs when this version is deployed, so it also covers the
sessions the old version issued between the DNS switch and the deploy.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def end_every_session(apps: Apps, _schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("sessions", "Session").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_remove_admin_dummy_notification"),
        ("sessions", "0001_initial"),
    ]

    operations = [migrations.RunPython(end_every_session, migrations.RunPython.noop)]
