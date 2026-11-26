"""Drive: periods, the folders the system makes, the access table, and uploading and downloading.

Every test runs on 19 November 2026: the current period is `2026-27 Vinter`, the previous one
`2026 Sommer`, and the next one, `2027 Sommer`, gets its folder on 18 March 2027.
"""

from collections.abc import Callable
from datetime import date
from io import BytesIO

import pytest
from django.test import Client
from django.urls import reverse

from audit import presentation
from audit.models import AuditChange, AuditEvent
from core.models import Room, Workgroup
from drive import services
from drive.access import Level, Viewer
from drive.models import File, Folder, Space, validate_name
from drive.periods import Period, period_containing, periods_with_folders, years_with_folders
from drive.structure import ensure_structure
from photo_album import direct_upload
from residents.models import Residency, Resident, Role

from .drive_support import (  # noqa: F401 - fixtures, used by name
    NEXT,
    OLD,
    SUMMER,
    TODAY,
    WINTER,
    _period_folder,
    _today,
    _upload,
    group,
    member,
    open_gate,
    resident_in,
    room,
)

# --- Periods ---------------------------------------------------------------------------------------


def test_periods_are_named_year_first_and_sort_by_date() -> None:
    assert SUMMER.name == "2026 Sommer"
    assert WINTER.name == "2026-27 Vinter"
    assert Period(date(2099, 10, 1)).name == "2099-00 Vinter"
    names = [SUMMER.name, WINTER.name, NEXT.name]
    assert sorted(names) == names


def test_a_period_knows_its_neighbours_and_its_months() -> None:
    assert WINTER.previous() == SUMMER
    assert WINTER.next() == NEXT
    assert SUMMER.previous() == OLD
    assert list(WINTER.months()) == [(2026, 10), (2026, 11), (2026, 12), (2027, 1), (2027, 2), (2027, 3)]


@pytest.mark.parametrize(
    ("day", "start"),
    [
        (date(2026, 3, 31), date(2025, 10, 1)),
        (date(2026, 4, 1), date(2026, 4, 1)),
        (date(2026, 9, 30), date(2026, 4, 1)),
        (date(2026, 10, 1), date(2026, 10, 1)),
        (date(2027, 1, 15), date(2026, 10, 1)),
    ],
)
def test_the_period_containing_a_day(day: date, start: date) -> None:
    assert period_containing(day).start == start


def test_the_next_period_and_year_appear_two_weeks_ahead() -> None:
    assert periods_with_folders(date(2027, 3, 17)) == [WINTER]
    assert periods_with_folders(date(2027, 3, 18)) == [WINTER, NEXT]
    assert years_with_folders(date(2026, 12, 17)) == [2026]
    assert years_with_folders(date(2026, 12, 18)) == [2026, 2027]


# --- Names -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", " ", "a/b", "a\\b", "tab\there", " lead", "trail ", "dot."])
def test_names_that_would_not_survive_a_zip_are_refused(name: str) -> None:
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        validate_name(name)


# --- The folders the system makes ------------------------------------------------------------------


def test_the_structure_has_roots_a_folder_per_group_and_the_current_period(group: Workgroup) -> None:
    ensure_structure(TODAY)
    ensure_structure(TODAY)  # idempotent

    common = Folder.objects.get(parent=None, kind=Folder.Kind.COMMON_ROOT)
    assert sorted(common.children.values_list("name", flat=True)) == ["2026", "Uden årstal"]
    group_folder = Folder.objects.get(workgroup=group, kind=Folder.Kind.GROUP)
    assert group_folder.parent is not None
    assert group_folder.parent.kind == Folder.Kind.GROUPS_ROOT
    assert sorted(group_folder.children.values_list("name", flat=True)) == ["2026-27 Vinter", "Uden periode"]
    assert Folder.objects.filter(kind=Folder.Kind.GROUP).count() == Workgroup.objects.count()


def test_the_next_period_gets_its_folder_two_weeks_before(group: Workgroup) -> None:
    ensure_structure(date(2027, 3, 18))
    names = Folder.objects.filter(workgroup=group, kind=Folder.Kind.PERIOD).values_list("name", flat=True)
    assert sorted(names) == ["2026-27 Vinter", "2027 Sommer"]


def test_a_group_folder_name_is_a_valid_folder_name_and_follows_a_rename(db: None) -> None:
    workgroup = Workgroup.objects.create(name="IT / Netværk")
    ensure_structure(TODAY)
    assert Folder.objects.get(workgroup=workgroup, kind=Folder.Kind.GROUP).name == "IT - Netværk"
    workgroup.name = "Netværksgruppen"
    workgroup.save()
    ensure_structure(TODAY)
    assert Folder.objects.get(workgroup=workgroup, kind=Folder.Kind.GROUP).name == "Netværksgruppen"


