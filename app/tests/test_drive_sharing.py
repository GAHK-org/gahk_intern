"""Drive: sharing files and folders with residents and with embedsgrupper, `Delt med mig`, `Delt med
<gruppe>`, and what a share may and may not give."""

from collections.abc import Callable

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Workgroup
from drive import services, sharing
from drive.access import Level, Viewer
from drive.models import Event, File, FileVersion, Folder, Share
from drive.structure import ensure_structure
from notifications.models import Notification
from residents.models import Resident, Role

from .drive_support import (  # noqa: F401 - fixtures, used by name
    OLD,
    WINTER,
    _period_folder,
    _today,
    _upload,
    group,
    open_gate,
    resident_in,
    room,
)


@pytest.fixture
def other(db: None) -> Workgroup:
    return Workgroup.objects.create(name="Køkkengruppen-test")


@pytest.fixture
def people(
    resident_in: Callable[..., Resident], group: Workgroup, other: Workgroup, open_gate: None
) -> dict[str, Resident]:
    found = {
        "owner": resident_in({(2026, 11): group}),
        "friend": resident_in({(2026, 11): None}),
        "kitchen": resident_in({(2026, 11): other}),
        "former_kitchen": resident_in({(2026, 6): other, (2026, 11): None}),
        "inspector": resident_in({(2026, 11): None}, roles=(Role.INSPEKTION,)),
    }
    ensure_structure()
    return found


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _viewer(resident: Resident) -> Viewer:
    return Viewer.for_resident(resident)


def _file(owner: Resident, folder: Folder, name: str = "Budget.xlsx") -> File:
    assert _upload(_client(owner), folder, name, b"one")["status"] == 201
    return File.objects.get(folder=folder, name=name)


def _share(owner: Resident, *, access: str = "read", **target: object) -> Share:
    return sharing.share(_viewer(owner), access=access, actor=owner, **target)  # type: ignore[arg-type]


# --- With a resident -------------------------------------------------------------------------------


def test_a_file_shared_to_read_is_read_and_found_under_delt_med_mig(
    people: dict[str, Resident], group: Workgroup
) -> None:
    winter = _period_folder(group, WINTER)
    file = _file(people["owner"], winter)
    friend = _client(people["friend"])
    assert friend.get(reverse("drive:file", args=[file.pk])).status_code == 404

    _share(people["owner"], file=file, resident_id=people["friend"].pk)
    page = friend.get(reverse("drive:file", args=[file.pk])).content.decode()
    assert "Budget.xlsx" in page
    assert reverse("drive:folder", args=[winter.pk]) not in page  # the period is a name, not a way in
    assert "Upload ny version" not in page
    assert friend.get(reverse("drive:download", args=[file.pk])).status_code == 200
    assert "Budget.xlsx" in friend.get(reverse("drive:shared_with_me")).content.decode()
    assert friend.get(reverse("drive:folder", args=[winter.pk])).status_code == 404
    assert Notification.objects.filter(
        recipient=people["friend"], title__contains="delte Budget.xlsx"
    ).exists()


def test_an_edit_share_on_a_file_adds_versions_but_does_not_rename_or_delete(
    people: dict[str, Resident], group: Workgroup
) -> None:
    winter = _period_folder(group, WINTER)
    file = _file(people["owner"], winter)
    _share(people["owner"], file=file, resident_id=people["friend"].pk, access="edit")
    friend = _client(people["friend"])
    from django.core.files.uploadedfile import SimpleUploadedFile

    response = friend.post(
        reverse("drive:version_direct", args=[file.pk]),
        {"file": SimpleUploadedFile("mit-budget.xlsx", b"two")},
    )
    assert response.status_code == 201
    assert file.versions.count() == 2
    friend.post(reverse("drive:rename_file", args=[file.pk]), {"name": "Andet.xlsx"})
    friend.post(reverse("drive:delete", args=[winter.pk]), {"file": file.pk})
    file.refresh_from_db()
    assert (file.name, file.deleted_at) == ("Budget.xlsx", None)


