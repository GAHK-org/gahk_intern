import subprocess
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import imageio_ffmpeg
import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile, File
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from photo_album import access
from photo_album.models import Album, DerivativeState, Media, MediaStatus
from photo_album.services import (
    MAX_DERIVATIVE_ATTEMPTS,
    build_derivatives,
    delete,
    extract_captured_at,
    pending_derivatives,
    purge_expired,
    reject,
    upload_media,
)
from residents.models import Resident, Role


@pytest.mark.django_db
def test_only_photo_group_or_administrator_can_create_album(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    client.force_login(resident)
    assert (
        client.post(reverse("photo_album:create_album"), {"folder": "2026", "name": "Fest"}).status_code
        == 403
    )

    administrator = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    client.force_login(administrator)
    response = client.post(reverse("photo_album:create_album"), {"folder": "2026", "name": "Fest"})
    assert response.status_code == 302
    assert Album.objects.get().name == "Fest"


@pytest.mark.django_db
def test_normal_upload_is_pending_and_only_visible_to_requester(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    requester = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(requester)
    response = client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {"uploads": SimpleUploadedFile("x.jpg", b"not-an-image", content_type="image/jpeg")},
    )
    assert response.status_code == 302
    media = Media.objects.get()
    assert media.status == MediaStatus.PENDING
    assert (
        "Afventer godkendelse" in client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()
    )

    other = make_resident(email="other@gahk.dk")
    client.force_login(other)
    assert "x.jpg" not in client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()


@pytest.mark.django_db
def test_requester_can_immediately_delete_their_own_pending_media(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    requester = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="pending",
        original="original.jpg",
        high_definition="high-definition.jpg",
        thumbnail="thumbnail.jpg",
        requested_by=requester,
        status=MediaStatus.PENDING,
    )
    client.force_login(requester)

    response = client.post(reverse("photo_album:delete", args=[media.pk]))

    assert response.status_code == 302
    assert not Media.objects.filter(pk=media.pk).exists()


@pytest.mark.django_db
def test_other_resident_cannot_delete_pending_media(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    requester = make_resident()
    other = make_resident(email="other@gahk.dk")
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="pending",
        original="original.jpg",
        high_definition="high-definition.jpg",
        thumbnail="thumbnail.jpg",
        requested_by=requester,
        status=MediaStatus.PENDING,
    )
    client.force_login(other)

    response = client.post(reverse("photo_album:delete", args=[media.pk]))

    assert response.status_code == 403
    assert Media.objects.filter(pk=media.pk).exists()


@pytest.mark.django_db
def test_upload_accepts_multiple_media_files(client: Client, make_resident: Callable[..., Resident]) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)
    response = client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {
            "uploads": [
                SimpleUploadedFile("one.jpg", b"one", content_type="image/jpeg"),
                SimpleUploadedFile("two.jpg", b"two", content_type="image/jpeg"),
            ]
        },
    )
    assert response.status_code == 302
    assert Media.objects.filter(album=album).count() == 2


@pytest.mark.django_db
def test_album_upload_page_has_a_selected_files_summary(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)

    content = client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()

    assert "data-album-upload-input" in content
    assert "data-album-upload-selection" in content
    assert "data-album-upload-count" in content
    assert "data-album-upload-files" in content


@pytest.mark.django_db
def test_folder_index_filters_albums_and_sorts_andet_first(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    andet = Album.objects.create(folder="Andet", name="Andet album")
    year = Album.objects.create(folder="2026", name="Årsalbum")
    client.force_login(resident)

    index = client.get(reverse("photo_album:index"))
    folder = client.get(reverse("photo_album:index"), {"folder": "Andet"})

    assert index.content.decode().index(andet.name) < index.content.decode().index(year.name)
    assert andet.name in folder.content.decode()
    assert year.name not in folder.content.decode()


@pytest.mark.django_db
def test_image_upload_generates_compressed_derivatives(make_resident: Callable[..., Resident]) -> None:
    from PIL import Image

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    source = BytesIO()
    Image.new("RGB", (2400, 1200), "red").save(source, format="JPEG")
    media = upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("wide.jpg", source.getvalue(), content_type="image/jpeg"),
        resident=resident,
        approved=True,
    )
    assert build_derivatives(media)
    with Image.open(media.high_definition) as high_definition:
        assert max(high_definition.size) == 1600
    with Image.open(media.thumbnail) as thumbnail:
        assert max(thumbnail.size) == 320


