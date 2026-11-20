"""Arkiv is gone (Drive replaces it, with nothing migrated), so its tables and bookkeeping go too.

Here rather than in the arkiv app, because that app no longer exists to run a migration. The tables
are dropped IF EXISTS: a database created after Arkiv's removal never had them. Files, then folders,
because a file points at its folder.

Not reversible in any useful sense: the rows are gone, so the reverse does nothing.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def forget_arkiv(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Its content types (and with them its permissions and admin log entries), and its rows in
    django_migrations, so a future app that happens to be called arkiv starts from nothing."""
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="arkiv").delete()
    schema_editor.execute("DELETE FROM django_migrations WHERE app = 'arkiv'")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_workerjoblog_progress"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunSQL("DROP TABLE IF EXISTS arkiv_archivefile", migrations.RunSQL.noop),
        migrations.RunSQL("DROP TABLE IF EXISTS arkiv_archivefolder", migrations.RunSQL.noop),
        migrations.RunPython(forget_arkiv, migrations.RunPython.noop),
    ]
