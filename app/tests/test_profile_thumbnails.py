"""Profile thumbnails (#177): every avatar draws a small square copy, never the original upload.

The worker makes the thumbnail (residents.tasks.make_profile_thumbnail). Resident.save() asks for one
whenever the picture changes, however it was changed (the edit form, FieldFile.save in seed_demo, the
admin), and avatar_url asks for one whenever it draws an avatar without, which is what makes the
thumbnails of pictures that predate it. Only the profile page links to the original, through the
shared viewer.

The suite runs Celery eagerly, so a queued job runs as soon as the transaction commits: in the
transaction=True tests on its own, and in the others under django_capture_on_commit_callbacks.
"""

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from PIL import Image

from residents.models import PROFILE_THUMBNAIL_SIZE, Resident
from residents.tasks import make_profile_thumbnail

from .media_samples import heic_bytes, jpeg_bytes


@pytest.fixture
def media(settings: object, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    # The once-an-hour key names the resident and the picture, and both repeat between tests.
    cache.clear()
    return tmp_path


def edit(client: Client, **files: object) -> None:
    response = client.post(
        reverse("edit_profile"), {"bio": "", "facebook_link": "", "instagram_handle": "", **files}
    )
    assert response.status_code == 302


@pytest.mark.django_db(transaction=True)
def test_an_uploaded_picture_gets_a_small_square_thumbnail(make_resident: Callable, media: Path) -> None:
    resident = make_resident()
    client = Client()
    client.force_login(resident)

    edit(
        client,
        profile_picture=SimpleUploadedFile(
            "mig.jpg", jpeg_bytes(size=(3000, 2000)), content_type="image/jpeg"
        ),
    )

    resident.refresh_from_db()
    assert resident.profile_thumbnail.name.startswith("profile_pictures/thumbnails/")
    with Image.open(media / resident.profile_thumbnail.name) as thumbnail:
        assert thumbnail.size == (PROFILE_THUMBNAIL_SIZE, PROFILE_THUMBNAIL_SIZE)
    assert resident.avatar_url == resident.profile_thumbnail.url


@pytest.mark.django_db(transaction=True)
def test_a_heic_profile_picture_is_stored_as_jpeg(make_resident: Callable, media: Path) -> None:
    resident = make_resident()
    client = Client()
    client.force_login(resident)

    edit(client, profile_picture=SimpleUploadedFile("IMG_0001.HEIC", heic_bytes(), content_type="image/heic"))

    resident.refresh_from_db()
    assert resident.profile_picture.name.endswith(".jpg")
    assert resident.profile_thumbnail


@pytest.mark.django_db(transaction=True)
def test_replacing_the_picture_replaces_the_thumbnail(make_resident: Callable, media: Path) -> None:
    resident = make_resident()
    resident.profile_picture.save("first.jpg", ContentFile(jpeg_bytes()), save=True)
    resident.refresh_from_db()
    first = resident.profile_thumbnail.name
    assert first

    resident.profile_picture.save("second.jpg", ContentFile(jpeg_bytes()), save=True)

    resident.refresh_from_db()
    assert resident.profile_thumbnail.name not in {"", first}
    assert not (media / first).exists(), "the old thumbnail is deleted, not leaked"


@pytest.mark.django_db(transaction=True)
def test_clearing_the_picture_clears_the_thumbnail(make_resident: Callable, media: Path) -> None:
    resident = make_resident()
    resident.profile_picture.save("mig.jpg", ContentFile(jpeg_bytes()), save=True)
    resident.refresh_from_db()
    thumbnail = resident.profile_thumbnail.name
    assert thumbnail
    client = Client()
    client.force_login(resident)

    edit(client, **{"profile_picture-clear": "on"})

    resident.refresh_from_db()
    assert not resident.profile_picture
    assert not resident.profile_thumbnail
    assert not (media / thumbnail).exists()
    assert resident.avatar_url == ""


@pytest.mark.django_db
def test_saving_anything_else_leaves_the_thumbnail_alone(
    make_resident: Callable, media: Path, django_capture_on_commit_callbacks: Callable
) -> None:
    """The thumbnail costs a read of the original from the bucket; a bio edit must not pay it."""
    resident = make_resident()
    with django_capture_on_commit_callbacks(execute=True):
        resident.profile_picture.save("mig.jpg", ContentFile(jpeg_bytes()), save=True)
    thumbnail = Resident.objects.get(pk=resident.pk).profile_thumbnail.name
    assert thumbnail

    fetched = Resident.objects.get(pk=resident.pk)
    fetched.bio = "Hej"
    with django_capture_on_commit_callbacks() as queued:
        fetched.save()

    assert queued == []
    fetched.refresh_from_db()
    assert fetched.profile_thumbnail.name == thumbnail


@pytest.mark.django_db
def test_an_unreadable_picture_falls_back_to_the_original(
    make_resident: Callable, media: Path, django_capture_on_commit_callbacks: Callable
) -> None:
    """No thumbnail is better than no avatar: avatar_url then draws the original, as before #177."""
    resident = make_resident()
    with django_capture_on_commit_callbacks(execute=True):
        resident.profile_picture.save("broken.jpg", ContentFile(b"not an image"), save=True)

    resident.refresh_from_db()
    assert not resident.profile_thumbnail
    assert resident.avatar_url == resident.profile_picture.url


@pytest.mark.django_db(transaction=True)
def test_an_older_picture_gets_its_thumbnail_once_an_avatar_is_drawn(
    make_resident: Callable, media: Path
) -> None:
    """Pictures from before the thumbnail existed need no backfill command: drawing one asks for it."""
    resident = make_resident()
    # As the rows look before this change: a picture, and no thumbnail.
    (media / "profile_pictures").mkdir()
    output = BytesIO()
    Image.new("RGB", (400, 300), "blue").save(output, format="PNG")
    (media / "profile_pictures" / "gammel.png").write_bytes(output.getvalue())
    Resident.objects.filter(pk=resident.pk).update(profile_picture="profile_pictures/gammel.png")
    client = Client()
    client.force_login(resident)

    first = client.get(reverse("resident_profile", args=[resident.pk])).content.decode()

    assert 'src="/media/profile_pictures/gammel.png"' in first, "the original until the worker is done"
    resident.refresh_from_db()
    assert resident.profile_thumbnail.name.startswith("profile_pictures/thumbnails/gammel")


@pytest.mark.django_db
def test_drawing_avatars_queues_the_job_once(
    make_resident: Callable, media: Path, django_capture_on_commit_callbacks: Callable
) -> None:
    """An alumneliste page draws dozens of avatars, and renders again on every visit."""
    resident = make_resident()
    Resident.objects.filter(pk=resident.pk).update(profile_picture="profile_pictures/gammel.png")
    resident.refresh_from_db()

    with (
        mock.patch("residents.tasks.make_profile_thumbnail.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        for _ in range(3):
            assert resident.avatar_url == resident.profile_picture.url

    delay.assert_called_once_with(resident.pk, "profile_pictures/gammel.png")


@pytest.mark.django_db
def test_a_queue_that_is_down_does_not_break_the_page(
    make_resident: Callable, media: Path, django_capture_on_commit_callbacks: Callable
) -> None:
    resident = make_resident()
    Resident.objects.filter(pk=resident.pk).update(profile_picture="profile_pictures/gammel.png")
    resident.refresh_from_db()

    with (
        mock.patch("residents.tasks.make_profile_thumbnail.delay", side_effect=OSError("broker down")),
        django_capture_on_commit_callbacks(execute=True),
    ):
        assert resident.avatar_url == resident.profile_picture.url


@pytest.mark.django_db
def test_a_job_for_a_picture_since_replaced_does_nothing(make_resident: Callable, media: Path) -> None:
    """It must not store a thumbnail of the old picture against the new one."""
    resident = make_resident()
    resident.profile_picture.save("first.jpg", ContentFile(jpeg_bytes()), save=False)
    first = resident.profile_picture.name
    Resident.objects.filter(pk=resident.pk).update(profile_picture="profile_pictures/second.jpg")

    assert make_profile_thumbnail(resident.pk, first) is False
    resident.refresh_from_db()
    assert not resident.profile_thumbnail
    assert not (media / "profile_pictures" / "thumbnails").exists()


@pytest.mark.django_db
def test_avatars_draw_the_thumbnail_and_only_the_profile_page_links_the_original(
    make_resident: Callable, media: Path, django_capture_on_commit_callbacks: Callable
) -> None:
    resident = make_resident()
    with django_capture_on_commit_callbacks(execute=True):
        resident.profile_picture.save("mig.jpg", ContentFile(jpeg_bytes()), save=True)
    resident.refresh_from_db()
    client = Client()
    client.force_login(resident)

    page = client.get(reverse("resident_profile", args=[resident.pk])).content.decode()

    assert f'src="{resident.profile_thumbnail.url}"' in page
    assert f'href="{resident.profile_picture.url}"' in page
    assert f'src="{resident.profile_picture.url}"' not in page
