"""Camera RAW files, the metadata the high-definition copies keep, and how large a file may be."""

import zipfile
from collections.abc import Callable
from io import BytesIO
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import File
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from PIL import Image, ImageCms, IptcImagePlugin

from photo_album.models import Album, Media
from photo_album.services import (
    _ffmpeg,
    build_derivatives,
    import_zip_album,
    probe_video,
    read_head,
    upload_media,
)
from photo_album.uploads import check_media_upload, validate_media_upload
from residents.models import Resident, Role

from .media_samples import _ffmpeg_output, dng_bytes, jpeg_bytes, mp4_bytes

SYSTEM = "system@gahk.dk"
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _upload(make_resident: Callable[..., Resident], name: str, content: bytes) -> Media:
    album, _ = Album.objects.get_or_create(folder="2004", name="Fremkaldning")
    resident = make_resident(email=f"uploader-{Media.objects.count()}@gahk.dk")
    return upload_media(album=album, uploaded_file=SimpleUploadedFile(name, content), resident=resident)


def _exif_photo(**tags: object) -> bytes:
    """A 2400x1200 JPEG with a camera's EXIF (a GPS position among it), XMP, IPTC and a colour
    profile, turned on its side by its orientation tag."""
    image = Image.new("RGB", (2400, 1200), "teal")
    exif = image.getexif()
    exif.update({271: "Canon", 272: "EOS 5D", 274: 6, 305: "Firmware 1.1.1", **tags})
    exif.get_ifd(0x8769).update({36867: "2004:05:06 07:08:09", 0xA434: "EF 50mm f/1.8"})
    exif.get_ifd(0x8825).update({1: "N", 2: (55.0, 40.0, 33.6), 3: "E", 4: (12.0, 34.0, 6.48)})
    xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:tiff="http://ns.adobe.com/tiff/1.0/"'
        b' tiff:Orientation="6"><dc:subject><rdf:Bag><rdf:li>Sommerfest</rdf:li></rdf:Bag></dc:subject>'
        b"</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    output = BytesIO()
    image.save(output, format="JPEG", exif=exif, xmp=xmp, icc_profile=SRGB)
    # IPTC's keywords and byline, as a Photoshop APP13 segment just after the JPEG's start.
    dataset = b"\x1c\x02\x19" + len(b"fest").to_bytes(2, "big") + b"fest"
    dataset += b"\x1c\x02\x50" + len(b"Fotogruppen").to_bytes(2, "big") + b"Fotogruppen"
    resource = b"8BIM\x04\x04\x00\x00" + len(dataset).to_bytes(4, "big") + dataset
    segment = b"Photoshop 3.0\0" + resource
    data = output.getvalue()
    return data[:2] + b"\xff\xed" + (len(segment) + 2).to_bytes(2, "big") + segment + data[2:]


# --- RAW ----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_raw_file_is_stored_as_it_came_flagged_and_given_upright_jpeg_copies(
    make_resident: Callable[..., Resident],
) -> None:
    original = dng_bytes((64, 48), orientation=6)
    media = _upload(make_resident, "IMG_0001.DNG", original)

    assert build_derivatives(media)

    assert media.is_raw
    assert media.content_type == "image/x-adobe-dng"
    with media.original.open("rb") as stored:
        assert stored.read() == original
    for copy in (media.high_definition, media.thumbnail):
        assert copy.name.endswith(".jpg")
        with Image.open(copy) as picture:
            assert picture.size == (48, 64)  # turned upright, as the camera was held


@pytest.mark.django_db
def test_a_raw_files_date_and_camera_are_read_from_it(make_resident: Callable[..., Resident]) -> None:
    """Pillow cannot open a RAW file at all, so exiv2 reads it."""
    original = dng_bytes(captured="2018:12:04 19:46:03", make="Panasonic", model="DMC-FZ1000")
    media = _upload(make_resident, "P1080125.DNG", original)

    assert media.captured_at is not None
    assert media.captured_at.strftime("%Y-%m-%d %H:%M:%S") == "2018-12-04 19:46:03"
    assert media.metadata["Kamera"] == "Panasonic DMC-FZ1000"


