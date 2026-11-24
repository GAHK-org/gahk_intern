"""The release date the file suggests is kept on the file and only offered ("Brug videoens dato"):
the release date itself is filled in by hand.

A video dated by a guess so far keeps that date, and its file gets the guess, so the button offers
it too. Other files have no guess: the tags it came from were not kept.
"""

from datetime import date

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

GUESSED = ("name", "metadata", "modified")


def iso(released_on: date, precision: str) -> str:
    if precision == "year":
        return f"{released_on.year:04d}"
    if precision == "month":
        return f"{released_on.year:04d}-{released_on.month:02d}"
    return released_on.isoformat()


def guesses_to_files(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Video = apps.get_model("gahkflix", "Video")
    VideoFile = apps.get_model("gahkflix", "VideoFile")
    for video in Video.objects.filter(
        release_source__in=GUESSED, released_on__isnull=False, file__isnull=False
    ).only("released_on", "release_precision", "release_source", "file_id"):
        VideoFile.objects.filter(pk=video.file_id).update(
            date_guess=iso(video.released_on, video.release_precision),
            date_guess_source=video.release_source,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("gahkflix", "0009_unpublish"),
    ]

    operations = [
        migrations.AddField(
            model_name="videofile",
            name="date_guess",
            field=models.CharField(blank=True, max_length=10),
        ),
        migrations.AddField(
            model_name="videofile",
            name="date_guess_source",
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
        migrations.RunPython(guesses_to_files, migrations.RunPython.noop),
    ]
