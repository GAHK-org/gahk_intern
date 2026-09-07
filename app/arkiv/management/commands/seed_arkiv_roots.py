"""Create the archive's top level: one gated folder per embedsgruppe, plus the shared ones.

Roots are `can_manage_roots` territory (Inspektionen and Netvaerksgruppen) and there is no screen for
them, because arranging the top level happens a handful of times in the archive's life. This command
does the first pass; the admin does the rest.

The logic itself is `arkiv.services.ensure_root_folders`, shared with the demo seeder so a fresh
developer database and a real deployment get the same shape instead of two that drift.

Idempotent. Re-running adds whatever is missing (a new embedsgruppe, say) and leaves everything else
exactly as it is - including a root somebody has since renamed, re-owned, or deliberately deleted.
"""

import argparse

from django.core.management.base import BaseCommand

from arkiv.services import ensure_root_folders


class Command(BaseCommand):
    help = "Opret Arkivets rodmapper: én pr. embedsgruppe plus de fælles. Kan køres igen."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        if opts["dry_run"]:
            # Nothing is written, so report by asking what ensure_root_folders would find. Done by
            # inspection rather than by rolling back a transaction: the answer is a cheap query and
            # a rolled-back write is a surprising thing for a --dry-run to do.
            from arkiv.models import ArchiveFolder
            from arkiv.services import SHARED_ROOTS
            from core.models import Workgroup

            wanted = SHARED_ROOTS + list(Workgroup.objects.order_by("name").values_list("name", flat=True))
            have = set(ArchiveFolder.objects.alive().filter(parent=None).values_list("name", flat=True))
            missing = [n for n in wanted if n not in have]
            if missing:
                self.stdout.write(f"[dry-run] ville oprette: {', '.join(missing)}")
            self.stdout.write(f"[dry-run] findes allerede: {len(wanted) - len(missing)}")
            return

        created, existing = ensure_root_folders()
        if created:
            self.stdout.write(self.style.SUCCESS(f"Oprettet: {', '.join(created)}"))
        else:
            self.stdout.write("Ingen nye rodmapper.")
        if existing:
            self.stdout.write(f"Fandtes allerede: {len(existing)}")