def test_a_photo_with_a_raw_name_and_other_content_is_refused() -> None:
    with pytest.raises(ValidationError, match="RAW-filen kunne ikke læses"):
        validate_media_upload(
            SimpleUploadedFile("IMG_0001.CR2", jpeg_bytes(), content_type="image/x-canon-cr2")
        )

    validate_media_upload(SimpleUploadedFile("IMG_0001.CR2", dng_bytes(), content_type="image/x-canon-cr2"))


def test_a_raw_name_may_come_with_any_image_content_type_but_no_other() -> None:
    """Each system names RAW types its own way, if at all."""
    for content_type in ("", "application/octet-stream", "image/x-canon-cr2", "image/x-dcraw", "image/tiff"):
        assert (
            check_media_upload(SimpleNamespace(name="IMG_0001.CR2", content_type=content_type, size=1))
            is None
        )
    assert "passer ikke" in (
        check_media_upload(SimpleNamespace(name="IMG_0001.CR2", content_type="application/pdf", size=1)) or ""
    )


@override_settings(PHOTO_ALBUM_IMAGE_MAX_MB=1, PHOTO_ALBUM_RAW_MAX_MB=3)
def test_a_raw_file_has_a_size_limit_of_its_own() -> None:
    two_mb = 2 * 1024 * 1024

    assert check_media_upload(SimpleNamespace(name="IMG_0001.NEF", content_type="", size=two_mb)) is None
    assert "over 1 MB" in (
        check_media_upload(SimpleNamespace(name="IMG_0001.JPG", content_type="", size=two_mb)) or ""
    )
    assert "over 3 MB" in (
        check_media_upload(SimpleNamespace(name="IMG_0001.NEF", content_type="", size=2 * two_mb)) or ""
    )


@pytest.mark.django_db
def test_the_viewer_says_when_the_original_is_a_raw_file(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2004", name="Fremkaldning")
    raw = upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("a.dng", dng_bytes()),
        resident=administrator,
        approved=True,
    )
    photo = upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("b.jpg", jpeg_bytes()),
        resident=administrator,
        approved=True,
    )
    client.force_login(administrator)

    assert client.get(reverse("photo_album:media_detail", args=[raw.pk])).json()["raw"] is True
    assert client.get(reverse("photo_album:media_detail", args=[photo.pk])).json()["raw"] is False


# --- Metadata in the high-definition copies -------------------------------------------------------


@pytest.mark.django_db
def test_the_hd_copy_of_a_photo_keeps_its_exif_iptc_and_xmp(make_resident: Callable[..., Resident]) -> None:
    """Pillow wrote nothing but the pixels: a photo downloaded as "Komprimeret (HD)" came without its
    date, camera or place."""
    media = _upload(make_resident, "IMG_0001.JPG", _exif_photo())

    assert build_derivatives(media)

    with Image.open(media.high_definition) as copy:
        assert copy.size == (800, 1600)  # turned upright
        exif = copy.getexif()
        assert (exif[271], exif[272], exif[305]) == ("Canon", "EOS 5D", "Firmware 1.1.1")
        assert exif[274] == 1  # already upright: turning it again would put it on its side
        photo = exif.get_ifd(0x8769)
        assert photo[36867] == "2004:05:06 07:08:09"
        assert photo[0xA434] == "EF 50mm f/1.8"
        assert exif.get_ifd(0x8825)[1] == "N"
        assert b"Sommerfest" in copy.info["xmp"]
        assert b'tiff:Orientation="1"' in copy.info["xmp"]
        assert copy.info["icc_profile"] == SRGB
        iptc = IptcImagePlugin.getiptcinfo(copy) or {}
        assert iptc[(2, 25)] == b"fest"
        assert iptc[(2, 80)] == b"Fotogruppen"
    with Image.open(media.thumbnail) as thumbnail:
        assert not thumbnail.getexif()


@pytest.mark.django_db
def test_the_hd_copy_of_a_heic_photo_keeps_its_exif(make_resident: Callable[..., Resident]) -> None:
    from pillow_heif import from_pillow

    image = Image.new("RGB", (2400, 1200), "red")
    exif = image.getexif()
    exif.update({271: "Apple", 272: "iPhone 15"})
    exif.get_ifd(0x8769)[36867] = "2024:06:21 22:15:00"
    heic = BytesIO()
    from_pillow(image).save(heic, exif=exif.tobytes())
    media = _upload(make_resident, "IMG_0001.HEIC", heic.getvalue())

    assert build_derivatives(media)

    with Image.open(media.high_definition) as copy:
        assert copy.getexif()[272] == "iPhone 15"
        assert copy.getexif().get_ifd(0x8769)[36867] == "2024:06:21 22:15:00"


