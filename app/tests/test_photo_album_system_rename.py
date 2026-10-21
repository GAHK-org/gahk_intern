"""The import script renaming an album it imported (photo_album.views.system_rename_album)."""

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from photo_album.models import Album

TOKEN = {"HTTP_AUTHORIZATION": "Bearer migration-token"}
URL = reverse("photo_album:system_rename_album")


def _rename(client: Client, album: str, name: str, folder: str = "2014", **headers: str) -> object:
    return client.post(URL, {"folder": folder, "album": album, "name": name}, **(headers or TOKEN))


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_an_imported_album_named_after_an_inner_folder_is_renamed_though_locked(client: Client) -> None:
    """Dropbox's "2014/GAHK Color Run" held only "101GOPRO/", so the import named the album after
    that. The script locks what it imports; it may still correct a name it gave itself."""
    album = Album.objects.create(folder="2014", name="101GOPRO", manually_locked_at=timezone.now())
    sub = Album.objects.create(folder="2014", name="Løbet", parent=album)

    response = _rename(client, "101gopro", "GAHK Color Run")  # found the way the server compares names

    assert response.status_code == 200
    assert response.json() == {"id": album.pk, "folder": "2014", "name": "GAHK Color Run"}
    album.refresh_from_db()
    assert album.name == "GAHK Color Run"
    assert album.is_locked()  # renamed, not unlocked
    assert Album.objects.get(pk=sub.pk).name == "Løbet"


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_a_rename_says_why_it_cannot_be_done(client: Client) -> None:
    Album.objects.create(folder="2014", name="101GOPRO")
    Album.objects.create(folder="2014", name="GAHK Color Run")
    Album.objects.create(folder="2013", name="Elsewhere")

    missing = _rename(client, "Elsewhere", "Anything")
    taken = _rename(client, "101GOPRO", "gahk color run")
    invalid = _rename(client, "101GOPRO", "GAHK/Color Run")

    assert missing.status_code == 404
    assert "intet album" in str(missing.json()["errors"]["album"])
    assert taken.status_code == 400
    assert "findes allerede" in str(taken.json()["errors"])
    assert invalid.status_code == 400
    assert Album.objects.filter(name="101GOPRO").exists()


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_renaming_needs_the_import_token(client: Client) -> None:
    Album.objects.create(folder="2014", name="101GOPRO")

    assert _rename(client, "101GOPRO", "GAHK Color Run", HTTP_AUTHORIZATION="Bearer wrong").status_code == 403
    assert Album.objects.filter(name="101GOPRO").exists()
