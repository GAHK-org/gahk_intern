import argparse

from django.core.management.base import BaseCommand

from photo_album.services import MAX_DERIVATIVE_ATTEMPTS, build_derivatives, pending_derivatives


class Command(BaseCommand):
    help = "Build the viewer and grid derivatives for uploaded videos that are still waiting."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Most items to transcode in one run (default 10). Keeps a scheduled run bounded.",
        )

    def handle(self, *args: object, **opts: object) -> None:
        limit = int(str(opts["limit"]))
        built = failed = 0
        for media in pending_derivatives()[:limit]:
            if build_derivatives(media):
                built += 1
                continue
            failed += 1
            remaining = MAX_DERIVATIVE_ATTEMPTS - media.derivative_attempts
            detail = f"{remaining} forsøg tilbage" if remaining > 0 else "opgivet"
            self.stderr.write(f"Kunne ikke behandle medie {media.pk} ({media.title}): {detail}.")
        self.stdout.write(f"Built derivatives for {built} media item(s); {failed} failed.")
