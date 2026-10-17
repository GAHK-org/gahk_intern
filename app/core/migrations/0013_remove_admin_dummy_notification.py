"""Remove the temporary `send-admin-dummy-notification` entry from the database scheduler.

It mailed every administrator twice a day to prove Beat and the worker were alive in production;
that is confirmed, so the entry and its task are gone from the code. The DatabaseScheduler only adds
and updates entries from CELERY_BEAT_SCHEDULE, never removes them, so without this the stored row
would go on queueing a task that no longer exists — and fail as unregistered twice a day.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def remove_admin_dummy_notification(apps: Apps, _schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(
        task="core.tasks.send_admin_dummy_notification"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_remove_celery_backend_cleanup"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(remove_admin_dummy_notification, migrations.RunPython.noop)]
