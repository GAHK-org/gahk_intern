"""The import script's archive straight into the bucket, past the proxy (photo_album.direct_upload)."""

import json
from collections.abc import Iterator
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from botocore.stub import ANY, Stubber
from django.test import Client, override_settings
from django.urls import reverse

from photo_album import direct_upload
from photo_album.models import AlbumImport
from photo_album.storage import PhotoAlbumS3Storage

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
