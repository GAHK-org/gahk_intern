"""ETL: legacy gahk_page / gahk_news / gahk_pylon_calendar -> cms models (F-006/07/08).

Pages get a `slug` seeded from the legacy routes.php named-route map (public URLs preserved for 301s).
News/pylon dates rebuilt from their day/month/year (+ epoch). Titles are imported as plain text and
bodies through cms.sanitize, the same as the Hjemmeside editor saves them. Legacy /public/image/
references stay as they are; the import_legacy_images command moves them into the image library.
"""

import datetime
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from cms.models import NewsItem, Page, PylonEvent
from cms.sanitize import clean_html, plain_text_from_html
from core.etl import epoch_to_dt, fetch_all

# legacy routes.php: page id -> public slug (kept verbatim for SEO)
SLUG_BY_PAGE_ID = {
    1: "velkommen",
    2: "faciliteter",
    3: "kollegielivet",
    4: "legater",
    21: "kontakt",
    22: "vision",
    10: "faciliteter/vaerelse",
    11: "faciliteter/faellesomraade",
    12: "faciliteter/kokken",
    18: "legater/modtagne",
    14: "kollegielivet/historie",
    15: "kollegielivet/aaretsgang",
    20: "kollegielivet/alumnerne",
    16: "kollegielivet/selvstyre",
    17: "kollegielivet/bestyrelse",
}


def _date_from_ymd(row: dict[str, Any]) -> datetime.date | None:
    try:
        return datetime.date(int(row["year"]), int(row["month"]), max(int(row["day"]), 1))
    except (ValueError, TypeError):
        dt = epoch_to_dt(row.get("timestamp"))
        return dt.date() if dt else None


class Command(BaseCommand):
    help = "Migrate CMS pages, news and pylon events from the legacy DB."

    @transaction.atomic
    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        pages = fetch_all("SELECT * FROM gahk_page")
        for p in pages:
            Page.objects.update_or_create(
                id=p["id"],
                defaults={
                    "menu_category": p["menuCat"] or 0,
                    "slug": SLUG_BY_PAGE_ID.get(p["id"]),  # None when unmapped (NULL allows many)
                    # Plain text and sanitized HTML, as everything the editor saves is.
                    "header": plain_text_from_html(p["header"])[:255],
                    "body": clean_html(p["text"]) or "",
                    "background_image": (p["bgpic"] or "").strip(),
                },
            )

        news = fetch_all("SELECT * FROM gahk_news")
        for n in news:
            published = epoch_to_dt(n["timestamp"])
            if published is None:
                d = _date_from_ymd(n)
                from django.utils import timezone

                published = (
                    timezone.make_aware(datetime.datetime.combine(d, datetime.time()))
                    if d
                    else timezone.now()
                )
            NewsItem.objects.update_or_create(
                id=n["id"],
                defaults={
                    "title": plain_text_from_html(n["title"])[:255],
                    "body": clean_html(n["text"]) or "",
                    "published_at": published,
                },
            )

        pylon = fetch_all("SELECT * FROM gahk_pylon_calendar")
        pylon_skipped = 0
        for e in pylon:
            d = _date_from_ymd(e)
            if d is None:
                pylon_skipped += 1
                continue
            PylonEvent.objects.update_or_create(
                id=e["id"],
                defaults={
                    "title": (e["name"] or "").strip(),
                    "description": e["description"] or "",
                    "starts_on": d,
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"CMS: {len(pages)} pages ({sum(1 for p in pages if p['id'] in SLUG_BY_PAGE_ID)} slugged), "
                f"{len(news)} news, {len(pylon) - pylon_skipped} pylon events (skipped {pylon_skipped} undated)."
            )
        )
