"""Delete a whole folder, album or sub-album from the photo album — locked or not.

For clearing out a botched import or test data by hand on the server. The web UI only deletes
empty, unlocked albums one at a time; this deletes a whole tree with its media (binned ones too),
their stored files, and every download ZIP that holds any of them. Two guards make up for skipping
the lock rules: it lists what it is about to delete and wants a random code typed back, and it
refuses outright when anything in scope was created or uploaded on or after the protection date —
those are the albums people have used for real.
"""

import secrets
import shutil
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files.storage import Storage
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from storages.backends.s3 import S3Storage
from storages.utils import clean_name

from photo_album.cache import HIGH_DEFINITION, THUMBNAIL
from photo_album.models import Album, AlbumDownload, Media, normalize_name
from photo_album.storage import get_photo_album_storage

# Nothing created or uploaded on or after this date is deleted here. PHOTO_ALBUM_DELETE_PROTECTED_FROM
# may move it, but never into the future.
PROTECTED_FROM = date(2026, 10, 1)
# No 0/O or 1/I, so the code cannot be misread.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


@dataclass(frozen=True)
class Plan:
    album_ids: frozenset[int]
    media_ids: frozenset[int]
    download_ids: frozenset[int]


def protected_from() -> date:
    configured = settings.PHOTO_ALBUM_DELETE_PROTECTED_FROM.strip()
    if not configured:
        return PROTECTED_FROM
    try:
        chosen = date.fromisoformat(configured)
    except ValueError:
        raise CommandError(
            f"PHOTO_ALBUM_DELETE_PROTECTED_FROM={configured!r} is not a date (YYYY-MM-DD)."
        ) from None
    if chosen > timezone.localdate():
        raise CommandError(
            f"PHOTO_ALBUM_DELETE_PROTECTED_FROM={configured} is in the future; it must be today or earlier."
        )
    return chosen


def _plan(albums: "QuerySet[Album]") -> Plan:
    album_ids = frozenset(albums.values_list("pk", flat=True))
    media_ids = frozenset(Media.objects.filter(album_id__in=album_ids).values_list("pk", flat=True))
    # A timeline selection or a parent album's download can hold these media too; its ZIP goes as well.
    download_ids = frozenset(
        download.pk
        for download in AlbumDownload.objects.only("pk", "album_id", "media_ids")
        if download.album_id in album_ids or media_ids.intersection(download.media_ids or [])
    )
    return Plan(album_ids, media_ids, download_ids)


def _keys_under(storage: Storage, prefix: str) -> list[str]:
    """Every stored key below `prefix`, however deep — what is left behind as well as what a row names."""
    if isinstance(storage, S3Storage):
        # One flat listing; walking it folder by folder would be a request per media item and variant.
        location = clean_name(storage.location).strip("/")
        full_prefix = storage._normalize_name(clean_name(prefix)).rstrip("/") + "/"
        return [
            obj.key[len(location) + 1 :] if location else obj.key
            for obj in storage.bucket.objects.filter(Prefix=full_prefix)
        ]
    try:
        directories, files = storage.listdir(prefix)
    except FileNotFoundError:
        return []
    keys = [f"{prefix}/{name}" for name in files]
    for directory in directories:
        keys.extend(_keys_under(storage, f"{prefix}/{directory}"))
    return keys


