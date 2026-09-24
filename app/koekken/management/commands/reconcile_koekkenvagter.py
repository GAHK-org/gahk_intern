"""Reconcile one calendar month's tier-A assignments against the real Residency list, now that it
may have been published -- Amendment 2 (A2.3), corrected by Amendment 3 (A3.1). Additive only, and
distinct from allocate_koekkenvagter's --force: this never touches an assignment for a resident
present on both the projection that was used and the real list now, it only vacates assignments for
whoever the projection got wrong and re-seats the resulting open capacity via the same
eligibility-aware seating core allocate_tier_a uses."""

import argparse

from django.core.management.base import BaseCommand
from django.db import transaction

from koekken.services import reconcile_month
from residents.models import next_period


class Command(BaseCommand):
    help = (
        "Afstem tier-A-tildelinger for en måned mod den reelle alumneliste (Amendment 2/3, "
        "A2.3/A3.1). Idempotent; ikke det samme som --force."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--year",
            type=int,
            default=None,
            help="Default: næste måned efter den aktive (indstillings-)liste.",
        )
        parser.add_argument("--month", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        year_opt, month_opt = opts["year"], opts["month"]
        if year_opt is None or month_opt is None:
            year, month = next_period()
        else:
            year, month = int(str(year_opt)), int(str(month_opt))
        dry_run = bool(opts["dry_run"])

        with transaction.atomic():
            result = reconcile_month(year, month)
            if dry_run:
                transaction.set_rollback(True)

        prefix = "[dry-run] " if dry_run else ""
        seated_count = len(result.seated.weekend_assigned) + len(result.seated.weekday_assigned)
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{year}-{month:02d}: {len(result.vacated)} tildeling(er) ledig-gjort, "
                f"{seated_count} genbesat, {len(result.still_unfilled)} vagt(er) stadig uden fuld "
                "besætning."
            )
        )