@pytest.mark.django_db
def test_heic_upload_generates_jpeg_derivatives(make_resident: Callable[..., Resident]) -> None:
    from PIL import Image
    from pillow_heif import from_pillow

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    source = BytesIO()
    from_pillow(Image.new("RGB", (2400, 1200), "red")).save(source)
    media = upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("wide.heic", source.getvalue(), content_type="image/heic"),
        resident=resident,
        approved=True,
    )
    assert build_derivatives(media)

    assert media.original.name.endswith(".heic")
    assert media.high_definition.name.endswith(".jpg")
    assert media.thumbnail.name.endswith(".jpg")
    with Image.open(media.high_definition) as high_definition:
        assert max(high_definition.size) == 1600


@pytest.mark.django_db
def test_original_download_returns_original_bytes_and_album_specific_keys(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    from PIL import Image

    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    first_album = Album.objects.create(folder="2026", name="Første")
    second_album = Album.objects.create(folder="2026", name="Andet")
    source_file = BytesIO()
    Image.new("RGB", (2400, 1200), "blue").save(source_file, format="JPEG", quality=100)
    source = source_file.getvalue()
    first_media = upload_media(
        album=first_album,
        uploaded_file=SimpleUploadedFile("same-name.jpg", source, content_type="image/jpeg"),
        resident=administrator,
        approved=True,
    )
    second_media = upload_media(
        album=second_album,
        uploaded_file=SimpleUploadedFile("same-name.jpg", source, content_type="image/jpeg"),
        resident=administrator,
        approved=True,
    )
    assert build_derivatives(first_media)
    assert build_derivatives(second_media)
    client.force_login(administrator)

    response = client.get(reverse("photo_album:download_original", args=[first_media.pk]))

    assert b"".join(response.streaming_content) == source
    assert first_media.high_definition.read() != source
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert first_media.original.name.startswith(f"photo-album/{first_album.pk}/original/")
    assert second_media.original.name.startswith(f"photo-album/{second_album.pk}/original/")


@pytest.mark.django_db
def test_album_download_is_built_by_the_worker_and_then_downloaded(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    from photo_album.models import AlbumDownload, AlbumDownloadState
    from photo_album.tasks import build_album_download

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Sommerfest")
    upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("first.jpg", b"first", content_type="image/jpeg"),
        resident=resident,
        approved=True,
    )
    upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("second.mp4", b"second", content_type="video/mp4"),
        resident=resident,
        approved=True,
    )
    client.force_login(resident)

    response = client.post(reverse("photo_album:download_album", args=[album.pk]))

    assert response.status_code == 302
    download = AlbumDownload.objects.get(album=album, requested_by=resident)
    assert download.state == AlbumDownloadState.QUEUED
    assert build_album_download.run(download.pk)
    download.refresh_from_db()
    assert download.state == AlbumDownloadState.READY

    response = client.get(reverse("photo_album:download_album_archive", args=[download.token]))

    assert response.headers["Content-Type"] == "application/zip"
    assert 'filename="Sommerfest.zip"' in response.headers["Content-Disposition"]
    with zipfile.ZipFile(BytesIO(b"".join(response.streaming_content))) as archive:
        assert archive.namelist() == ["first.jpg", "second.mp4"]
        assert archive.read("first.jpg") == b"first"
        assert archive.read("second.mp4") == b"second"


@pytest.mark.django_db
def test_album_download_excludes_another_residents_pending_upload(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    from photo_album.models import AlbumDownload
    from photo_album.tasks import build_album_download

    resident = make_resident()
    other_resident = make_resident(email="other@example.com")
    album = Album.objects.create(folder="2026", name="Sommerfest")
    upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("visible.jpg", b"visible", content_type="image/jpeg"),
        resident=resident,
        approved=True,
    )
    upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("pending.jpg", b"pending", content_type="image/jpeg"),
        resident=other_resident,
        approved=False,
    )
    client.force_login(resident)

    response = client.post(reverse("photo_album:download_album", args=[album.pk]))
    download = AlbumDownload.objects.get(album=album, requested_by=resident)
    build_album_download.run(download.pk)
    response = client.get(reverse("photo_album:download_album_archive", args=[download.token]))

    with zipfile.ZipFile(BytesIO(b"".join(response.streaming_content))) as archive:
        assert archive.namelist() == ["visible.jpg"]


