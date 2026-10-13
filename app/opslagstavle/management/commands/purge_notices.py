"""Sweep opslagstavlen's uploads that no post ever referenced.

    THIS NO LONGER DELETES POSTS. Opslag are kept indefinitely — see opslagstavle/models.py.

It used to do two jobs, and the other one is gone: posts past a two-year window were hard-deleted
unless pinned. The name is kept anyway, deliberately. It is what DEPLOY.md §4b schedules and what
the Coolify task runs, and renaming it to match the narrower job would trade a slightly inaccurate
name for a live cron that fails until somebody edits it in a web UI. If it is ever renamed, §4b and
the Coolify task have to move in the same change.

What remains is the half that must not stop running, and object storage is exactly why. The compose
toolbar uploads an image *before* the post exists — the reference lives inside Markdown text, so
there is no FK to hang it on until the post is saved (see opslagstavle/images.py). An upload nobody
ever referenced is `notice_id IS NULL`: one exact, indexed query. Without this sweep every opened-
then-abandoned composer leaves a file in the bucket forever, unreachable from any page, counting
against storage and never noticed by anyone.

ORPHAN_IMAGE_GRACE is what makes it safe: long enough that removing an image from a draft and
immediately re-adding it cannot lose the file.

Run nightly (DEPLOY.md §4b). Idempotent and repeatable; `--dry-run` prints without deleting.

**Deliberately NOT also swept lazily on page load**, unlike Den Hurtige. Its lazy guard is right
because its promise is "gone in 30 minutes" — a message that should have vanished is visibly wrong
to a reader within the hour, so cron failing silently has an immediate cost. Here nothing a reader
can see is affected at all: a missed night leaves a few unreferenced files in a bucket. Do not "fix"
the inconsistency.

One subtlety worth keeping straight: a bulk queryset `.delete()` never calls `Model.delete()`, but
it *does* fire `post_delete`. That is exactly why NoticeImage cleans its file up in a receiver — and
why the files here actually go, now via core.files' on-commit delete against the bucket.
"""

import argparse

from django.core.management.base import BaseCommand
from django.utils import timezone

from opslagstavle.models import ORPHAN_IMAGE_GRACE, NoticeImage


class Command(BaseCommand):
    help = "Ryd billed-uploads som intet opslag bruger. (Opslag slettes ikke — de bevares.)"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        now = timezone.now()
        orphans = NoticeImage.objects.filter(notice__isnull=True, uploaded_at__lt=now - ORPHAN_IMAGE_GRACE)
        n_orphans = orphans.count()

        if opts["dry_run"]:
            self.stdout.write(f"[dry-run] would clear {n_orphans} unused image upload(s).")
            return

        orphans.delete()
        self.stdout.write(self.style.SUCCESS(f"Opslagstavlen: {n_orphans} ubrugte billeder ryddet."))
