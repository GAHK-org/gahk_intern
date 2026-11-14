"""Fallback: move the old site's content images into the Hjemmeside image library (S3) now.

The 2-minute sync does this by itself (cms.legacy_images), a few files per run. This command runs the
same import at once, without that budget, and also tries the files an earlier run recorded as
missing or unusable:

    python manage.py import_legacy_images                 # from GitHub, the pinned commit
    python manage.py import_legacy_images --from DIR      # from a local copy of the old image dir

Every referenced file is imported once and the content is pointed at the library copy; then gahk.dk
is published. Safe to run again: a second run imports nothing and reports only what is still missing.
"""

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from cms.legacy_images import DirectorySource, GitHubSource, LegacySource, convert_legacy_images
from cms.services import publish_on_commit


class Command(BaseCommand):
    help = "Import the old site's images into the Hjemmeside image library (from GitHub, or a directory)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--from",
            dest="source_dirs",
            action="append",
            type=Path,
            metavar="DIR",
            help="Read the files from a copy of the old static/legacy/image/ (or public/image/) "
            "directory, the one that holds upload/, instead of GitHub. May be given more than once; "
            "the first directory that has a file wins.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        source_dirs: list[Path] = options["source_dirs"] or []
        for directory in source_dirs:
            if not directory.is_dir():
                raise CommandError(f"{directory} is not a directory")
        source: LegacySource = DirectorySource(source_dirs) if source_dirs else GitHubSource()
        self.stdout.write(f"Reading from {source.description}")

        result = convert_legacy_images(source, retry_failed=True)
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
        if result.unavailable:
            raise CommandError(
                f"Stopped with {result.deferred} images left: {result.unavailable}. Run it again later."
            )
        if not result.missing and not result.unusable:
            self.stdout.write(self.style.SUCCESS("Every referenced legacy image is in the library."))
