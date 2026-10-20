"""Where each item's `sort_at` comes from, and which images to read again for a capture date.

`date_source` is filled in from what `sort_at` was already built from, so nothing moves on deploy.
Whether a file time is believable, and the approximations for items without a date, need every
album looked at, which is the nightly `settle_media_dates` job (or `manage.py
settle_photo_album_dates`). So does reading originals again for XMP and IPTC dates: the images
without a capture date are marked here for it.
"""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def fill_date_source(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    media = apps.get_model("photo_album", "Media").objects
    media.filter(captured_at__isnull=False).update(date_source="captured")
    media.filter(captured_at__isnull=True, file_modified_at__isnull=False).update(date_source="file")
    media.filter(captured_at__isnull=True).exclude(content_type__startswith="video/").update(
        capture_date_checked=False
    )


class Migration(migrations.Migration):
    dependencies = [
        ("photo_album", "0015_media_hash_failed_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="media",
            name="capture_date_checked",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="media",
            name="date_source",
            field=models.CharField(
                choices=[
                    ("captured", "Optaget"),
                    ("file", "Filens dato"),
                    ("neighbours", "Anslået ud fra albummet"),
                    ("album_year", "Anslået ud fra albummets år"),
                    ("added", "Uploadet"),
                ],
                default="added",
                max_length=12,
            ),
        ),
        migrations.RunPython(fill_date_source, migrations.RunPython.noop),
    ]
