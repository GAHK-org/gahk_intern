"""Where a photo's date comes from, and the approximations for the ones without one."""

import struct
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from typing import Any

import pytest
from django.core.files.base import ContentFile, File
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import ExifTags, Image

from photo_album import services
from photo_album.models import Album, DateSource, DerivativeState, Media, MediaStatus, believable_date
from photo_album.services import (
    Variants,
    appledouble_dates,
    build_derivatives,
    extract_captured_at,
    import_zip_album,
    settle_dates,
    upload_media,
)
from residents.models import Resident, Role

from .media_samples import jpeg_bytes, video_upload


def local(*parts: int) -> datetime:
    return timezone.make_aware(datetime(*parts))


def _item(album: Album, resident: Resident, filename: str, **fields: object) -> Media:
    defaults: dict[str, object] = {
        "title": filename,
        "filename": filename,
        "original": f"photo-album/x/{filename}",
        "high_definition": "b",
        "thumbnail": "c",
        "derivative_state": DerivativeState.READY,
        "status": MediaStatus.APPROVED,
    }
    return Media.objects.create(album=album, requested_by=resident, **{**defaults, **fields})


def _dates(*items: Media) -> list[tuple[datetime | None, str]]:
    rows = {media.pk: media for media in Media.objects.filter(pk__in=[item.pk for item in items])}
    return [(rows[item.pk].sort_at, rows[item.pk].date_source) for item in items]


def _exif_jpeg(taken: str) -> bytes:
    exif = Image.Exif()
    exif.get_ifd(ExifTags.IFD.Exif)[36867] = taken
    output = BytesIO()
    Image.new("RGB", (16, 16), "red").save(output, format="JPEG", exif=exif)
    return output.getvalue()


def _xmp_jpeg(xmp: bytes) -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 16), "blue").save(output, format="JPEG", xmp=xmp)
    return output.getvalue()


def _iptc_jpeg(records: dict[tuple[int, int], bytes]) -> bytes:
    """A JPEG with an IPTC block in Photoshop's APP13 segment, which Pillow can read but not write."""
    iptc = b"".join(
        b"\x1c" + bytes(key) + struct.pack(">H", len(value)) + value for key, value in records.items()
    )
    iptc += b"\x00" * (len(iptc) % 2)
    resource = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" + struct.pack(">I", len(iptc)) + iptc
    payload = b"Photoshop 3.0\x00" + resource
    plain = jpeg_bytes()
    return plain[:2] + b"\xff\xed" + struct.pack(">H", len(payload) + 2) + payload + plain[2:]


def _appledouble(*dates: datetime | None) -> bytes:
    """A netatalk sidecar with a File Dates entry: creation, modification, backup, access."""
    stamps = [
        int((moment - datetime(2000, 1, 1, tzinfo=UTC)).total_seconds()) if moment else -0x80000000
        for moment in (*dates, None, None, None, None)[:4]
    ]
    header = struct.pack(">II16sH", 0x00051607, 0x00020000, b"", 1)
    return header + struct.pack(">III", 8, len(header) + 12, 16) + struct.pack(">iiii", *stamps)


def _upload(name: str, content: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="image/jpeg")


# --- What the file says -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (_xmp_jpeg(b'<rdf:Description photoshop:DateCreated="2004-03-12"/>'), local(2004, 3, 12, 12)),
        (
            _xmp_jpeg(b"<exif:DateTimeOriginal>2004-03-12T18:30:05+01:00</exif:DateTimeOriginal>"),
            datetime(2004, 3, 12, 17, 30, 5, tzinfo=UTC),
        ),
        (_xmp_jpeg(b'<rdf:Description xmp:CreateDate="2004-03"/>'), None),  # a month is not a date
        (
            _iptc_jpeg({(2, 55): b"20040312", (2, 60): b"183000+0100"}),
            datetime(2004, 3, 12, 17, 30, tzinfo=UTC),
        ),
        (_iptc_jpeg({(2, 55): b"20040312"}), local(2004, 3, 12, 12)),
        (jpeg_bytes(), None),
    ],
    ids=["xmp-attribute-day", "xmp-element-with-offset", "xmp-month-only", "iptc", "iptc-day", "nothing"],
)
def test_a_capture_date_is_also_read_from_xmp_and_iptc(content: bytes, expected: datetime | None) -> None:
    assert extract_captured_at(_upload("x.jpg", content)) == expected


