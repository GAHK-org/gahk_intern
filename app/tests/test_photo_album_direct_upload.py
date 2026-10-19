"""The import script's archive straight into the bucket, past the proxy (photo_album.direct_upload)."""

import json
from collections.abc import Callable, Iterator
from io import BytesIO
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber
from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from photo_album import direct_upload
from photo_album.models import Album, AlbumImport, Media, MediaStatus
from photo_album.services import upload_media
from photo_album.storage import PhotoAlbumS3Storage
from residents.models import Resident, Role

from .media_samples import image_upload, jpeg_bytes

TOKEN = {"HTTP_AUTHORIZATION": "Bearer migration-token"}
MB = 1024 * 1024


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
    """A bucket that answers what the test says it will, and complains about anything else."""
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
    monkeypatch.setattr(direct_upload, "get_photo_album_storage", lambda: storage)
    with Stubber(storage.connection.meta.client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _begin(client: Client, bucket: Stubber, size: int) -> dict[str, object]:
    bucket.add_response(
        "create_multipart_upload",
        {"UploadId": "upload-1"},
        {"Bucket": "test-bucket", "Key": ANY, "ContentType": "application/zip"},
    )
    response = client.post(
        reverse("photo_album:system_import_upload"), {"name": "Bornholm med GAHK.zip", "size": size}, **TOKEN
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_without_a_bucket_the_archive_goes_with_the_import(client: Client) -> None:
    url = reverse("photo_album:system_import_upload")

    assert client.post(url, {"name": "Fest.zip", "size": 10}).status_code == 403
    assert client.post(url, {"name": "Fest.zip", "size": 10}, **TOKEN).json() == {"direct": False}
    assert client.post(url, {"name": "Fest.rar", "size": 10}, **TOKEN).status_code == 400


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_an_archive_sent_to_the_bucket_in_parts_is_imported_from_there(
    client: Client, bucket: Stubber
) -> None:
    """The 3.2 GB album that the proxy cut off: no part of it passes through the app."""
    upload = _begin(client, bucket, 150 * MB)

    assert upload["direct"] is True and upload["partSize"] == 64 * MB
    urls = [urlparse(url) for url in upload["urls"]]  # type: ignore[attr-defined]
    assert [parse_qs(url.query)["partNumber"] for url in urls] == [["1"], ["2"], ["3"]]
    assert {parse_qs(url.query)["uploadId"][0] for url in urls} == {"upload-1"}
    key = unquote(urls[0].path).lstrip("/")
    assert key.startswith("photo-album-imports/") and key.endswith("/Bornholm med GAHK.zip")

    etags = ['"a"', '"b"', '"c"']
    bucket.add_response(
        "complete_multipart_upload",
        {},
        {
            "Bucket": "test-bucket",
            "Key": key,
            "UploadId": "upload-1",
            "MultipartUpload": {
                "Parts": [{"ETag": etag, "PartNumber": n} for n, etag in enumerate(etags, 1)]
            },
        },
    )
    bucket.add_response("head_object", {"ContentLength": 150 * MB}, {"Bucket": "test-bucket", "Key": key})
    response = client.post(
        reverse("photo_album:system_import_zip"),
        {"folder": "2006", "ticket": upload["ticket"], "parts": json.dumps(etags)},
        **TOKEN,
    )

    assert response.status_code == 202
    album_import = AlbumImport.objects.get()
    assert (album_import.archive.name, album_import.archive_name) == (key, "Bornholm med GAHK.zip")
    assert album_import.lock_when_done and album_import.merge_into_existing  # as any system import


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_an_upload_is_only_imported_as_the_server_set_it_up(client: Client, bucket: Stubber) -> None:
    upload = _begin(client, bucket, 10 * MB)
    url = reverse("photo_album:system_import_zip")

    forged = client.post(url, {"folder": "2006", "ticket": "forged", "parts": '["x"]'}, **TOKEN)
    assert forged.status_code == 400
    assert "ukendt" in forged.json()["errors"]["archive"][0]["message"]

    bucket.add_response("complete_multipart_upload", {}, None)
    bucket.add_response("head_object", {"ContentLength": 5 * MB}, None)  # half of it went missing
    bucket.add_response("delete_object", {}, None)
    short = client.post(url, {"folder": "2006", "ticket": upload["ticket"], "parts": '["x"]'}, **TOKEN)
    assert short.status_code == 400
    assert "5242880 bytes" in short.json()["errors"]["archive"][0]["message"]
    assert not AlbumImport.objects.exists()


# --- From the browser ---------------------------------------------------------------------------


def _complete(bucket: Stubber, key: str, content: bytes) -> None:
    """The bucket's side of committing `content`, sent to `key`: complete it, then read it back."""
    bucket.add_response("complete_multipart_upload", {}, None)
    bucket.add_response("head_object", {"ContentLength": len(content)}, {"Bucket": "test-bucket", "Key": key})
    bucket.add_response(
        "get_object",
        {"Body": StreamingBody(BytesIO(content), len(content)), "ContentLength": len(content)},
        {"Bucket": "test-bucket", "Key": key},
    )


def _begin_media(client: Client, bucket: Stubber, album: Album, name: str, size: int) -> dict[str, object]:
    bucket.add_response("create_multipart_upload", {"UploadId": "upload-2"}, None)
    response = client.post(
        reverse("photo_album:upload_begin", args=[album.pk]),
        {"name": name, "size": size, "content_type": "image/jpeg"},
    )
    assert response.status_code == 200, response.content
    return response.json()


def _key(upload: dict[str, object]) -> str:
    return unquote(urlparse(upload["urls"][0]).path).lstrip("/")  # type: ignore[index]


def _commit(client: Client, album: Album, upload: dict[str, object]) -> HttpResponse:
    return client.post(  # type: ignore[return-value]
        reverse("photo_album:upload_commit", args=[album.pk]),
        {"ticket": upload["ticket"], "parts": '["\\"a\\""]', "title": "", "last_modified": "1083700000000"},
    )


@pytest.mark.django_db
def test_a_large_photo_sent_to_the_bucket_is_checked_and_stored_where_it_landed(
    client: Client, bucket: Stubber, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    client.force_login(resident)
    content = jpeg_bytes()
    upload = _begin_media(client, bucket, album, "IMG 1.JPG", len(content))
    key = _key(upload)
    assert key.startswith(f"photo-album/{album.pk}/") and key.endswith("/original/IMG_1.JPG")

    _complete(bucket, key, content)
    response = _commit(client, album, upload)

    assert response.status_code == 201
    assert response.json() == {"uploaded": 1, "skipped": [], "warnings": []}
    media = Media.objects.get()
    assert (media.original.name, media.filename, media.album) == (key, "IMG 1.JPG", album)
    assert key.split("/")[2] == str(media.uuid)
    assert media.status == MediaStatus.PENDING  # as any resident's upload


@pytest.mark.django_db
def test_a_file_sent_to_the_bucket_is_refused_as_loudly_as_one_sent_with_the_form(
    client: Client, bucket: Stubber, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    client.force_login(resident)
    begin = reverse("photo_album:upload_begin", args=[album.pk])

    # By name and size before a byte is sent.
    svg = client.post(begin, {"name": "x.svg", "size": 10, "content_type": "image/svg+xml"})
    assert (
        svg.status_code == 400
        and "Filtypen er ikke understøttet" in svg.json()["errors"]["uploads"][0]["message"]
    )

    # By content once it is in: removed from the bucket again.
    upload = _begin_media(client, bucket, album, "broken.jpg", 12)
    _complete(bucket, _key(upload), b"not a jpeg!!")
    bucket.add_response("delete_object", {}, {"Bucket": "test-bucket", "Key": _key(upload)})
    broken = _commit(client, album, upload)
    assert broken.status_code == 400
    assert "Billedet kunne ikke læses" in broken.json()["errors"]["uploads"][0]["message"]

    # A ticket for one album is no good in another.
    elsewhere = Album.objects.create(folder="2004", name="Andet album")
    upload = _begin_media(client, bucket, album, "IMG_2.JPG", 12)
    bucket.add_response("complete_multipart_upload", {}, None)
    bucket.add_response("head_object", {"ContentLength": 12}, None)
    bucket.add_response("delete_object", {}, None)
    assert _commit(client, elsewhere, upload).status_code == 400

    assert not Media.objects.exists()


@pytest.mark.django_db
def test_a_duplicate_sent_to_the_bucket_is_skipped_and_removed(
    client: Client, bucket: Stubber, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest")
    client.force_login(resident)
    content = jpeg_bytes()
    upload_media(album=album, uploaded_file=image_upload("IMG_1.JPG", content), resident=resident)
    upload = _begin_media(client, bucket, album, "IMG_1.JPG", len(content))
    _complete(bucket, _key(upload), content)
    bucket.add_response("delete_object", {}, {"Bucket": "test-bucket", "Key": _key(upload)})

    response = _commit(client, album, upload)

    assert response.json()["uploaded"] == 0
    assert "findes allerede" in response.json()["skipped"][0]
    assert Media.objects.count() == 1


@pytest.mark.django_db
def test_without_a_bucket_the_browser_sends_with_the_form(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident(roles=(Role.FOTO,))
    album = Album.objects.create(folder="2004", name="Fest")
    client.force_login(resident)

    begin = client.post(reverse("photo_album:upload_begin", args=[album.pk]), {"name": "a.jpg", "size": 10})
    assert begin.json() == {"direct": False}
    zip_begin = client.post(reverse("photo_album:import_zip_upload"), {"name": "Fest.zip", "size": 10})
    assert zip_begin.json() == {"direct": False}


@pytest.mark.django_db
def test_the_import_page_imports_a_zip_sent_to_the_bucket(
    client: Client, bucket: Stubber, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident(roles=(Role.FOTO,))
    client.force_login(resident)
    bucket.add_response("create_multipart_upload", {"UploadId": "upload-3"}, None)
    upload = client.post(
        reverse("photo_album:import_zip_upload"), {"name": "Fest.zip", "size": 70 * MB}
    ).json()
    assert len(upload["urls"]) == 2
    bucket.add_response("complete_multipart_upload", {}, None)
    bucket.add_response("head_object", {"ContentLength": 70 * MB}, None)

    response = client.post(
        reverse("photo_album:import_zip"),
        {"folder": "2026", "ticket": upload["ticket"], "parts": '["a", "b"]'},
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 202
    album_import = AlbumImport.objects.get()
    assert album_import.archive_name == "Fest.zip" and album_import.archive.name.startswith(
        "photo-album-imports/"
    )
    assert not album_import.lock_when_done  # a resident's import, not the Dropbox archive
    # An album upload's ticket cannot start an import.
    forged = client.post(
        reverse("photo_album:import_zip"),
        {
            "folder": "2026",
            "ticket": _begin_media(client, bucket, Album.objects.create(folder="2026", name="X"), "a.jpg", 9)[
                "ticket"
            ],
            "parts": '["a"]',
        },
        HTTP_ACCEPT="application/json",
    )
    assert forged.status_code == 400


@pytest.mark.django_db
def test_only_who_may_upload_or_import_may_begin(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2004", name="Fest", manually_locked_at=timezone.now())
    client.force_login(resident)

    assert (
        client.post(
            reverse("photo_album:upload_begin", args=[album.pk]), {"name": "a.jpg", "size": 1}
        ).status_code
        == 403
    )
    assert (
        client.post(reverse("photo_album:import_zip_upload"), {"name": "a.zip", "size": 1}).status_code == 403
    )
