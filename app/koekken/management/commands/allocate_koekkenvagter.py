"""Allocate tier-A (morgen/frokost) køkkenvagter for one calendar month. Not scheduled — allocation
is a Køkkengruppen decision, run when they are ready to publish a month, not a nightly job (unlike
generate_koekkenvagter/post_koekken_obligation; see config/settings.py's CELERY_BEAT_SCHEDULE)."""

import argparse

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from koekken.services import KoekkenAllocationError, WeekendCapacityExceeded, allocate_tier_a


class Command(BaseCommand):
    help = "Fordel tier-A køkkenvagter (morgen/frokost) for en given måned. Idempotent."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("year", type=int)
        parser.add_argument("month", type=int)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        year, month = int(str(opts["year"])), int(str(opts["month"]))
        dry_run = bool(opts["dry_run"])
        try:
            with transaction.atomic():
                result = allocate_tier_a(year, month)
                if dry_run:
                    transaction.set_rollback(True)
        except (WeekendCapacityExceeded, KoekkenAllocationError) as exc:
            raise CommandError(str(exc)) from exc

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{year}-{month:02d}: {len(result.weekend_assigned)} tildelt weekend "
                f"({len(result.drafted)} udtrukket), {len(result.weekday_assigned)} tildelt hverdag, "
                f"{len(result.unassigned)} uden tier-A-vagt denne måned."
            )
        )
