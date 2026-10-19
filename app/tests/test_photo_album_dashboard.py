"""The administrators' photo-album dashboard: what it counts, what it lists, where a file time is
from, and the storage measurement (photo_album.dashboard, photo_album.views_admin)."""

import importlib
import os
import struct
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import Stubber
from django.apps import apps
from django.conf import settings
from django.core.files.base import ContentFile, File
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from photo_album import dashboard
from photo_album.models import (
    Album,
    AlbumImport,
    AlbumImportState,
    DateSource,
    DerivativeState,
    FileTimeSource,
    Media,
    MediaStatus,
    StorageReport,
)
from photo_album.services import apply_sidecars, check_file_time_sources, import_zip_album, store_upload
from photo_album.storage import PhotoAlbumS3Storage
from photo_album.tasks import measure_photo_album_storage, settle_media_dates
from residents.models import Resident, Role

from .media_samples import image_upload, jpeg_bytes

URL = "siteadmin:photo_album"


def local(*parts: int) -> datetime:
    return timezone.make_aware(datetime(*parts))


def _appledouble(moment: datetime) -> bytes:
    """A netatalk sidecar whose File Dates entry has `moment` as creation and modification time."""
    stamp = int((moment - datetime(2000, 1, 1, tzinfo=UTC)).total_seconds())
    header = struct.pack(">II16sH", 0x00051607, 0x00020000, b"", 1)
    return (
        header
        + struct.pack(">III", 8, len(header) + 12, 16)
        + struct.pack(">iiii", stamp, stamp, -(2**31), -(2**31))
    )


def _item(album: Album, resident: Resident, filename: str, **fields: object) -> Media:
    defaults: dict[str, object] = {
        "title": filename,
        "filename": filename,
        "original": f"photo-album/{album.pk}/x/original/{filename}",
        "thumbnail": f"photo-album/{album.pk}/x/thumbnail/{filename}",
        "derivative_state": DerivativeState.READY,
        "status": MediaStatus.APPROVED,
    }
    return Media.objects.create(album=album, requested_by=resident, **{**defaults, **fields})


def _settled(album: Album, resident: Resident, filename: str, source: DateSource, at: datetime) -> Media:
    """An item without a date of its own, as settle_dates leaves it."""
    media = _item(album, resident, filename)
    Media.objects.filter(pk=media.pk).update(sort_at=at, date_source=source)
    return Media.objects.get(pk=media.pk)


@pytest.fixture
def admin_client(client: Client, make_resident: Callable[..., Resident]) -> Client:
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    return client


@pytest.fixture
def archive(make_resident: Callable[..., Resident]) -> dict[str, Media]:
    """2004 with a sub-album, and Andet: an item of every kind of date, one binned."""
    resident = make_resident(email="foto@gahk.dk")
    fest = Album.objects.create(folder="2004", name="Fest")
    aften = Album.objects.create(folder="2004", name="Aften", parent=fest)
    andet = Album.objects.create(folder="Andet", name="Diverse")
    Album.objects.create(folder="2004", name="Tom")
    items = {
        "captured": _item(fest, resident, "IMG_1.JPG", captured_at=local(2004, 6, 12, 20)),
        # A camera whose clock reset: its own date, shown outside the year.
        "reset": _item(fest, resident, "IMG_2.JPG", captured_at=local(2001, 1, 1, 0, 5)),
        "sidecar": _item(
            aften, resident, "IMG_3.JPG", file_modified_at=local(2004, 6, 12, 21), file_time_source="sidecar"
        ),
        "archive": _item(
            aften, resident, "IMG_4.JPG", file_modified_at=local(2004, 6, 12, 22), file_time_source="archive"
        ),
        "neighbours": _settled(
            fest, resident, "IMG_5.JPG", DateSource.NEIGHBOURS, local(2004, 6, 12, 20, 0, 1)
        ),
        "album_year": _settled(aften, resident, "IMG_6.JPG", DateSource.ALBUM_YEAR, local(2004, 1, 1)),
        # Dropbox's 2022 file time: not believed for 2004, so estimated from the album instead.
        "rejected": _item(fest, resident, "IMG_7.JPG", file_modified_at=local(2022, 3, 1, 12)),
        "video": _item(andet, resident, "clip.mp4", content_type="video/mp4", captured_at=local(2019, 5, 1)),
        "added": _item(andet, resident, "scan.png"),
    }
    Media.objects.filter(pk=items["rejected"].pk).update(
        sort_at=local(2004, 6, 12, 20, 0, 2), date_source="neighbours"
    )
    _item(fest, resident, "binned.jpg", captured_at=local(1999, 1, 1), deleted_at=timezone.now())
    return items


