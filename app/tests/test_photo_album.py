from collections.abc import Callable
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from photo_album.models import Album, Media, MediaStatus
from photo_album.services import delete, purge_expired
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
        {"file": SimpleUploadedFile("x.jpg", b"not-an-image", content_type="image/jpeg")},
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
def test_photo_manager_upload_is_approved_immediately(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)
    client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {"file": SimpleUploadedFile("x.jpg", b"not-an-image", content_type="image/jpeg")},
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
            reverse("photo_album:upload", args=[album.pk]), {"file": SimpleUploadedFile("x.jpg", b"x")}
        ).status_code
        == 403
    )
    assert client.post(reverse("photo_album:delete", args=[media.pk])).status_code == 403


@pytest.mark.django_db
def test_delete_uses_bin_then_purges_after_30_days(make_resident: Callable[..., Resident]) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    media = Media.objects.create(
        album=album, title="photo", original="a", high_definition="b", thumbnail="c", requested_by=resident
    )
    Media.objects.filter(pk=media.pk).update(added_at=timezone.now() - timedelta(hours=2))
    media.refresh_from_db()
    assert delete(media, resident) is False
    Media.objects.filter(pk=media.pk).update(deleted_at=timezone.now() - timedelta(days=31))
    assert purge_expired() == 1
    assert not Media.objects.exists()


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