def test_exif_comes_before_xmp() -> None:
    exif = Image.Exif()
    exif.get_ifd(ExifTags.IFD.Exif)[36867] = "2004:03:01 10:00:00"
    output = BytesIO()
    Image.new("RGB", (16, 16)).save(
        output, format="JPEG", exif=exif, xmp=b'<rdf:Description photoshop:DateCreated="1999-01-01"/>'
    )

    assert extract_captured_at(_upload("x.jpg", output.getvalue())) == local(2004, 3, 1, 10)


def test_an_appledouble_sidecar_gives_its_files_dates() -> None:
    created, modified = datetime(2004, 10, 24, 20, 3, tzinfo=UTC), datetime(2004, 10, 30, 11, 51, tzinfo=UTC)

    assert appledouble_dates(_appledouble(created, modified)) == [created, modified]
    assert appledouble_dates(_appledouble(None, modified)) == [modified]
    assert appledouble_dates(b"\x00\x05\x16\x07 not a sidecar") == []
    assert appledouble_dates(b"") == []


@pytest.mark.parametrize(
    ("moment", "folder", "file_time", "believed"),
    [
        (local(2004, 6, 1), "2004", True, True),
        (local(2003, 12, 15), "2004", True, True),  # a December film, developed in the new year
        (local(2005, 1, 1, 1), "2004", True, True),  # New Year's Eve ran over
        (local(2022, 3, 1), "2004", True, False),  # the day the archive went into Dropbox
        (local(2001, 1, 5), "2004", False, False),  # a camera whose clock had reset
        (local(1975, 6, 1), "Andet", False, True),  # a scanned print says when it was taken
        (local(1985, 6, 1), "Andet", True, False),  # but no file was saved then
    ],
)
def test_which_dates_are_believed(moment: datetime, folder: str, file_time: bool, believed: bool) -> None:
    assert believable_date(moment, folder, file_time=file_time) is believed


# --- Approximations -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_undated_item_is_placed_between_its_neighbours_in_file_name_order(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    last = _item(album, resident, "IMG_10.JPG", captured_at=local(2004, 3, 1, 13))
    first = _item(album, resident, "IMG_1.JPG", captured_at=local(2004, 3, 1, 10))
    second, third = _item(album, resident, "IMG_2.JPG"), _item(album, resident, "img_3.jpg")
    after = _item(album, resident, "IMG_11.JPG")

    assert settle_dates(album) == 3
    assert _dates(first, second, third, last, after) == [
        (local(2004, 3, 1, 10), DateSource.CAPTURED),
        (local(2004, 3, 1, 11), DateSource.NEIGHBOURS),
        (local(2004, 3, 1, 12), DateSource.NEIGHBOURS),
        (local(2004, 3, 1, 13), DateSource.CAPTURED),
        (local(2004, 3, 1, 13, 0, 1), DateSource.NEIGHBOURS),
    ]
    assert settle_dates(album) == 0  # settled once is settled