# --- Who sees it --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_administrators_see_the_dashboard(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    assert client.get(reverse(URL)).status_code == 302  # to the login
    client.force_login(make_resident(roles=(Role.FOTO,)))  # Fotogruppen curates, but this is the admin's
    assert client.get(reverse(URL)).status_code == 403
    assert client.post(reverse("siteadmin:photo_album_measure")).status_code == 403
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    assert client.get(reverse(URL)).status_code == 200
    assert reverse(URL) in client.get(reverse("siteadmin:home")).content.decode()


# --- What it counts -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_it_counts_how_every_item_is_dated(archive: dict[str, Media]) -> None:
    view = dashboard.overview()

    assert dict(view.counts.tiers) == {
        "captured": 3,
        "sidecar": 1,
        "archive": 1,
        "neighbours": 2,
        "album_year": 1,
        "added": 1,
    }  # the binned one is not counted
    assert (view.counts.total, view.counts.videos, view.counts.photos) == (9, 1, 8)
    assert (view.counts.file_time, view.counts.estimated, view.counts.without_own_date) == (2, 3, 4)
    assert (view.roots, view.sub_albums, view.empty_albums) == (3, 1, 1)
    assert view.counts.outside == 1  # the reset camera; nothing in Andet has a window
    assert view.rejected_file_times == 1  # the 2022 file time

    folders = {row.folder: row for row in view.folders}
    assert [row.folder for row in view.folders] == ["2004", "Andet"]  # years newest first, then the rest
    assert (folders["2004"].roots, folders["2004"].sub_albums, folders["2004"].counts.total) == (2, 1, 7)
    assert round(folders["2004"].counts.captured_percent) == 29  # 2 of 7
    assert folders["2004"].bytes is None  # not measured yet

    timeline = {column.year: column.counts.total for column in view.timeline.columns}
    assert timeline[2001] == 1 and timeline[2004] == 6 and timeline[2019] == 1
    assert timeline[2010] == 0  # every year in between has a column


@pytest.mark.django_db
def test_the_albums_with_most_without_a_date_of_their_own_come_first(archive: dict[str, Media]) -> None:
    view = dashboard.overview()

    worst = dashboard.album_rows(view)
    assert [row.label for row in worst] == ["Fest", "Diverse", "Fest / Aften"]
    assert [(row.counts.without_own_date, row.counts.outside) for row in worst] == [(2, 1), (1, 0), (1, 0)]
    in_2004 = dashboard.album_rows(view, "2004")
    assert [row.label for row in in_2004] == ["Fest", "Fest / Aften"]  # an empty album has no row


