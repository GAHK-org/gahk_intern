"""One-off: move the old site's content images into the Hjemmeside image library (S3).

The images are not in this repository. Give the command a copy of the old site's image directory,
the one that holds upload/ (DEPLOY.md, "gahk.dk (gahk_web)", says where to get it):

    python manage.py import_legacy_images --from /tmp/legacy-image

Every referenced file is imported once and the content is pointed at the library copy (see
cms.legacy_images); then gahk.dk is published. Safe to run again: a second run imports nothing and
reports only what is still missing. Delete the copy afterwards.
"""

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from cms.legacy_images import convert_legacy_images
from cms.services import publish_on_commit


class Command(BaseCommand):
    help = "Import the old site's images (from a directory you give) into the Hjemmeside image library."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--from",
            dest="source_dirs",
            action="append",
            required=True,
            type=Path,
            metavar="DIR",
            help="A copy of the old static/legacy/image/ (or public/image/) directory, which holds "
            "upload/. May be given more than once; the first directory that has a file wins.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        source_dirs: list[Path] = options["source_dirs"]
        for directory in source_dirs:
            if not directory.is_dir():
                raise CommandError(f"{directory} is not a directory")

        result = convert_legacy_images(source_dirs)

        self.stdout.write(f"Imported {len(result.imported)} images; rewrote {result.rewritten} rows.")
        for label, paths in (("Missing", result.missing), ("Not a usable image", result.unusable)):
            for path in paths:
                self.stdout.write(self.style.WARNING(f"{label}: {path}"))
        if result.imported or result.rewritten:
            publish_on_commit("save")
            self.stdout.write("Queued a publish to gahk.dk.")
        if not result.missing and not result.unusable:
            self.stdout.write(self.style.SUCCESS("Every referenced legacy image is in the library."))