@pytest.mark.django_db
def test_the_hd_copy_of_a_raw_file_keeps_its_exif_but_not_how_the_raw_stores_its_pixels(
    make_resident: Callable[..., Resident],
) -> None:
    media = _upload(make_resident, "IMG_0001.DNG", dng_bytes(orientation=6))

    assert build_derivatives(media)

    with Image.open(media.high_definition) as copy:
        exif = copy.getexif()
        assert (exif[271], exif[272], exif[274]) == ("Canon", "EOS 5D", 1)
        assert exif.get_ifd(0x8769)[36867] == "2004:05:06 07:08:09"
        # Its sensor's width, its compression, its strips, its colour filter, DNG's own tags
        for tag in (256, 257, 259, 273, 279, 33422, 50706, 50721):
            assert tag not in exif


@pytest.mark.django_db
def test_a_photo_without_metadata_gets_a_copy_without_any(make_resident: Callable[..., Resident]) -> None:
    media = _upload(make_resident, "plain.jpg", jpeg_bytes(size=(2000, 1000)))

    assert build_derivatives(media)

    with Image.open(media.high_definition) as copy:
        assert copy.size == (1600, 800)
        assert not copy.getexif()


@pytest.mark.django_db
def test_the_hd_copy_of_a_video_keeps_when_where_and_with_what_it_was_recorded(
    make_resident: Callable[..., Resident],
) -> None:
    """ffmpeg left out the recording time and every key an iPhone writes."""
    recorded = _ffmpeg_output(
        ".mov",
        *("-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=5"),
        *("-c:v", "libx264", "-pix_fmt", "yuv420p"),
        *("-metadata", "creation_time=2019-07-24T01:25:04Z"),
        *("-metadata", "com.apple.quicktime.location.ISO6709=+55.6761+012.5683+010.000/"),
        *("-metadata", "com.apple.quicktime.make=Apple", "-metadata", "com.apple.quicktime.model=iPhone X"),
        *("-movflags", "use_metadata_tags"),
    )
    media = _upload(make_resident, "IMG_0001.MOV", recorded)

    assert build_derivatives(media)

    copy = probe_video(_ffmpeg(), "", read_head(media.high_definition, 1024 * 1024)).text
    assert "creation_time   : 2019-07-24T01:25:04" in copy
    assert "com.apple.quicktime.location.ISO6709: +55.6761+012.5683+010.000/" in copy
    assert "com.apple.quicktime.model: iPhone X" in copy
    assert "major_brand     : isom" in copy  # the MP4's own, not the QuickTime file's


# --- How large a video may be -------------------------------------------------------------------


def test_a_browser_may_upload_a_video_of_up_to_3_gb() -> None:
    gigabyte = 1024 * 1024 * 1024

    assert (
        check_media_upload(SimpleNamespace(name="clip.mp4", content_type="video/mp4", size=3 * gigabyte))
        is None
    )
    assert "over 3072 MB" in (
        check_media_upload(SimpleNamespace(name="clip.mp4", content_type="video/mp4", size=3 * gigabyte + 1))
        or ""
    )


def _video_archive() -> File:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Fest/clip.mp4", mp4_bytes())
    payload.seek(0)
    return File(payload, name="Fest.zip")


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_VIDEO_MAX_MB=0, PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL=SYSTEM)
def test_the_import_script_brings_a_video_in_whatever_its_size(
    make_resident: Callable[..., Resident],
) -> None:
    with pytest.raises(ValidationError, match="Videoen er for stor"):
        import_zip_album(archive=_video_archive(), folder="2016", resident=make_resident())

    result = import_zip_album(archive=_video_archive(), folder="2016", resident=make_resident(email=SYSTEM))

    assert result.added == 1


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_the_import_scripts_rules_take_raw_files_and_videos_of_any_size(client: Client) -> None:
    response = client.get(
        reverse("photo_album:system_albums"), {"folder": "2016"}, HTTP_AUTHORIZATION="Bearer migration-token"
    )

    rules = response.json()["rules"]
    assert ".cr2" in rules["rawExtensions"] and ".cr2" in rules["imageExtensions"]
    assert rules["rawMaxMb"] == 200
    assert rules["videoMaxMb"] is None
