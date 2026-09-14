"""Sweep tickets that have sat in Færdig for ARCHIVE_AFTER_DAYS off the board.

Stamps `archived_at` rather than deleting: unlike opslagstavle's purge_notices, a repair's history
is worth keeping searchable (see reparationer/views.archive_list), so this never removes a row.

Run nightly (DEPLOY.md §4b). Idempotent and repeatable; `--dry-run` prints without archiving.
"""

import argparse

from django.core.management.base import BaseCommand
from django.utils import timezone

from reparationer.models import ARCHIVE_AFTER_DAYS, RepairTask


class Command(BaseCommand):
    help = f"Arkivér reparationer der har ligget i Færdig i mere end {ARCHIVE_AFTER_DAYS} dage."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        now = timezone.now()
        due = RepairTask.objects.due_for_archive(now)
        n_due = due.count()

        if opts["dry_run"]:
            self.stdout.write(
                f"[dry-run] would archive {n_due} repair(s) closed over {ARCHIVE_AFTER_DAYS} days ago."
            )
            return

        due.update(archived_at=now)
        self.stdout.write(self.style.SUCCESS(f"Reparationer: {n_due} arkiveret."))
