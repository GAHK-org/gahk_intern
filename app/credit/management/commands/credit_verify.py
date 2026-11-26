"""Check GAHK-konto end to end: every chain, balance and book total, every anchor ever made, and,
given anchor files kept outside the database (from the bucket or an e-mail), that each still
matches the ledger. For an auditor, or after a scare; the nightly check does the recent part."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandParser

from credit import anchors, checks
from credit.models import Anchor


class Command(BaseCommand):
    help = "Verify GAHK-konto's chains, balances and anchors."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("anchor_files", nargs="*", help="Delivered anchor JSON files to compare.")

    def handle(self, *args: object, **opts: list[str]) -> None:
        faults = list(checks.run().faults)
        for anchor in Anchor.objects.order_by("id"):
            faults += anchors.verify(anchor)
        for name in opts["anchor_files"]:
            document = json.loads(Path(name).read_text())
            wanted = document["anchor_hash"]
            stored = next((a for a in Anchor.objects.all() if bytes(a.anchor_hash).hex() == wanted), None)
            if stored is None:
                faults.append(f"{name}: no anchor in the database has this hash.")
            else:
                faults += anchors.verify(stored)
        for fault in faults:
            self.stdout.write(self.style.ERROR(fault))
        if not faults:
            self.stdout.write(self.style.SUCCESS("GAHK-konto holds: every chain, balance, book and anchor."))
