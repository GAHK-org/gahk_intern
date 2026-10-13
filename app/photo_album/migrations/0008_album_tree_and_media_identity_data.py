"""Turn the old flat "Fest/Lørdag/Aften" album names into a real tree, and fill the new Media columns.

Before sub-albums existed, a ZIP import named its albums after their archive paths, so a single
album name could hold slashes. Each such album becomes the leaf of a chain of albums named after the
parts, reusing (case-insensitively) whichever parents already exist and creating the rest. Names that
would now clash with a sibling get " (2)", " (3)" …; chains deeper than five levels have their tail
joined with " - " into the fifth.

The Media side: a uuid for the new storage-key scheme (existing keys keep working — see
`views.serve_media`), the original filename recovered from the storage key, and `sort_at`. The
SHA-256 of existing originals needs every file read back from storage, which is no job for a
migration — that is the nightly `backfill_media_hashes` job (or `manage.py backfill_photo_album_hashes`).
"""

import re
import unicodedata
import uuid
from pathlib import PurePosixPath

from django.db import migrations
from django.db.models.functions import Coalesce

MAX_DEPTH = 4
NAME_MAX = 140
# Django's get_available_name: "<stem>_<7 random letters/digits><ext>".
DJANGO_SUFFIX = re.compile(r"^(?P<stem>.+)_(?P<suffix>[A-Za-z0-9]{7})(?P<ext>\.[^./]*)?$")


def _clean_part(part: str) -> str:
    part = unicodedata.normalize("NFC", part).replace("\\", "-")
    part = "".join(character for character in part if unicodedata.category(character) != "Cc")
    part = part.strip().rstrip(".").strip()
    return "" if part in {"", ".", ".."} else part[:NAME_MAX]


def _with_suffix(name: str, number: int) -> str:
    suffix = f" ({number})"
    return f"{name[: NAME_MAX - len(suffix)]}{suffix}"


def split_album_names(apps, schema_editor) -> None:  # noqa: ANN001
    Album = apps.get_model("photo_album", "Album")
    taken: dict = {}  # (folder, parent_id, lower(name)) -> Album

    def unique_name(folder: str, parent_id: int | None, name: str) -> str:
        candidate, number = name, 1
        while (folder, parent_id, candidate.lower()) in taken:
            number += 1
            candidate = _with_suffix(name, number)
        return candidate

    def place(album, folder: str, parent, name: str):  # noqa: ANN001, ANN202
        album.folder = folder
        album.parent_id = parent.pk if parent else None
        album.depth = parent.depth + 1 if parent else 0
        album.name = unique_name(folder, album.parent_id, name)
        album.save()
        album.path = f"{parent.path if parent else '/'}{album.pk}/"
        album.save(update_fields=["path"])
        taken[(folder, album.parent_id, album.name.lower())] = album
        return album

    albums = list(Album.objects.order_by("pk"))
    parts_by_pk = {}
    for album in albums:
        parts = [part for part in (_clean_part(raw) for raw in album.name.split("/")) if part]
        if not parts:
            parts = [f"Album {album.pk}"]
        if len(parts) > MAX_DEPTH + 1:
            parts = [*parts[:MAX_DEPTH], " - ".join(parts[MAX_DEPTH:])[:NAME_MAX]]
        parts_by_pk[album.pk] = parts

    # Shallowest first, so an existing "Fest" is in place before "Fest/Lørdag" looks for its parent.
    for album in sorted(albums, key=lambda album: (len(parts_by_pk[album.pk]), album.pk)):
        parts = parts_by_pk[album.pk]
        parent = None
        for part in parts[:-1]:
            parent_id = parent.pk if parent else None
            existing = taken.get((album.folder, parent_id, part.lower()))
            if existing is None:
                existing = place(Album(created_at=album.created_at), album.folder, parent, part)
            parent = existing
        place(album, album.folder, parent, parts[-1])


def fill_media_columns(apps, schema_editor) -> None:  # noqa: ANN001
    Media = apps.get_model("photo_album", "Media")
    rows = list(Media.objects.only("pk", "album_id", "original"))
    basenames_by_album = {}
    for media in rows:
        basenames_by_album.setdefault(media.album_id, set()).add(
            PurePosixPath(media.original.name or "").name
        )

    for media in rows:
        name = PurePosixPath(media.original.name or "").name
        match = DJANGO_SUFFIX.match(name)
        if match:
            suffix = match["suffix"]
            stripped = f"{match['stem']}{match['ext'] or ''}"
            mixed = (any(c.isalpha() for c in suffix) and any(c.isdigit() for c in suffix)) or (
                any(c.isupper() for c in suffix) and any(c.islower() for c in suffix)
            )
            # Only strip what is very likely Django's collision suffix: the unsuffixed name is right
            # there in the same album, or the seven characters look random. A file genuinely named
            # "ferie_holiday.jpg" keeps its name.
            if mixed or stripped in basenames_by_album[media.album_id]:
                name = stripped
        media.uuid = uuid.uuid4()
        media.filename = unicodedata.normalize("NFC", name)[:255]
        media.save(update_fields=["uuid", "filename"])

    Media.objects.update(sort_at=Coalesce("captured_at", "added_at"))


class Migration(migrations.Migration):
    dependencies = [("photo_album", "0007_album_tree_and_media_identity")]

    operations = [
        migrations.RunPython(split_album_names, migrations.RunPython.noop),
        migrations.RunPython(fill_media_columns, migrations.RunPython.noop),
    ]
