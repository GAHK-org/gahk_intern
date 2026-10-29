"""A view becomes a viewing: from opening a video in the player to what came of it.

Every view counted so far was opened and counted at the same moment, as far as anyone knows; how
long it played and whether it reached the end were not kept, so those stay 0 and false.
"""

import django.utils.timezone
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import F


def open_when_viewed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("gahkflix", "ViewEvent").objects.update(opened_at=F("viewed_at"), active_at=F("viewed_at"))


def drop_uncounted(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Going back: a viewing that never counted as a view was not a row before."""
    apps.get_model("gahkflix", "ViewEvent").objects.filter(viewed_at__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("gahkflix", "0006_videoevent"),
    ]

    operations = [
        migrations.AddField(
            model_name="viewevent",
            name="opened_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="viewevent",
            name="active_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="viewevent",
            name="watched",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="viewevent",
            name="finished",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(open_when_viewed, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="viewevent",
            name="viewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(migrations.RunPython.noop, drop_uncounted),
    ]