@pytest.mark.django_db
def test_an_album_without_dates_borrows_its_parents_then_its_years(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    root = Album.objects.create(folder="2004", name="Fremkaldt")
    for day in (3, 5, 20):
        _item(root, resident, f"{day}.jpg", captured_at=local(2004, 6, day))
    child = Album.objects.create(folder="2004", name="Skovtur", parent=root)
    first, second = _item(child, resident, "a.jpg"), _item(child, resident, "b.jpg")
    alone = Album.objects.create(folder="2004", name="Scannet")
    scan = _item(alone, resident, "scan.jpg")

    settle_dates(root)
    settle_dates(alone)

    assert _dates(first, second, scan) == [
        (local(2004, 6, 5), DateSource.NEIGHBOURS),  # the middle of the parent's dates
        (local(2004, 6, 5, 0, 0, 1), DateSource.NEIGHBOURS),
        (local(2004, 1, 1), DateSource.ALBUM_YEAR),
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("folder", ["Andet", str(timezone.localdate().year)])
def test_the_upload_time_stands_where_it_could_be_true(
    folder: str, make_resident: Callable[..., Resident]
) -> None:
    """In Andet there is no year to go by, and in this year's album an upload from this year is a
    better guess than New Year's Day."""
    resident = make_resident()
    album = Album.objects.create(folder=folder, name="Billeder")
    scan = _item(album, resident, "scan.jpg")

    settle_dates(album)

    scan.refresh_from_db()
    assert (scan.sort_at, scan.date_source) == (scan.added_at, DateSource.ADDED)


@pytest.mark.django_db
def test_a_file_time_outside_the_folders_year_is_not_believed(make_resident: Callable[..., Resident]) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    dropbox = _item(album, resident, "IMG_1.JPG", file_modified_at=local(2022, 3, 1))
    server = _item(album, resident, "IMG_2.JPG", file_modified_at=local(2004, 9, 18))

    settle_dates(album)

    assert _dates(dropbox, server) == [
        (local(2004, 9, 17, 23, 59, 59), DateSource.NEIGHBOURS),
        (local(2004, 9, 18), DateSource.FILE),
    ]


@pytest.mark.django_db
def test_a_capture_date_outside_the_year_stays_its_own_but_is_not_spread(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="St. Bededagsaften")
    reset = _item(album, resident, "SIMG0941.JPG", captured_at=local(2001, 1, 5, 2, 40))
    beside = _item(album, resident, "SIMG0942.JPG")

    settle_dates(album)

    assert _dates(reset, beside) == [
        (local(2001, 1, 5, 2, 40), DateSource.CAPTURED),
        (local(2004, 1, 1, 0, 0, 1), DateSource.ALBUM_YEAR),
    ]


@pytest.mark.django_db
def test_saving_keeps_an_approximation_until_the_item_has_a_date_of_its_own(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    media = _item(album, resident, "scan.jpg")
    settle_dates(album)
    media.refresh_from_db()

    media.title = "Nyt navn"
    media.save()
    media.refresh_from_db()
    assert (media.sort_at, media.date_source) == (local(2004, 1, 1), DateSource.ALBUM_YEAR)

    media.captured_at = local(2004, 5, 1)
    media.save(update_fields=["captured_at"])
    media.refresh_from_db()
    assert (media.sort_at, media.date_source) == (local(2004, 5, 1), DateSource.CAPTURED)


@pytest.mark.django_db
def test_a_save_without_a_date_does_not_load_the_album(
    make_resident: Callable[..., Resident], django_assert_num_queries: Callable[..., Any]
) -> None:
    """Approving or binning a selection saves every item: an album query each would add up."""
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    media = Media.objects.get(pk=_item(album, resident, "IMG_1.JPG", file_modified_at=local(2004, 5, 1)).pk)
    media.status = MediaStatus.REJECTED

    with django_assert_num_queries(1):
        media.save(update_fields=["status"])


# --- When it happens ----------------------------------------------------------------------------


def _zip(members: dict[str, bytes], *, entry_time: tuple[int, ...] = (2022, 3, 1, 12, 0, 0)) -> File:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=entry_time), data)  # type: ignore[arg-type]
    buffer.seek(0)
    return File(buffer, name="Fest.zip")


@pytest.mark.django_db
def test_an_import_takes_a_files_date_from_its_appledouble_sidecar(
    make_resident: Callable[..., Resident],
) -> None:
    """The Dropbox archive: every entry stamped 2022, the old server's sidecars beside the files."""
    resident = make_resident()
    real = datetime(2004, 10, 24, 20, 3, tzinfo=UTC)
    archive = _zip(
        {
            "Fest/IMG_1.JPG": jpeg_bytes(),
            "Fest/.AppleDouble/IMG_1.JPG": _appledouble(real, real + timedelta(days=6)),
            "Fest/IMG_2.JPG": jpeg_bytes(),
            "Fest/.AppleDouble/IMG_2.JPG": _appledouble(datetime(2001, 1, 5, tzinfo=UTC)),  # clock reset
            "Fest/.AppleDouble/.Parent": _appledouble(real),
        }
    )

    result = import_zip_album(archive=archive, folder="2004", resident=resident)

    first, second = Media.objects.filter(album__in=result.albums).order_by("filename")
    assert (first.file_modified_at, first.sort_at, first.date_source) == (real, real, DateSource.FILE)
    assert second.file_modified_at == local(2022, 3, 1, 12)  # the entry's, and not believed
    assert (second.sort_at, second.date_source) == (real + timedelta(seconds=1), DateSource.NEIGHBOURS)


@pytest.mark.django_db
def test_an_upload_settles_its_album(client: Client, make_resident: Callable[..., Resident]) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2004", name="Fest")
    client.force_login(administrator)

    client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {
            "uploads": [
                _upload("IMG_1.JPG", _exif_jpeg("2004:03:01 10:00:00")),
                _upload("IMG_2.JPG", jpeg_bytes()),
            ]
        },
    )

    undated = Media.objects.get(filename="IMG_2.JPG")
    assert (undated.sort_at, undated.date_source) == (local(2004, 3, 1, 10, 0, 1), DateSource.NEIGHBOURS)