# --- Access ----------------------------------------------------------------------------------------


def _viewer(resident: Resident, *, oversees: bool = False) -> Viewer:
    return Viewer.build(resident.pk, oversees=oversees, today=TODAY)


def test_current_members_write_now_read_the_past_and_edit_only_the_last_period_they_were_in(
    group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    ensure_structure(TODAY)
    continuing = resident_in({(2026, 9): group, (2026, 11): group})
    newcomer = resident_in({(2026, 9): None, (2026, 11): group})
    viewer, new = _viewer(continuing), _viewer(newcomer)

    assert viewer.level(_period_folder(group, WINTER)) == Level.WRITE
    assert viewer.level(_period_folder(group, SUMMER)) == Level.WRITE
    assert new.level(_period_folder(group, SUMMER)) == Level.READ
    assert viewer.level(_period_folder(group, OLD)) == Level.READ
    timeless = Folder.objects.get(workgroup=group, kind=Folder.Kind.TIMELESS)
    assert viewer.level(timeless) == new.level(timeless) == Level.WRITE


def test_former_members_read_only_the_periods_they_were_in(
    group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    ensure_structure(TODAY)
    other = Workgroup.objects.create(name="Køkkengruppen-test")
    former = resident_in({(2026, 5): group, (2026, 11): other})
    viewer = _viewer(former)

    assert viewer.level(_period_folder(group, SUMMER)) == Level.READ
    assert viewer.level(_period_folder(group, OLD)) == Level.NONE
    assert viewer.level(_period_folder(group, WINTER)) == Level.NONE
    assert viewer.level(Folder.objects.get(workgroup=group, kind=Folder.Kind.TIMELESS)) == Level.NONE
    assert viewer.level(Folder.objects.get(workgroup=group, kind=Folder.Kind.GROUP)) == Level.READ


def test_who_left_during_the_current_period_reads_it(
    group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    ensure_structure(TODAY)
    other = Workgroup.objects.create(name="Haven-test")
    left = resident_in({(2026, 10): group, (2026, 11): other})
    assert _viewer(left).level(_period_folder(group, WINTER)) == Level.READ


def test_the_next_period_is_for_current_and_incoming_members(
    group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    ensure_structure(TODAY)
    upcoming = _period_folder(group, NEXT)
    other = Workgroup.objects.create(name="Haven-test")
    current = resident_in({(2026, 11): group})
    incoming = resident_in({(2026, 11): other, (2026, 12): group})
    outsider = resident_in({(2026, 11): other})

    assert _viewer(current).level(upcoming) == Level.WRITE
    assert _viewer(incoming).level(upcoming) == Level.WRITE
    assert _viewer(outsider).level(upcoming) == Level.NONE


def test_overseers_see_current_periods_exist_and_read_the_past(
    group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    ensure_structure(TODAY)
    other = Workgroup.objects.create(name="Inspektionen-test")
    inspector = _viewer(resident_in({(2026, 11): other}, roles=(Role.INSPEKTION,)), oversees=True)

    assert inspector.level(_period_folder(group, WINTER)) == Level.META
    assert inspector.level(_period_folder(group, NEXT)) == Level.META
    assert inspector.level(_period_folder(group, SUMMER)) == Level.READ
    assert inspector.level(_period_folder(group, OLD)) == Level.READ
    assert inspector.level(Folder.objects.get(workgroup=group, kind=Folder.Kind.TIMELESS)) == Level.READ


def test_moving_out_suspends_everything_and_moving_back_restores_it(
    group: Workgroup, resident_in: Callable[..., Resident], room: Room
) -> None:
    ensure_structure(TODAY)
    resident_in({(2026, 11): None})  # November's list is out, so November is the month in effect
    away = resident_in({(2026, 5): group})
    summer = _period_folder(group, SUMMER)
    assert _viewer(away).level(summer) == Level.NONE
    assert _viewer(away).level(Folder.objects.get(kind=Folder.Kind.COMMON_ROOT)) == Level.NONE

    Residency.objects.create(resident=away, room=room, year=2026, month=11, workgroup=None)
    assert _viewer(away).level(summer) == Level.READ


def test_faelles_is_written_by_everyone_except_old_years(resident_in: Callable[..., Resident]) -> None:
    ensure_structure(TODAY)
    viewer = _viewer(resident_in({(2026, 11): None}))
    common = Folder.objects.get(kind=Folder.Kind.COMMON_ROOT)
    timeless = common.children.get(kind=Folder.Kind.TIMELESS)
    this_year = common.children.get(kind=Folder.Kind.YEAR)
    last_year = Folder(
        parent=common, name="2025", kind=Folder.Kind.YEAR, space=Space.COMMON, period_start=date(2025, 1, 1)
    )
    last_year.save()
    older = Folder(
        parent=common, name="2024", kind=Folder.Kind.YEAR, space=Space.COMMON, period_start=date(2024, 1, 1)
    )
    older.save()

    assert viewer.level(common) == Level.READ
    assert viewer.level(timeless) == viewer.level(this_year) == viewer.level(last_year) == Level.WRITE
    assert viewer.level(older) == Level.READ
    # The first level of Uden årstal is arranged by admins and Inspektionen; files may go there.
    assert not viewer.can_create_in(timeless)
    assert viewer.can_upload_to(timeless)
    assert viewer.can_create_in(this_year)


# --- Views -----------------------------------------------------------------------------------------


def test_the_rollout_gate_keeps_plain_residents_out(
    group: Workgroup, resident_in: Callable[..., Resident]
) -> None:
    client = Client()
    client.force_login(resident_in({(2026, 11): group}))
    assert client.get(reverse("drive:home")).status_code == 403


def test_home_lists_the_spaces(member: Client) -> None:
    page = member.get(reverse("drive:home")).content.decode()
    assert "Fælles" in page
    assert "Embedsgrupper" in page


def test_a_group_folder_a_resident_was_never_in_is_a_404(
    member: Client, resident_in: Callable[..., Resident]
) -> None:
    other = Workgroup.objects.create(name="Haven-test")
    ensure_structure(TODAY)
    folder = Folder.objects.get(workgroup=other, kind=Folder.Kind.GROUP)
    assert member.get(reverse("drive:folder", args=[folder.uuid])).status_code == 404


def test_an_overseer_sees_the_current_period_listed_but_cannot_open_it(
    group: Workgroup, resident_in: Callable[..., Resident], open_gate: None
) -> None:
    ensure_structure(TODAY)
    client = Client()
    client.force_login(resident_in({(2026, 11): None}, roles=(Role.INSPEKTION,)))
    group_folder = Folder.objects.get(workgroup=group, kind=Folder.Kind.GROUP)
    page = client.get(reverse("drive:folder", args=[group_folder.uuid])).content.decode()
    assert "2026-27 Vinter" in page
    assert reverse("drive:folder", args=[_period_folder(group, WINTER).uuid]) not in page
    assert client.get(reverse("drive:folder", args=[_period_folder(group, WINTER).uuid])).status_code == 403


def test_creating_a_folder_takes_its_parents_space_and_period(member: Client, group: Workgroup) -> None:
    winter = _period_folder(group, WINTER)
    response = member.post(reverse("drive:create_folder", args=[winter.uuid]), {"name": "Budget"})
    created = Folder.objects.get(parent=winter, name="Budget")
    assert response["Location"] == reverse("drive:folder", args=[created.uuid])
    assert (created.space, created.workgroup, created.period_start) == (Space.GROUP, group, WINTER.start)
    assert created.path == f"{winter.path}{created.pk}/"
    # Links name it by its uuid; its counting key is nowhere on the page, nor a way in.
    page = member.get(reverse("drive:folder", args=[winter.uuid])).content.decode()
    assert f"/drive/folder/{created.uuid}/" in page
    assert f"/drive/folder/{created.pk}/" not in page
    assert member.get(f"/drive/folder/{created.pk}/").status_code == 404
    member.post(reverse("drive:create_folder", args=[winter.uuid]), {"name": "BUDGET"})
    assert Folder.objects.filter(parent=winter, name_key="budget").count() == 1


def test_no_folders_directly_in_a_group_folder(member: Client, group: Workgroup) -> None:
    group_folder = Folder.objects.get(workgroup=group, kind=Folder.Kind.GROUP)
    assert (
        member.post(reverse("drive:create_folder", args=[group_folder.uuid]), {"name": "x"}).status_code
        == 403
    )


def test_upload_and_download_round_trip_under_the_name(member: Client, group: Workgroup) -> None:
    winter = _period_folder(group, WINTER)
    assert _upload(member, winter, "Referat æøå.txt", b"hej")["status"] == 201
    file = File.objects.get(folder=winter)
    assert (file.name, file.size, file.versions.count()) == ("Referat æøå.txt", 3, 1)

    response = member.get(reverse("drive:download", args=[file.uuid]))
    assert b"".join(response.streaming_content) == b"hej"  # type: ignore[attr-defined]
    assert "filename*=UTF-8''Referat%20%C3%A6%C3%B8%C3%A5.txt" in response["Content-Disposition"]


def test_the_same_name_asks_then_replaces_or_keeps_both(member: Client, group: Workgroup) -> None:
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Budget.xlsx", b"one")
    conflict = _upload(member, winter, "budget.xlsx", b"two")
    assert (conflict["status"], conflict["conflict"]) == (409, True)

    assert _upload(member, winter, "Budget.xlsx", b"three", services.REPLACE)["replaced"] is True
    file = File.objects.get(folder=winter, name="Budget.xlsx")
    assert list(file.versions.order_by("number").values_list("number", "size")) == [(1, 3), (2, 5)]
    assert file.size == 5

    kept = _upload(member, winter, "Budget.xlsx", b"four", services.KEEP_BOTH)
    assert kept["name"] == "Budget (2).xlsx"


def test_every_change_is_in_the_audit_trail(member: Client, group: Workgroup) -> None:
    winter = _period_folder(group, WINTER)
    made = AuditEvent.objects.filter(action="drive.structure.ensured").get()
    assert made.actor_id is None and made.entities.filter(kind="drive.folder", operation="created").exists()
    ensure_structure(TODAY)  # nothing new to make: nothing written
    assert AuditEvent.objects.filter(action="drive.structure.ensured").count() == 1

    member.post(reverse("drive:create_folder", args=[winter.uuid]), {"name": "Budget"})
    _upload(member, winter, "a.txt", b"one")
    _upload(member, winter, "a.txt", b"three", services.REPLACE)
    changes = AuditEvent.objects.filter(action__startswith="drive.").exclude(action="drive.structure.ensured")
    created, uploaded, replaced = changes.order_by("id")
    assert [created.action, uploaded.action, replaced.action] == [
        "drive.folder.created",
        "drive.file.uploaded",
        "drive.file.version_uploaded",
    ]
    assert created.actor_id is not None
    assert (
        created.entities.get(role="subject").name == f"Embedsgrupper / {group.name} / 2026-27 Vinter / Budget"
    )
    assert set(uploaded.entities.values_list("kind", "operation")) == {
        ("drive.file", "created"),
        ("drive.file_version", "created"),
        ("drive.folder", "involved"),
    }
    size = AuditChange.objects.get(entity__event=replaced, entity__kind="drive.file", field="size")
    assert (size.before, size.after) == (3, 5)
    # Kept in full, but the audit page names Drive's items by a tag.
    shown, _ = presentation.page(presentation.Filters(area="drive"))
    names = {entity.name for line in shown for entity in line.entities}
    assert names and all(name.split()[0] in {"Mappe", "Fil", "Filversion"} for name in names)
    assert not presentation.Filters(q="Budget").queryset().exists()


def test_uploads_need_write_access(
    group: Workgroup, resident_in: Callable[..., Resident], open_gate: None
) -> None:
    ensure_structure(TODAY)
    other = Workgroup.objects.create(name="Haven-test")
    client = Client()
    client.force_login(resident_in({(2026, 5): group, (2026, 11): other}))
    summer = _period_folder(group, SUMMER)  # read-only for a former member
    assert _upload(client, summer, "a.txt", b"x")["status"] == 403
    assert not File.objects.exists()


def test_a_bucket_upload_names_its_folder_and_file_by_uuid_in_the_ticket(
    member: Client, group: Workgroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticket the browser carries between begin and commit: what it names is found again."""
    monkeypatch.setattr(direct_upload, "_storage", object)  # any bucket at all

    def finish(ticket: str, etags: object, *, purpose: str, owner: int) -> dict:
        upload = direct_upload._load(ticket, purpose=purpose)
        services.store_local(upload["key"], BytesIO(b"x" * upload["size"]))
        return upload

    monkeypatch.setattr(direct_upload, "finish", finish)
    winter = _period_folder(group, WINTER)

    def commit(begun: dict) -> int:
        assert begun["direct"] is True
        return member.post(reverse("drive:upload_commit"), {"ticket": begun["ticket"]}).status_code

    begin = member.post(reverse("drive:upload_begin", args=[winter.uuid]), {"name": "Plan.txt", "size": 3})
    assert commit(begin.json()) == 201
    file = File.objects.get(folder=winter, name="Plan.txt")
    begin = member.post(reverse("drive:version_begin", args=[file.uuid]), {"name": "Plan.txt", "size": 5})
    assert commit(begin.json()) == 201
    assert file.versions.count() == 2


def test_a_download_rechecks_access(
    group: Workgroup, member: Client, resident_in: Callable[..., Resident]
) -> None:
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "a.txt", b"x")
    outsider = Client()
    outsider.force_login(resident_in({(2026, 11): None}))
    assert outsider.get(reverse("drive:download", args=[File.objects.get().uuid])).status_code == 404


def test_a_listing_counts_the_whole_subtree(member: Client, group: Workgroup) -> None:
    winter = _period_folder(group, WINTER)
    member.post(reverse("drive:create_folder", args=[winter.uuid]), {"name": "Bilag"})
    _upload(member, Folder.objects.get(name="Bilag"), "a.txt", b"12345")
    _upload(member, winter, "b.txt", b"1")
    group_folder = Folder.objects.get(workgroup=group, kind=Folder.Kind.GROUP)
    page = member.get(reverse("drive:folder", args=[group_folder.uuid])).content.decode()
    assert "2 filer" in page
