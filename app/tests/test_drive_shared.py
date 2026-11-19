"""Drive: Delte mapper - creating one, its members and roles, what others see, expiry with deletion
or archiving, and its requests and activity."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Workgroup
from drive import locks, services, shared
from drive.access import Level, Viewer
from drive.models import ApprovalRequest, Event, File, Folder, SharedFolder, SharedMember
from drive.structure import ensure_structure
from notifications.models import Notification
from residents.models import Residency, Resident, Role

from .drive_support import (  # noqa: F401 - fixtures, used by name
    TODAY,
    WINTER,
    _period_folder,
    _today,
    _upload,
    group,
    open_gate,
    resident_in,
    room,
)

SOON = TODAY + timedelta(days=60)


@pytest.fixture
def people(resident_in: Callable[..., Resident], group: Workgroup, open_gate: None) -> dict[str, Resident]:
    found = {name: resident_in({(2026, 11): group}) for name in ("anna", "bo", "cecilie", "dan")}
    found["inspector"] = resident_in({(2026, 11): None}, roles=(Role.INSPEKTION,))
    found["outsider"] = resident_in({(2026, 11): None})
    ensure_structure()
    return found


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _viewer(resident: Resident) -> Viewer:
    return Viewer.for_resident(resident)


def _make(people: dict[str, Resident], name: str = "Temafest", on_expiry: str = "archive") -> Folder:
    anna = people["anna"]
    return shared.create(
        _viewer(anna),
        name=name,
        members=[people["bo"].pk, people["cecilie"].pk],
        expires_on=SOON,
        on_expiry=on_expiry,
        actor=anna,
    )


# --- Creating and seeing ---------------------------------------------------------------------------


def test_creating_one_needs_three_members_and_tells_the_invited(people: dict[str, Resident]) -> None:
    anna = people["anna"]
    with pytest.raises(ValidationError):
        shared.create(
            _viewer(anna),
            name="To",
            members=[people["bo"].pk],
            expires_on=SOON,
            on_expiry="archive",
            actor=anna,
        )

    response = _client(anna).post(
        reverse("drive:create_shared"),
        {
            "name": "Temafest",
            "members": [people["bo"].pk, people["cecilie"].pk],
            "expires_on": SOON.isoformat(),
            "on_expiry": "archive",
        },
    )
    folder = Folder.objects.get(kind=Folder.Kind.SHARED)
    assert response["Location"] == reverse("drive:folder", args=[folder.pk])
    roles = dict(folder.shared.members.values_list("resident_id", "role"))
    assert roles == {anna.pk: "moderator", people["bo"].pk: "edit", people["cecilie"].pk: "edit"}
    assert Notification.objects.filter(
        recipient=people["bo"], title__contains="delte mappen Temafest"
    ).exists()


def test_only_members_see_it_and_overseers_see_only_its_number(people: dict[str, Resident]) -> None:
    folder = _make(people)
    root = Folder.objects.get(kind=Folder.Kind.SHARED_ROOT)
    url = reverse("drive:folder", args=[root.pk])

    assert "Temafest" in _client(people["bo"]).get(url).content.decode()
    outsider = _client(people["outsider"])
    assert "Temafest" not in outsider.get(url).content.decode()
    assert outsider.get(reverse("drive:folder", args=[folder.pk])).status_code == 404

    inspector = _client(people["inspector"])
    page = inspector.get(url).content.decode()
    assert "Temafest" not in page
    assert f"Delt mappe {folder.shared.number}" in page
    assert "3 medlemmer" in page
    assert inspector.get(reverse("drive:folder", args=[folder.pk])).status_code == 403
    assert inspector.get(reverse("drive:shared", args=[folder.shared_id])).status_code == 404


def test_two_delte_mapper_may_have_the_same_name(people: dict[str, Resident]) -> None:
    _make(people)
    _make(people)
    assert Folder.objects.filter(kind=Folder.Kind.SHARED, name="Temafest").count() == 2


def test_readers_read_and_editors_write(people: dict[str, Resident]) -> None:
    folder = _make(people)
    shared.set_role(_viewer(people["anna"]), folder.shared, people["cecilie"].pk, "read", people["anna"])
    assert _upload(_client(people["bo"]), folder, "plan.txt", b"x")["status"] == 201
    reader = _client(people["cecilie"])
    assert _upload(reader, folder, "mit.txt", b"x")["status"] == 403
    assert reader.get(reverse("drive:folder", args=[folder.pk])).status_code == 200


def test_moving_into_a_delt_mappe_takes_the_subtree_and_says_who_sees_it(
    people: dict[str, Resident], group: Workgroup
) -> None:
    folder = _make(people)
    anna = _client(people["anna"])
    winter = _period_folder(group, WINTER)
    anna.post(reverse("drive:create_folder", args=[winter.pk]), {"name": "Plakater"})
    posters = Folder.objects.get(name="Plakater")
    picker = anna.get(
        reverse("drive:move", args=[winter.pk]), {"op": "move", "folder": posters.pk, "at": folder.pk}
    )
    assert "Kun den delte mappes medlemmer kan se det." in picker.content.decode()
    anna.post(
        reverse("drive:move", args=[winter.pk]),
        {"op": "move", "folder": posters.pk, "to": folder.pk, "confirm": "1"},
    )
    posters.refresh_from_db()
    assert (posters.shared_id, posters.workgroup_id) == (folder.shared_id, None)
    assert _viewer(people["dan"]).level(posters) == Level.NONE  # in the group, not in the Delt mappe


# --- Members ---------------------------------------------------------------------------------------


def test_never_fewer_than_three_and_always_a_moderator(people: dict[str, Resident]) -> None:
    folder = _make(people)
    item, anna = folder.shared, people["anna"]
    with pytest.raises(ValidationError):
        shared.remove_member(_viewer(anna), item, people["bo"].pk, anna)
    with pytest.raises(PermissionDenied):
        shared.add_member(_viewer(people["bo"]), item, people["dan"].pk, "edit", people["bo"])

    shared.add_member(_viewer(anna), item, people["dan"].pk, "edit", anna)
    with pytest.raises(ValidationError):  # the last moderator cannot step down, or leave
        shared.set_role(_viewer(anna), item, anna.pk, "edit", anna)
    with pytest.raises(ValidationError):
        shared.remove_member(_viewer(anna), item, anna.pk, anna)

    shared.set_role(_viewer(anna), item, people["bo"].pk, "moderator", anna)
    shared.remove_member(_viewer(anna), item, anna.pk, anna)  # leaving, now that Bo moderates
    assert not item.members.filter(resident=anna).exists()
    lines = list(Event.objects.filter(folder=folder).values_list("action", flat=True))
    assert Event.Action.MEMBER_LEFT in lines
    assert Event.Action.ROLE_CHANGED in lines


def test_a_member_who_moves_out_still_counts_but_has_no_access(people: dict[str, Resident]) -> None:
    folder = _make(people)
    cecilie = people["cecilie"]
    Residency.objects.filter(resident=cecilie).delete()
    Residency.objects.create(
        resident=cecilie, room=people["anna"].residencies.first().room, year=2026, month=6
    )
    assert _viewer(cecilie).level(folder) == Level.NONE
    with pytest.raises(ValidationError):
        shared.remove_member(_viewer(people["anna"]), folder.shared, people["bo"].pk, people["anna"])


# --- Expiry ----------------------------------------------------------------------------------------


def test_the_expiry_is_within_two_years_and_delete_never_turns_into_archive(
    people: dict[str, Resident],
) -> None:
    folder = _make(people)
    item, anna = folder.shared, people["anna"]
    with pytest.raises(ValidationError):
        shared.set_expiry(_viewer(anna), item, TODAY, "archive", anna)
    with pytest.raises(ValidationError):
        shared.set_expiry(_viewer(anna), item, item.latest_expiry() + timedelta(days=1), "archive", anna)
    shared.set_expiry(_viewer(anna), item, item.latest_expiry(), "delete", anna)
    with pytest.raises(ValidationError):
        shared.set_expiry(_viewer(anna), item, SOON, "archive", anna)


def test_moderators_are_warned_two_weeks_before_and_again_after_a_move(people: dict[str, Resident]) -> None:
    folder = _make(people)
    item = folder.shared
    assert shared.run_expiry(SOON - timedelta(days=15)) == (0, 0, 0)
    assert shared.run_expiry(SOON - timedelta(days=14)) == (1, 0, 0)
    assert shared.run_expiry(SOON - timedelta(days=13)) == (0, 0, 0)
    assert Notification.objects.filter(recipient=people["anna"], title__contains="udløber").count() == 1
    assert not Notification.objects.filter(recipient=people["bo"], title__contains="udløber").exists()

    SharedFolder.objects.filter(pk=item.pk).update(expires_on=SOON + timedelta(days=5))
    assert shared.run_expiry(SOON - timedelta(days=10)) == (0, 0, 0)
    assert shared.run_expiry(SOON - timedelta(days=9)) == (1, 0, 0)


def test_expiry_with_deletion_removes_everything_for_good(
    people: dict[str, Resident],
    django_capture_on_commit_callbacks,  # noqa: ANN001
) -> None:
    folder = _make(people, on_expiry="delete")
    client = _client(people["anna"])
    client.post(reverse("drive:create_folder", args=[folder.pk]), {"name": "Billeder"})
    _upload(client, Folder.objects.get(name="Billeder"), "a.txt", b"a")
    key = File.objects.get().latest_version().key

    assert shared.run_expiry(SOON) == (1, 0, 0)  # the warning; it expires the day after
    with django_capture_on_commit_callbacks(execute=True):
        assert shared.run_expiry(SOON + timedelta(days=1)) == (0, 1, 0)
    assert not Folder.objects.filter(pk=folder.pk).exists()
    assert not File.objects.exists()
    assert not SharedFolder.objects.exists()
    assert not services.storage().exists(key)


def test_expiry_with_archiving_shelves_it_by_year_for_everyone(people: dict[str, Resident]) -> None:
    first = _make(people)
    second = _make(people)
    year = str(timezone.localtime(first.shared.created_at).year)
    _upload(_client(people["anna"]), first, "referat.txt", b"x")

    assert shared.run_expiry(SOON + timedelta(days=1)) == (0, 0, 2)
    first.refresh_from_db()
    second.refresh_from_db()
    shelf = Folder.objects.get(kind=Folder.Kind.ARCHIVE_YEAR)
    assert shelf.name == year
    assert {first.parent_id, second.parent_id} == {shelf.pk}
    assert sorted([first.name, second.name]) == ["Temafest", "Temafest (2)"]
    assert first.path.startswith(shelf.path)

    outsider = _viewer(people["outsider"])
    assert outsider.level(first) == Level.READ
    assert outsider.level(File.objects.get().folder) == Level.READ
    assert not outsider.can_upload_to(first)
    assert not _viewer(people["anna"]).can_upload_to(first)
    page = _client(people["outsider"]).get(reverse("drive:folder", args=[shelf.pk])).content.decode()
    assert "Temafest (2)" in page


# --- Requests and activity -------------------------------------------------------------------------


def test_its_moderators_decide_its_requests_not_inspektionen(people: dict[str, Resident]) -> None:
    folder = _make(people)
    bo = people["bo"]
    _upload(_client(bo), folder, "plan.txt", b"x")
    file = File.objects.get()
    locks.lock_file(_viewer(bo), file, bo)
    request = locks.request_unlock(_viewer(bo), file, 3, "Rettelse", bo)
    assert request.shared_id == folder.shared_id

    inspector = people["inspector"]
    assert request not in locks.decides(_viewer(inspector))
    with pytest.raises(PermissionDenied):
        locks.decide(_viewer(inspector), request, approve=True, reason="", actor=inspector)
    page = _client(people["anna"]).get(reverse("drive:requests")).content.decode()
    assert "plan.txt" in page
    locks.decide(_viewer(people["anna"]), request, approve=True, reason="", actor=people["anna"])
    assert ApprovalRequest.objects.get().state == ApprovalRequest.State.APPROVED


def test_its_activity_shows_member_changes_to_members(people: dict[str, Resident]) -> None:
    folder = _make(people)
    shared.add_member(_viewer(people["anna"]), folder.shared, people["dan"].pk, "read", people["anna"])
    url = reverse("drive:activity", args=[folder.pk])
    page = _client(people["bo"]).get(url).content.decode()
    assert f"Gav {people['dan'].full_name} adgang som læser" in page
    assert _client(people["inspector"]).get(url).status_code == 403
    assert SharedMember.objects.filter(shared=folder.shared).count() == 4


def test_the_history_of_delte_mapper_does_not_tell_others_what_is_in_it(people: dict[str, Resident]) -> None:
    _make(people)
    root = Folder.objects.get(kind=Folder.Kind.SHARED_ROOT)
    url = reverse("drive:folder_history", args=[root.pk])
    assert "Temafest" in _client(people["bo"]).get(url).content.decode()
    for name in ("outsider", "inspector"):
        page = _client(people[name]).get(url).content.decode()
        assert "Temafest" not in page
        assert people["bo"].full_name not in page
