"""Copy everything under MEDIA_ROOT into the media bucket, preserving names exactly.

The point of "exactly" is that there is no database migration in this move. Every FileField stores a
*name* relative to the storage root (`opslag/2026/09/x.jpg`), never a URL and never an absolute path,
so if the object lands under the same name the existing rows resolve against the bucket with nothing
rewritten. Get a name wrong here and the row silently points at nothing.

Idempotent, and deliberately so rather than as a nicety: this runs against a live site, so it has to
be safe to interrupt, re-run, and run once more after the cutover to catch whatever was uploaded in
between. A file already present with the same size and content hash is skipped.

Run it with --dry-run first. It never deletes anything — neither locally nor remotely — so the worst
case is wasted upload, but the count is worth eyeballing against `find app/media -type f | wc -l`
before committing to it.
"""

import argparse
import hashlib
from pathlib import Path

from django.conf import settings
from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError

from core.storage import MediaS3Storage


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 — matching S3's ETag, not authenticating anything
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Command(BaseCommand):
    help = "Upload MEDIA_ROOT into the media bucket, keeping every FileField name unchanged."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        storage = storages["default"]
        if not isinstance(storage, MediaS3Storage):
            # Without this the command would cheerfully copy MEDIA_ROOT onto itself.
            raise CommandError(
                "STORAGES['default'] is not the object-storage backend — set S3_BUCKET (and the "
                "S3_ACCESS_KEY / S3_SECRET_KEY that go with it) before running this."
            )

        root = Path(settings.MEDIA_ROOT)
        if not root.is_dir():
            raise CommandError(f"MEDIA_ROOT does not exist: {root}")

        dry = bool(opts["dry_run"])
        uploaded = skipped = 0
        # Sorted so two runs produce comparable logs, and so an interrupted run resumes predictably.
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            # as_posix(): a Windows dev box would otherwise produce backslash names, which are a
            # legal S3 key and would land beside — not on — the real object.
            name = path.relative_to(root).as_posix()

            if self._already_there(storage, name, path):
                skipped += 1
                continue

            if dry:
                self.stdout.write(f"[dry-run] would upload {name} ({path.stat().st_size} B)")
            else:
                with path.open("rb") as fh:
                    # _save, not save(): save() would route through get_available_name and, with
                    # file_overwrite=False, quietly suffix a name that must not change.
                    storage._save(name, fh)
            uploaded += 1

        prefix = "[dry-run] " if dry else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Media: {uploaded} file(s) {'to upload' if dry else 'uploaded'}, "
                f"{skipped} already present and identical."
            )
        )
        if not dry and uploaded:
            self.stdout.write("Run `manage.py audit_media` to confirm nothing is missing.")

    def _already_there(self, storage: MediaS3Storage, name: str, path: Path) -> bool:
        """Same name, same size, same bytes.

        Size alone would skip a file that was replaced in place; the ETag is S3's MD5 for a
        single-part upload, and every upload here is single-part because every feature caps its
        images at 5 MB (see the *_MAX_MB settings), well under the multipart threshold. An ETag with
        a `-` in it is a multipart object and cannot be compared this way, so those fall back to
        size.
        """
        try:
            obj = storage.bucket.Object(storage._normalize_name(name))
            remote_size = obj.content_length
            etag = obj.e_tag.strip('"')
        except Exception:  # botocore raises ClientError(404) for a key that is simply not there yet
            return False

        if remote_size != path.stat().st_size:
            return False
        if "-" in etag:
            return True
        return etag == _md5(path)
