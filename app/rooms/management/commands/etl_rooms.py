"""ETL: rooms domain (F-004 kvotient + F-005 condition).

kvotient tables map straight across (resolving residents via the dedup remap, rooms via Room.legacy_index
= vaerelse_id). The condition inspection normalizes the legacy delimited blobs into RoomConditionScore
rows: `criteria` = "id:score;…", `comments` = "id:comment;…", `images` = "id:path|…". Only the current
state (is_newest=1) is migrated (decided). room_id is the Room *number*.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.etl import epoch_to_dt, fetch_all, resident_id_remap
from core.models import Room
from rooms.models import (
    KvotientApplication,
    KvotientOrlov,
    KvotientPriority,
    RoomCondition,
    RoomConditionScore,
    RoomCriterion,
    RoomOffer,
)


def parse_kv(blob: str | None, sep: str) -> dict[str, str]:
    out = {}
    for part in (blob or "").split(sep):
        if not part:
            continue
        key, _, value = part.partition(":")
        out[key.strip()] = value
    return out


class Command(BaseCommand):
    help = "Migrate kvotient applications and room conditions from the legacy DB."

    @transaction.atomic
    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        from residents.models import Resident

        remap = resident_id_remap()
        resident_ids = set(Resident.objects.values_list("id", flat=True))
        by_index = {r.legacy_index: r for r in Room.objects.all()}
        by_number = {r.number: r for r in Room.objects.all()}

        # ---- kvotient applications / priorities / orlov / offers ----
        app_skipped = 0
        for a in fetch_all("SELECT * FROM intern_kvotient_nyintern"):
            rid = remap.get(a["alumne_id"])
            if rid not in resident_ids:
                app_skipped += 1
                continue
            KvotientApplication.objects.update_or_create(
                id=a["ID"],
                defaults={
                    "resident_id": rid,
                    "move_month": a["moveMonth"],
                    "move_in_month": a["moveInMonth"],
                    "done_studying_month": a["doneStudyingMonth"],
                    "k": a["K"],
                    "apply_datetime": epoch_to_dt(a["applyDatetime"]) or timezone.now(),
                },
            )
        app_ids = set(KvotientApplication.objects.values_list("id", flat=True))

        KvotientPriority.objects.all().delete()
        prio_skipped = 0
        for p in fetch_all("SELECT * FROM intern_kvotient_priority_nyintern"):
            room = by_index.get(p["vaerelse_id"])
            if p["ansoegnings_id"] not in app_ids or room is None:
                prio_skipped += 1
                continue
            KvotientPriority.objects.create(
                application_id=p["ansoegnings_id"],
                room=room,
                priority=p["priority"],
                month=p["month"] or None,
            )

        KvotientOrlov.objects.all().delete()
        for o in fetch_all("SELECT * FROM intern_kvotient_orlov_nyintern"):
            if o["ansoegnings_id"] not in app_ids:
                continue
            KvotientOrlov.objects.create(
                application_id=o["ansoegnings_id"],
                start_month=o["orlov_start"],
                end_month=o["orlov_end"],
            )

        offers = 0
        for f in fetch_all("SELECT * FROM intern_kvotient_offer_nyintern"):
            room = by_index.get(f["vaerelses_id"])
            if room is None:
                continue
            RoomOffer.objects.update_or_create(id=f["id"], defaults={"room": room, "month": f["month"]})
            offers += 1

        # ---- room criteria ----
        for c in fetch_all("SELECT * FROM intern_room_criteria"):
            RoomCriterion.objects.update_or_create(
                code=c["id"],
                defaults={"name": c["name"], "description": c["description"] or "", "options": c["options"]},
            )
        criteria = {c.code: c for c in RoomCriterion.objects.all()}

        # ---- room conditions (rebuild) + normalized scores ----
        # WARNING: this delete makes the command destructive on re-run — it removes inspections written
        # in the new system too. Never run this against production after cutover; use the additive
        # `backfill_room_history` command instead.
        RoomCondition.objects.all().delete()
        cond_skipped_room = scores = 0
        # All reports, not just is_newest=1: the superseded ones are the "vis tidligere rapport"
        # history on the inspection form (F-005). 679 rows in the dump, of which 620 are superseded.
        for c in fetch_all("SELECT * FROM intern_room_condition ORDER BY date"):
            room = by_number.get(c["room_id"])
            if room is None:
                cond_skipped_room += 1
                continue
            rid = remap.get(c["alumne_id"])
            cond = RoomCondition.objects.create(
                room=room,
                resident_id=rid if rid in resident_ids else None,
                recorded_by_name=(c["alumne_fullname"] or "").strip(),
                recorded_at=c["date"] or timezone.now(),
                is_current=bool(c["is_newest"]),
            )
            crit_scores = parse_kv(c["criteria"], ";")
            comments = parse_kv(c["comments"], ";")
            images = parse_kv(c["images"], "|")
            for code in crit_scores:
                crit = criteria.get(code)
                if crit is None:
                    continue
                raw = crit_scores.get(code, "").strip()
                RoomConditionScore.objects.create(
                    condition=cond,
                    criterion=crit,
                    score=int(raw) if raw.lstrip("-").isdigit() else None,
                    comment=(comments.get(code) or "").strip(),
                    image=(images.get(code) or "").strip().lstrip("/"),
                )
                scores += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Kvotient: {KvotientApplication.objects.count()} applications (skipped {app_skipped}), "
                f"{KvotientPriority.objects.count()} priorities (skipped {prio_skipped}), "
                f"{KvotientOrlov.objects.count()} orlov, {offers} offers. "
                f"Conditions: {RoomCondition.objects.count()} current (skipped {cond_skipped_room} unmapped-room), "
                f"{scores} scores; {len(criteria)} criteria."
            )
        )
