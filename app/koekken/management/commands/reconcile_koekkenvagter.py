"""Reconcile tier-A assignments against the real Residency list, now that it may have been
published -- Amendment 2 (A2.3), corrected by Amendment 3 (A3.1). Additive only, and distinct from
allocate_koekkenvagter's --force: this never touches an assignment for a resident present on both
the projection that was used and the real list now, it only vacates assignments for whoever the
projection got wrong and re-seats the resulting open capacity via the same eligibility-aware seating
core allocate_tier_a uses.

**F2: the default sweep.** With no --year/--month, this reconciles EVERY month in the look-ahead
window that already has tier-A allocation (existing `VagtTildeling` rows), not a single hardcoded
next-month guess -- `next_period()` (active_period() + 1) targets a month whose real list has usually
not been published yet (triggering the old F1 bug, or a correct no-op now), and by the time that list
DOES arrive, `active_period()` has itself advanced past it, so a single fixed target never actually
lands on the month that just became reconcilable. Sweeping every allocated month instead costs
nothing extra: `reconcile_month` already no-ops for any of them that aren't ready yet."""

import argparse
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.clock import current_date
from koekken.models import Vagt, VagtRegel, VagtTildeling
from koekken.services import reconcile_month, resolve_periode

_TIER_A_KINDS = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]


def _allocated_months_in_window() -> list[tuple[int, int]]:
    """Every (year, month) inside the periode containing today that already has tier-A allocation
    (any `VagtTildeling` row at all) -- F2's sweep target. Scoped to the current periode only,
    mirroring `roll_forward_allocation`'s own periode-scoped walk (Amendment 1, A1.2): the look-ahead
    window this job needs to catch up with never crosses a periode boundary either."""
    periode = resolve_periode(current_date())
    months: list[tuple[int, int]] = []
    cursor = periode.start_date
    while cursor <= periode.end_date:
        year, month = cursor.year, cursor.month
        vagter = Vagt.objects.filter(
            periode=periode, date__year=year, date__month=month, kind__in=_TIER_A_KINDS
        )
        if vagter.exists() and VagtTildeling.objects.filter(vagt__in=vagter).exists():
            months.append((year, month))
        cursor = date(year + (1 if month == 12 else 0), month % 12 + 1, 1)
    return months


class Command(BaseCommand):
    help = (
        "Afstem tier-A-tildelinger mod den reelle alumneliste (Amendment 2/3, A2.3/A3.1). "
        "Uden --year/--month afstemmes hver allerede allokerede måned i den aktive periodes "
        "look-ahead-vindue (F2). Idempotent; ikke det samme som --force."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--year",
            type=int,
            default=None,
            help="Manuel overstyring af én enkelt måned (f.eks. til test/debugging). Kræver --month.",
        )
        parser.add_argument("--month", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        year_opt, month_opt = opts["year"], opts["month"]
        dry_run = bool(opts["dry_run"])

        if (year_opt is None) != (month_opt is None):
            raise CommandError("--year kræver --month (og omvendt).")
        single_month = (int(str(year_opt)), int(str(month_opt))) if year_opt is not None else None

        prefix = "[dry-run] " if dry_run else ""
        lines: list[str] = []
        with transaction.atomic():
            # `_allocated_months_in_window()` calls `resolve_periode()` (get_or_create) -- kept inside
            # this block so --dry-run's set_rollback(True) below also undoes any Periode row it might
            # have created, matching the explicit --year/--month path's own resolve_periode() call.
            months = [single_month] if single_month is not None else _allocated_months_in_window()
            for year, month in months:
                result = reconcile_month(year, month)
                seated_count = len(result.seated.weekend_assigned) + len(result.seated.weekday_assigned)
                lines.append(
                    f"{year}-{month:02d}: {len(result.vacated)} tildeling(er) ledig-gjort, "
                    f"{seated_count} genbesat, {len(result.still_unfilled)} vagt(er) stadig uden fuld "
                    "besætning."
                )
            if dry_run:
                transaction.set_rollback(True)

        if not lines:
            self.stdout.write(f"{prefix}Intet allokeret at afstemme.")
            return
        for line in lines:
            self.stdout.write(self.style.SUCCESS(prefix + line))
