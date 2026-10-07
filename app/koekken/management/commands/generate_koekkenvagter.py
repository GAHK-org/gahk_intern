"""Generate this month's (or a given date's) køkkenvagter (F-tier-A shift rows). Idempotent: safe to
schedule, safe to re-run — `koekken.services.generate_vagter` only ever creates rows that don't
already exist for a (date, kind); it never touches or re-derives an already-generated Vagt.

With no `--date`, generates every periode `koekken.services.periodes_to_generate(today)` returns -- the
current one, plus the coming SOMMER from 1 May (its deadline) so residents can claim summer shifts --
in one atomic block. With `--date`, only the periode containing that date, as always."""

import argparse
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from core.clock import current_date
from koekken.services import generate_vagter, periodes_to_generate, resolve_periode


class Command(BaseCommand):
    help = "Generér Vagt-rækker for perioden der indeholder en given dato (default: i dag). Idempotent."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--date", type=str, default=None, help="YYYY-MM-DD; default er dags dato (core.clock)."
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        date_str = opts["date"]
        dry_run = bool(opts["dry_run"])

        results = []
        with transaction.atomic():
            if date_str:
                periodes = [resolve_periode(date.fromisoformat(str(date_str)))]
            else:
                periodes = periodes_to_generate(current_date())
            for periode in periodes:
                results.append((periode, generate_vagter(periode)))
            if dry_run:
                transaction.set_rollback(True)

        for periode, created in results:
            if dry_run:
                self.stdout.write(f"[dry-run] ville oprette {len(created)} vagt(er) for {periode}.")
            else:
                self.stdout.write(self.style.SUCCESS(f"{len(created)} vagt(er) oprettet for {periode}."))
