"""Drive: search by name across what the resident can open, zips of files and folders, and sorting the
list."""

import zipfile
from collections.abc import Callable
from datetime import timedelta
from io import BytesIO

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Workgroup
from drive import search, services, sharing, zips
from drive.access import Viewer
from drive.models import File, Folder, ZipDownload
from drive.structure import ensure_structure
from residents.models import Residency, Resident, Role

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
        "member": resident_in({(2025, 11): group, (2026, 11): group}),
        "kitchen": resident_in({(2026, 11): other}),
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


def _names(results: search.Results) -> list[str]:
    return [hit.name for hit in results.hits]


def _folder(client: Client, parent: Folder, name: str) -> Folder:
    client.post(reverse("drive:create_folder", args=[parent.pk]), {"name": name})
    return Folder.objects.get(parent=parent, name=name)


# --- Search ----------------------------------------------------------------------------------------


def test_search_finds_names_the_resident_can_open_and_says_where(
    people: dict[str, Resident], group: Workgroup
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Budget 2027.xlsx", b"x")
    _folder(member, winter, "Budgetmøder")
    found = search.search(_viewer(people["member"]), "BUDGET")
    assert _names(found) == ["Budgetmøder", "Budget 2027.xlsx"]  # folders first
    assert found.hits[1].where == f"Embedsgrupper / {group.name} / 2026-27 Vinter"

    # Not by another group, nor by Inspektionen while the period is running: not even the name.
    assert _names(search.search(_viewer(people["kitchen"]), "budget")) == []
    assert _names(search.search(_viewer(people["inspector"]), "budget")) == []


def test_search_finds_a_file_shared_with_the_resident(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    _upload(member, _period_folder(group, WINTER), "Indkøbsliste.txt", b"x")
    file = File.objects.get(name="Indkøbsliste.txt")
    sharing.share(
        _viewer(people["member"]),
        file=file,
        resident_id=people["kitchen"].pk,
        access="read",
        actor=people["member"],
    )
    assert _names(search.search(_viewer(people["kitchen"]), "indkøb")) == ["Indkøbsliste.txt"]


def test_search_filters_by_space_kind_and_period(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    old = _period_folder(group, OLD)
    timeless = Folder.objects.get(workgroup=group, kind=Folder.Kind.TIMELESS)
    _upload(member, winter, "Regnskab.xlsx", b"x")
    _upload(member, winter, "Regnskab.pdf", b"x")
    _upload(member, timeless, "Regnskab skabelon.xlsx", b"x")
    File.objects.create(folder=old, name="Regnskab 2025.xlsx", size=1, changed_at=timezone.now())
    viewer = _viewer(people["member"])

    assert _names(search.search(viewer, "regnskab", kind="spreadsheet")) == [
        "Regnskab 2025.xlsx",
        "Regnskab skabelon.xlsx",
        "Regnskab.xlsx",
    ]
    assert _names(search.search(viewer, "regnskab", kind="pdf")) == ["Regnskab.pdf"]
    assert _names(search.search(viewer, "regnskab", period="none")) == ["Regnskab skabelon.xlsx"]
    assert _names(search.search(viewer, "regnskab", period=OLD.start.isoformat())) == ["Regnskab 2025.xlsx"]
    assert _names(search.search(viewer, "regnskab", space="common")) == []
    assert ("2026-10-01", "2026-27 Vinter") in search.periods()


def test_the_search_page(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    _upload(member, _period_folder(group, WINTER), "Referat.pdf", b"x")
    page = member.get(reverse("drive:search"), {"q": "refer"}).content.decode()
    assert "Referat.pdf" in page
    assert "1 navn, der indeholder" in page
    assert "Skriv mindst to tegn" in member.get(reverse("drive:search"), {"q": "r"}).content.decode()
    assert 'action="/drive/search/"' in member.get(reverse("drive:home")).content.decode()


# --- Zips ------------------------------------------------------------------------------------------


def _zip_of(client: Client, source: Folder, captured: Callable, **items: object) -> ZipDownload:
    with captured(execute=True):
        response = client.post(reverse("drive:request_zip", args=[source.pk]), items)
    download = ZipDownload.objects.order_by("-pk").first()
    assert download is not None, response
    assert response["Location"] == reverse("drive:zip", args=[download.token])
    return download


def _contents(client: Client, download: ZipDownload) -> dict[str, bytes]:
    response = client.get(reverse("drive:fetch_zip", args=[download.token]))
    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(b"".join(response.streaming_content))) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_ticked_files_and_folders_come_as_one_zip(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Budget.xlsx", b"budget")
    festen = _folder(member, winter, "Festen")
    _upload(member, festen, "Plan.txt", b"plan")
    _folder(member, festen, "Billeder")
    _upload(member, winter, "Ikke med.txt", b"no")
    _upload(member, festen, "Slettet.txt", b"gone")
    File.objects.filter(name="Slettet.txt").update(deleted_at=timezone.now())

    download = _zip_of(
        member,
        winter,
        django_capture_on_commit_callbacks,
        file=[File.objects.get(name="Budget.xlsx").pk],
        folder=[festen.pk],
    )
    assert download.name == "2026-27 Vinter.zip"
    assert download.state == ZipDownload.State.READY
    assert "Hent zip" in member.get(reverse("drive:zip", args=[download.token])).content.decode()
    assert _contents(member, download) == {
        "Budget.xlsx": b"budget",
        "Festen/Plan.txt": b"plan",
        "Festen/Billeder/": b"",
    }


def test_a_folders_zip_leaves_out_what_the_reader_cannot_open(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    _upload(member, _period_folder(group, WINTER), "Nu.txt", b"now")
    old = _period_folder(group, OLD)
    key = services.new_key()
    services.store_local(key, BytesIO(b"then"))
    file = File.objects.create(folder=old, name="Dengang.txt", size=4, changed_at=timezone.now())
    file.versions.create(number=1, key=key, size=4)

    # Inspektionen reads last year's period, not the one running now.
    inspector = _client(people["inspector"])
    group_folder = Folder.objects.get(workgroup=group, kind=Folder.Kind.GROUP)
    download = _zip_of(inspector, group_folder, django_capture_on_commit_callbacks)
    assert download.name == f"{group.name}.zip"
    contents = _contents(inspector, download)
    assert contents[f"{group.name}/2025-26 Vinter/Dengang.txt"] == b"then"
    assert not any("2026-27 Vinter" in path for path in contents)


def test_only_the_one_who_asked_fetches_a_zip_and_only_while_they_can_read_it(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Budget.xlsx", b"budget")
    download = _zip_of(member, winter, django_capture_on_commit_callbacks)
    assert _client(people["kitchen"]).get(reverse("drive:zip", args=[download.token])).status_code == 404
    assert (
        _client(people["kitchen"]).get(reverse("drive:fetch_zip", args=[download.token])).status_code == 404
    )

    Residency.objects.filter(resident=people["member"], year=2026).update(workgroup=None)
    Residency.objects.filter(resident=people["member"], year=2025).delete()
    response = member.get(reverse("drive:fetch_zip", args=[download.token]))
    assert response["Location"] == reverse("drive:zip", args=[download.token])


def test_a_zip_over_the_limit_is_refused(
    people: dict[str, Resident],
    group: Workgroup,
    settings: object,
    django_capture_on_commit_callbacks: Callable,
) -> None:
    settings.DRIVE_MAX_ZIP_GB = 10 / 1024**3  # type: ignore[attr-defined]
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Stor.bin", b"x" * 11)
    response = member.post(reverse("drive:request_zip", args=[winter.pk]))
    assert response["Location"] == reverse("drive:folder", args=[winter.pk])
    assert not ZipDownload.objects.exists()


def test_zips_go_after_a_day_and_a_dead_build_says_so(
    people: dict[str, Resident], group: Workgroup, django_capture_on_commit_callbacks: Callable
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Budget.xlsx", b"budget")
    download = _zip_of(member, winter, django_capture_on_commit_callbacks)
    stuck = ZipDownload.objects.create(requested_by=people["member"], name="x.zip")
    ZipDownload.objects.filter(pk=stuck.pk).update(created_at=timezone.now() - timedelta(hours=3))
    assert "kunne ikke laves" in member.get(reverse("drive:zip", args=[stuck.token])).content.decode()

    ZipDownload.objects.filter(pk=download.pk).update(created_at=timezone.now() - timedelta(hours=25))
    assert zips.expire() == 1
    assert not services.storage().exists(download.archive_key)
    assert list(ZipDownload.objects.all()) == [stuck]  # three hours old: it goes tomorrow


# --- The list --------------------------------------------------------------------------------------


def test_the_files_sort_by_each_column(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "a-lille.txt", b"x")
    _upload(member, winter, "b-stor.txt", b"x" * 100)
    _upload(member, winter, "c-mellem.txt", b"x" * 10)

    def order(**query: str) -> list[str]:
        page = member.get(reverse("drive:folder", args=[winter.pk]), query).content.decode()
        return sorted(["a-lille.txt", "b-stor.txt", "c-mellem.txt"], key=page.index)

    assert order() == ["a-lille.txt", "b-stor.txt", "c-mellem.txt"]
    assert order(sort="size") == ["b-stor.txt", "c-mellem.txt", "a-lille.txt"]
    assert order(sort="size", order="asc") == ["a-lille.txt", "c-mellem.txt", "b-stor.txt"]
    assert order(sort="name", order="desc") == ["c-mellem.txt", "b-stor.txt", "a-lille.txt"]
    page = member.get(reverse("drive:folder", args=[winter.pk]), {"sort": "size"}).content.decode()
    assert '<a href="?sort=size&amp;order=asc" aria-current="true">' in page
