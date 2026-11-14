"""Diff legacy (MariaDB) row counts against the migrated clean (Postgres) data.

The scope's "diff against the running old site" for the *data* layer: for each domain, compare the legacy
source count to the clean count and flag anything unexpected. Requires the legacy MariaDB container up
(`task db:up`). Behavioural diffing against the live PHP site belongs in staging (see DEPLOY.md).
"""

from django.core.management.base import BaseCommand

from admissions.models import Application
from ak.models import AkEntry
from cms.models import NewsItem, Page, PylonEvent
from core.etl import fetch_all
from oelkaelder.models import Deposit, Product, Transaction
from residents.models import Residency, Resident
from rooms.models import KvotientApplication, RoomCondition
from stats.models import DailyVisitCount


def _legacy(sql: str) -> int:
    return fetch_all(sql)[0]["n"]


class Command(BaseCommand):
    help = "Compare legacy MariaDB counts to the migrated Postgres counts."

    def handle(self, *args: object, **opts: object) -> None:
        try:
            # Pages made here since (the /optagelse page from cms migration 0007, anything an editor
            # created) are not legacy rows: only the ids the ETL kept are compared.
            last_legacy_page = _legacy("SELECT COALESCE(MAX(id), 0) n FROM gahk_page")
        except Exception as e:  # MariaDB not up
            self.stderr.write(f"legacy DB unavailable ({e}); run `task db:up`.")
            return
        checks = [
            (
                "residents",
                "SELECT COUNT(*) n FROM intern_alumne",
                Resident.objects.count(),
                "clean ≤ legacy (dup emails merged, empty dropped)",
            ),
            (
                "residencies",
                "SELECT COUNT(*) n FROM intern_alumne_liste",
                Residency.objects.count(),
                "clean ≤ legacy (former residents not in intern_alumne dropped)",
            ),
            (
                "applications",
                "SELECT COUNT(*) n FROM gahk_ansoegninger",
                Application.objects.count(),
                "equal",
            ),
            (
                "pages",
                "SELECT COUNT(*) n FROM gahk_page",
                Page.objects.filter(pk__lte=last_legacy_page).count(),
                "equal",
            ),
            ("news", "SELECT COUNT(*) n FROM gahk_news", NewsItem.objects.count(), "equal"),
            (
                "pylon events",
                "SELECT COUNT(*) n FROM gahk_pylon_calendar",
                PylonEvent.objects.count(),
                "equal",
            ),
            (
                "ak log entries",
                "SELECT COUNT(*) n FROM intern_alumne_aklog",
                AkEntry.objects.filter(kind="labour").count()
                + AkEntry.objects.filter(kind="adjustment").count(),
                "clean ≤ legacy (former residents skipped); + opening entries not counted here",
            ),
            (
                "oel products",
                "SELECT COUNT(*) n FROM intern_oelkaelder_product",
                Product.objects.count(),
                "equal",
            ),
            (
                "oel transactions",
                "SELECT COUNT(*) n FROM intern_oelkaelder_transaction",
                Transaction.objects.count(),
                "equal",
            ),
            (
                "oel deposits",
                "SELECT COUNT(*) n FROM intern_oelkaelder_deposit",
                Deposit.objects.count(),
                "clean ≤ legacy (former-resident shoppers skipped)",
            ),
            (
                "kvotient apps",
                "SELECT COUNT(*) n FROM intern_kvotient_nyintern",
                KvotientApplication.objects.count(),
                "clean ≤ legacy (former residents skipped)",
            ),
            (
                "room conditions (current)",
                "SELECT COUNT(*) n FROM intern_room_condition WHERE is_newest=1",
                RoomCondition.objects.count(),
                "equal (only current migrated)",
            ),
            (
                "daily visit counts",
                "SELECT COUNT(*) n FROM gahk_counterdato",
                DailyVisitCount.objects.count(),
                "equal",
            ),
        ]
        self.stdout.write(f"{'domain':<28}{'legacy':>9}{'clean':>9}   note")
        self.stdout.write("-" * 78)
        for label, sql, clean, note in checks:
            try:
                leg = _legacy(sql)
            except Exception as e:  # MariaDB not up
                self.stderr.write(f"legacy DB unavailable ({e}); run `task db:up`.")
                return
            flag = "OK" if (leg == clean or ("≤" in note and clean <= leg)) else "CHECK"
            self.stdout.write(f"{label:<28}{leg:>9}{clean:>9}   [{flag}] {note}")
