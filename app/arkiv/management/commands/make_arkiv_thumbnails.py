"""Generate previews for archive images that have none — the imported backlog.

    RUN THIS LOCALLY, NOT IN PRODUCTION. It needs Pillow, which is a dev-only dependency.

Live uploads make their own preview in the browser (frontend/src/arkiv.ts), which is why this
project still has no Pillow in production and no background worker. What the browser cannot do is
the 2 TB that arrived through `import_arkiv`: nobody is going to re-upload twenty years of
photographs through a file picker.

So this is the one-off counterpart. It runs from a developer machine against the real bucket -
`S3_BUCKET=… uv run python manage.py make_arkiv_thumbnails` - reads each image object, makes a
320px JPEG, puts it back, and flips `has_thumbnail`. It never touches an original.

**Idempotent and resumable**, which for a backlog this size is the whole design: it only looks at
rows with `has_thumbnail = False`, and re-checks the store before doing any work, so an interrupted
run resumes and a second run costs one query.

Pillow is imported inside `handle`, not at module scope. A management command in `arkiv/management/`
is imported by Django's command discovery on every `manage.py` invocation, and a top-level import
would make Pillow a hard requirement of running any command at all - including in production, where
it is deliberately absent.
"""

import argparse
from io import BytesIO
from typing import cast

from django.core.management.base import BaseCommand, CommandError

from arkiv.models import ArchiveFile
from arkiv.storage import LocalArchiveStore, get_store

# Matches THUMB_DIM/THUMB_QUALITY in frontend/src/imageupload.ts. Two implementations of the same
# size is a drift risk worth naming: if one changes, change both, or a folder will show previews at
# two different sizes depending on how its files arrived.
THUMB_DIM = 320
THUMB_QUALITY = 70


class Command(BaseCommand):
    help = "Lav miniaturer for arkivbilleder der mangler dem (kræver Pillow; kør lokalt)."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=0, help="Stop after N files (0 = all).")

    def handle(self, *args: object, **opts: object) -> None:
        try:
            import PIL.Image  # noqa: F401 — imported for the availability check only
        except ImportError as exc:
            raise CommandError(
                "Pillow is not installed. It is a dev-only dependency on purpose - run this from a "
                "developer machine with `uv sync`, not from the production container."
            ) from exc

        store = get_store()
        dry = bool(opts["dry_run"])
        limit = cast("int", opts["limit"]) or 0

        candidates = ArchiveFile.objects.alive().filter(
            has_thumbnail=False, content_type__startswith="image/"
        )
        if limit:
            candidates = candidates[:limit]

        made = skipped = failed = 0
        for file in candidates.iterator():
            if store.exists(file.thumb_key):
                # An earlier run got this far. Flip the flag and move on rather than re-rendering.
                if not dry:
                    ArchiveFile.objects.filter(pk=file.pk).update(has_thumbnail=True)
                skipped += 1
                continue

            if dry:
                made += 1
                continue

            try:
                thumb = self._render(store, file)
            except Exception as exc:
                self.stderr.write(f"  {file.name}: {exc.__class__.__name__}: {exc}")
                failed += 1
                continue

            if thumb is None:
                failed += 1
                continue

            store.save(file.thumb_key, thumb)
            ArchiveFile.objects.filter(pk=file.pk).update(has_thumbnail=True)
            made += 1
            if made % 250 == 0:
                self.stdout.write(f"  … {made} so far")

        prefix = "[dry-run] " if dry else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Miniaturer: {made} lavet, {skipped} fandtes allerede, {failed} kunne ikke."
            )
        )

    def _render(self, store: object, file: ArchiveFile) -> BytesIO | None:
        """Read the original, return a 320px JPEG, or None if it is not a decodable image.

        Imports Pillow here rather than taking it as an argument: passing the module and its
        exception class around made every signature lie about what they were. `handle` has already
        proved the import works, so this one cannot fail.

        Reads the whole object into memory. Fine at this size and this cadence - one file at a time
        on a developer machine - and streaming into Pillow buys nothing, since it decodes the whole
        frame anyway.
        """
        from PIL import Image, UnidentifiedImageError

        raw = self._read(store, file)
        if raw is None:
            return None
        try:
            opened = Image.open(BytesIO(raw))
            opened.thumbnail((THUMB_DIM, THUMB_DIM))
            # RGB because a PNG with alpha, or a palette GIF, cannot be written as JPEG otherwise.
            # A separate name because convert() returns Image, not the ImageFile open() gave us.
            img: Image.Image = opened if opened.mode in ("RGB", "L") else opened.convert("RGB")
            out = BytesIO()
            img.save(out, format="JPEG", quality=THUMB_QUALITY, optimize=True)
        except (UnidentifiedImageError, OSError):
            # Not an image despite its content type, or truncated in transit. One bad file must not
            # stop a backlog of two hundred thousand.
            return None
        out.seek(0)
        return out

    def _read(self, store: object, file: ArchiveFile) -> bytes | None:
        if isinstance(store, LocalArchiveStore):
            path = store.path(file.key)
            return path.read_bytes() if path.is_file() else None
        body = BytesIO()
        store._bucket.download_fileobj(file.key, body)  # type: ignore[attr-defined]
        return body.getvalue()
