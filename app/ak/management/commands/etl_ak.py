"""ETL: legacy intern_alumne_aklog / intern_alumne_akstatus -> ak.AkEntry ledger (F-009).

Each legacy log row becomes a ledger entry. Because the legacy running total (`akstatus.totalkrydser`)
can diverge from the sum of the log rows (the old buggy bulk ops), we add a per-resident OPENING entry
equal to (legacy total − sum of that resident's log deltas) so the new derived balance matches the
balance residents currently see. Idempotent (rebuilds the ledger).
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ak.models import AkEntry
from core.etl import epoch_to_dt, fetch_all, resident_id_remap


class Command(BaseCommand):
    help = "Migrate AK krydser into the ledger from the legacy DB."

    @transaction.atomic
    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        from residents.api import Resident

        remap = resident_id_remap()
        resident_ids = set(Resident.objects.values_list("id", flat=True))

        AkEntry.objects.all().delete()

        log_sum: defaultdict[int, int] = defaultdict(int)
        skipped = 0
        entries = []
        for row in fetch_all("SELECT * FROM intern_alumne_aklog"):
            rid = remap.get(row["alumne_id"])
            if rid is None or rid not in resident_ids:
                skipped += 1
                continue
            delta = int(row["krydser"] or 0)
            log_sum[rid] += delta
            entries.append(
                AkEntry(
                    resident_id=rid,
                    delta=delta,
                    kind=AkEntry.Kind.LABOUR if delta > 0 else AkEntry.Kind.ADJUSTMENT,
                    reason=(row["comment"] or "").strip(),
                    created_at=epoch_to_dt(row["timestamp"]) or timezone.now(),
                )
            )
        AkEntry.objects.bulk_create(entries)

        opening = 0
        for row in fetch_all("SELECT * FROM intern_alumne_akstatus"):
            rid = remap.get(row["alumne_id"])
            if rid not in resident_ids:
                continue
            delta = int(row["totalkrydser"] or 0) - log_sum.get(rid, 0)
            if delta:
                AkEntry.objects.create(
                    resident_id=rid,
                    delta=delta,
                    kind=AkEntry.Kind.OPENING,
                    reason="Migreret startsaldo",
                    created_at=timezone.now(),
                )
                opening += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"AK: {len(entries)} log entries (skipped {skipped} for unknown residents), "
                f"{opening} opening-balance adjustments. Total ledger rows: {AkEntry.objects.count()}."
            )
        )
