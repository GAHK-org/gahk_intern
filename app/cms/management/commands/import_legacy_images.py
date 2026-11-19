"""Move ALL of the old site's images into the Hjemmeside image library (S3) now, in one go.

The 2-minute sync does this by itself (cms.legacy_images), a few files per run. This command runs the
same import at once, without that budget, and also tries the files an earlier run recorded as
missing or unusable. Run it right after the deploy, so no old picture address on gahk.dk goes
without its copy for the minutes the sync would take:

    python manage.py import_legacy_images

Every file below static/legacy/image/upload/, and every other file the content refers to, is
imported once, with a line per file; the content is pointed at the library copies, and then gahk.dk
is published. Safe to run again, or after an interruption: what is in the library already is
skipped, and so is what an editor deleted from it.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from cms import legacy_images
from cms.legacy_images import convert_legacy_images, legacy_state
from cms.services import publish_on_commit


class Command(BaseCommand):
    help = "Import every old-site image (static/legacy/image/upload/) into the Hjemmeside image library."

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        self.stdout.write(f"Reading from {legacy_images.LEGACY_DIRECTORY}")

        def progress(number: int, total: int, path: str, outcome: str) -> None:
            line = f"[{number}/{total}] {path}: {outcome}"
            self.stdout.write(line if outcome == "imported" else self.style.WARNING(line))
            self.stdout.flush()

        result = convert_legacy_images(retry_failed=True, progress=progress)
        if result.busy:
            raise CommandError("Another legacy image import is running; try again in a minute.")

        self.stdout.write(f"Imported {len(result.imported)} images; rewrote {result.rewritten} rows.")
        for label, paths in (("Missing", result.missing), ("Not a usable image", result.unusable)):
            for path in paths:
                self.stdout.write(self.style.WARNING(f"{label}: {path}"))
        if result.imported or result.rewritten:
            publish_on_commit("save")
            self.stdout.write("Queued a publish to gahk.dk.")
        if result.error:
            raise CommandError(
                f"Stopped with {result.deferred} images left: {result.error}. Run it again later."
            )
        state = legacy_state()
        deleted = f" ({state.deleted} deleted from it)" if state.deleted else ""
        self.stdout.write(f"The library has {state.in_library} of the {state.total} old images{deleted}.")
        if not result.missing and not result.unusable and not state.waiting:
            self.stdout.write(self.style.SUCCESS("Every old image is in the library."))
