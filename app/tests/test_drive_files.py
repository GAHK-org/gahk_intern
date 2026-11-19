"""Drive: renaming, moving, copying and deleting, the bin, versions, the warning about a file changed
since you downloaded it, the history, and the creator's notifications."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Workgroup
from drive import operations, services
from drive.models import Event, File, FileVersion, Folder, Space
from notifications.models import Notification
from residents.models import Resident

from .drive_support import (  # noqa: F401 - fixtures, used by name
    SUMMER,
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
def members(
    group: Workgroup, resident_in: Callable[..., Resident], open_gate: None
) -> tuple[Resident, Resident]:
    """Two current members of the group, and the structure for today."""
    from drive.structure import ensure_structure

    first = resident_in({(2026, 11): group})
    second = resident_in({(2026, 11): group})
    ensure_structure()
    return first, second


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _content(response) -> bytes:  # noqa: ANN001
    return b"".join(response.streaming_content)


def _notes(resident: Resident) -> list[str]:
    return list(Notification.objects.filter(recipient=resident).values_list("title", flat=True))


# --- Rename ----------------------------------------------------------------------------------------


def test_renaming_a_file_keeps_it_and_tells_its_creator(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    creator, other = members
    winter = _period_folder(group, WINTER)
    _upload(_client(creator), winter, "Budget.xlsx", b"one")
    file = File.objects.get(folder=winter)

    response = _client(other).post(reverse("drive:rename_file", args=[file.pk]), {"name": "Budget 2027.xlsx"})
    assert response.status_code == 302
    file.refresh_from_db()
    assert file.name == "Budget 2027.xlsx"
    assert file.events.filter(action=Event.Action.RENAMED, details__old="Budget.xlsx").exists()
    assert _notes(creator) == [f"{other.full_name} omdøbte din fil Budget.xlsx til Budget 2027.xlsx"]


def test_a_rename_to_a_name_in_use_is_refused_but_a_change_of_case_is_not(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    _upload(client, winter, "a.txt", b"a")
    _upload(client, winter, "b.txt", b"b")
    b = File.objects.get(name="b.txt")
    client.post(reverse("drive:rename_file", args=[b.pk]), {"name": "A.TXT"})
    b.refresh_from_db()
    assert b.name == "b.txt"
    client.post(reverse("drive:rename_file", args=[b.pk]), {"name": "B.txt"})
    b.refresh_from_db()
    assert b.name == "B.txt"


def test_the_systems_folders_cannot_be_renamed_and_residents_folders_can(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    client.post(reverse("drive:rename_folder", args=[winter.pk]), {"name": "Mit eget navn"})
    winter.refresh_from_db()
    assert winter.name == "2026-27 Vinter"

    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Referater"})
    folder = Folder.objects.get(parent=winter, name="Referater")
    client.post(reverse("drive:rename_folder", args=[folder.pk]), {"name": "Mødereferater"})
    folder.refresh_from_db()
    assert folder.name == "Mødereferater"


# --- Move and copy ---------------------------------------------------------------------------------


def test_moving_a_file_takes_it_there_and_tells_its_creator_once(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    creator, other = members
    winter = _period_folder(group, WINTER)
    _upload(_client(creator), winter, "a.txt", b"a")
    _upload(_client(creator), winter, "b.txt", b"b")
    client = _client(other)
    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Arkiveret"})
    target = Folder.objects.get(parent=winter, name="Arkiveret")
    ids = list(File.objects.filter(folder=winter).values_list("pk", flat=True))

    response = client.post(
        reverse("drive:move", args=[winter.pk]), {"op": "move", "to": target.pk, "file": ids}
    )
    assert response["Location"] == reverse("drive:folder", args=[target.pk])
    assert File.objects.filter(folder=target).count() == 2
    assert Event.objects.filter(action=Event.Action.MOVED).count() == 2
    assert _notes(creator) == [f"{other.full_name} flyttede 2 af dine filer"]


def test_a_move_where_the_name_is_taken_moves_nothing(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Ny"})
    target = Folder.objects.get(name="Ny")
    _upload(client, target, "a.txt", b"old")
    _upload(client, winter, "a.txt", b"new")
    _upload(client, winter, "b.txt", b"b")
    ids = list(File.objects.filter(folder=winter).values_list("pk", flat=True))
    client.post(reverse("drive:move", args=[winter.pk]), {"op": "move", "to": target.pk, "file": ids})
    assert File.objects.filter(folder=winter).count() == 2


def test_moving_a_folder_to_another_space_asks_then_takes_its_subtree_along(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Fest"})
    fest = Folder.objects.get(name="Fest")
    client.post(reverse("drive:create_folder", args=[fest.pk]), {"name": "Billeder"})
    pictures = Folder.objects.get(name="Billeder")
    _upload(client, pictures, "plan.txt", b"x")
    common_year = Folder.objects.get(kind=Folder.Kind.YEAR, period_start__year=2026)

    picker = client.get(
        reverse("drive:move", args=[winter.pk]), {"op": "move", "folder": fest.pk, "at": common_year.pk}
    )
    assert "Det ændrer, hvem der kan se det." in picker.content.decode()
    asked = client.post(
        reverse("drive:move", args=[winter.pk]), {"op": "move", "to": common_year.pk, "folder": fest.pk}
    )
    assert asked.status_code == 302
    assert "at=" in asked["Location"]
    assert Folder.objects.get(pk=fest.pk).parent_id == winter.pk

    client.post(
        reverse("drive:move", args=[winter.pk]),
        {"op": "move", "to": common_year.pk, "folder": fest.pk, "confirm": "1"},
    )
    pictures.refresh_from_db()
    assert (pictures.space, pictures.workgroup_id, pictures.period_start) == (
        Space.COMMON,
        None,
        common_year.period_start,
    )
    assert pictures.path == f"{common_year.path}{fest.pk}/{pictures.pk}/"
    assert services.subtree_totals([common_year])[common_year.pk][0] == 1


def test_a_folder_cannot_go_inside_itself(members: tuple[Resident, Resident], group: Workgroup) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Ydre"})
    outer = Folder.objects.get(name="Ydre")
    client.post(reverse("drive:create_folder", args=[outer.pk]), {"name": "Indre"})
    inner = Folder.objects.get(name="Indre")
    client.post(reverse("drive:move", args=[winter.pk]), {"op": "move", "to": inner.pk, "folder": outer.pk})
    outer.refresh_from_db()
    assert outer.parent_id == winter.pk


def test_a_copy_is_a_new_file_of_the_copier_sharing_the_bytes(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    creator, copier = members
    winter = _period_folder(group, WINTER)
    _upload(_client(creator), winter, "Skabelon.docx", b"template")
    original = File.objects.get(folder=winter)
    client = _client(copier)
    client.post(reverse("drive:move", args=[winter.pk]), {"op": "copy", "to": winter.pk, "file": original.pk})

    copy = File.objects.exclude(pk=original.pk).get()
    assert (copy.name, copy.created_by_id, copy.versions.count()) == ("Skabelon (2).docx", copier.pk, 1)
    assert copy.latest_version().key == original.latest_version().key
    assert _content(client.get(reverse("drive:download", args=[copy.pk]))) == b"template"
    assert list(copy.events.values_list("action", flat=True)) == [Event.Action.COPIED]


def test_copying_a_folder_copies_what_is_in_it(members: tuple[Resident, Resident], group: Workgroup) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    timeless = Folder.objects.get(workgroup=group, kind=Folder.Kind.TIMELESS)
    client.post(reverse("drive:create_folder", args=[timeless.pk]), {"name": "Skabeloner"})
    templates = Folder.objects.get(name="Skabeloner")
    client.post(reverse("drive:create_folder", args=[templates.pk]), {"name": "Breve"})
    _upload(client, templates, "a.txt", b"a")
    _upload(client, Folder.objects.get(name="Breve"), "b.txt", b"b")

    client.post(
        reverse("drive:move", args=[timeless.pk]), {"op": "copy", "to": winter.pk, "folder": templates.pk}
    )
    copied = Folder.objects.get(parent=winter, name="Skabeloner")
    assert copied.period_start == WINTER.start
    assert services.subtree_totals([copied])[copied.pk] == (2, 2)


def test_a_reader_cannot_move_rename_or_delete(
    members: tuple[Resident, Resident], group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    summer = _period_folder(group, SUMMER)
    file = File.objects.create(
        folder=summer, name="Regnskab.pdf", size=1, created_by=members[0], changed_at=timezone.now()
    )
    FileVersion.objects.create(file=file, number=1, key=services.new_key(), size=1)
    former = _client(resident_in({(2026, 6): group, (2026, 11): None}))
    assert former.get(reverse("drive:file", args=[file.pk])).status_code == 200

    former.post(reverse("drive:rename_file", args=[file.pk]), {"name": "x.pdf"})
    former.post(reverse("drive:delete", args=[summer.pk]), {"file": file.pk})
    winter = _period_folder(group, WINTER)
    former.post(reverse("drive:move", args=[summer.pk]), {"op": "move", "to": winter.pk, "file": file.pk})
    file.refresh_from_db()
    assert (file.name, file.deleted_at, file.folder_id) == ("Regnskab.pdf", None, summer.pk)


# --- Delete and the bin ----------------------------------------------------------------------------


def test_a_deleted_file_waits_in_the_bin_and_comes_back(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    creator, other = members
    winter = _period_folder(group, WINTER)
    _upload(_client(creator), winter, "Budget.xlsx", b"one")
    file = File.objects.get()
    client = _client(other)
    client.post(reverse("drive:delete", args=[winter.pk]), {"file": file.pk})

    file.refresh_from_db()
    assert file.deleted_at is not None
    listing = client.get(reverse("drive:folder", args=[winter.pk])).content.decode()
    assert reverse("drive:file", args=[file.pk]) not in listing
    assert client.get(reverse("drive:file", args=[file.pk])).status_code == 404
    assert _notes(creator) == [f"{other.full_name} slettede din fil Budget.xlsx"]
    group_folder = Folder.objects.get(workgroup=group, kind=Folder.Kind.GROUP)
    assert "Budget.xlsx" in client.get(reverse("drive:bin", args=[group_folder.pk])).content.decode()

    # The name is free meanwhile; the restored file takes a number.
    assert _upload(client, winter, "Budget.xlsx", b"new")["status"] == 201
    client.post(reverse("drive:restore", args=[file.pk]))
    file.refresh_from_db()
    assert (file.deleted_at, file.name) == (None, "Budget (2).xlsx")


def test_no_notification_about_your_own_files_or_a_folder_you_cannot_see(
    members: tuple[Resident, Resident], group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    creator, other = members
    winter = _period_folder(group, WINTER)
    _upload(_client(creator), winter, "mine.txt", b"x")
    _client(creator).post(reverse("drive:delete", args=[winter.pk]), {"file": File.objects.get().pk})
    assert _notes(creator) == []

    # Somebody who lives here but was never in the group cannot see the folder, so is not told.
    outsider = resident_in({(2026, 11): None})
    file = File.objects.create(
        folder=winter, name="old.txt", size=1, created_by=outsider, changed_at=timezone.now()
    )
    FileVersion.objects.create(file=file, number=1, key=services.new_key(), size=1)
    _client(other).post(reverse("drive:delete", args=[winter.pk]), {"file": file.pk})
    assert _notes(outsider) == []


def test_only_an_empty_folder_can_be_deleted_and_restoring_a_file_brings_it_back(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Fest"})
    fest = Folder.objects.get(name="Fest")
    _upload(client, fest, "plan.txt", b"x")
    file = File.objects.get()

    client.post(reverse("drive:delete", args=[winter.pk]), {"folder": fest.pk})
    fest.refresh_from_db()
    assert fest.deleted_at is None

    client.post(reverse("drive:delete", args=[fest.pk]), {"file": file.pk})
    client.post(reverse("drive:delete", args=[winter.pk]), {"folder": fest.pk})
    fest.refresh_from_db()
    assert fest.deleted_at is not None
    assert client.get(reverse("drive:folder", args=[fest.pk])).status_code == 404

    client.post(reverse("drive:restore", args=[file.pk]))
    fest.refresh_from_db()
    file.refresh_from_db()
    assert (fest.deleted_at, file.deleted_at, file.folder_id) == (None, None, fest.pk)


def test_the_bin_is_emptied_after_30_days_and_shared_bytes_stay(
    members: tuple[Resident, Resident],
    group: Workgroup,
    django_capture_on_commit_callbacks,  # noqa: ANN001
) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    _upload(client, winter, "a.txt", b"a")
    _upload(client, winter, "b.txt", b"b")
    a, b = File.objects.order_by("name")
    client.post(reverse("drive:move", args=[winter.pk]), {"op": "copy", "to": winter.pk, "file": b.pk})
    client.post(reverse("drive:delete", args=[winter.pk]), {"file": [a.pk, b.pk]})
    a_key, b_key = a.latest_version().key, b.latest_version().key
    store = services.storage()

    assert operations.empty_bin(timezone.now() + timedelta(days=29)) == (0, 0)
    with django_capture_on_commit_callbacks(execute=True):
        assert operations.empty_bin(timezone.now() + timedelta(days=31)) == (2, 0)
    assert not File.objects.filter(pk__in=[a.pk, b.pk]).exists()
    assert not store.exists(a_key)
    assert store.exists(b_key)  # the copy still points at it
    assert Event.objects.filter(action=Event.Action.DELETED, file=None, name="a.txt").exists()


# --- Versions --------------------------------------------------------------------------------------


def test_old_versions_download_and_restore_on_top(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    _upload(client, winter, "Budget.xlsx", b"one")
    _upload(client, winter, "Budget.xlsx", b"two", "replace")
    file = File.objects.get()
    assert _content(client.get(reverse("drive:download_version", args=[file.pk, 1]))) == b"one"

    client.post(reverse("drive:restore_version", args=[file.pk, 1]))
    latest = file.latest_version()
    assert (latest.number, latest.restored_from) == (3, 1)
    assert _content(client.get(reverse("drive:download", args=[file.pk]))) == b"one"
    page = client.get(reverse("drive:file", args=[file.pk])).content.decode()
    assert "Gendannede version 1 af Budget.xlsx som version 3" in page
    assert "Lagde version 2 af Budget.xlsx op" in page


def test_a_new_version_can_have_any_name(members: tuple[Resident, Resident], group: Workgroup) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    _upload(client, winter, "Budget.xlsx", b"one")
    assert (
        _upload(client, winter, "budget-final-v2.xlsx", b"two", "replace", name="Budget.xlsx")["status"]
        == 201
    )
    file = File.objects.get()
    assert (file.name, file.versions.count()) == ("Budget.xlsx", 2)


def test_replacing_a_file_changed_since_you_downloaded_it_asks_first(
    members: tuple[Resident, Resident], group: Workgroup
) -> None:
    mine, theirs = (_client(resident) for resident in members)
    winter = _period_folder(group, WINTER)
    _upload(mine, winter, "Budget.xlsx", b"one")
    file = File.objects.get()
    mine.get(reverse("drive:download", args=[file.pk]))
    # Your own new version is no surprise.
    assert _upload(mine, winter, "Budget.xlsx", b"two", "replace")["status"] == 201

    _upload(theirs, winter, "Budget.xlsx", b"three", "replace")
    stale = _upload(mine, winter, "Budget.xlsx", b"four", "replace")
    assert (stale["status"], stale["stale"]) == (409, True)
    assert members[1].full_name in stale["error"]
    assert _upload(mine, winter, "Budget.xlsx", b"four", "replace", ignore_stale="1")["status"] == 201
    assert file.versions.count() == 4


# --- History ---------------------------------------------------------------------------------------


def test_a_folder_shows_its_history(members: tuple[Resident, Resident], group: Workgroup) -> None:
    client = _client(members[0])
    winter = _period_folder(group, WINTER)
    _upload(client, winter, "a.txt", b"a")
    client.post(reverse("drive:rename_file", args=[File.objects.get().pk]), {"name": "b.txt"})
    page = client.get(reverse("drive:folder_history", args=[winter.pk])).content.decode()
    assert "Oprettede a.txt" in page
    assert "Omdøbte a.txt til b.txt" in page