def test_an_edit_share_on_a_folder_lets_the_recipient_work_inside_it(
    people: dict[str, Resident], group: Workgroup
) -> None:
    owner = _client(people["owner"])
    winter = _period_folder(group, WINTER)
    owner.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Festudvalg"})
    folder = Folder.objects.get(name="Festudvalg")
    _share(people["owner"], folder=folder, resident_id=people["friend"].pk, access="edit")

    friend = _client(people["friend"])
    assert _upload(friend, folder, "plan.txt", b"x")["status"] == 201
    friend.post(reverse("drive:create_folder", args=[folder.pk]), {"name": "Musik"})
    assert Folder.objects.filter(parent=folder, name="Musik").exists()
    friend.post(reverse("drive:rename_folder", args=[folder.pk]), {"name": "Mit"})
    folder.refresh_from_db()
    assert folder.name == "Festudvalg"
    # An edit share is not a right to share further.
    with pytest.raises(PermissionDenied):
        _share(people["friend"], folder=folder, resident_id=people["kitchen"].pk)


def test_an_edit_share_reads_only_once_the_period_is_closed(
    people: dict[str, Resident], group: Workgroup
) -> None:
    old = _period_folder(group, OLD)
    file = File.objects.create(folder=old, name="Gammel.pdf", size=1, changed_at=timezone.now())
    FileVersion.objects.create(file=file, number=1, key=services.new_key(), size=1)
    Share.objects.create(file=file, resident=people["friend"], can_edit=True)
    viewer = _viewer(people["friend"])
    assert viewer.file_level(file) == Level.READ
    assert not viewer.can_version(file)


def test_stopping_a_share_takes_the_access_away(people: dict[str, Resident], group: Workgroup) -> None:
    file = _file(people["owner"], _period_folder(group, WINTER))
    row = _share(people["owner"], file=file, resident_id=people["friend"].pk)
    sharing.stop(_viewer(people["owner"]), row, people["owner"])
    assert _viewer(people["friend"]).file_level(file) == Level.NONE
    lines = list(file.events.values_list("action", flat=True))
    assert Event.Action.SHARED in lines
    assert Event.Action.UNSHARED in lines


# --- With a group ----------------------------------------------------------------------------------


def test_a_group_share_appears_in_the_groups_matching_period_for_its_members_only(
    people: dict[str, Resident], group: Workgroup, other: Workgroup
) -> None:
    file = _file(people["owner"], _period_folder(group, WINTER))
    _share(people["owner"], file=file, workgroup_id=other.pk)

    theirs = _period_folder(other, WINTER)
    kitchen = _client(people["kitchen"])
    listing = kitchen.get(reverse("drive:folder", args=[theirs.pk])).content.decode()
    assert f"Delt med {other.name}" in listing
    page = kitchen.get(reverse("drive:shared_with_group", args=[theirs.pk])).content.decode()
    assert "Budget.xlsx" in page
    assert _viewer(people["kitchen"]).file_level(file) == Level.READ

    # Neither a former member, who was not in the group this period, nor Inspektionen, whose
    # oversight is of the group's own folders, not of what others share with it.
    assert _viewer(people["former_kitchen"]).file_level(file) == Level.NONE
    assert _viewer(people["inspector"]).file_level(file) < Level.READ


def test_only_what_is_in_a_groups_space_can_be_shared(people: dict[str, Resident]) -> None:
    common = Folder.objects.get(kind=Folder.Kind.TIMELESS, space="common")
    file = File.objects.create(folder=common, name="Regler.pdf", size=1, changed_at=timezone.now())
    with pytest.raises(ValidationError):
        _share(people["owner"], file=file, resident_id=people["friend"].pk)


def test_moving_a_shared_item_out_of_the_groups_space_ends_its_shares(
    people: dict[str, Resident], group: Workgroup
) -> None:
    owner = _client(people["owner"])
    winter = _period_folder(group, WINTER)
    owner.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Ud"})
    folder = Folder.objects.get(name="Ud")
    inner = _file(people["owner"], folder, "inde.txt")
    _share(people["owner"], folder=folder, resident_id=people["friend"].pk)
    _share(people["owner"], file=inner, resident_id=people["friend"].pk)
    year = Folder.objects.get(kind=Folder.Kind.YEAR, period_start__year=2026)

    picker = owner.get(
        reverse("drive:move", args=[winter.pk]), {"op": "move", "folder": folder.pk, "at": year.pk}
    )
    assert "holder op med at være delt (2 delinger)" in picker.content.decode()
    owner.post(
        reverse("drive:move", args=[winter.pk]),
        {"op": "move", "folder": folder.pk, "to": year.pk, "confirm": "1"},
    )
    assert not Share.objects.exists()
