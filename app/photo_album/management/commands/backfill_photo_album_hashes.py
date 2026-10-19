import argparse

from django.core.management.base import BaseCommand

from photo_album.services import backfill_hashes


class Command(BaseCommand):
    help = (
        "Compute the SHA-256 of originals uploaded before hashes were stored, so duplicate detection "
        "also knows about them. Reads every such original back from storage; safe to stop and rerun. "
        "The nightly `backfill-photo-album-hashes` job does the same in portions."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Most items to hash in one run (default: all).",
        )

    def handle(self, *args: object, **opts: object) -> None:
        result = backfill_hashes(limit=int(str(opts["limit"])))
        for line in result.unreadable:
            self.stderr.write(f"Kunne ikke læse medie {line}")
        self.stdout.write(
            f"Hashed {result.hashed} media item(s); {len(result.unreadable)} could not be read; "
            f"{result.remaining} still without a hash."
        )
