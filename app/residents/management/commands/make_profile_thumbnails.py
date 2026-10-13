"""Make the profile thumbnail for every resident whose picture predates it (#177).

Resident.save() makes the thumbnail whenever the picture changes, so this is only for pictures
uploaded before that existed. Until it has run, those residents' avatars fall back to the original
(Resident.avatar_url), so nothing is broken in the meantime; the pages are just as heavy as before.

Idempotent: it looks only at residents with a picture and no thumbnail, so an interrupted run resumes
where it stopped and a finished one costs a single query. A picture Pillow cannot read stays without
a thumbnail and is counted as skipped, and will be retried by the next run.

    python manage.py make_profile_thumbnails
"""

import argparse

from django.core.management.base import BaseCommand

from residents.models import Resident


class Command(BaseCommand):
    help = "Lav miniaturer af profilbilleder, der mangler en."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        pending = Resident.objects.exclude(profile_picture="").filter(profile_thumbnail="").order_by("pk")
        made = skipped = 0
        for resident in pending.iterator():
            if opts["dry_run"]:
                self.stdout.write(f"would make: {resident.profile_picture.name}")
                continue
            try:
                resident.refresh_profile_thumbnail()
            except OSError as exc:
                # Typically the original is missing from storage. One bad row must not stop the run.
                self.stderr.write(f"{resident.pk}: {resident.profile_picture.name}: {exc}")
                skipped += 1
                continue
            if resident.profile_thumbnail:
                made += 1
            else:
                skipped += 1
        self.stdout.write(self.style.SUCCESS(f"{made} thumbnails made, {skipped} skipped."))