class Command(BaseCommand):
    help = (
        "Delete a whole folder, or an album with all its sub-albums, including every media item and "
        "stored file — ignoring locks. Refuses anything created or uploaded on or after "
        f"{PROTECTED_FROM.isoformat()} (or PHOTO_ALBUM_DELETE_PROTECTED_FROM), and asks for a typed "
        "confirmation code."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--folder", help="Delete every album in this folder, e.g. 2019 or 'Uden årstal'.")
        scope.add_argument(
            "--album", type=int, help="Delete this album (root or sub-album) and its sub-albums."
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        cutoff_day = protected_from()
        description, albums = self._scope(options)
        plan = _plan(albums)
        self._refuse_protected(plan, cutoff_day)
        self._summarise(description, plan, cutoff_day)

        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        try:
            answer = input(f"Are you sure? This cannot be undone. Type {code} to delete: ")
        except EOFError:
            answer = ""
        if answer.strip() != code:
            raise CommandError("The code did not match. Nothing was deleted.")

        storage = get_photo_album_storage()
        with transaction.atomic():
            # Locking the rows also holds off uploads into them: inserting a media row needs a share
            # lock on its album. Whatever arrived while the prompt waited stops the delete.
            list(Album.objects.select_for_update().filter(pk__in=plan.album_ids).values_list("pk", flat=True))
            if _plan(albums) != plan:
                raise CommandError(
                    "The albums changed while waiting for the code. Nothing was deleted; run it again."
                )
            stored = [
                name
                for names in Media.objects.filter(pk__in=plan.media_ids).values_list(
                    "original", "high_definition", "thumbnail", "sidecar"
                )
                for name in names
                if name
            ]
            archives = list(
                AlbumDownload.objects.filter(pk__in=plan.download_ids)
                .exclude(archive_key="")
                .values_list("archive_key", flat=True)
            )
            AlbumDownload.objects.filter(pk__in=plan.download_ids).delete()
            Media.objects.filter(pk__in=plan.media_ids).delete()
            # Deepest first: a parent is protected while it still has children.
            depths = Album.objects.filter(pk__in=plan.album_ids).values_list("depth", flat=True).distinct()
            for depth in sorted(depths, reverse=True):
                Album.objects.filter(pk__in=plan.album_ids, depth=depth).delete()

        prefixes = [f"photo-album/{album_id}" for album_id in sorted(plan.album_ids)]
        keys = {*stored, *archives}
        for prefix in prefixes:
            keys.update(_keys_under(storage, prefix))
        failed: list[str] = []
        for key in sorted(keys):
            try:
                storage.delete(key)
            except Exception as exc:  # every key gets its chance; failures are reported below
                failed.append(f"{key}: {exc}")
        self._clear_cache(plan)

        self.stdout.write(
            f"Deleted {len(plan.album_ids)} album(s), {len(plan.media_ids)} media item(s) and "
            f"{len(plan.download_ids)} download(s); removed {len(keys) - len(failed)} stored file(s)."
        )
        left = sorted(
            {
                *(key for prefix in prefixes for key in _keys_under(storage, prefix)),
                *filter(storage.exists, archives),
            }
        )
        if failed or left:
            for line in failed:
                self.stderr.write(f"Could not remove {line}")
            for key in left:
                self.stderr.write(f"Still stored: {key}")
            raise CommandError(
                "The albums are gone from the database, but stored files remain (listed above). "
                "Remove them from the bucket by hand."
            )

    def _scope(self, options: dict[str, Any]) -> "tuple[str, QuerySet[Album]]":
        if options["album"] is not None:
            album = Album.objects.filter(pk=options["album"]).first()
            if album is None:
                raise CommandError(f"There is no album {options['album']}.")
            return f"album {album.pk} “{album.name}” with its sub-albums", album.subtree()
        folder = normalize_name(options["folder"])
        albums = Album.objects.filter(folder=folder)
        if not albums.exists():
            raise CommandError(f"There are no albums in folder {folder!r}.")
        return f"folder {folder}", albums

    def _refuse_protected(self, plan: Plan, cutoff_day: date) -> None:
        cutoff = timezone.make_aware(datetime.combine(cutoff_day, time.min))
        new_albums = Album.objects.filter(pk__in=plan.album_ids, created_at__gte=cutoff).order_by("path")
        new_media = Media.objects.filter(pk__in=plan.media_ids, added_at__gte=cutoff)
        if not new_albums.exists() and not new_media.exists():
            return
        for album in new_albums[:20]:
            self.stderr.write(
                f"  album {album.pk} “{album.name}” created {timezone.localtime(album.created_at):%Y-%m-%d}"
            )
        if count := new_media.count():
            self.stderr.write(f"  {count} media item(s) uploaded on or after {cutoff_day.isoformat()}")
        raise CommandError(
            f"Refusing: this includes albums or media from {cutoff_day.isoformat()} or later (listed above). "
            "Nothing was deleted."
        )

    def _summarise(self, description: str, plan: Plan, cutoff_day: date) -> None:
        albums = list(Album.objects.filter(pk__in=plan.album_ids).order_by("folder", "path"))
        top = min(album.depth for album in albums)
        binned = Media.objects.filter(pk__in=plan.media_ids, deleted_at__isnull=False).count()
        self.stdout.write(f"About to delete {description} for good. Locks are ignored.")
        self.stdout.write(f"  {len(albums)} album(s):")
        for album in albums:
            self.stdout.write(
                f"    {'  ' * (album.depth - top)}{album.name} (id {album.pk}, folder {album.folder})"
            )
        self.stdout.write(f"  {len(plan.media_ids)} media item(s), {binned} of them in the bin")
        self.stdout.write(f"  {len(plan.download_ids)} download ZIP(s) that hold any of them")
        self.stdout.write("  every stored file under photo-album/<album id>/ for these albums")
        self.stdout.write(f"Nothing in it was created or uploaded on or after {cutoff_day.isoformat()}.")
        self.stdout.flush()

    def _clear_cache(self, plan: Plan) -> None:
        if not settings.PHOTO_ALBUM_CACHE_DIR:
            return
        for variant in (THUMBNAIL, HIGH_DEFINITION):
            for album_id in plan.album_ids:
                shutil.rmtree(
                    Path(settings.PHOTO_ALBUM_CACHE_DIR) / variant / "photo-album" / str(album_id),
                    ignore_errors=True,
                )
