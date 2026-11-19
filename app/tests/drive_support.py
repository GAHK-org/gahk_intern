"""What the Drive tests share: the day they run on, a group, residents with their months in it, a
logged-in member, and uploading through the no-bucket path.

Every test runs on 19 November 2026: the current period is `2026-27 Vinter`, the previous one
`2026 Sommer`, and the next one, `2027 Sommer`, gets its folder on 18 March 2027. A test module
imports the fixtures it uses by name; `_today` must be imported too, so the date is patched.
"""

from collections.abc import Callable, Iterator
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from core.models import Room, Workgroup
from drive import access
from drive.models import Folder, Space
from drive.periods import Period
from drive.structure import ensure_structure
from residents.models import Residency, Resident

TODAY = date(2026, 11, 19)
WINTER = Period(date(2026, 10, 1))
SUMMER = Period(date(2026, 4, 1))
OLD = Period(date(2025, 10, 1))
NEXT = Period(date(2027, 4, 1))


@pytest.fixture(autouse=True)
def _today(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.clock
    import drive.access
    import drive.shared
    import drive.structure

    for module in (core.clock, drive.access, drive.shared, drive.structure):
        monkeypatch.setattr(module, "current_date", lambda: TODAY)


@pytest.fixture
def group(db: None) -> Workgroup:
    return Workgroup.objects.create(name="Regnskabsgruppen-test")


def _period_folder(workgroup: Workgroup, period: Period) -> Folder:
    folder = Folder.objects.filter(
        workgroup=workgroup, kind=Folder.Kind.PERIOD, period_start=period.start
    ).first()
    if folder is None:  # an earlier period, made by hand: the system only makes current and next
        group_folder = Folder.objects.get(workgroup=workgroup, kind=Folder.Kind.GROUP)
        folder = Folder(
            parent=group_folder,
            name=period.name,
            kind=Folder.Kind.PERIOD,
            space=Space.GROUP,
            workgroup=workgroup,
            period_start=period.start,
        )
        folder.save()
    return folder


@pytest.fixture
def room(db: None) -> Room:
    return Room.objects.create(legacy_index=99, number=999, floor="stuen", side="mod gaden")


@pytest.fixture
def resident_in(room: Room, make_resident: Callable[..., Resident]) -> Callable[..., Resident]:
    """A resident and the months they lived here, with the group they were in each month."""
    counter = iter(range(1000))

    def _make(months: dict[tuple[int, int], Workgroup | None], roles: tuple[str, ...] = ()) -> Resident:
        resident = make_resident(email=f"drive{next(counter)}@gahk.dk", roles=roles)
        for (year, month), workgroup in months.items():
            Residency.objects.create(
                resident=resident, room=room, year=year, month=month, workgroup=workgroup
            )
        return resident

    return _make


@pytest.fixture
def open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "ACCESS_ROLES", None)


@pytest.fixture
def member(group: Workgroup, resident_in: Callable[..., Resident], open_gate: None) -> Iterator[Client]:
    client = Client()
    client.force_login(resident_in({(2026, 11): group}))
    ensure_structure(TODAY)
    yield client


def _upload(
    client: Client, folder: Folder, filename: str, content: bytes, on_conflict: str = "", **fields: str
) -> dict:
    response = client.post(
        reverse("drive:upload_direct", args=[folder.pk]),
        {
            "file": SimpleUploadedFile(filename, content, content_type="text/plain"),
            "on_conflict": on_conflict,
            **fields,
        },
    )
    body = response.json() if response["Content-Type"] == "application/json" else {}
    return {"status": response.status_code, **body}
