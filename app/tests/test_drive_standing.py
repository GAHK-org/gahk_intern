"""Drive: standing folders (made in every period or year, a combined view, a naming rule, a write
limit in Fælles), and the question before anything goes directly in `Uden periode`."""

from collections.abc import Callable
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from core.models import Workgroup
from drive import standing
from drive.access import Level, Viewer
from drive.models import Folder, StandingFolder
from drive.structure import ensure_structure
from residents.models import Resident, Role

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


@pytest.fixture
def people(resident_in: Callable[..., Resident], group: Workgroup, open_gate: None) -> dict[str, Resident]:
    found = {
        "member": resident_in({(2026, 11): group}),
        "resident": resident_in({(2026, 11): None}),
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


def _year(year: int) -> Folder:
    return Folder.objects.get(kind=Folder.Kind.YEAR, period_start=date(year, 1, 1))


def _meetings(**fields: object) -> StandingFolder:
    """Fællesmøder, already running this year: written only by admins and Inspektionen."""
    item = StandingFolder.objects.create(
        space="common",
        name="Fællesmøder",
        item_label="Fællesmøde",
        limited=True,
        starts_on=date(2026, 1, 1),
        **fields,
    )
    ensure_structure(TODAY)
    return item


def test_a_new_standing_folder_starts_next_year_and_appears_two_weeks_before(
    people: dict[str, Resident],
) -> None:
    common = Folder.objects.get(kind=Folder.Kind.COMMON_ROOT)
    with pytest.raises(PermissionDenied):
        standing.define(
            _viewer(people["resident"]),
            common,
            name="Referater",
            item_label="",
            limited=False,
            writer_groups=[],
            actor=people["resident"],
        )
    item = standing.define(
        _viewer(people["inspector"]),
        common,
        name="Referater",
        item_label="",
        limited=False,
        writer_groups=[],
        actor=people["inspector"],
    )
    assert item.starts_on == date(2027, 1, 1)
    assert not Folder.objects.filter(standing=item).exists()
    ensure_structure(date(2026, 12, 18))
    assert Folder.objects.get(standing=item).parent == _year(2027)


def test_a_groups_members_define_their_own_from_the_next_period(
    people: dict[str, Resident], group: Workgroup
) -> None:
    group_folder = Folder.objects.get(workgroup=group, kind=Folder.Kind.GROUP)
    with pytest.raises(PermissionDenied):
        standing.define(
            _viewer(people["inspector"]),
            group_folder,
            name="Erfaringsrapporter",
            item_label="",
            limited=False,
            writer_groups=[],
            actor=people["inspector"],
        )
    item = standing.define(
        _viewer(people["member"]),
        group_folder,
        name="Erfaringsrapporter",
        item_label="",
        limited=True,
        writer_groups=[],
        actor=people["member"],
    )
    assert (item.starts_on, item.limited) == (date(2027, 4, 1), False)  # no limits in a group's space
    ensure_structure(date(2027, 3, 18))
    copy = Folder.objects.get(standing=item)
    assert (copy.parent.name, copy.workgroup_id) == ("2027 Sommer", group.pk)


def test_faellesmoeder_is_read_by_everyone_and_written_by_inspektionen_and_named_groups(
    people: dict[str, Resident], group: Workgroup
) -> None:
    item = _meetings()
    copy = Folder.objects.get(standing=item)
    assert copy.parent == _year(2026)
    assert _viewer(people["resident"]).level(copy) == Level.READ
    assert not _viewer(people["resident"]).can_upload_to(copy)
    assert _viewer(people["inspector"]).can_upload_to(copy)
    assert not _viewer(people["member"]).can_upload_to(copy)
    item.writer_groups.add(group)
    assert _viewer(people["member"]).can_upload_to(copy)
    # The rest of the year is everybody's, as before.
    assert _viewer(people["resident"]).can_upload_to(_year(2026))


def test_the_naming_rule_makes_a_dated_folder_in_that_years_copy(people: dict[str, Resident]) -> None:
    item = _meetings()
    inspector = _client(people["inspector"])
    response = inspector.post(reverse("drive:standing_item", args=[item.uuid]), {"day": "2026-11-03"})
    made = Folder.objects.get(name="2026-11-03 Fællesmøde")
    assert response["Location"] == reverse("drive:folder", args=[made.uuid])
    assert (made.parent.standing_id, made.standing_id) == (item.pk, item.pk)
    # Below the copy, the limit holds too.
    assert not _viewer(people["resident"]).can_upload_to(made)
    with pytest.raises(PermissionDenied):
        standing.create_item(_viewer(people["resident"]), item, date(2026, 11, 4), people["resident"])


def test_the_combined_view_shows_every_year_newest_first(people: dict[str, Resident]) -> None:
    item = _meetings()
    ensure_structure(date(2026, 12, 18))
    inspector = _viewer(people["inspector"])
    standing.create_item(inspector, item, date(2026, 3, 1), people["inspector"])
    standing.create_item(inspector, item, date(2027, 1, 10), people["inspector"])
    page = _client(people["resident"]).get(reverse("drive:standing", args=[item.uuid])).content.decode()
    assert "Fællesmøder (alle år)" in page
    assert page.index("2027-01-10 Fællesmøde") < page.index("2026-03-01 Fællesmøde")
    assert "Opret fællesmøde" not in page  # residents read; Inspektionen writes


def test_standing_copies_cannot_be_renamed_or_deleted_and_never_lock(people: dict[str, Resident]) -> None:
    copy = Folder.objects.get(standing=_meetings())
    viewer = _viewer(people["inspector"])
    assert copy.is_system
    assert not viewer.can_change_folder(copy)


def test_a_residents_folder_of_the_same_name_keeps_its_name(people: dict[str, Resident]) -> None:
    resident = _client(people["resident"])
    resident.post(reverse("drive:create_folder", args=[_year(2026).uuid]), {"name": "Fællesmøder"})
    copy = Folder.objects.get(standing=_meetings())
    assert copy.name == "Fællesmøder (2)"


# --- Uden periode ----------------------------------------------------------------------------------


def test_uden_periode_offers_the_current_period_first(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    timeless = Folder.objects.get(workgroup=group, kind=Folder.Kind.TIMELESS)
    page = member.get(reverse("drive:folder", args=[timeless.uuid])).content.decode()
    assert "data-drive-timeless" in page
    assert "Læg i 2026-27 Vinter" in page
    assert (
        f'data-drive-instead="{reverse("drive:create_folder", args=[_period_folder(group, WINTER).uuid])}"'
        in page
    )
    period_page = member.get(
        reverse("drive:folder", args=[_period_folder(group, WINTER).uuid])
    ).content.decode()
    assert "data-drive-timeless" not in period_page
