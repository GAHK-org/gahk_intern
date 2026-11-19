"""Drive: locks (by hand and after a year), unlock and purge requests and their decisions, and a
space's activity page."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Workgroup
from drive import locks, services
from drive.access import Viewer
from drive.models import ApprovalRequest, Event, File, FileVersion, Folder
from drive.structure import ensure_structure
from notifications.models import Notification
from residents.models import Resident, Role

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

YEAR_AND_A_DAY = timedelta(days=366)


@pytest.fixture
def people(group: Workgroup, resident_in: Callable[..., Resident], open_gate: None) -> dict[str, Resident]:
    """Two members of the group, and two of Inspektionen, who are in no group."""
    found = {
        "member": resident_in({(2026, 11): group}),
        "other": resident_in({(2026, 11): group}),
        "inspector": resident_in({(2026, 11): None}, roles=(Role.INSPEKTION,)),
        "second": resident_in({(2026, 11): None}, roles=(Role.INSPEKTION,)),
    }
    ensure_structure()
    return found


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _file(client: Client, folder: Folder, name: str = "Budget.xlsx", content: bytes = b"one") -> File:
    assert _upload(client, folder, name, content)["status"] == 201
    return File.objects.get(folder=folder, name=name)


def _age(file: File, by: timedelta = YEAR_AND_A_DAY) -> None:
    FileVersion.objects.filter(file=file).update(created_at=timezone.now() - by)


def _viewer(resident: Resident) -> Viewer:
    return Viewer.for_resident(resident)


# --- Locks -----------------------------------------------------------------------------------------


def test_a_file_locks_a_year_after_its_last_version_and_a_folder_after_a_year_with_nothing_new(
    people: dict[str, Resident], group: Workgroup
) -> None:
    client = _client(people["member"])
    winter = _period_folder(group, WINTER)
    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Gammel"})
    old_folder = Folder.objects.get(name="Gammel")
    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Ny"})
    new_folder = Folder.objects.get(name="Ny")
    old = _file(client, old_folder, "old.txt")
    fresh = _file(client, new_folder, "fresh.txt")
    Folder.objects.filter(pk__in=[old_folder.pk, new_folder.pk]).update(
        created_at=timezone.now() - YEAR_AND_A_DAY
    )
    _age(old)

    assert locks.auto_lock() == (1, 1)
    old.refresh_from_db()
    fresh.refresh_from_db()
    assert (old.lock_active(), fresh.lock_active()) == (True, False)
    assert Folder.objects.get(pk=old_folder.pk).lock_active()
    assert not Folder.objects.get(pk=new_folder.pk).lock_active()
    assert not Folder.objects.filter(kind__in=Folder.SYSTEM_KINDS, locked_at__isnull=False).exists()
    assert old.events.filter(action=Event.Action.LOCKED, details__automatic=True).exists()


def test_a_locked_file_can_be_read_and_copied_but_not_changed(
    people: dict[str, Resident], group: Workgroup
) -> None:
    client = _client(people["member"])
    winter = _period_folder(group, WINTER)
    file = _file(client, winter)
    client.post(reverse("drive:lock_file", args=[file.pk]))
    file.refresh_from_db()
    assert file.lock_active()

    client.post(reverse("drive:rename_file", args=[file.pk]), {"name": "x.xlsx"})
    client.post(reverse("drive:delete", args=[winter.pk]), {"file": file.pk})
    replaced = _upload(client, winter, "Budget.xlsx", b"two", "replace")
    assert replaced["status"] == 400
    assert "låst" in replaced["error"]
    file.refresh_from_db()
    assert (file.name, file.deleted_at, file.versions.count()) == ("Budget.xlsx", None, 1)

    assert client.get(reverse("drive:download", args=[file.pk])).status_code == 200
    client.post(reverse("drive:move", args=[winter.pk]), {"op": "copy", "to": winter.pk, "file": file.pk})
    assert File.objects.filter(name="Budget (2).xlsx").exists()
    assert _upload(client, winter, "Budget.xlsx", b"two", "keep_both")["status"] == 201


def test_a_locked_folder_covers_everything_below_it(people: dict[str, Resident], group: Workgroup) -> None:
    client = _client(people["member"])
    winter = _period_folder(group, WINTER)
    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Regnskab"})
    folder = Folder.objects.get(name="Regnskab")
    client.post(reverse("drive:create_folder", args=[folder.pk]), {"name": "Bilag"})
    inner = Folder.objects.get(name="Bilag")
    file = _file(client, inner)
    client.post(reverse("drive:lock_folder", args=[folder.pk]))

    assert _upload(client, inner, "new.txt", b"x")["status"] == 403
    client.post(reverse("drive:rename_folder", args=[folder.pk]), {"name": "Andet"})
    client.post(reverse("drive:delete", args=[inner.pk]), {"file": file.pk})
    folder.refresh_from_db()
    file.refresh_from_db()
    assert (folder.name, file.deleted_at) == ("Regnskab", None)
    assert "Låst" in client.get(reverse("drive:folder", args=[inner.pk])).content.decode()


# --- Unlocking -------------------------------------------------------------------------------------


def test_an_approved_unlock_lasts_its_days_then_the_lock_holds_again(
    people: dict[str, Resident], group: Workgroup
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    file = _file(member, winter)
    member.post(reverse("drive:lock_file", args=[file.pk]))
    member.post(reverse("drive:unlock_file", args=[file.pk]), {"days": "7", "reason": "Rettelse"})
    request = ApprovalRequest.objects.get()
    assert (request.kind, request.days, request.where) == (
        ApprovalRequest.Kind.UNLOCK,
        7,
        "Embedsgrupper / Regnskabsgruppen-test / 2026-27 Vinter",
    )

    inspector = _client(people["inspector"])
    page = inspector.get(reverse("drive:requests")).content.decode()
    assert "Budget.xlsx" in page
    assert reverse("drive:file", args=[file.pk]) not in page  # names and paths, never a way in
    inspector.post(reverse("drive:decide", args=[request.pk]), {"decision": "approve"})

    file.refresh_from_db()
    assert not file.lock_active()
    assert file.lock_active(timezone.now() + timedelta(days=8))
    assert Notification.objects.get(recipient=people["member"]).title == (
        "Din anmodning om at låse Budget.xlsx op er godkendt"
    )
    assert _upload(member, winter, "Budget.xlsx", b"two", "replace")["status"] == 201


def test_nobody_decides_their_own_request_and_a_rejection_needs_a_reason(
    people: dict[str, Resident], group: Workgroup
) -> None:
    file = _file(_client(people["member"]), _period_folder(group, WINTER))
    locks.lock_file(_viewer(people["member"]), file, people["member"])
    request = locks.request_unlock(_viewer(people["member"]), file, 3, "Mangler en side", people["member"])

    from django.core.exceptions import ValidationError

    inspector = people["inspector"]
    with pytest.raises(ValidationError):
        locks.decide(_viewer(inspector), request, approve=False, reason="", actor=inspector)
    locks.decide(_viewer(inspector), request, approve=False, reason="Lav en kopi", actor=inspector)
    request.refresh_from_db()
    assert request.state == ApprovalRequest.State.REJECTED
    note = Notification.objects.get(recipient=people["member"])
    assert (note.title, note.body) == ("Din anmodning om at låse Budget.xlsx op er afvist", "Lav en kopi")

    own = locks.request_unlock(_viewer(people["member"]), file, 3, "Igen", people["member"])
    own.requested_by = inspector
    own.save()
    with pytest.raises(ValidationError):
        locks.decide(_viewer(inspector), own, approve=True, reason="", actor=inspector)


def test_unlocking_never_widens_access(
    people: dict[str, Resident], group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    """A former member reads last period, and cannot ask to edit it."""
    from django.core.exceptions import PermissionDenied

    summer = _period_folder(group, SUMMER)
    file = File.objects.create(folder=summer, name="Regnskab.pdf", size=1, changed_at=timezone.now())
    FileVersion.objects.create(file=file, number=1, key=services.new_key(), size=1)
    File.objects.filter(pk=file.pk).update(locked_at=timezone.now())
    file.refresh_from_db()
    former = resident_in({(2026, 6): group, (2026, 11): None})
    with pytest.raises(PermissionDenied):
        locks.request_unlock(_viewer(former), file, 3, "Vil rette", former)


def test_unlocking_a_folder_leaves_its_files_locked(people: dict[str, Resident], group: Workgroup) -> None:
    member = people["member"]
    client = _client(member)
    winter = _period_folder(group, WINTER)
    client.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Gammelt"})
    folder = Folder.objects.get(name="Gammelt")
    file = _file(client, folder)
    locks.lock_file(_viewer(member), file, member)
    locks.lock_folder(_viewer(member), folder, member)
    request = locks.request_unlock(_viewer(member), folder, 5, "Et nyt bilag", member)
    locks.decide(_viewer(people["inspector"]), request, approve=True, reason="", actor=people["inspector"])

    assert _upload(client, folder, "Bilag.pdf", b"x")["status"] == 201
    file.refresh_from_db()
    assert file.lock_active()


# --- Purging ---------------------------------------------------------------------------------------


def test_an_old_version_is_purged_only_with_a_second_person(
    people: dict[str, Resident],
    group: Workgroup,
    django_capture_on_commit_callbacks,  # noqa: ANN001
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    file = _file(member, winter, content=b"secret")
    _upload(member, winter, "Budget.xlsx", b"two", "replace")
    first_key = file.versions.get(number=1).key

    member.post(reverse("drive:purge_request", args=[file.pk]), {"number": "2", "reason": "Nyeste"})
    assert not ApprovalRequest.objects.exists()  # the newest version is not purged; bin the file
    member.post(reverse("drive:purge_request", args=[file.pk]), {"number": "1", "reason": "Persondata"})
    request = ApprovalRequest.objects.get()

    with django_capture_on_commit_callbacks(execute=True):
        _client(people["inspector"]).post(reverse("drive:decide", args=[request.pk]), {"decision": "approve"})
    assert list(file.versions.values_list("number", flat=True)) == [2]
    assert not services.storage().exists(first_key)
    line = file.events.get(action=Event.Action.PURGED)
    assert line.summary == (
        f"Version 1 af Budget.xlsx slettet permanent af {people['member'].full_name}, "
        f"godkendt af {people['inspector'].full_name}"
    )


def test_a_binned_file_is_purged_from_the_bin(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    file = _file(member, winter)
    member.post(reverse("drive:purge_request", args=[file.pk]), {"reason": "Fejl"})
    assert not ApprovalRequest.objects.exists()  # only from the bin
    member.post(reverse("drive:delete", args=[winter.pk]), {"file": file.pk})
    member.post(reverse("drive:purge_request", args=[file.pk]), {"reason": "Fejl", "bin": winter.pk})
    request = ApprovalRequest.objects.get()
    locks.decide(_viewer(people["inspector"]), request, approve=True, reason="", actor=people["inspector"])
    assert not File.objects.filter(pk=file.pk).exists()
    assert Event.objects.filter(action=Event.Action.PURGED, file=None, name="Budget.xlsx").exists()


def test_the_requests_page_is_for_admins_and_inspektionen(people: dict[str, Resident]) -> None:
    assert _client(people["member"]).get(reverse("drive:requests")).status_code == 403
    assert _client(people["inspector"]).get(reverse("drive:requests")).status_code == 200


# --- Activity --------------------------------------------------------------------------------------


def test_a_groups_activity_shows_each_line_to_who_could_read_it(
    people: dict[str, Resident], group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    timeless = Folder.objects.get(workgroup=group, kind=Folder.Kind.TIMELESS)
    _file(member, winter, "Nu.txt")
    _file(member, timeless, "Altid.txt")
    group_folder = Folder.objects.get(workgroup=group, kind=Folder.Kind.GROUP)
    url = reverse("drive:activity", args=[group_folder.pk])

    mine = member.get(url).content.decode()
    assert "Oprettede Nu.txt" in mine
    assert "Oprettede Altid.txt" in mine
    # Inspektionen reads Uden periode, and the current period only once it has ended.
    theirs = _client(people["inspector"]).get(url).content.decode()
    assert "Oprettede Altid.txt" in theirs
    assert "Oprettede Nu.txt" not in theirs
    assert _client(resident_in({(2026, 11): None})).get(url).status_code == 404
