"""The film's own date becomes its release date, known to the day, the month or only the year.

`recorded_on` is renamed `released_on`, every date there is kept to the day, and `release_source`
says where it came from in place of the old "set by hand" flag. A guessed date is put down to the
live file's metadata when the file has a recording time, to its last-modified time when it has
only that. Proposals waiting for approval keep their proposed date under its new name.
"""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def fill_release_source(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Video = apps.get_model("gahkflix", "Video")
    for video in Video.objects.filter(released_on__isnull=False).select_related("file"):
        if video.recorded_on_manual:
            source = "manual"
        elif video.file is not None and video.file.recorded_at is None and video.file.file_modified_at:
            source = "modified"
        else:
            source = "metadata"
        Video.objects.filter(pk=video.pk).update(release_source=source)
    _rename_proposed(apps, "recorded_on", "released_on")


def restore_manual_flag(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("gahkflix", "Video").objects.filter(release_source="manual").update(
        recorded_on_manual=True
    )
    _rename_proposed(apps, "released_on", "recorded_on")


def _rename_proposed(apps: Apps, old: str, new: str) -> None:
    ChangeRequest = apps.get_model("gahkflix", "ChangeRequest")
    for change in ChangeRequest.objects.all():
        if old in change.changes:
            change.changes = {new if key == old else key: value for key, value in change.changes.items()}
            change.save(update_fields=["changes"])


class Migration(migrations.Migration):
    dependencies = [
        ("gahkflix", "0004_subtitles_and_claiming_job"),
    ]

    operations = [
        migrations.RenameField(model_name="video", old_name="recorded_on", new_name="released_on"),
        migrations.AlterField(
            model_name="video",
            name="released_on",
            field=models.DateField(blank=True, null=True, verbose_name="Udgivelsesdato"),
        ),
        migrations.AddField(
            model_name="video",
            name="release_precision",
            field=models.CharField(
                choices=[("day", "Dag"), ("month", "Måned"), ("year", "År")], default="day", max_length=5
            ),
        ),
        migrations.AddField(
            model_name="video",
            name="release_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("manual", "Sat i hånden"),
                    ("name", "Filnavnet"),
                    ("metadata", "Videofilens metadata"),
                    ("modified", "Filens ændringsdato"),
                ],
                max_length=8,
            ),
        ),
        migrations.RunPython(fill_release_source, restore_manual_flag),
        migrations.RemoveField(model_name="video", name="recorded_on_manual"),
    ]
