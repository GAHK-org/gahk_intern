import argparse

from django.core.management.base import BaseCommand

from photo_album.services import check_file_time_sources, reread_capture_dates, settle_all_dates


class Command(BaseCommand):
    help = (
        "Read originals stored before XMP and IPTC were read for a capture date, and the sidecars of "
        "files whose time's source is not known yet, then give every item without a believable date "
        "an approximate one from its album. Safe to stop and rerun. The nightly "
        "`settle-photo-album-dates` job does the same, reading in portions."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Most originals to read in one run (default: all).",
        )
        parser.add_argument(
            "--skip-originals",
            action="store_true",
            help="Only settle the approximate dates; read no originals or sidecars.",
        )

    def handle(self, *args: object, **opts: object) -> None:
        if not opts["skip_originals"]:
            reread = reread_capture_dates(limit=int(str(opts["limit"])))
            for line in reread.unreadable:
                self.stderr.write(f"Kunne ikke læse medie {line}")
            self.stdout.write(
                f"Read {reread.checked} original(s) again and found {reread.found} capture date(s); "
                f"{len(reread.unreadable)} could not be read; {reread.remaining} left."
            )
            sources = check_file_time_sources()
            for line in sources.unreadable:
                self.stderr.write(f"Kunne ikke læse sidecaren til medie {line}")
            self.stdout.write(
                f"Read {sources.checked} sidecar(s) for where a file time is from, "
                f"{sources.from_sidecar} of them its source; {len(sources.unreadable)} could not be read."
            )
        self.stdout.write(f"{settle_all_dates()} item(s) re-dated.")
