import argparse
import hashlib

from django.core.management.base import BaseCommand

from photo_album.models import Media


class Command(BaseCommand):
    help = (
        "Compute the SHA-256 of originals uploaded before hashes were stored, so duplicate detection "
        "also knows about them. Reads every such original back from storage; safe to stop and rerun."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Most items to hash in one run (default: all).",
        )

    def handle(self, *args: object, **opts: object) -> None:
        limit = int(str(opts["limit"]))
        missing = Media.objects.filter(sha256="").exclude(original="").order_by("pk")
        if limit:
            missing = missing[:limit]
        hashed = failed = 0
        for media in missing.iterator():
            digest = hashlib.sha256()
            try:
                with media.original.open("rb") as original:
                    while chunk := original.read(1024 * 1024):
                        digest.update(chunk)
            except OSError as exc:
                failed += 1
                self.stderr.write(f"Kunne ikke læse medie {media.pk} ({media.original.name}): {exc}")
                continue
            Media.objects.filter(pk=media.pk).update(sha256=digest.hexdigest())
            hashed += 1
        self.stdout.write(f"Hashed {hashed} media item(s); {failed} could not be read.")
