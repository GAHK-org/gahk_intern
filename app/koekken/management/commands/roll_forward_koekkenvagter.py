"""Roll the tier-A allocation window forward one month -- Amendment 1's monthly roll-forward job
(A1.2). Scheduled (see koekken.tasks.roll_forward_koekkenvagter and CELERY_BEAT_SCHEDULE): this is
the deliberate reversal of P1's "allocation is manual-only" decision, because a rolling look-ahead
window nobody remembers to advance isn't actually a window (design doc A1.4). Idempotent -- a no-op
once the active periode is fully allocated, or if it has no Vagt rows generated yet."""

import argparse
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.clock import current_date
from koekken.services import (
    KoekkenAllocationError,
    _periode_from_bounds,
    periode_is_allocated,
    roll_forward_allocation,
)


class Command(BaseCommand):
    help = "Allokér næste ikke-allokerede måned i den aktive periode (Amendment 1, A1.2). Idempotent."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--date", type=str, default=None, help="YYYY-MM-DD; default er dags dato (core.clock)."
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        date_str = opts["date"]
        for_date = date.fromisoformat(str(date_str)) if date_str else current_date()
        dry_run = bool(opts["dry_run"])

        try:
            with transaction.atomic():
                result = roll_forward_allocation(for_date)
                if dry_run:
                    transaction.set_rollback(True)
        except KoekkenAllocationError as exc:
            raise CommandError(str(exc)) from exc

        prefix = "[dry-run] " if dry_run else ""
        if result is None:
            if not periode_is_allocated(_periode_from_bounds(for_date).kind):
                self.stdout.write(
                    f"{prefix}Sommerperioden allokeres ikke automatisk -- vagter tages af beboerne selv."
                )
                return
            self.stdout.write(f"{prefix}Intet at rulle frem -- perioden er allerede fuldt allokeret.")
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Rullede vinduet frem: {len(result.weekend_assigned)} tildelt weekend "
                f"({len(result.drafted)} udtrukket), {len(result.weekday_assigned)} tildelt hverdag, "
                f"{len(result.unassigned)} uden tier-A-vagt denne måned."
            )
        )