@pytest.mark.django_db
def test_album_download_archive_is_private_to_its_requester(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    from photo_album.models import AlbumDownload

    requester = make_resident()
    other_resident = make_resident(email="other@example.com")
    album = Album.objects.create(folder="2026", name="Sommerfest")
    download = AlbumDownload.objects.create(
        album=album,
        requested_by=requester,
        state="ready",
        archive_key="photo-album-zips/secret.zip",
    )
    client.force_login(other_resident)

    assert client.get(reverse("photo_album:download_album_archive", args=[download.token])).status_code == 404


@pytest.mark.django_db
def test_expired_album_downloads_remove_their_archives(make_resident: Callable[..., Resident]) -> None:
    from photo_album.models import AlbumDownload
    from photo_album.storage import get_photo_album_storage
    from photo_album.tasks import purge_expired_downloads

    album = Album.objects.create(folder="2026", name="Sommerfest")
    key = "photo-album-zips/expired.zip"
    get_photo_album_storage().save(key, ContentFile(b"zip bytes"))
    download = AlbumDownload.objects.create(
        album=album,
        requested_by=make_resident(),
        state="ready",
        archive_key=key,
        completed_at=timezone.now() - timedelta(days=8),
    )

    assert purge_expired_downloads.run() == 1
    assert not AlbumDownload.objects.filter(pk=download.pk).exists()
    assert not get_photo_album_storage().exists(key)


@pytest.mark.django_db
def test_ready_album_download_shows_its_expiry(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    from photo_album.models import ALBUM_DOWNLOAD_RETENTION, AlbumDownload

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Sommerfest")
    completed_at = timezone.now()
    AlbumDownload.objects.create(
        album=album,
        requested_by=resident,
        state="ready",
        archive_key="photo-album-zips/ready.zip",
        completed_at=completed_at,
    )
    client.force_login(resident)

    response = client.get(reverse("photo_album:detail", args=[album.pk]))

    assert "Downloadet udløber" in response.content.decode()
    assert (
        timezone.localtime(completed_at + ALBUM_DOWNLOAD_RETENTION).strftime("%H:%M")
        in response.content.decode()
    )


@pytest.mark.django_db
def test_serve_media_redirects_anonymous_requests_to_login(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    album = Album.objects.create(folder="2026", name="Fest")
    media = upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("x.jpg", b"bytes", content_type="image/jpeg"),
        resident=make_resident(),
        approved=True,
    )
    assert build_derivatives(media)

    response = client.get(media.thumbnail.url)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/intern/admin/login")


@pytest.mark.django_db
def test_serve_media_streams_a_visible_item(client: Client, make_resident: Callable[..., Resident]) -> None:
    requester = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    media = upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("x.jpg", b"bytes", content_type="image/jpeg"),
        resident=requester,
        approved=True,
    )
    assert build_derivatives(media)
    client.force_login(requester)

    response = client.get(media.thumbnail.url)

    assert response.status_code == 200
    assert b"".join(response.streaming_content)


@pytest.mark.django_db
def test_serve_media_refuses_a_pending_upload_to_a_stranger(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The rule a bare presigned URL can't enforce: only the uploader (or Fotogruppen) may see a
    PENDING item — see photo_album.access.visible_media. serve_media re-checks it per request."""
    requester = make_resident()
    stranger = make_resident(email="stranger@gahk.dk")
    album = Album.objects.create(folder="2026", name="Fest")
    media = upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("x.jpg", b"bytes", content_type="image/jpeg"),
        resident=requester,
        approved=False,
    )
    assert build_derivatives(media)
    client.force_login(stranger)

    assert client.get(media.thumbnail.url).status_code == 403


@pytest.mark.django_db
def test_serve_media_404s_a_key_with_no_matching_row(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    client.force_login(make_resident())

    response = client.get("/fotoalbum-media/photo-album/999999/thumbnail/nope.jpg")

    assert response.status_code == 404


@pytest.mark.django_db
def test_image_metadata_and_upload_attribution_are_available_to_the_viewer(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    from PIL import Image

    resident = make_resident(first_name="Mette", last_name="Metadata")
    album = Album.objects.create(folder="2026", name="Fest")
    source = BytesIO()
    exif = Image.Exif()
    exif[36867] = "2026:09:15 20:30:00"
    exif[271] = "Demo Camera"
    exif[272] = "Model 1"
    Image.new("RGB", (80, 80), "blue").save(source, format="JPEG", exif=exif)
    media = upload_media(
        album=album,
        uploaded_file=SimpleUploadedFile("metadata.jpg", source.getvalue(), content_type="image/jpeg"),
        resident=resident,
        approved=True,
    )

    assert media.metadata == {
        "Oprindelig dato": "2026:09:15 20:30:00",
        "Kamera": "Demo Camera Model 1",
    }
    assert media.captured_at == timezone.make_aware(datetime(2026, 9, 15, 20, 30))
    client.force_login(resident)
    detail = client.get(reverse("photo_album:media_detail", args=[media.pk])).json()
    assert detail["uploadedBy"] == "Mette Metadata"
    assert detail["capturedAt"] == "15. september 2026, 20:30"
    assert detail["metadata"]["Oprindelig dato"] == "2026:09:15 20:30:00"
    # And none of it is in the grid, which is the point of fetching it.
    grid = client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()
    assert "Mette Metadata" not in grid
    assert "Oprindelig dato" not in grid


def test_capture_time_reads_nested_exif_ifd(monkeypatch: pytest.MonkeyPatch) -> None:
    class Exif:
        def get(self, key: int) -> None:
            return None

        def get_ifd(self, key: int) -> dict[int, str]:
            assert key == 34665
            return {36867: "2026:08:06 23:43:39"}

    class ImageFile:
        def getexif(self) -> Exif:
            return Exif()

    monkeypatch.setattr("photo_album.services.Image.open", lambda _: ImageFile())
    uploaded_file = SimpleUploadedFile("nested.jpg", b"image", content_type="image/jpeg")

    assert extract_captured_at(uploaded_file) == timezone.make_aware(datetime(2026, 8, 6, 23, 43, 39))


@pytest.mark.django_db
def test_album_media_orders_capture_time_then_upload_time(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    captured = Media.objects.create(
        album=album,
        title="captured",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=resident,
        captured_at=timezone.make_aware(datetime(2026, 9, 15, 12, 0)),
        status=MediaStatus.APPROVED,
    )
    uploaded = Media.objects.create(
        album=album,
        title="uploaded",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=resident,
        status=MediaStatus.APPROVED,
    )
    Media.objects.filter(pk=captured.pk).update(added_at=timezone.make_aware(datetime(2026, 9, 1)))
    Media.objects.filter(pk=uploaded.pk).update(added_at=timezone.make_aware(datetime(2026, 9, 14)))
    client.force_login(resident)

    content = client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()

    assert content.index('alt="captured"') < content.index('alt="uploaded"')


@pytest.mark.django_db
def test_photo_manager_upload_is_approved_immediately(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)
    client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {"uploads": SimpleUploadedFile("x.jpg", b"not-an-image", content_type="image/jpeg")},
    )
    assert Media.objects.get().status == MediaStatus.APPROVED


@pytest.mark.django_db
def test_old_album_and_old_media_cannot_be_changed(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    admin = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2025", name="Fest")
    media = Media.objects.create(
        album=album,
        title="old",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=admin,
        status=MediaStatus.APPROVED,
    )
    Media.objects.filter(pk=media.pk).update(added_at=timezone.now() - timedelta(days=91))
    client.force_login(admin)
    assert (
        client.post(
            reverse("photo_album:upload", args=[album.pk]),
            {"uploads": SimpleUploadedFile("x.jpg", b"x")},
        ).status_code
        == 403
    )
    assert client.post(reverse("photo_album:delete", args=[media.pk])).status_code == 403


@pytest.mark.django_db
def test_album_lock_can_only_be_removed_within_six_calendar_months() -> None:
    album = Album.objects.create(folder="2026", name="Fest")
    locked_at = timezone.make_aware(datetime(2026, 2, 28, 12, 0))
    album.manually_locked_at = locked_at

    assert album.is_locked()
    assert album.can_be_unlocked(timezone.make_aware(datetime(2026, 8, 27, 12, 0)))
    assert not album.can_be_unlocked(timezone.make_aware(datetime(2026, 8, 28, 12, 0)))


@pytest.mark.django_db
def test_photo_manager_can_manually_lock_and_unlock_album(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)

    lock_response = client.post(reverse("photo_album:lock_album", args=[album.pk]))

    album.refresh_from_db()
    assert lock_response.status_code == 302
    assert album.manually_locked_at is not None
    assert client.post(reverse("photo_album:unlock_album", args=[album.pk])).status_code == 302
    album.refresh_from_db()
    assert album.manually_locked_at is None


@pytest.mark.django_db
def test_expired_album_lock_explains_why_it_cannot_be_unlocked(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(
        folder="2026", name="Fest", manually_locked_at=timezone.now() - timedelta(days=190)
    )
    client.force_login(administrator)

    response = client.post(reverse("photo_album:unlock_album", args=[album.pk]), follow=True)

    album.refresh_from_db()
    assert response.status_code == 200
    assert "Albummet har været låst i mere end 6 måneder og kan ikke låses op." in response.content.decode()
    assert album.manually_locked_at is not None


@pytest.mark.django_db
def test_recent_automatic_album_lock_can_be_unlocked(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="old",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=administrator,
        status=MediaStatus.APPROVED,
    )
    Media.objects.filter(pk=media.pk).update(added_at=timezone.now() - timedelta(days=91))
    client.force_login(administrator)

    response = client.post(reverse("photo_album:unlock_album", args=[album.pk]))

    album.refresh_from_db()
    assert response.status_code == 302
    assert album.unlocked_at is not None
    assert not album.is_locked()
    assert album.is_locked(album.unlocked_at + timedelta(days=90))


@pytest.mark.django_db
def test_delete_uses_bin_then_purges_after_30_days(make_resident: Callable[..., Resident]) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album, title="photo", original="a", high_definition="b", thumbnail="c", requested_by=resident
    )
    delete(media, resident)
    media.refresh_from_db()
    assert media.deleted_at is not None
    Media.objects.filter(pk=media.pk).update(deleted_at=timezone.now() - timedelta(days=31))
    assert purge_expired() == 1
    assert not Media.objects.exists()


@pytest.mark.django_db
def test_recent_media_can_be_permanently_deleted_from_bin(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="photo",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=administrator,
    )
    delete(media, administrator)
    client.force_login(administrator)

    response = client.post(reverse("photo_album:permanently_delete", args=[media.pk]))

    assert response.status_code == 302
    assert not Media.objects.filter(pk=media.pk).exists()


@pytest.mark.django_db
def test_old_media_cannot_be_permanently_deleted_from_bin(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="photo",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=administrator,
    )
    Media.objects.filter(pk=media.pk).update(added_at=timezone.now() - timedelta(hours=1, seconds=1))
    media.refresh_from_db()
    delete(media, administrator)
    client.force_login(administrator)

    response = client.post(reverse("photo_album:permanently_delete", args=[media.pk]))

    assert response.status_code == 403
    assert Media.objects.filter(pk=media.pk).exists()


@pytest.mark.django_db
def test_bin_displays_media_in_the_gallery_viewer(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="photo",
        original="original.jpg",
        high_definition="high-definition.jpg",
        thumbnail="thumbnail.jpg",
        requested_by=administrator,
    )
    delete(media, administrator)
    client.force_login(administrator)

    response = client.get(reverse("photo_album:bin"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "data-album-gallery" in content
    assert 'src="/fotoalbum-media/thumbnail.jpg"' in content
    assert "Fra album: Fest" in content
    # The viewer-sized file is not in the grid any more; it arrives when the item is opened.
    assert "/fotoalbum-media/high-definition.jpg" not in content
    detail = client.get(reverse("photo_album:media_detail", args=[media.pk])).json()
    assert detail["full"] == "/fotoalbum-media/high-definition.jpg"
    assert detail["album"] == "Fest"


@pytest.mark.django_db
def test_restore_returns_media_to_its_original_album(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Original album")
    media = Media.objects.create(
        album=album,
        title="photo",
        original="original.jpg",
        high_definition="high-definition.jpg",
        thumbnail="thumbnail.jpg",
        requested_by=administrator,
    )
    delete(media, administrator)
    client.force_login(administrator)

    response = client.post(reverse("photo_album:restore", args=[media.pk]))

    media.refresh_from_db()
    assert response.url == reverse("photo_album:detail", args=[album.pk])
    assert media.album_id == album.pk
    assert media.deleted_at is None


@pytest.mark.django_db
def test_album_cannot_be_renamed_or_deleted_while_it_has_media(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    Media.objects.create(
        album=album, title="photo", original="a", high_definition="b", thumbnail="c", requested_by=resident
    )
    album.name = "Nyt navn"
    with pytest.raises(ValidationError, match="omdøbes"):
        album.save()
    with pytest.raises(ValidationError, match="tomt"):
        album.delete()


# --- Regression tests for the PR review findings -------------------------------------------------
#
# Each of these fails against the code as it was. Several of the bugs below survived the original
# suite precisely because the existing tests asserted the buggy behaviour, so they are spelled out
# here in terms of what a resident or a manager actually sees.


def _image(name: str = "photo.jpg") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"bytes", content_type="image/jpeg")


def _video(name: str = "clip.mp4") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"bytes", content_type="video/mp4")


@pytest.mark.django_db
def test_album_upload_form_opts_out_of_client_side_downscaling(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The album keeps originals at full resolution, so it opts OUT of the shared downscaler.

    The marker has to be the opt-out one: making the downscaler opt-in silently disabled it for
    every other upload form in the building.
    """
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)

    content = client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()

    assert "data-no-downscale-images" in content


@pytest.mark.django_db
def test_upload_refuses_an_svg_even_with_an_image_content_type(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """An SVG stored unchanged becomes all three variants, served straight off our own storage."""
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)

    response = client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {
            "uploads": [
                SimpleUploadedFile("payload.svg", b"<svg onload='alert(1)'/>", content_type="image/png")
            ]
        },
        follow=True,
    )

    assert not Media.objects.exists()
    assert "Filtypen er ikke understøttet" in response.content.decode()


