"""Allocate a calendar month's køkkenvagter -- tier A (morgen/frokost) THEN tier B (aftenvagt), in
one run (P2 design doc §4: "the monthly pass becomes tier-A, then tier-B, in one run") -- or, with
--batch, a period's first three months in one call (Amendment 1, A1.2: the deadline-triggered batch
Køkkengruppen runs by hand at a period's preference deadline). Not scheduled itself — allocating a
month for the first time remains a Køkkengruppen decision; `roll_forward_koekkenvagter` is the
scheduled follow-up that only ever advances an already-opened period one month at a time."""

import argparse
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from koekken.services import KoekkenAllocationError, allocate_month, resolve_periode


class Command(BaseCommand):
    help = (
        "Fordel køkkenvagter (tier A + tier B) for en given måned, eller -- med --batch -- "
        "periodens tre første måneder i ét kald. Idempotent (kræver --force ved gentildeling)."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("year", type=int)
        parser.add_argument("month", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Gentildel (allerede tildelte) måned(er) -- Amendment 1, A1.2.",
        )
        parser.add_argument(
            "--batch",
            action="store_true",
            help="Allokér de tre første måneder i den periode, år/måned falder i.",
        )

    def handle(self, *args: object, **opts: object) -> None:
        year, month = int(str(opts["year"])), int(str(opts["month"]))
        dry_run = bool(opts["dry_run"])
        force = bool(opts["force"])
        batch = bool(opts["batch"])

        months: list[tuple[int, int]]
        if batch:
            periode = resolve_periode(date(year, month, 1))
            months = []
            cursor = periode.start_date
            # Clamped to the periode's actual length (A2.8): a fixed 3-month walk steps past a
            # 2-month SOMMER periode into the next EFTERAAR's September, which either has no Vagt
            # rows yet or belongs to a periode whose preference deadline hasn't passed -- either way
            # allocating it here would be wrong, not just early.
            while cursor <= periode.end_date and len(months) < 3:
                months.append((cursor.year, cursor.month))
                cursor = date(cursor.year + (1 if cursor.month == 12 else 0), cursor.month % 12 + 1, 1)
        else:
            months = [(year, month)]

        lines: list[str] = []
        try:
            with transaction.atomic():
                for y, m in months:
                    tier_a, tier_b = allocate_month(y, m, force=force)
                    lines.append(
                        f"{y}-{m:02d}: tier A -- {len(tier_a.weekend_assigned)} tildelt weekend "
                        f"({len(tier_a.drafted)} udtrukket), {len(tier_a.weekday_assigned)} tildelt "
                        f"hverdag, {len(tier_a.unassigned)} uden tier-A-vagt denne måned "
                        f"({len(tier_a.refused_weekend)} pga. overfyldt weekend-pulje). "
                        f"Tier B -- {len(tier_b.assigned)} tildelt aftenvagt "
                        f"({len(tier_b.skipped_avoidance)} fik ekstra tier-A i stedet)."
                    )
                if dry_run:
                    transaction.set_rollback(True)
        except KoekkenAllocationError as exc:
            raise CommandError(str(exc)) from exc

        prefix = "[dry-run] " if dry_run else ""
        for line in lines:
            self.stdout.write(self.style.SUCCESS(prefix + line))
