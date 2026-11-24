"""Seed core.Room from the hard-coded room map in legacy intern/delt.php.

There is no rooms table in the dump — the floors/sides/numbers/notes are computed by a loop over
indices 0..61 in delt.php. Index 0 is the "Intet valgt" sentinel (not a real room) and is skipped;
indices 1..61 become Room rows. `legacy_index` is the kvotient `vaerelse_id`; `number` is the 3-digit
room number used elsewhere.
"""

from typing import cast

from django.core.management.base import BaseCommand

from audit import trail
from core.models import Room


def build_rooms() -> list[dict[str, str | int]]:
    """Return a list of dicts suitable for Room.objects.update_or_create(legacy_index=...)."""
    rooms = []
    room_on_floor = 0
    for i in range(62):
        room_on_floor += 1
        floor = side = note = None
        number = None
        if 1 <= i <= 8:
            if i == 1:
                room_on_floor = 1
            floor, side, number = "stuen", "mod gaden", room_on_floor
        elif i in (9, 10):
            floor, side = "stuen", "mod gården"
            note = "(røvhullet)" if i == 9 else None
            number = room_on_floor
        elif 11 <= i <= 19:
            floor, side = "1. sal", "mod gaden"
            if i == 11:
                room_on_floor = 1
            number = room_on_floor + 100
        elif 20 <= i <= 24:
            floor, side, number = "1. sal", "mod gården", room_on_floor + 100
        elif 25 <= i <= 33:
            floor, side = "2. sal", "mod gaden"
            if i == 25:
                room_on_floor = 1
            number = room_on_floor + 200
        elif 34 <= i <= 38:
            floor, side, number = "2. sal", "mod gården", room_on_floor + 200
        elif 39 <= i <= 47:
            floor, side = "3. sal", "mod gaden"
            if i == 39:
                room_on_floor = 1
            number = room_on_floor + 300
        elif 48 <= i <= 52:
            floor, side, number = "3. sal", "mod gården", room_on_floor + 300
        elif 53 <= i <= 56:
            floor, side = "4. sal", "mod gaden"
            note = {53: "(atelierværelse)", 54: "(arresten)", 55: "(fængslet)", 56: "(atelierværelse)"}.get(i)
            if i == 53:
                room_on_floor = 1
            number = room_on_floor + 400
        elif 57 <= i <= 61:
            floor, side, note = "4. sal", "mod gården", "(hemseværelse)"
            number = room_on_floor + 400
        else:
            continue  # i == 0 -> "Intet valgt"
        rooms.append(
            cast(
                "dict[str, str | int]",
                {"legacy_index": i, "number": number, "floor": floor, "side": side, "note": note or ""},
            )
        )
    return rooms


class Command(BaseCommand):
    help = "Seed core.Room from the legacy delt.php room map (idempotent)."

    def handle(self, *args, **opts) -> None:  # noqa: ANN002, ANN003
        n = 0
        # Loading the old site's room map is nobody's action: not one audit event per room.
        with trail.unaudited():
            for r in build_rooms():
                Room.objects.update_or_create(legacy_index=r["legacy_index"], defaults=r)
                n += 1
        self.stdout.write(
            self.style.SUCCESS(f"Seeded/updated {n} rooms (Room.objects={Room.objects.count()}).")
        )
