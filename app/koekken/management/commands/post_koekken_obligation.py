"""Post the FORPLIGTELSE ledger entries for one calendar month (default: the active period's current
month, so this is schedulable with no arguments — see koekken.tasks). Idempotent: guarded by
uniq_koekken_forpligtelse_per_period_month, exactly ak_monthly_assessment's pattern."""

import argparse
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from koekken.services import post_obligation, resolve_periode
from residents.models import active_period


class Command(BaseCommand):
    help = "Bogfør FORPLIGTELSE for en måned (default: den aktive måned). Idempotent."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--year", type=int, default=None)
        parser.add_argument("--month", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        year_opt, month_opt = opts["year"], opts["month"]
        if year_opt is None or month_opt is None:
            year, month = active_period()
        else:
            year, month = int(str(year_opt)), int(str(month_opt))
        dry_run = bool(opts["dry_run"])

        with transaction.atomic():
            periode = resolve_periode(date(year, month, 1))
            written, present = post_obligation(periode, month)
            if dry_run:
                transaction.set_rollback(True)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{year}-{month:02d}: {written} FORPLIGTELSE bogført ({present} beboere)."
            )
        )
