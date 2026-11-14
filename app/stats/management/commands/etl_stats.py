"""ETL: legacy gahk_counterdato -> stats.DailyVisitCount (F-012).

Only the per-date aggregate is migrated (for the visitor chart's history). The per-IP `gahk_counter`
is NOT migrated — it was operational dedup state. gahk.dk now counts visits itself (no IPs stored)
and intern pulls the day totals (stats.services). Legacy date format is "dd/mm-YYYY".
"""

import datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from core.etl import fetch_all
from stats.models import DailyVisitCount


class Command(BaseCommand):
    help = "Migrate the per-date visit counter from the legacy DB."

    @transaction.atomic
    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        rows = fetch_all("SELECT dato, count FROM gahk_counterdato")
        ok = bad = 0
        for r in rows:
            try:
                # Legacy value is a pure date ("dd/mm-yyyy"); we take .date() immediately, so the
                # timezone is irrelevant and adding tzinfo would be noise (hence the suppression).
                d = datetime.datetime.strptime((r["dato"] or "").strip(), "%d/%m-%Y").date()  # noqa: DTZ007
            except ValueError:
                bad += 1
                continue
            DailyVisitCount.objects.update_or_create(date=d, defaults={"count": r["count"] or 0})
            ok += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"DailyVisitCount: {ok} days imported (skipped {bad} unparseable). "
                f"gahk_counter (per-IP) not migrated by design."
            )
        )
