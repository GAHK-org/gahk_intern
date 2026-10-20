"""Where the file times stored so far came from, as far as the database can tell.

A file stored by an import, into one of its albums while it ran, got its time from the ZIP; any
other file time came with a browser upload. A file with a sidecar is left blank: whether its time
is the sidecar's takes reading the sidecar, which the nightly tasks.settle_media_dates does.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def fill(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Media = apps.get_model("photo_album", "Media")
    AlbumImport = apps.get_model("photo_album", "AlbumImport")
    unknown = Media.objects.filter(file_modified_at__isnull=False, file_time_source="", sidecar="")
    for album_import in AlbumImport.objects.filter(completed_at__isnull=False).exclude(album_ids=[]):
        unknown.filter(
            album_id__in=album_import.album_ids,
            added_at__gte=album_import.created_at,
            added_at__lte=album_import.completed_at,
        ).update(file_time_source="archive")
    unknown.update(file_time_source="upload")


class Migration(migrations.Migration):
    dependencies = [("photo_album", "0019_media_file_time_source_storagereport")]

    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
