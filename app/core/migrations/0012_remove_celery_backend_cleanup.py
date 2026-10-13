"""Remove the `celery.backend_cleanup` entry an earlier Beat stored in the database scheduler.

Beat installs it while result_expires is set, and it deletes every expired result, failed jobs
included. With CELERY_RESULT_EXPIRES = None Beat no longer installs it, but the DatabaseScheduler
keeps the row it already wrote, and that row would go on running the task — which with no expiry
set would fail every night. core.tasks.purge_worker_job_results takes its place.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def remove_backend_cleanup(apps: Apps, _schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(
        task="celery.backend_cleanup"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_worker_job_log"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(remove_backend_cleanup, migrations.RunPython.noop)]
