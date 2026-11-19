from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from photo_album.models import DerivativeState, Media, normalize_name, validate_folder
from photo_album.tasks import retry_derivatives


class Command(BaseCommand):
    help = (
        'Give media whose conversion failed ("Videoen kunne ikke behandles") three fresh attempts, '
        "and queue the first now. For after a fix to the conversion; nothing is deleted either way."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--year", help="Only albums in this folder: a year, or 'Uden årstal'.")
        parser.add_argument("--dry-run", action="store_true", help="List them, change nothing.")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        failed = Media.objects.filter(derivative_state=DerivativeState.FAILED, deleted_at__isnull=True)
        if year := normalize_name(options["year"] or ""):
            try:
                validate_folder(year)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from exc
            failed = failed.filter(album__folder=year)
        items = list(failed.select_related("album").order_by("album__folder", "pk"))
        for media in items:
            self.stdout.write(f"{media.album}: {media.display_name_and_size}")
        if options["dry_run"]:
            self.stdout.write(f"{len(items)} failed media item(s); nothing changed (--dry-run).")
            return
        queued = retry_derivatives(Media.objects.filter(pk__in=[media.pk for media in items]))
        self.stdout.write(
            f"Queued {queued} media item(s) again, each with three fresh attempts. "
            "Follow them on /admin/worker-jobs."
        )
