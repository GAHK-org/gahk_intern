"""Allocate a calendar month's køkkenvagter -- tier A (morgen/frokost) THEN tier B (aftenvagt), in
one run (P2 design doc §4: "the monthly pass becomes tier-A, then tier-B, in one run") -- or, with
--batch, a period's first three months in one call (Amendment 1, A1.2: the deadline-triggered batch
Køkkengruppen runs by hand at a period's preference deadline). ANY allocation of a month -- with or
without --batch -- is refused until the day after that month's own periode's preference deadline has
passed (the A1.3 supplement of 2026-10-01, enforced in `koekken.services.allocate_month` and
`allocate_batch` alike). Not scheduled itself — allocating a month for the first time remains a
Køkkengruppen decision; `roll_forward_koekkenvagter` is the scheduled follow-up that only ever
advances an already-opened period one month at a time."""

import argparse

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from koekken.services import KoekkenAllocationError, TierAResult, TierBResult, allocate_batch, allocate_month


def _format_line(year: int, month: int, tier_a: TierAResult, tier_b: TierBResult) -> str:
    return (
        f"{year}-{month:02d}: tier A -- {len(tier_a.weekend_assigned)} tildelt weekend "
        f"({len(tier_a.drafted)} udtrukket), {len(tier_a.weekday_assigned)} tildelt "
        f"hverdag, {len(tier_a.unassigned)} uden tier-A-vagt denne måned "
        f"({len(tier_a.refused_weekend)} pga. overfyldt weekend-pulje). "
        f"Tier B -- {len(tier_b.assigned)} tildelt aftenvagt "
        f"({len(tier_b.skipped_avoidance)} fik ekstra tier-A i stedet)."
    )


class Command(BaseCommand):
    help = (
        "Fordel køkkenvagter (tier A + tier B) for en given måned, eller -- med --batch -- "
        "periodens tre første måneder i ét kald. Idempotent (kræver --force ved gentildeling). "
        "Enhver allokering -- med eller uden --batch -- nægter at køre på eller før den måneds "
        "periodes egen præferencefrist -- --force omgår ikke det."
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

        lines: list[str] = []
        try:
            with transaction.atomic():
                if batch:
                    for y, m, tier_a, tier_b in allocate_batch(year, month, force=force):
                        lines.append(_format_line(y, m, tier_a, tier_b))
                else:
                    tier_a, tier_b = allocate_month(year, month, force=force)
                    lines.append(_format_line(year, month, tier_a, tier_b))
                if dry_run:
                    transaction.set_rollback(True)
        except KoekkenAllocationError as exc:
            raise CommandError(str(exc)) from exc

        prefix = "[dry-run] " if dry_run else ""
        for line in lines:
            self.stdout.write(self.style.SUCCESS(prefix + line))
