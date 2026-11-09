"""The folders albums sit in: a year each, or "Uden årstal" for albums without one."""

import unicodedata
from collections.abc import Callable

import pytest
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from photo_album.forms import ZipAlbumImportForm
from photo_album.models import NO_YEAR_FOLDER, Album, validate_folder
from photo_album.views import album_folder_options
from residents.models import Resident, Role

# "Uden årstal" as a Mac or Dropbox may send it: the "å" as an "a" and a combining ring.
DECOMPOSED = unicodedata.normalize("NFD", NO_YEAR_FOLDER)
THIS_YEAR = str(timezone.localdate().year)


def test_a_folder_is_a_year_or_uden_aarstal() -> None:
    assert DECOMPOSED != NO_YEAR_FOLDER
    for folder in ("2004", THIS_YEAR, NO_YEAR_FOLDER, DECOMPOSED):
        validate_folder(folder)
    for folder in ("Andet", "04", "20045", "Diverse", "uden årstal", ""):
        with pytest.raises(ValidationError, match="Mappen skal være et årstal eller Uden årstal"):
            validate_folder(folder)


@pytest.mark.django_db
def test_an_album_without_a_year_is_created_in_uden_aarstal_however_its_a_is_spelled(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))
    url = reverse("photo_album:create_album")

    refused = client.post(url, {"folder": "Andet", "name": "Diverse"})
    created = client.post(url, {"folder": DECOMPOSED, "name": "Portrætter"})

    assert (
        refused.status_code == 200
        and "Mappen skal være et årstal eller Uden årstal" in refused.content.decode()
    )
    assert created.status_code == 302
    assert Album.objects.get().folder == NO_YEAR_FOLDER


@pytest.mark.django_db
def test_the_folder_choice_lists_the_years_newest_first_then_uden_aarstal_and_starts_at_this_year(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    Album.objects.create(folder="2004", name="Fest")
    Album.objects.create(folder="2019", name="Fest")
    Album.objects.create(folder=NO_YEAR_FOLDER, name="Portrætter")

    assert album_folder_options() == [THIS_YEAR, "2019", "2004", NO_YEAR_FOLDER]

    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))
    for url in (reverse("photo_album:create_album"), reverse("photo_album:import_zip")):
        page = client.get(url).content.decode()
        assert f'data-current-value="{THIS_YEAR}"' in page
        assert page.index(f'<option value="{THIS_YEAR}">') < page.index(f'<option value="{NO_YEAR_FOLDER}">')
        assert page.index(f'<option value="{NO_YEAR_FOLDER}">') < page.index('<option value="__new__">')


def test_a_zip_is_imported_into_a_year_or_uden_aarstal() -> None:
    def folder_error(folder: str) -> list[str]:
        form = ZipAlbumImportForm(data={"folder": folder})
        form.is_valid()
        return form.errors.get("folder", [])

    assert folder_error("2004") == []
    assert folder_error(DECOMPOSED) == []
    assert folder_error("Andet") == ["Mappen skal være et årstal eller Uden årstal."]
    form = ZipAlbumImportForm(data={"folder": DECOMPOSED})
    form.is_valid()
    assert form.cleaned_data["folder"] == NO_YEAR_FOLDER


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="secret")
def test_the_import_script_finds_the_albums_of_uden_aarstal_by_a_decomposed_name(client: Client) -> None:
    Album.objects.create(folder=NO_YEAR_FOLDER, name="Portrætter")

    response = client.get(
        reverse("photo_album:system_albums"), {"folder": DECOMPOSED}, HTTP_AUTHORIZATION="Bearer secret"
    )

    assert [album["name"] for album in response.json()["albums"]] == ["Portrætter"]