@pytest.mark.django_db
def test_a_videos_date_from_its_transcode_settles_its_album(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    video = upload_media(
        album=album, uploaded_file=video_upload("MVI_1.AVI"), resident=resident, approved=True
    )
    beside = _item(album, resident, "MVI_2.JPG")
    recorded = local(2004, 5, 1, 20)
    monkeypatch.setattr(
        services,
        "video_variants",
        lambda *_: Variants(
            ContentFile(b"v", name="MVI_1.mp4"), ContentFile(jpeg_bytes(), name="MVI_1.jpg"), recorded
        ),
    )

    assert build_derivatives(video)

    assert _dates(video, beside) == [
        (recorded, DateSource.CAPTURED),
        (recorded + timedelta(seconds=1), DateSource.NEIGHBOURS),
    ]


@pytest.mark.django_db
def test_the_nightly_job_reads_old_originals_again_and_settles_every_album(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    scan = upload_media(
        album=album,
        uploaded_file=_upload(
            "scan.jpg", _xmp_jpeg(b'<rdf:Description photoshop:DateCreated="2004-03-12"/>')
        ),
        resident=resident,
    )
    dropbox = _item(album, resident, "zz.jpg", file_modified_at=local(2022, 3, 1))
    gone = _item(album, resident, "zzz.jpg")
    # As the migration leaves rows stored before: sorted by the file time, the scan not read for XMP.
    Media.objects.filter(pk=scan.pk).update(captured_at=None, capture_date_checked=False, date_source="added")
    Media.objects.filter(pk=dropbox.pk).update(sort_at=local(2022, 3, 1), date_source="file")
    Media.objects.filter(pk=gone.pk).update(capture_date_checked=False)
    stdout, stderr = StringIO(), StringIO()

    call_command("settle_photo_album_dates", stdout=stdout, stderr=stderr)

    assert _dates(scan, dropbox, gone) == [
        (local(2004, 3, 12, 12), DateSource.CAPTURED),
        (local(2004, 3, 12, 12, 0, 1), DateSource.NEIGHBOURS),
        (local(2004, 3, 12, 12, 0, 2), DateSource.NEIGHBOURS),
    ]
    assert "found 1 capture date(s); 1 could not be read; 0 left" in stdout.getvalue()
    assert f"Kunne ikke læse medie {gone.pk}" in stderr.getvalue()
    assert not Media.objects.filter(capture_date_checked=False).exists()


# --- What the reader sees -----------------------------------------------------------------------


@pytest.mark.django_db
def test_the_viewer_and_the_grid_say_a_date_is_approximate(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    dated = _item(album, resident, "IMG_1.JPG", captured_at=local(2004, 3, 1, 10))
    near = _item(album, resident, "IMG_2.JPG")
    elsewhere = Album.objects.create(folder="2004", name="Scannet")
    yearly = _item(elsewhere, resident, "scan.jpg")
    settle_dates(album)
    settle_dates(elsewhere)
    client.force_login(resident)

    def detail(media: Media) -> dict[str, object]:
        return client.get(reverse("photo_album:media_detail", args=[media.pk])).json()

    assert (detail(dated)["approximateDate"], detail(dated)["approximateDateBasis"]) == ("", "")
    assert detail(near)["approximateDate"] == "ca. marts 2004"
    assert detail(near)["approximateDateBasis"] == "ud fra de andre billeder i albummet"
    assert detail(yearly)["approximateDate"] == "ca. 2004"
    assert detail(yearly)["approximateDateBasis"] == "ud fra albummets årstal"

    grid = client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()
    assert grid.count('class="album-date-mark"') == 1
    assert 'data-month-label="Marts 2004"' in grid
    yearly_grid = client.get(reverse("photo_album:detail", args=[elsewhere.pk])).content.decode()
    assert 'data-month="2004" data-month-label="2004 · ukendt måned"' in yearly_grid
