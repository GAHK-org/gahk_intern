"""Generate the derived sizes for archive images that have none — the imported backlog.

    NEEDS PILLOW, WHICH THIS PROJECT DOES NOT SHIP. Install it for the run, not in the image.

Live uploads make their own preview in the browser (frontend/src/arkiv.ts), which is why this
project still has no Pillow in production and no background worker. What the browser cannot do is
the 2 TB that arrived through `import_arkiv`: nobody is going to re-upload twenty years of
photographs through a file picker.

So this is the one-off counterpart. It reads each image object once and writes back BOTH derived
sizes - the 320px thumbnail the listing draws and the ~1600px preview the viewer shows - then flips
the two flags. It never touches an original.

**Both sizes in one pass, deliberately.** The cost here is not the resizing, it is pulling 57,000
originals back out of the bucket; doing that a second time to add another size later would double a
multi-hour job for nothing. A third size, if it is ever wanted, belongs here rather than in a
second command.

Where to run it is a real choice, and the answer changed once the backlog was 57,000 photographs.
Locally means every original crosses a domestic line twice and the whole archive goes out through
egress; from the server it is free internal `fsn1` traffic and several times faster. What the
no-Pillow rule protects is the IMAGE, not the machine - so the way to run this in production is

    docker exec <web> uv pip install --python /app/.venv/bin/python pillow
    docker exec -d <web> sh -c 'python manage.py make_arkiv_thumbnails > /tmp/thumbs.log 2>&1'

which leaves the image untouched and evaporates on the next deploy. Note `uv pip`, not `pip`: deps
are installed with `uv sync` into /app/.venv, and a uv-created venv has no pip in it at all.

Detached, because it runs for hours and an SSH session that drops would otherwise take it with it.

**Idempotent and resumable**, which for a backlog this size is the whole design: it looks only at
rows missing a size, re-checks the store before doing any work, and flips both flags together - so
an interrupted run resumes where it stopped and a finished one costs a single query.

Pillow is imported inside `handle`, not at module scope. A management command in `arkiv/management/`
is imported by Django's command discovery on every `manage.py` invocation, and a top-level import
would make Pillow a hard requirement of running any command at all - including in production, where
it is deliberately absent.
"""

import argparse
from io import BytesIO
from typing import cast

from django.core.management.base import BaseCommand, CommandError
from django.db import models

from arkiv.models import ArchiveFile
from arkiv.storage import ArchiveStore, get_store

# Matches THUMB_DIM/THUMB_QUALITY in frontend/src/imageupload.ts. Two implementations of the same
# size is a drift risk worth naming: if one changes, change both, or a folder will show previews at
# two different sizes depending on how its files arrived.
THUMB_DIM = 320
THUMB_QUALITY = 70

# The viewer size. 1600px covers every screen in the building including a phone at 3x, and lands
# around 200-400 kB per photograph - against originals that average tens of megabytes. Quality 80
# rather than the thumbnail's 70 because this one is looked at full-screen.
PREVIEW_DIM = 1600
PREVIEW_QUALITY = 80


class Command(BaseCommand):
    help = "Lav miniature og stort preview for arkivbilleder der mangler dem (kræver Pillow)."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=0, help="Stop after N files (0 = all).")

    def handle(self, *args: object, **opts: object) -> None:
        try:
            import PIL.Image  # noqa: F401 — imported for the availability check only
        except ImportError as exc:
            raise CommandError(
                "Pillow is not installed - it is deliberately not in the image. Install it for "
                "the run with `uv pip install --python /app/.venv/bin/python pillow` (uv pip, not "
                "pip: a uv-created venv has none), or `uv sync` on a developer machine."
            ) from exc

        store = get_store()
        dry = bool(opts["dry_run"])
        limit = cast("int", opts["limit"]) or 0

        candidates = ArchiveFile.objects.alive().filter(
            models.Q(has_thumbnail=False) | models.Q(has_preview=False),
            content_type__startswith="image/",
        )
        if limit:
            candidates = candidates[:limit]

        made = skipped = failed = 0
        for file in candidates.iterator():
            # Which sizes are actually missing, asked BEFORE anything is downloaded. Two rows
            # sharing bytes share both derived objects, so on an archive this full of duplicates
            # this check is most of the work not done.
            wanted = {
                "thumb": (file.thumb_key, THUMB_DIM, THUMB_QUALITY, file.has_thumbnail),
                "preview": (file.preview_key, PREVIEW_DIM, PREVIEW_QUALITY, file.has_preview),
            }
            missing = [
                (key, dim, quality)
                for key, dim, quality, flagged in wanted.values()
                if not flagged and not store.exists(key)
            ]
            if not missing:
                # An earlier run got this far, or another row's bytes did. Flip the flags and move
                # on rather than re-rendering what is already in the bucket.
                if not dry:
                    ArchiveFile.objects.filter(pk=file.pk).update(has_thumbnail=True, has_preview=True)
                skipped += 1
                continue

            if dry:
                made += 1
                continue

            try:
                rendered = self._render(store, file, missing)
            except Exception as exc:
                self.stderr.write(f"  {file.name}: {exc.__class__.__name__}: {exc}")
                failed += 1
                continue

            if rendered is None:
                failed += 1
                continue

            for key, body in rendered:
                store.save(key, body)
            ArchiveFile.objects.filter(pk=file.pk).update(has_thumbnail=True, has_preview=True)
            made += 1
            if made % 250 == 0:
                self.stdout.write(f"  … {made} so far")

        prefix = "[dry-run] " if dry else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Billedstørrelser: {made} lavet, {skipped} fandtes allerede, {failed} kunne ikke."
            )
        )

    def _render(
        self, store: ArchiveStore, file: ArchiveFile, specs: list[tuple[str, int, int]]
    ) -> list[tuple[str, BytesIO]] | None:
        """Read the original ONCE, return one JPEG per requested (key, dim, quality).

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
        out: list[tuple[str, BytesIO]] = []
        try:
            for key, dim, quality in specs:
                # Reopened per size rather than resized once and reused. thumbnail() mutates in
                # place, so 1600-then-320 would work while 320-then-1600 would quietly produce a
                # 320px "preview" - a bug that depends on dict order and would be invisible until
                # somebody opened the viewer. Decoding twice from bytes already in memory costs
                # nothing next to the download, and it makes the order irrelevant.
                opened = Image.open(BytesIO(raw))
                opened.thumbnail((dim, dim))  # never upscales; a small original stays its own size
                # RGB because a PNG with alpha, or a palette GIF, cannot be written as JPEG
                # otherwise. A separate name because convert() returns Image, not the ImageFile
                # open() gave us.
                img: Image.Image = opened if opened.mode in ("RGB", "L") else opened.convert("RGB")
                body = BytesIO()
                img.save(body, format="JPEG", quality=quality, optimize=True)
                body.seek(0)
                out.append((key, body))
        except (UnidentifiedImageError, OSError):
            # Not an image despite its content type, or truncated in transit. One bad file must not
            # stop a backlog of two hundred thousand.
            return None
        return out

    def _read(self, store: ArchiveStore, file: ArchiveFile) -> bytes | None:
        """The whole object, or None if it is not there.

        Goes through `store.chunks`, which both backends implement, rather than the bucket handle
        this used to reach for - so the command runs against the local store in dev and CI too.
        """
        if not store.exists(file.key):
            return None
        return b"".join(store.chunks(file.key))