@pytest.mark.django_db
def test_upload_refuses_a_file_over_the_size_cap(
    client: Client, make_resident: Callable[..., Resident], settings: pytest.FixtureRequest
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    settings.PHOTO_ALBUM_IMAGE_MAX_MB = 1  # type: ignore[attr-defined]
    client.force_login(administrator)

    response = client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {"uploads": [SimpleUploadedFile("big.jpg", b"x" * (2 * 1024 * 1024), content_type="image/jpeg")]},
        follow=True,
    )

    assert not Media.objects.exists()
    assert "for stort" in response.content.decode()


@pytest.mark.django_db
def test_upload_with_no_files_reports_the_problem_instead_of_looking_successful(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)

    response = client.post(reverse("photo_album:upload", args=[album.pk]), {"title": "Fest"}, follow=True)

    assert not Media.objects.exists()
    assert "Dette felt er påkrævet" in response.content.decode()


@pytest.mark.django_db
def test_video_upload_stores_the_original_and_defers_its_derivatives(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Transcoding in the request outran gunicorn's 60 s timeout and lost the upload."""
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)

    response = client.post(
        reverse("photo_album:upload", args=[album.pk]), {"uploads": [_video()]}, follow=True
    )

    media = Media.objects.get()
    assert media.derivative_state == DerivativeState.PENDING
    assert media.original
    assert not media.thumbnail
    assert not media.has_derivatives
    # The grid must not try to render a thumbnail that does not exist yet.
    assert "behandles" in response.content.decode().lower()