@pytest.mark.django_db
def test_the_page_shows_the_numbers_and_the_items_to_look_at(
    admin_client: Client, archive: dict[str, Media]
) -> None:
    page = admin_client.get(reverse(URL)).content.decode()

    assert "Filens dato: sidecar" in page and "Anslået ud fra albummets år" in page
    assert "IMG_2.JPG" in page  # outside the window, as shown
    assert f"?media={archive['reset'].pk}" in page
    assert "scan.png" in page and "IMG_6.JPG" in page  # without a date of their own

    by_file_time = admin_client.get(reverse(URL), {"filtid": "1"}).context
    assert [row.media for row in by_file_time["outside"].items] == [archive["rejected"]]
    estimated = admin_client.get(reverse(URL), {"kilde": "album_year"}).context
    assert [row.media for row in estimated["undated"].items] == [archive["album_year"]]
    in_andet = admin_client.get(reverse(URL), {"mappe": "Andet"}).context
    assert [row.media for row in in_andet["undated"].items] == [archive["added"]]
    assert in_andet["outside"].page.paginator.count == 0
    assert [row.label for row in in_andet["album_rows"]] == ["Diverse"]
    assert admin_client.get(reverse(URL), {"mappe": "1999"}).context["folder"] == ""  # no such folder


@pytest.mark.django_db
def test_a_list_pages_on_and_keeps_the_rest_of_the_query(
    admin_client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident(email="foto@gahk.dk")
    album = Album.objects.create(folder="2004", name="Fest")
    for index in range(30):
        _settled(
            album, resident, f"IMG_{index:02}.JPG", DateSource.ALBUM_YEAR, local(2004, 1, 1, 0, 0, index)
        )

    first = admin_client.get(reverse(URL), {"mappe": "2004", "uden_for": "2"}).context["undated"]
    assert len(first.items) == 25 and first.previous_url == ""
    assert first.next_url == "?mappe=2004&uden_for=2&uden_dato=2#uden-dato"
    second = admin_client.get(reverse(URL) + first.next_url.split("#")[0]).context["undated"]
    assert [row.media.filename for row in second.items] == [f"IMG_{index}.JPG" for index in range(25, 30)]


@pytest.mark.django_db
def test_it_lists_what_needs_looking_after(make_resident: Callable[..., Resident]) -> None:
    resident = make_resident(email="foto@gahk.dk")
    album = Album.objects.create(folder="2004", name="Fest")
    _item(album, resident, "a.jpg", sha256="1" * 64, status=MediaStatus.PENDING)
    _item(album, resident, "b.jpg", sha256="1" * 64)
    _item(album, resident, "c.jpg", sha256="1" * 64, derivative_state=DerivativeState.FAILED)
    _item(album, resident, "d.mp4", derivative_state=DerivativeState.PENDING, capture_date_checked=False)
    AlbumImport.objects.create(requested_by=resident, folder="2004", archive_name="Fest.zip", state="ready")

    upkeep = dashboard.upkeep()

    assert (upkeep.pending, upkeep.derivatives_pending, upkeep.derivatives_failed_count) == (1, 1, 1)
    assert (upkeep.duplicate_files, upkeep.duplicate_copies) == (1, 2)
    assert (upkeep.unhashed, upkeep.capture_dates_to_read) == (1, 1)
    assert [each.archive_name for each in upkeep.recent_imports] == ["Fest.zip"]


# --- Where a file time is from ------------------------------------------------------------------


@pytest.mark.django_db
def test_an_upload_records_its_file_time_as_the_uploaders(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))
    album = Album.objects.create(folder="2004", name="Fest")

    client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {
            "uploads": [image_upload("a.jpg"), image_upload("b.jpg", jpeg_bytes((1, 2, 3)))],
            "last_modified": ["1087070400000", ""],
        },
    )

    sources = dict(Media.objects.values_list("filename", "file_time_source"))
    assert sources == {"a.jpg": FileTimeSource.UPLOAD, "b.jpg": FileTimeSource.UNKNOWN}  # b had none


