"""ETL: legacy intern_alumne / gahk_admin_user / intern_alumne_liste -> clean residents domain.

Applies the decided transforms (02-schema-etl.md §5, 99-index.md F-010):
  * dedupe by lower/trim email, keeping the highest legacy ID; empty-email rows dropped.
  * duplicate accounts are *merged*: their residencies/roles re-point to the kept resident.
  * legacy unsalted sha256 password imported as `gahk_sha256$$<hex>` (upgrade-on-login).
  * roles (gahk_admin_user flags) seeded for the *active* (newest) month only — roles are monthly.
  * residency per month decoded from monthNumber; workgroup/cleaning looked up by name.
  * fylgje -> sponsor resolved by unambiguous full-name match; otherwise kept as fylgje_raw.

Idempotent (keyed on preserved PKs). Run `seed_rooms` first.
"""

import datetime as dt
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.etl import decode_month_number, fetch_all
from core.models import Cleaning, Room, Workgroup
from residents.models import Residency, Resident, Role, RoleAssignment

FLAG_TO_ROLE = [
    ("indstilling", Role.INDSTILLING),
    ("inspektion", Role.INSPEKTION),
    ("kokkengruppe", Role.KOKKENGRUPPE),
    ("ak", Role.AK),
    ("oelkaelder", Role.OELKAELDER),
    ("administrator", Role.ADMINISTRATOR),
    # legacy `editpage` intentionally omitted — no CMS editing
]


def _date(value: dt.datetime | dt.date | None) -> dt.date | None:
    return value if isinstance(value, dt.date) else None


class Command(BaseCommand):
    help = "Migrate residents, monthly roles and residency from the legacy DB."

    @transaction.atomic
    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        if not Room.objects.exists():
            self.stderr.write("No rooms — run `manage.py seed_rooms` first.")
            return

        # ---- 1. dedupe/merge residents by normalized email -------------------------------------
        rows = fetch_all("SELECT * FROM intern_alumne")
        by_email: dict[str, list[dict[str, Any]]] = {}
        dropped_empty = 0
        for r in rows:
            key = (r["email"] or "").strip().lower()
            if not key:
                dropped_empty += 1
                continue
            by_email.setdefault(key, []).append(r)

        id_remap = {}  # every legacy ID -> the kept resident's ID (duplicates merge into the newest)
        kept = []
        for group in by_email.values():
            group.sort(key=lambda r: r["ID"])
            keep = group[-1]
            kept.append(keep)
            for r in group:
                id_remap[r["ID"]] = keep["ID"]
        merged = len(rows) - dropped_empty - len(kept)

        for r in kept:
            pw = (r["password"] or "").strip()
            password = f"gahk_sha256$${pw}" if pw else "!unusable"
            Resident.objects.update_or_create(
                id=r["ID"],
                defaults={
                    "email": r["email"].strip().lower(),
                    "first_name": (r["firstName"] or "").strip(),
                    "last_name": (r["lastName"] or "").strip(),
                    "phone": (r["phone"] or "").strip(),
                    "birthday": _date(r["birthday"]),
                    "move_in_date": _date(r["moveInDay"]),
                    "move_out_date": _date(r["moveOutDay"]),
                    "study": (r["study"] or "").strip(),
                    "fylgje_raw": (r["fylgje"] or "").strip(),
                    "password": password,
                    "is_active": True,
                    "is_staff": False,
                },
            )

        # ---- 2. active period (newest published month) -----------------------------------------
        mn = fetch_all("SELECT MAX(monthNumber) AS m FROM intern_alumne_liste")[0]["m"]
        decoded = decode_month_number(mn)
        if decoded is None:
            raise CommandError("No months in intern_alumne_liste — nothing to migrate.")
        active_year, active_month = decoded

        # ---- 3. roles for the active month (gahk_admin_user flags) ------------------------------
        RoleAssignment.objects.all().delete()
        role_residents = set()
        for a in fetch_all("SELECT * FROM gahk_admin_user"):
            rid = id_remap.get(a["alumne_id"])
            if not rid:
                continue
            for flag, role in FLAG_TO_ROLE:
                if a[flag]:
                    RoleAssignment.objects.update_or_create(
                        resident_id=rid, role=role, year=active_year, month=active_month
                    )
                    role_residents.add(rid)
        Resident.objects.filter(id__in=role_residents).update(is_staff=True)

        # ---- 4. residency per month -------------------------------------------------------------
        rooms_by_number = {room.number: room for room in Room.objects.all()}
        wg_cache: dict[str, Workgroup] = {}
        cl_cache: dict[str, Cleaning] = {}
        orphan_resident = orphan_room = residencies = 0
        for row in fetch_all("SELECT * FROM intern_alumne_liste"):
            rid = id_remap.get(row["alumne_ID"])
            if not rid:
                orphan_resident += 1
                continue
            room = rooms_by_number.get(row["room"])
            if room is None:
                orphan_room += 1
                continue
            decoded = decode_month_number(row["monthNumber"])
            if decoded is None:
                raise CommandError(f"Residency row for {row['alumne_ID']} has no monthNumber.")
            year, month = decoded
            wg = cl = None
            wgname = (row["workgroup"] or "").strip()
            if wgname:
                wg = wg_cache.get(wgname) or Workgroup.objects.get_or_create(name=wgname)[0]
                wg_cache[wgname] = wg
            clname = (row["cleaning"] or "").strip()
            if clname:
                cl = cl_cache.get(clname) or Cleaning.objects.get_or_create(name=clname)[0]
                cl_cache[clname] = cl
            Residency.objects.update_or_create(
                resident_id=rid,
                year=year,
                month=month,
                defaults={"room": room, "workgroup": wg, "cleaning": cl},
            )
            residencies += 1

        # ---- 5. fylgje -> sponsor (unambiguous full-name match) --------------------------------
        name_index: dict[str, list[int]] = {}
        for rid_, fn, ln in Resident.objects.values_list("id", "first_name", "last_name"):
            name_index.setdefault(f"{fn} {ln}".strip().lower(), []).append(rid_)
        sponsor_set = sponsor_ambiguous = 0
        for res in Resident.objects.exclude(fylgje_raw="").only("id", "fylgje_raw"):
            matches = name_index.get(res.fylgje_raw.strip().lower(), [])
            matches = [m for m in matches if m != res.id]
            if len(matches) == 1:
                Resident.objects.filter(id=res.id).update(sponsor_id=matches[0])
                sponsor_set += 1
            elif len(matches) > 1:
                sponsor_ambiguous += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Residents: {len(kept)} kept (merged {merged} dup, dropped {dropped_empty} empty-email). "
                f"Active period {active_year}-{active_month:02d}: {RoleAssignment.objects.count()} role "
                f"assignments across {len(role_residents)} residents. Residencies: {residencies} "
                f"(orphan-resident {orphan_resident}, unmapped-room {orphan_room}). "
                f"Sponsors set {sponsor_set} (ambiguous {sponsor_ambiguous})."
            )
        )
