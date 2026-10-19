"""Backfill the superseded værelsestjek reports the main ETL discards (F-005).

`etl_rooms` imports only `is_newest=1` — 59 of 679 rows in the legacy dump — so 620 historical
reports going back to 2018 never reached the new system. The "vis tidligere rapport" dropdown on the
inspection form is empty without them.

This is a **separate, additive** command rather than a fix to `etl_rooms` on purpose: that command
opens with `RoomCondition.objects.all().delete()`, so re-running it against production would destroy
every inspection written since cutover. This one never deletes and is idempotent, so it is safe to
run against a live database and safe to re-run.

`intern_room_condition` has no primary key, so `(room, recorded_at)` is the only handle on a report —
that pair is what dedupes here.

    python manage.py backfill_room_history --dry-run    # counts only, writes nothing
    python manage.py backfill_room_history
"""

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.etl import fetch_all, resident_id_remap
from core.models import Room
from residents.models import Resident
from rooms.models import RoomCondition, RoomConditionScore, RoomCriterion

from .etl_rooms import parse_kv


class Command(BaseCommand):
    help = "Import the superseded (is_newest=0) room-condition reports the main ETL skips."

    def add_arguments(self, parser: Any) -> None:  # noqa: ANN401 (Django's ArgumentParser)
        parser.add_argument("--dry-run", action="store_true", help="Count only; write nothing.")

    @transaction.atomic
    def handle(self, *args: object, **opts: object) -> None:
        dry_run = bool(opts.get("dry_run"))
        by_number = {r.number: r for r in Room.objects.all()}
        criteria = {c.code: c for c in RoomCriterion.objects.all()}
        remap = resident_id_remap()
        resident_ids = set(Resident.objects.values_list("id", flat=True))
        seen = {
            (room_id, recorded_at)
            for room_id, recorded_at in RoomCondition.objects.values_list("room_id", "recorded_at")
        }

        imported = scores = skipped_existing = skipped_room = 0
        for c in fetch_all("SELECT * FROM intern_room_condition WHERE is_newest=0 ORDER BY date"):
            room = by_number.get(c["room_id"])
            if room is None:
                skipped_room += 1
                continue
            # Make the legacy timestamp aware BEFORE comparing. MySQL hands back a naive datetime;
            # Django stores it interpreted as TIME_ZONE and reads it back as aware, so comparing the
            # raw naive value against `seen` never matches and every re-run would duplicate the lot.
            recorded_at = c["date"]
            if recorded_at is not None and timezone.is_naive(recorded_at):
                recorded_at = timezone.make_aware(recorded_at)
            if recorded_at is None or (room.id, recorded_at) in seen:
                skipped_existing += 1
                continue
            seen.add((room.id, recorded_at))
            imported += 1
            crit_scores = parse_kv(c["criteria"], ";")
            if dry_run:
                # Count what would be written so the dry-run figure is comparable to the real run.
                scores += sum(1 for code in crit_scores if code in criteria)
                continue

            rid = remap.get(c["alumne_id"])
            cond = RoomCondition.objects.create(
                room=room,
                resident_id=rid if rid in resident_ids else None,
                recorded_by_name=(c["alumne_fullname"] or "").strip(),
                recorded_at=recorded_at,
                is_current=False,  # superseded by definition — never touch the current report
            )
            comments = parse_kv(c["comments"], ";")
            images = parse_kv(c["images"], "|")
            for code, raw in crit_scores.items():
                crit = criteria.get(code)
                if crit is None:
                    continue  # renamed legacy codes (innerdoor, closetundersink, …) have no criterion
                raw = raw.strip()
                RoomConditionScore.objects.create(
                    condition=cond,
                    criterion=crit,
                    score=int(raw) if raw.lstrip("-").isdigit() else None,
                    comment=(comments.get(code) or "").strip(),
                    image=(images.get(code) or "").strip().lstrip("/"),
                )
                scores += 1

        verb = "Would import" if dry_run else "Imported"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {imported} historical conditions ({scores} scores); "
                f"skipped {skipped_existing} already present, {skipped_room} unmapped-room. "
                f"Total conditions now: {RoomCondition.objects.count()}."
            )
        )
        if dry_run:
            transaction.set_rollback(True)