@pytest.mark.django_db
def test_an_import_records_whether_a_file_time_is_its_sidecars_or_the_zips(
    make_resident: Callable[..., Resident],
) -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, colour in (("IMG_1.JPG", (1, 1, 1)), ("IMG_2.JPG", (2, 2, 2)), ("IMG_3.JPG", (3, 3, 3))):
            archive.writestr(
                zipfile.ZipInfo(f"Fest/{name}", date_time=(2004, 8, 1, 12, 0, 0)), jpeg_bytes(colour)
            )
        archive.writestr("Fest/.AppleDouble/IMG_1.JPG", _appledouble(datetime(2004, 6, 12, 18, tzinfo=UTC)))
        # A sidecar with no date for 2004: the ZIP's time it is, then.
        archive.writestr("Fest/.AppleDouble/IMG_2.JPG", _appledouble(datetime(2001, 1, 1, tzinfo=UTC)))
    buffer.seek(0)

    import_zip_album(
        archive=File(buffer, name="Fest.zip"), folder="2004", resident=make_resident(), lock=False
    )

    sources = dict(Media.objects.values_list("filename", "file_time_source"))
    assert sources == {"IMG_1.JPG": "sidecar", "IMG_2.JPG": "archive", "IMG_3.JPG": "archive"}
    assert Media.objects.get(filename="IMG_1.JPG").file_modified_at == datetime(2004, 6, 12, 18, tzinfo=UTC)


