"""Profile thumbnails (#177): every avatar draws a small square copy, never the original upload.

Resident.save() makes the thumbnail whenever the picture changes, however it was changed (the edit
form, FieldFile.save in seed_demo, the admin), and make_profile_thumbnails backfills the pictures
that predate it. Only the profile page links to the original, through the shared viewer.
"""

from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from PIL import Image

from residents.models import PROFILE_THUMBNAIL_SIZE, Resident

from .media_samples import heic_bytes, jpeg_bytes


@pytest.fixture
def media(settings: object, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
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
    first = resident.profile_thumbnail.name
    assert first

    resident.profile_picture.save("second.jpg", ContentFile(jpeg_bytes()), save=True)

    assert resident.profile_thumbnail.name not in {"", first}
    assert not (media / first).exists(), "the old thumbnail is deleted, not leaked"


@pytest.mark.django_db(transaction=True)
def test_clearing_the_picture_clears_the_thumbnail(make_resident: Callable, media: Path) -> None:
    resident = make_resident()
    resident.profile_picture.save("mig.jpg", ContentFile(jpeg_bytes()), save=True)
    thumbnail = resident.profile_thumbnail.name
    client = Client()
    client.force_login(resident)

    edit(client, **{"profile_picture-clear": "on"})

    resident.refresh_from_db()
    assert not resident.profile_picture
    assert not resident.profile_thumbnail
    assert not (media / thumbnail).exists()
    assert resident.avatar_url == ""


@pytest.mark.django_db
def test_saving_anything_else_leaves_the_thumbnail_alone(make_resident: Callable, media: Path) -> None:
    """The thumbnail costs a read of the original from the bucket; a bio edit must not pay it."""
    resident = make_resident()
    resident.profile_picture.save("mig.jpg", ContentFile(jpeg_bytes()), save=True)
    thumbnail = resident.profile_thumbnail.name

    fetched = Resident.objects.get(pk=resident.pk)
    fetched.bio = "Hej"
    fetched.save()

    fetched.refresh_from_db()
    assert fetched.profile_thumbnail.name == thumbnail


@pytest.mark.django_db
def test_an_unreadable_picture_falls_back_to_the_original(make_resident: Callable, media: Path) -> None:
    """No thumbnail is better than no avatar: avatar_url then draws the original, as before #177."""
    resident = make_resident()
    resident.profile_picture.save("broken.jpg", ContentFile(b"not an image"), save=True)

    assert not resident.profile_thumbnail
    assert resident.avatar_url == resident.profile_picture.url


@pytest.mark.django_db
def test_the_backfill_makes_thumbnails_for_existing_pictures(make_resident: Callable, media: Path) -> None:
    resident = make_resident()
    # As the rows look before this change: a picture, and no thumbnail.
    (media / "profile_pictures").mkdir()
    output = BytesIO()
    Image.new("RGB", (400, 300), "blue").save(output, format="PNG")
    (media / "profile_pictures" / "gammel.png").write_bytes(output.getvalue())
    Resident.objects.filter(pk=resident.pk).update(profile_picture="profile_pictures/gammel.png")

    call_command("make_profile_thumbnails")

    resident.refresh_from_db()
    assert resident.profile_thumbnail.name.startswith("profile_pictures/thumbnails/gammel")


@pytest.mark.django_db
def test_avatars_draw_the_thumbnail_and_only_the_profile_page_links_the_original(
    make_resident: Callable, media: Path
) -> None:
    resident = make_resident()
    resident.profile_picture.save("mig.jpg", ContentFile(jpeg_bytes()), save=True)
    client = Client()
    client.force_login(resident)

    page = client.get(reverse("resident_profile", args=[resident.pk])).content.decode()

    assert f'src="{resident.profile_thumbnail.url}"' in page
    assert f'href="{resident.profile_picture.url}"' in page
    assert f'src="{resident.profile_picture.url}"' not in page
