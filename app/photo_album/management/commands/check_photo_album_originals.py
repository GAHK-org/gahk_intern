from collections import defaultdict
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from photo_album.models import Album, Media, validate_folder
from photo_album.services import _delete_files
from photo_album.storage import get_photo_album_storage


class Command(BaseCommand):
    help = (
        "Find media whose original is missing from storage, one HEAD request each. With --delete, "
        "remove them, and the albums that leaves with nothing in them. Such an item cannot be shown, "
        "converted or downloaded, and is not on its way back: an import can send it again once it "
        "is gone (the Dropbox script with --retry-failed), not while it is in the way."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--year", help="Only albums in this folder: a year, or Andet.")
        parser.add_argument(
            "--delete", action="store_true", help="Remove what is found. Without it, only list."
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        media = Media.objects.select_related("album").order_by("album__path", "pk")
        if year := options["year"]:
            try:
                validate_folder(year)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from exc
            media = media.filter(album__folder=year)
        storage = get_photo_album_storage()
        missing: dict[Album, list[Media]] = defaultdict(list)
        checked = 0
        for item in media.iterator():
            checked += 1
            if checked % 500 == 0:
                self.stdout.write(f"… {checked} checked")
            if not item.original.name or not storage.exists(item.original.name):
                missing[item.album].append(item)

        for album, items in missing.items():
            self.stdout.write(f"{album.folder}: {album} — {len(items)} without an original")
            for item in items:
                self.stdout.write(f"    {item.pk}: {item.display_name}")
        found = sum(len(items) for items in missing.values())
        self.stdout.write(f"{found} of {checked} media item(s) have no original.")
        if not options["delete"] or not found:
            if found:
                self.stdout.write("Nothing removed; run again with --delete to remove them.")
            return

        with transaction.atomic():
            for items in missing.values():
                for item in items:
                    _delete_files(item)  # the variants that are there; the original is already gone
                    item.delete()
            removed = self._remove_empty(set(missing))
        self.stdout.write(
            f"Removed {found} media item(s) and {len(removed)} album(s) left empty"
            + (f": {', '.join(removed)}." if removed else ".")
        )

    def _remove_empty(self, albums: set[Album]) -> list[str]:
        """Delete those of `albums` that now hold nothing — no media, binned or not, and no
        sub-albums — and then their parents on the same terms, deepest first."""
        removed: list[str] = []
        pending = sorted(albums, key=lambda album: -album.depth)
        while pending:
            album = pending.pop(0)
            if Media.objects.filter(album=album).exists() or album.children.exists():
                continue
            parent = album.parent
            removed.append(f"{album.folder}: {album}")
            album.delete()
            if parent is not None and parent not in pending:
                pending.append(parent)
                pending.sort(key=lambda candidate: -candidate.depth)
        return removed