@pytest.mark.django_db
def test_an_image_upload_defers_its_derivatives(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")

    media = upload_media(album=album, uploaded_file=_image(), resident=resident)

    assert media.derivative_state == DerivativeState.PENDING
    assert not media.thumbnail


@pytest.mark.django_db
def test_a_video_whose_transcode_keeps_failing_is_given_up_on(
    make_resident: Callable[..., Resident],
) -> None:
    """Otherwise one corrupt upload burns every scheduled run forever."""
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    media = upload_media(album=album, uploaded_file=_video(), resident=resident)

    for _ in range(MAX_DERIVATIVE_ATTEMPTS):
        assert build_derivatives(media) is False  # the bytes are not a real video

    assert media.derivative_state == DerivativeState.FAILED
    assert media not in list(pending_derivatives())


@pytest.mark.django_db
def test_deleting_an_album_whose_media_are_all_binned_explains_itself(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The Slet album button reappears once the last item is binned; it used to 500."""
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="photo",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=administrator,
    )
    delete(media, administrator)
    client.force_login(administrator)

    response = client.post(reverse("photo_album:delete_album", args=[album.pk]), follow=True)

    assert response.status_code == 200
    assert Album.objects.filter(pk=album.pk).exists()
    assert "papirkurven" in response.content.decode()


@pytest.mark.django_db
def test_restoring_a_rejected_item_makes_it_reviewable_again(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Restore used to clear deleted_at but leave status=rejected.

    That combination is reachable by nothing in the app: invisible to residents and to the uploader,
    no approve/reject controls, gone from the bin, and matched by neither arm of purge_expired — so
    the row and all three files were stranded permanently.
    """
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    uploader = make_resident(email="beboer@gahk.dk")
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album, title="photo", original="a", high_definition="b", thumbnail="c", requested_by=uploader
    )
    reject(media, administrator)
    client.force_login(administrator)

    client.post(reverse("photo_album:restore", args=[media.pk]))

    media.refresh_from_db()
    assert media.deleted_at is None
    assert media.status == MediaStatus.PENDING
    # Reachable again: its uploader can see it, and the 30-day sweep can still reach it.
    client.force_login(uploader)
    assert media.title in client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()


@pytest.mark.django_db
def test_old_media_stays_in_the_bin_even_when_it_was_only_just_binned(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The hour runs from the UPLOAD, so binning something old does not reopen the purge window.

    Deliberate, and the case most likely to be mistaken for a bug: manual purge is for undoing a
    mistake just made, not a way to erase the album's history on demand. Anything older leaves only
    via the 30-day sweep, which is the bin's recovery window. See access.can_permanently_delete.
    """
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="photo",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=administrator,
    )
    Media.objects.filter(pk=media.pk).update(added_at=timezone.now() - timedelta(days=20))
    media.refresh_from_db()
    delete(media, administrator)  # binned just now
    client.force_login(administrator)

    response = client.post(reverse("photo_album:permanently_delete", args=[media.pk]))

    assert response.status_code == 403
    assert Media.objects.filter(pk=media.pk).exists()


@pytest.mark.django_db
def test_the_bin_renders_metadata_the_viewer_can_parse(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """It used to emit the Python dict repr, so JSON.parse threw and metadata never appeared."""
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="photo",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=administrator,
        metadata={"Kamera": "Apple iPhone 15"},
    )
    delete(media, administrator)
    client.force_login(administrator)

    detail = client.get(reverse("photo_album:media_detail", args=[media.pk])).json()

    # Real JSON, not the Python dict repr the bin template used to emit, which JSON.parse could
    # never read. Serialised by JsonResponse now, so it cannot drift back.
    assert detail["metadata"] == {"Kamera": "Apple iPhone 15"}


@pytest.mark.django_db
def test_album_detail_query_count_does_not_grow_with_the_number_of_photos(
    client: Client, make_resident: Callable[..., Resident], django_assert_num_queries: Callable[..., object]
) -> None:
    """can_delete asked is_locked() and Fotogruppen-membership once PER ITEM: ~1000 queries at 300."""
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    for index in range(30):
        Media.objects.create(
            album=album,
            title=f"photo-{index}",
            original="a",
            high_definition="b",
            thumbnail="c",
            requested_by=administrator,
            status=MediaStatus.APPROVED,
        )
    client.force_login(administrator)
    url = reverse("photo_album:detail", args=[album.pk])
    client.get(url)  # warm any per-process caches so the count below is the steady state

    with django_assert_num_queries(10):  # type: ignore[operator]
        client.get(url)


@pytest.mark.django_db
def test_captured_at_is_null_when_only_the_file_modification_time_is_present() -> None:
    """EXIF 306 is DateTime — last modified. A re-saved photo was given a confident, wrong date."""
    image = Image.new("RGB", (8, 8))
    exif = image.getexif()
    exif[306] = "2020:01:02 03:04:05"
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    buffer.seek(0)

    assert extract_captured_at(File(buffer, name="photo.jpg")) is None


@pytest.mark.django_db
def test_a_real_video_gets_its_derivatives_from_the_scheduled_command(
    make_resident: Callable[..., Resident], tmp_path: Path
) -> None:
    """The whole deferred path, on real bytes: upload stores the original, the command transcodes.

    Runs actual FFmpeg — the binary ships inside the imageio-ffmpeg wheel, which is a production
    dependency, so it is present anywhere the app is installed. Worth the ~2 s: the failure this
    covers (a worker killed mid-encode, upload lost) is invisible to every test that stubs it out.
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=320x240:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    upload = SimpleUploadedFile("clip.mp4", source.read_bytes(), content_type="video/mp4")

    media = upload_media(album=album, uploaded_file=upload, resident=resident)
    assert media.derivative_state == DerivativeState.PENDING
    assert list(pending_derivatives()) == [media]

    call_command("process_photo_album_media")

    media.refresh_from_db()
    assert media.derivative_state == DerivativeState.READY
    assert media.has_derivatives
    assert media.high_definition.name.endswith(".mp4")
    assert media.thumbnail.name.endswith(".jpg")
    assert media.high_definition.size > 0
    assert media.thumbnail.size > 0
    assert not list(pending_derivatives())


@pytest.mark.django_db
def test_fotogruppen_role_grants_management_without_being_an_administrator(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Role.FOTO is an ordinary embedsgruppe role, granted like repper or vicevaert.

    Before this, access asked for a Residency in a workgroup named "Fotogruppen" — a row no
    database had — so every test here used an administrator and the Fotogruppen path was never
    actually exercised.
    """
    photographer = make_resident(email="foto@gahk.dk", roles=(Role.FOTO,))
    uploader = make_resident(email="beboer@gahk.dk")
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="photo",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=uploader,
    )
    client.force_login(photographer)

    # Can create albums, approve submissions, and reach the bin — none of which a plain resident can.
    assert client.post(reverse("photo_album:approve", args=[media.pk])).status_code == 302
    assert client.get(reverse("photo_album:bin")).status_code == 200
    assert (
        client.post(
            reverse("photo_album:create_album"), {"folder": "2026", "name": "Julefrokost"}
        ).status_code
        == 302
    )

    media.refresh_from_db()
    assert media.status == MediaStatus.APPROVED

    client.force_login(uploader)
    assert client.get(reverse("photo_album:bin")).status_code == 403


@pytest.mark.django_db
def test_fotogruppen_workgroup_exists_so_indstilling_can_assign_it(db: None) -> None:
    """The role is only reachable if the embedsgruppe it is derived from is in the database."""
    from core.models import Workgroup
    from residents.models import WORKGROUP_ROLE

    assert Workgroup.objects.filter(name="Fotogruppen").exists()
    assert WORKGROUP_ROLE["Fotogruppen"] == Role.FOTO


@pytest.mark.django_db
def test_media_detail_refuses_pending_media_belonging_to_someone_else(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The lazy endpoint is a new surface, and it has to keep the rule the grid already kept.

    Pending media is visible to Fotogruppen, administrators and its own uploader. Fetching details
    by id must not become the way around that.
    """
    uploader = make_resident(email="uploader@gahk.dk")
    nosy = make_resident(email="nysgerrig@gahk.dk")
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="pending",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=uploader,
    )
    url = reverse("photo_album:media_detail", args=[media.pk])

    client.force_login(nosy)
    assert client.get(url).status_code == 403

    client.force_login(uploader)
    assert client.get(url).status_code == 200

    client.force_login(make_resident(email="foto@gahk.dk", roles=(Role.FOTO,)))
    assert client.get(url).status_code == 200


@pytest.mark.django_db
def test_media_detail_offers_a_delete_url_only_to_someone_who_may_delete(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    photographer = make_resident(email="foto@gahk.dk", roles=(Role.FOTO,))
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album,
        title="photo",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=photographer,
        status=MediaStatus.APPROVED,
    )
    url = reverse("photo_album:media_detail", args=[media.pk])

    client.force_login(photographer)
    assert client.get(url).json()["deleteUrl"] == reverse("photo_album:delete", args=[media.pk])

    # An ordinary resident sees the approved photo but is offered no way to remove it.
    client.force_login(make_resident(email="beboer@gahk.dk"))
    assert client.get(url).json()["deleteUrl"] == ""


@pytest.mark.django_db
def test_withdrawing_your_own_pending_upload_is_a_hard_delete_not_a_trip_to_the_bin(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The two routes out of an album are different actions, and views.delete picks by can_withdraw."""
    uploader = make_resident(email="beboer@gahk.dk")
    album = Album.objects.create(folder="2026", name="Fest")
    mine = Media.objects.create(
        album=album, title="mine", original="a", high_definition="b", thumbnail="c", requested_by=uploader
    )
    client.force_login(uploader)

    client.post(reverse("photo_album:delete", args=[mine.pk]))

    # Nothing was ever accepted into the album, so there is nothing to recover.
    assert not Media.objects.filter(pk=mine.pk).exists()


@pytest.mark.django_db
def test_a_curator_binning_someone_elses_media_keeps_it_recoverable(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    photographer = make_resident(email="foto@gahk.dk", roles=(Role.FOTO,))
    uploader = make_resident(email="beboer@gahk.dk")
    album = Album.objects.create(folder="2026", name="Fest")
    theirs = Media.objects.create(
        album=album,
        title="theirs",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=uploader,
        status=MediaStatus.APPROVED,
    )
    client.force_login(photographer)

    client.post(reverse("photo_album:delete", args=[theirs.pk]))

    theirs.refresh_from_db()
    assert theirs.deleted_at is not None  # in the bin, restorable for 30 days


@pytest.mark.django_db
def test_a_locked_album_stops_deletion_for_everyone_including_administrators(
    make_resident: Callable[..., Resident], rf: object
) -> None:
    """The lock is a property of the album, not of the reader — no role gets past it."""
    from django.test import RequestFactory

    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest", manually_locked_at=timezone.now())
    media = Media.objects.create(
        album=album,
        title="photo",
        original="a",
        high_definition="b",
        thumbnail="c",
        requested_by=administrator,
        status=MediaStatus.APPROVED,
    )
    request = RequestFactory().get("/")
    request.user = administrator

    assert access.can_delete(media, request) is False
    assert access.can_curate_delete(media, request) is False
    assert access.can_withdraw(media, request) is False
