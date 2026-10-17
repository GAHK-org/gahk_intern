"""ETL: legacy gahk_ansoegninger -> admissions.Application (F-001).

Transforms: latin1->UTF-8 (via the utf8mb4 connection), `female` bool -> gender male/female, the
day/month/year/timestamp quartet -> a single aware `submitted_at`, and `receivedByAlumneId` ->
`received_by` (resolved through the resident dedup remap; 0/NULL/unknown -> None). Explicit fields
(no mass-assignment). Imports the full history; the 1-year retention is a separate scheduled purge
(`purge_applications`), not applied here, so the stats history is preserved.
"""

import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from admissions.models import Application
from core.etl import epoch_to_dt, fetch_all, resident_id_remap

TYPES = {c.value for c in Application.Type}


class Command(BaseCommand):
    help = "Migrate admission applications from the legacy DB."

    @transaction.atomic
    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        remap = resident_id_remap()
        from residents.api import Resident

        resident_ids = set(Resident.objects.values_list("id", flat=True))

        rows = fetch_all("SELECT * FROM gahk_ansoegninger")
        unknown_type = no_date = receiver_dropped = 0
        for r in rows:
            atype = (r["typeOfAnsoegning"] or "").strip()
            if atype not in TYPES:
                unknown_type += 1
                atype = atype or Application.Type.TOUR  # keep row; flag count

            submitted = epoch_to_dt(r["timestamp"])
            if submitted is None:
                # fall back to day/month/year at midnight if the epoch is junk
                try:
                    submitted = timezone.make_aware(
                        datetime.datetime.combine(
                            datetime.date(int(r["year"]), int(r["month"]), max(int(r["day"]), 1)),
                            datetime.time(),
                        )
                    )
                except (ValueError, TypeError):
                    submitted = timezone.now()
                no_date += 1

            recv = r["receivedByAlumneId"]
            recv_id = remap.get(recv) if recv else None
            if recv and recv_id not in resident_ids:
                recv_id = None
                receiver_dropped += 1

            Application.objects.update_or_create(
                id=r["id"],
                defaults={
                    "type": atype,
                    "full_name": (r["fullName"] or "").strip(),
                    "email": (r["email"] or "").strip(),
                    "gender": Application.Gender.FEMALE if r["female"] else Application.Gender.MALE,
                    "age": (r["age"] or "").strip(),
                    "study_year": (r["studyyear"] or "").strip(),
                    "year_left": (r["yearleft"] or "").strip(),
                    "university": (r["university"] or "").strip(),
                    "field_of_study": (r["fieldofstudy"] or "").strip(),
                    "occupation": (r["occupation"] or "").strip(),
                    "heard_about_us": (r["heardAboutUs"] or "").strip(),
                    "motivation": (r["motivation"] or "").strip(),
                    "submitted_at": submitted,
                    "received_by_id": recv_id,
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Applications: {len(rows)} imported "
                f"(unknown-type {unknown_type}, junk-date->reconstructed {no_date}, "
                f"receiver-unresolved {receiver_dropped})."
            )
        )