@pytest.mark.django_db
def test_sidecar_dates_sent_later_are_recorded_as_the_sidecars(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    moment = datetime(2004, 6, 12, 18, tzinfo=UTC)
    changed = _item(
        album, resident, "IMG_1.JPG", file_modified_at=local(2022, 3, 1, 12), file_time_source="archive"
    )
    same = _item(album, resident, "IMG_2.JPG", file_modified_at=moment)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(".AppleDouble/IMG_1.JPG", _appledouble(moment))
        archive.writestr(".AppleDouble/IMG_2.JPG", _appledouble(moment))
    buffer.seek(0)

    apply_sidecars(archive=File(buffer, name="datoer.zip"), folder="2004", root_name="Fest")

    for media in (changed, same):
        media.refresh_from_db()
        assert (media.file_modified_at, media.file_time_source) == (moment, FileTimeSource.SIDECAR)


@pytest.mark.django_db
def test_the_night_reads_sidecars_to_tell_where_older_file_times_are_from(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    moment = datetime(2004, 6, 12, 18, tzinfo=UTC)

    def stored(name: str, file_time: datetime, sidecar_date: datetime) -> Media:
        media = _item(album, resident, name, file_modified_at=file_time)
        media.sidecar.save(name, ContentFile(_appledouble(sidecar_date)), save=False)
        Media.objects.filter(pk=media.pk).update(sidecar=media.sidecar.name, file_time_source="")
        return media

    stored("IMG_1.JPG", moment, moment)
    stored("IMG_2.JPG", local(2004, 8, 1, 12), datetime(2001, 1, 1, tzinfo=UTC))  # the ZIP's, then
    gone = stored("IMG_3.JPG", moment, moment)
    gone.sidecar.storage.delete(gone.sidecar.name)

    result = check_file_time_sources()

    assert (result.checked, result.from_sidecar, len(result.unreadable), result.remaining) == (2, 1, 1, 1)
    assert dict(Media.objects.values_list("filename", "file_time_source")) == {
        "IMG_1.JPG": "sidecar",
        "IMG_2.JPG": "archive",
        "IMG_3.JPG": "",  # tried again next night
    }
    settle_media_dates()  # the nightly job does it too, and minds the one it cannot read
    assert Media.objects.get(filename="IMG_3.JPG").file_time_source == ""


@pytest.mark.django_db
def test_the_migration_tells_imported_file_times_from_uploaded_ones(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    imported = _item(album, resident, "IMG_1.JPG", file_modified_at=local(2004, 8, 1))
    uploaded = _item(album, resident, "IMG_2.JPG", file_modified_at=local(2004, 8, 1))
    with_sidecar = _item(
        album, resident, "IMG_3.JPG", file_modified_at=local(2004, 8, 1), sidecar="photo-album/x.ad"
    )
    undated = _item(album, resident, "IMG_4.JPG")
    started = timezone.now() - timedelta(hours=2)
    Media.objects.filter(pk__in=[imported.pk, with_sidecar.pk]).update(
        added_at=started + timedelta(minutes=5)
    )
    Media.objects.filter(pk=uploaded.pk).update(added_at=started + timedelta(hours=1))
    album_import = AlbumImport.objects.create(
        requested_by=resident,
        folder="2004",
        archive_name="Fest.zip",
        state=AlbumImportState.READY,
        album_ids=[album.pk],
        completed_at=started + timedelta(minutes=10),
    )
    AlbumImport.objects.filter(pk=album_import.pk).update(created_at=started)
    Media.objects.update(file_time_source="")

    importlib.import_module("photo_album.migrations.0019_fill_media_file_time_source").fill(apps, None)

    assert dict(Media.objects.values_list("pk", "file_time_source")) == {
        imported.pk: "archive",
        uploaded.pk: "upload",
        with_sidecar.pk: "",  # for the night to read
        undated.pk: "",
    }


# --- Storage ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_storage_is_measured_from_the_files_themselves(
    admin_client: Client,
    make_resident: Callable[..., Resident],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    resident = make_resident(email="foto@gahk.dk")
    album = Album.objects.create(folder="2004", name="Fest")
    sub = Album.objects.create(folder="2004", name="Aften", parent=album)
    content = jpeg_bytes()
    for target, name in ((album, "a.jpg"), (sub, "b.jpg")):
        with django_capture_on_commit_callbacks(execute=True):  # the derivatives, built eagerly
            store_upload(
                album=target,
                uploaded_file=image_upload(name, content),
                resident=resident,
                approved=True,
                sidecar=b"x" * 10,
            )
    leftover = Path(settings.MEDIA_ROOT) / "photo-album-imports" / "abc" / "Fest.zip"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"z" * 100)
    # Files no row points at: one left over, and one an upload under way has just written.
    old = Path(settings.MEDIA_ROOT) / f"photo-album/{album.pk}/gone/original/c.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"o" * 7)
    stamp = (timezone.now() - timedelta(days=2)).timestamp()
    os.utime(old, (stamp, stamp))
    fresh = Path(settings.MEDIA_ROOT) / f"photo-album/{album.pk}/new/original/d.jpg"
    fresh.parent.mkdir(parents=True)
    fresh.write_bytes(b"n" * 3)

    response = admin_client.post(reverse("siteadmin:photo_album_measure"))  # eager in the tests

    assert response.status_code == 302 and response["Location"].endswith("#lager")
    report = StorageReport.objects.get()
    assert report.totals["original"] == {"objects": 4, "bytes": 2 * len(content) + 10}
    assert report.orphans == {
        "objects": 1,
        "bytes": 7,
        "keys": [f"photo-album/{album.pk}/gone/original/c.jpg"],
    }
    assert report.totals["sidecar"] == {"objects": 2, "bytes": 20}
    assert report.totals["imports"] == {"objects": 1, "bytes": 100}
    assert report.totals["high-definition"]["objects"] == report.totals["thumbnail"]["objects"] == 2
    assert set(report.by_album) == {str(album.pk), str(sub.pk)}
    assert report.media_bytes + 100 == report.total_bytes
    page_text = admin_client.get(reverse(URL)).content.decode()
    assert "1 fil uden billede" in page_text and f"photo-album/{album.pk}/gone/original/c.jpg" in page_text

    page = admin_client.get(reverse(URL)).context
    assert page["storage"].largest[0].label == "Fest"  # with its sub-album
    assert page["storage"].largest[0].bytes == report.by_album[str(album.pk)] + report.by_album[str(sub.pk)]
    assert page["storage"].largest[0].items == 2
    assert {row.folder: row.bytes for row in page["view"].folders} == {"2004": report.media_bytes}


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
    storage = PhotoAlbumS3Storage(
        bucket_name="test-bucket",
        access_key="k",
        secret_key="s",
        endpoint_url="https://fsn1.your-objectstorage.com",
        region_name="fsn1",
        signature_version="s3v4",
        addressing_style="virtual",
        location="",
    )
    monkeypatch.setattr(dashboard, "get_photo_album_storage", lambda: storage)
    with Stubber(storage.connection.meta.client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _version(key: str, size: int, *, latest: bool = True, age: timedelta = timedelta(0)) -> dict[str, object]:
    return {
        "Key": key,
        "Size": size,
        "IsLatest": latest,
        "VersionId": "v",
        "LastModified": timezone.now() - age,
    }


@pytest.mark.django_db
def test_a_bucket_is_listed_with_the_older_versions_it_keeps(bucket: Stubber) -> None:
    bucket.add_response(
        "list_object_versions",
        {
            "Versions": [
                _version("photo-album/7/u1/original/a.jpg", 1000),
                _version("photo-album/7/u1/original/a.jpg", 900, latest=False),
                _version("photo-album/7/u1/thumbnail/a.jpg", 10),
            ],
            "IsTruncated": True,
            "NextKeyMarker": "k",
            "NextVersionIdMarker": "v",
        },
        {"Bucket": "test-bucket", "Prefix": "photo-album/"},
    )
    bucket.add_response(
        "list_object_versions",
        {
            "Versions": [
                _version("photo-album/8/u2/high-definition/b.jpg", 500),
                _version("photo-album/9/u3/original/c.jpg", 5, age=timedelta(days=3)),  # no row has it
            ],
            "IsTruncated": False,
        },
        {"Bucket": "test-bucket", "Prefix": "photo-album/", "KeyMarker": "k", "VersionIdMarker": "v"},
    )
    # A store that cannot list versions: its objects, then.
    bucket.add_client_error("list_object_versions", service_error_code="NotImplemented", http_status_code=501)
    bucket.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "photo-album-imports/t/Fest.zip", "Size": 70}], "IsTruncated": False},
        {"Bucket": "test-bucket", "Prefix": "photo-album-imports/"},
    )
    bucket.add_response(
        "list_object_versions",
        {"Versions": [], "IsTruncated": False},
        {"Bucket": "test-bucket", "Prefix": "photo-album-zips/"},
    )
    old = StorageReport.objects.create(totals={})
    StorageReport.objects.filter(pk=old.pk).update(measured_at=timezone.now() - timedelta(days=400))

    measure_photo_album_storage()

    report = StorageReport.objects.get()  # the year-old one is gone
    assert report.totals == {
        "original": {"objects": 2, "bytes": 1005},
        "noncurrent": {"objects": 1, "bytes": 900},
        "thumbnail": {"objects": 1, "bytes": 10},
        "high-definition": {"objects": 1, "bytes": 500},
        "imports": {"objects": 1, "bytes": 70},
    }
    assert report.by_album == {"7": 1010, "8": 500, "9": 5}
    # No row points at any of them, but only the old one is past an upload still under way.
    assert report.orphans == {"objects": 1, "bytes": 5, "keys": ["photo-album/9/u3/original/c.jpg"]}


@pytest.mark.django_db
def test_the_page_says_how_much_the_storage_grew(admin_client: Client) -> None:
    earlier = StorageReport.objects.create(totals={"original": {"objects": 1, "bytes": 1000}})
    StorageReport.objects.filter(pk=earlier.pk).update(measured_at=timezone.now() - timedelta(days=30))
    StorageReport.objects.create(totals={"original": {"objects": 2, "bytes": 3000}})

    storage = admin_client.get(reverse(URL)).context["storage"]

    assert (storage.growth, storage.growth_size) == (2000, 2000)
    assert [(line.key, line.bytes) for line in storage.lines] == [("original", 3000)]


def test_the_measurement_runs_every_night_on_the_media_queue() -> None:
    schedule = settings.CELERY_BEAT_SCHEDULE["measure-photo-album-storage"]
    assert schedule["task"] == "photo_album.tasks.measure_photo_album_storage"
    assert settings.CELERY_TASK_ROUTES["photo_album.tasks.measure_photo_album_storage"] == {"queue": "media"}
