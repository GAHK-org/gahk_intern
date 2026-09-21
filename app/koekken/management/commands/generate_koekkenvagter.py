"""Generate this month's (or a given date's) køkkenvagter (F-tier-A shift rows). Idempotent: safe to
schedule, safe to re-run — `koekken.services.generate_vagter` only ever creates rows that don't
already exist for a (date, kind); it never touches or re-derives an already-generated Vagt."""

import argparse
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from core.clock import current_date
from koekken.services import generate_vagter, resolve_periode


class Command(BaseCommand):
    help = "Generér Vagt-rækker for perioden der indeholder en given dato (default: i dag). Idempotent."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--date", type=str, default=None, help="YYYY-MM-DD; default er dags dato (core.clock)."
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        date_str = opts["date"]
        for_date = date.fromisoformat(str(date_str)) if date_str else current_date()
        dry_run = bool(opts["dry_run"])

        with transaction.atomic():
            periode = resolve_periode(for_date)
            created = generate_vagter(periode)
            if dry_run:
                transaction.set_rollback(True)
                self.stdout.write(f"[dry-run] ville oprette {len(created)} vagt(er) for {periode}.")
                return

        self.stdout.write(self.style.SUCCESS(f"{len(created)} vagt(er) oprettet for {periode}."))
