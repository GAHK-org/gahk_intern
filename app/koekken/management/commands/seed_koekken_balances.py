"""Launch migration (design doc "Launch migration"): import the known informal kitchen-credit
balances, rebase them so the house average is EXACTLY zero, and write one STARTSALDO KoekkenPost per
resident. This is what stops the old scoreboard's structural debt (up to ~28 h for a long-standing
resident, per the design doc's arithmetic) from becoming a real monetary penalty at move-out.

INPUT FORMAT (a judgement call — the design doc says only "known informal balances", not a format):
a JSON file, a flat array of objects:

    [{"email": "anna@gahk.dk", "balance_hours": 3.5}, {"email": "bo@gahk.dk", "balance_hours": -2}, ...]

Hours, not minutes: the informal scoreboard the design doc describes ("marks per task done, a stated
4 h/month obligation") was tracked in hours, so hours is the natural unit for whoever transcribes it.
Each value is converted to integer minutes (rounded) before rebasing — everything downstream of that
point is exact integer arithmetic, never float (see koekken.models' module docstring).

RE-RUNNING THIS RESETS THE ANCHOR (one STARTSALDO row per resident, upserted by resident+kind, not
accumulated) — the design doc's documented escape hatch: "If [the absolute-balance drift] ever does
grow uncomfortable, re-running the launch rebase as an admin action resets the anchor while keeping
the numbers exactly as legible." A second run is therefore a full replacement of the baseline, not an
addition on top of it.
"""

import argparse
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.clock import current_date
from koekken.models import KoekkenPost
from koekken.services import rebase_to_zero_mean, resolve_periode
from residents.models import Resident


class Command(BaseCommand):
    help = (
        "Importér kendte, uformelle køkken-saldi fra en JSON-fil og skriv én STARTSALDO-postering "
        "pr. beboer, rebaseret så husets gennemsnit bliver præcis nul. Se kommandoens docstring for "
        "filformatet."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("file", type=str, help="Sti til JSON-fil: [{'email', 'balance_hours'}, ...]")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        path = Path(str(opts["file"]))
        try:
            raw = json.loads(path.read_text())
        except OSError as exc:
            raise CommandError(f"Kunne ikke læse {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CommandError(f"Ugyldig JSON i {path}: {exc}") from exc

        entries: list[tuple[Resident, int]] = []
        for row in raw:
            email = row["email"]
            try:
                resident = Resident.objects.get(email__iexact=email)
            except Resident.DoesNotExist as exc:
                raise CommandError(f"Ukendt beboer: {email}") from exc
            minutes = round(float(row["balance_hours"]) * 60)
            entries.append((resident, minutes))

        if not entries:
            raise CommandError("Ingen saldi at importere.")

        rebased = rebase_to_zero_mean(entries)
        periode = resolve_periode(current_date())

        with transaction.atomic():
            written = 0
            for resident, _ in entries:
                KoekkenPost.objects.update_or_create(
                    resident=resident,
                    kind=KoekkenPost.Kind.STARTSALDO,
                    defaults={"delta_minutes": rebased[resident.pk], "periode": periode, "vagt": None},
                )
                written += 1
            if bool(opts["dry_run"]):
                transaction.set_rollback(True)

        prefix = "[dry-run] " if opts["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(f"{prefix}{written} STARTSALDO-postering(er) skrevet."))
