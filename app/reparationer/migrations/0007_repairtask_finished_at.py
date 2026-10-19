from django.db import migrations, models
from django.db.models import F


def backfill_finished_at(apps, schema_editor):  # noqa: ANN001, ANN201
    """Tickets already in Færdig get their last change as the moment they got there.

    That is exactly what the archive sweep counted from before this field existed, so no ticket's
    archive date moves on deploy — the fix only stops the clock restarting from here on.
    """
    RepairTask = apps.get_model("reparationer", "RepairTask")
    RepairTask.objects.filter(status="faerdig").update(finished_at=F("updated_at"))


class Migration(migrations.Migration):
    dependencies = [
        ("reparationer", "0006_repairtask_archived_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="repairtask",
            name="finished_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(backfill_finished_at, migrations.RunPython.noop),
    ]
