from collections.abc import Callable
from datetime import datetime, timedelta
from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from photo_album.models import Album, Media, MediaStatus
from photo_album.services import delete, extract_captured_at, purge_expired, upload_media
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
        {"uploads": [SimpleUploadedFile("one.jpg", b"one"), SimpleUploadedFile("two.jpg", b"two")]},
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
    client.force_login(administrator)

    response = client.get(reverse("photo_album:download_original", args=[first_media.pk]))

    assert b"".join(response.streaming_content) == source
    assert first_media.high_definition.read() != source
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert first_media.original.name.startswith(f"photo-album/{first_album.pk}/original/")
    assert second_media.original.name.startswith(f"photo-album/{second_album.pk}/original/")


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
    content = client.get(reverse("photo_album:detail", args=[album.pk])).content.decode()
    assert 'data-uploaded-by="Mette Metadata"' in content
    assert 'data-captured-at="15. september 2026, 20:30"' in content
    assert "Oprindelig dato" in content


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

    assert content.index('data-title="captured"') < content.index('data-title="uploaded"')


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
    assert delete(media, resident) is False
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
    assert "data-album-gallery" in response.content.decode()
    assert 'data-full="/media/high-definition.jpg"' in response.content.decode()
    assert 'src="/media/thumbnail.jpg"' in response.content.decode()
    assert 'data-album="Fest"' in response.content.decode()
    assert "Fra album: Fest" in response.content.decode()


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
