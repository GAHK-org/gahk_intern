"""Enforce the 1-year retention policy on applications (F-001).

Deletes applications older than 1 year. Run on a schedule (cron / scheduled task). NOT run by the ETL,
so the migrated history (and the stats charts) stay intact until you choose to enforce retention.
Use --dry-run to preview.
"""

import argparse
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from admissions.models import Application
from audit import trail


class Command(BaseCommand):
    help = "Delete applications older than 1 year (retention policy, F-001)."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        cutoff = timezone.now() - timedelta(days=365)
        qs = Application.objects.filter(submitted_at__lt=cutoff)
        n = qs.count()
        if opts["dry_run"]:
            self.stdout.write(f"[dry-run] would delete {n} applications older than {cutoff:%Y-%m-%d}.")
            return
        # One event for the purge (each application in it by kind and number only, as always).
        if n:
            with trail.event(
                "admissions.application.purged", actor=None, details={"count": n, "before": cutoff.date()}
            ):
                qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {n} applications older than {cutoff:%Y-%m-%d}."))
