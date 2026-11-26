"""Demo board for Reparationer, used by ``manage.py seed_demo``.

One ticket per column, so the whole kanban, its "move to…" targets and both owners (Vicevært and
Repper) are visible on a fresh checkout, plus the cases that are otherwise slow to reach:

  * a card that has sat in its column long enough to turn **yellow**, and one long enough for **red**
    (RepairTask.column_age), which by hand takes a month or two of waiting;
  * a **Godkend af Repper** card past that column's shorter clock;
  * a ticket already **archived** by the nightly sweep, so the archive page has a row;
  * notes, one of them a photo with no text.

`seed_kitchen` adds what the kitchen account reported, since Reparationer is one of the few pages
open to it (spec/features/account-kinds.md).
"""

import random
from datetime import datetime, timedelta
from io import BytesIO

from django.core.files.base import ContentFile

from core.models import Room
from residents.models import Resident

from .models import RepairComment, RepairImage, RepairTask

Status = RepairTask.Status
Responsible = RepairTask.Responsible

TASKS = [
    # title, location, description, status, owner, age, days in column, archived, notes
    (
        "Dørpumpen ved porten smækker",
        "Hallen",
        "Døren smækker hårdt i, når den lukker. Den har gjort det i et par uger.",
        Status.NY,
        Responsible.VICEVAERT,
        timedelta(hours=6),
        0,
        False,
        [],
    ),
    (
        "Vaskemaskine 2 tager ikke imod mønter",
        "Vaskerummet",
        "Mønterne falder lige igennem. Maskine 1 og 3 virker.",
        Status.I_GANG,
        Responsible.VICEVAERT,
        timedelta(days=4),
        3,
        False,
        ["Jeg har ringet til servicefirmaet. De kommer torsdag."],
    ),
    # Yellow: past STALE_AFTER_DAYS in its column.
    (
        "Lampen i cykelkælderen blinker",
        "Cykelkælderen",
        "",
        Status.AFVENTER,
        Responsible.REPPER,
        timedelta(days=50),
        40,
        False,
        ["Venter på et nyt armatur, det gamle er udgået."],
    ),
    # Red: past OVERDUE_AFTER_DAYS.
    (
        "Fugerne i badeværelset på 2. sal er sorte",
        "Toilet, 2. sal",
        "Skimmel i hjørnet ved bruseren.",
        Status.AK_PROJEKT,
        Responsible.REPPER,
        timedelta(days=90),
        70,
        False,
        ["Tages på næste AK-dag.", "Jeg har købt fugemasse."],
    ),
    # Godkend af Repper has a shorter clock (COLUMN_AGE_DAYS): ten days is already yellow there.
    (
        "Radiatoren på værelset er kold",
        None,  # the first room, filled in below
        "Den bliver ikke varm, selv på 5.",
        Status.GODKENDT,
        Responsible.REPPER,
        timedelta(days=20),
        10,
        False,
        ["Udluftet. Den er varm nu."],
    ),
    (
        "Bordtennisbordet mangler net",
        "Festsalen",
        "",
        Status.FAERDIG,
        Responsible.REPPER,
        timedelta(days=12),
        2,
        False,
        [],
    ),
    # Swept by archive_finished_repairs: only on the archive page.
    (
        "Ølkælderens køleskab larmer",
        "Ølkælderen",
        "Kompressoren brummer hele natten.",
        Status.FAERDIG,
        Responsible.REPPER,
        timedelta(days=80),
        45,
        True,
        ["Ny kompressor sat i."],
    ),
]


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    """Create the demo board. Returns the number of tickets made."""
    if len(residents) < 3:
        return 0
    room = Room.objects.order_by("pk").first()
    made = []
    for index, (title, location, description, status, owner, age, in_column, archived, notes) in enumerate(
        TASKS
    ):
        task = _task(
            title=title,
            location=location if location is not None else (str(room) if room else ""),
            description=description,
            status=status,
            responsible=owner,
            reported_by=residents[index % len(residents)],
            created_at=now - age,
            status_changed_at=now - timedelta(days=in_column),
            archived_at=now - timedelta(days=in_column - 30) if archived else None,
        )
        made.append(task)
        for note_index, body in enumerate(notes):
            _note(task, rng.choice(residents), body, now - age + timedelta(days=note_index + 1))

    # A note that is only a photo ("se her"), which the form allows and the card counts separately.
    pumpen = made[0]
    photo_note = _note(pumpen, residents[1], "", now - timedelta(hours=5))
    RepairImage.objects.create(
        task=pumpen, comment=photo_note, file=ContentFile(_image("Dørpumpen", 0), name="doerpumpe.jpg")
    )
    return len(made)


def seed_kitchen(kitchen: Resident, residents: list[Resident], now: datetime) -> int:
    """What the kitchen account reported: one new ticket with a photo and an answer from a resident,
    so the kitchen's own card, its detail page and a note it did not write are all there to look at."""
    if not residents:
        return 0
    task = _task(
        title="Emhætten i det varme køkken larmer",
        location="Varme Køkken",
        description="Den hyler, når den kører på højeste trin. Vi bruger den kun på trin 2 indtil videre.",
        status=Status.NY,
        responsible=Responsible.VICEVAERT,
        reported_by=kitchen,
        created_at=now - timedelta(days=1, hours=2),
        status_changed_at=now - timedelta(days=1, hours=2),
        archived_at=None,
    )
    RepairImage.objects.create(task=task, file=ContentFile(_image("Emhætten", 1), name="emhaette.jpg"))
    _note(task, residents[0], "Jeg kigger på filtrene i morgen formiddag.", now - timedelta(hours=20))
    _note(task, kitchen, "Tak! Vi er her fra kl. 8.", now - timedelta(hours=19))
    return 1


def _task(created_at: datetime, **fields: object) -> RepairTask:
    task = RepairTask.objects.create(**fields)
    # created_at is auto_now_add, so backdating takes a second write.
    RepairTask.objects.filter(pk=task.pk).update(created_at=created_at, updated_at=created_at)
    return task


def _note(task: RepairTask, author: Resident, body: str, at: datetime) -> RepairComment:
    note = RepairComment.objects.create(task=task, author=author, body=body)
    RepairComment.objects.filter(pk=note.pk).update(created_at=at)
    return note


def _image(label: str, index: int) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (900, 600), ["#5b5b5b", "#7a5230"][index % 2])
    ImageDraw.Draw(image).text((40, 40), label, fill="white")
    output = BytesIO()
    image.save(output, format="JPEG", quality=82)
    return output.getvalue()
