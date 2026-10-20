from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from photo_album.models import DerivativeState, Media, validate_folder
from photo_album.services import HIGH_DEFINITION_SIZE, is_video_media, stored_video_dimensions
from photo_album.tasks import reencode_enlarged_video


class Command(BaseCommand):
    help = (
        f"Re-encode the viewer copies of videos that an earlier conversion enlarged to "
        f"{HIGH_DEFINITION_SIZE} px, at the videos' own size. Finds them by reading the first "
        "megabytes of each copy (and original); queues one job per video on the media queue, which "
        "checks the original again before it re-encodes. A video stays viewable throughout."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--year", help="Only albums in this folder: a year, or Andet.")
        parser.add_argument("--dry-run", action="store_true", help="List them, queue nothing.")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        videos = (
            Media.objects.filter(derivative_state=DerivativeState.READY, deleted_at__isnull=True)
            .exclude(high_definition="")
            .select_related("album")
            .order_by("album__folder", "pk")
        )
        if year := options["year"]:
            try:
                validate_folder(year)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from exc
            videos = videos.filter(album__folder=year)

        enlarged: list[Media] = []
        checked = 0
        for media in videos.iterator():
            if not is_video_media(media):
                continue
            checked += 1
            copy = stored_video_dimensions(media.high_definition)
            if copy is not None and max(copy) < HIGH_DEFINITION_SIZE:
                continue  # made at the video's own size already
            # Most originals say their size in their first megabytes too; a phone's MP4 often only at
            # the end, and is then left for the job to find out.
            original = stored_video_dimensions(media.original)
            if original is not None and max(original) >= HIGH_DEFINITION_SIZE:
                continue
            enlarged.append(media)
            sizes = " -> ".join(
                f"{label} {dimensions[0]}x{dimensions[1]}"
                for label, dimensions in (("original", original), ("copy", copy))
                if dimensions is not None
            )
            self.stdout.write(f"{media.album}: {media.display_name_and_size}{f' ({sizes})' if sizes else ''}")

        if options["dry_run"]:
            self.stdout.write(
                f"{len(enlarged)} of {checked} video(s) to re-encode; nothing queued (--dry-run)."
            )
            return
        for media in enlarged:
            reencode_enlarged_video.delay(media.pk)
        self.stdout.write(
            f"Queued {len(enlarged)} of {checked} video(s) on the media queue, one at a time. "
            "Follow them on /admin/worker-jobs."
        )
