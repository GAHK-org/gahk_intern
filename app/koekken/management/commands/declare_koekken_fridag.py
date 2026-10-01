"""Declare a fridag -- Amendment 5 (A5.4): a date (or a chosen set of shift kinds on it) the kitchen
does not need covering, e.g. juleaften. The normal case, not an edge case: a periode is allocated
~122 days before it starts, so a fridag declared in the months leading up to it is almost always
against an already-generated/already-allocated month -- this ONE action creates the Fridag row(s),
deletes the matching Vagt row(s) if that month was already generated, re-allocates the affected month
(the existing --force mechanism) and re-posts its obligation, all in one atomic step, and notifies
any resident who loses an assignment. See koekken.services.declare_fridag for the full mechanics and
its two guards (no UDFOERT/ANMELDT assignment may be disturbed, no date in the past).

No UI view exists for this yet (P2 design doc's "Phasing" -- this amendment adds no new UI surface);
--dry-run is this action's preview, matching every other koekken management command's own --dry-run
convention: run it first to see exactly what would change (how many Fridag rows, how many Vagt rows
deleted, which month(s) re-allocated and re-posted) before committing for real."""

import argparse
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from koekken.models import VagtRegel
from koekken.services import KoekkenAllocationError, declare_fridag


class Command(BaseCommand):
    help = (
        "Erklær en fridag for en dato (eller udvalgte vagttyper på den) -- opret Fridag-række(r), "
        "slet evt. allerede genererede vagter, genallokér og genbogfør de(n) berørte måned(er), og "
        "notificér berørte beboere. --dry-run viser konsekvensen uden at gennemføre den."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("date", type=str, help="YYYY-MM-DD")
        parser.add_argument(
            "--kind",
            action="append",
            dest="kinds",
            choices=[k.value for k in VagtRegel.Kind],
            default=None,
            help="Gentag for flere vagttyper. Udelades: hele dagen (alle vagttyper).",
        )
        parser.add_argument("--reason", type=str, default="", help="F.eks. 'Juleaften'.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        for_date = date.fromisoformat(str(opts["date"]))
        kinds_opt = opts["kinds"]
        kinds: list[str] = (
            list(kinds_opt) if isinstance(kinds_opt, list) else [k.value for k in VagtRegel.Kind]
        )
        reason = str(opts["reason"])
        dry_run = bool(opts["dry_run"])

        try:
            with transaction.atomic():
                result = declare_fridag(for_date, kinds, reason)
                if dry_run:
                    transaction.set_rollback(True)
        except KoekkenAllocationError as exc:
            raise CommandError(str(exc)) from exc

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{for_date}: {len(result.created)} fridag-række(r) oprettet, "
                f"{result.deleted_vagter} vagt(er) slettet, "
                f"{len(result.reallocated_months)} måned(er) genallokeret og genbogført."
            )
        )
