from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.utils import timezone

from photo_album.models import Album, AlbumDownload, Media
from photo_album.storage import get_photo_album_storage
from residents.models import Resident

OLD = datetime(2025, 5, 1, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _own_media_root(settings: object, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path / "media"  # type: ignore[attr-defined]
    settings.PHOTO_ALBUM_DELETE_PROTECTED_FROM = ""  # type: ignore[attr-defined]


def _code(prompt: str) -> str:
    return prompt.split("Type ", maxsplit=1)[1].split(" ", maxsplit=1)[0]


@pytest.fixture
def confirm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer the prompt with the code it asks for, and keep the prompts for the test to read."""
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return _code(prompt)

    monkeypatch.setattr("builtins.input", answer)
    return prompts


def _media(album: Album, resident: Resident, name: str = "photo.jpg", *, binned: bool = False) -> Media:
    media = Media(album=album, title=name, filename=name, requested_by=resident)
    media.original.save(name, ContentFile(b"original"), save=False)
    media.high_definition.save(name, ContentFile(b"hd"), save=False)
    media.thumbnail.save(name, ContentFile(b"thumb"), save=False)
    if binned:
        media.deleted_at = timezone.now()
    media.save()
    return media


def _download(resident: Resident, media: list[Media], album: Album | None = None) -> AlbumDownload:
    storage = get_photo_album_storage()
    download = AlbumDownload.objects.create(
        album=album, requested_by=resident, media_ids=[item.pk for item in media], name="zip"
    )
    download.archive_key = storage.save(f"photo-album-zips/{download.token}.zip", ContentFile(b"zip"))
    download.save(update_fields=["archive_key"])
    return download


def _backdate() -> None:
    Album.objects.update(created_at=OLD)
    Media.objects.update(added_at=OLD)


def _stored(media: Media) -> list[bool]:
    storage = get_photo_album_storage()
    return [storage.exists(field.name) for field in (media.original, media.high_definition, media.thumbnail)]


@pytest.mark.django_db
def test_deleting_a_sub_album_removes_its_tree_files_and_downloads_ignoring_locks(
    make_resident: Callable[..., Resident], confirm: list[str], settings: object, tmp_path: Path
) -> None:
    resident = make_resident()
    root = Album.objects.create(folder="2019", name="Sommerfest")
    child = Album.objects.create(folder="2019", name="Lørdag", parent=root)
    grandchild = Album.objects.create(folder="2019", name="Natten", parent=child)
    kept = _media(root, resident, "kept.jpg")
    doomed = [_media(child, resident, "a.jpg"), _media(grandchild, resident, "b.jpg", binned=True)]
    root.manually_locked_at = timezone.now()
    root.save(update_fields=["manually_locked_at"])
    storage = get_photo_album_storage()
    stray = storage.save(f"photo-album/{grandchild.pk}/left-behind/thumbnail/x.jpg", ContentFile(b"x"))
    whole_tree = _download(resident, [kept, *doomed], album=root)
    timeline = _download(resident, [doomed[1]])
    untouched = _download(resident, [kept], album=root)
    settings.PHOTO_ALBUM_CACHE_DIR = str(tmp_path / "cache")  # type: ignore[attr-defined]
    cached = tmp_path / "cache" / "thumbnail" / doomed[0].thumbnail.name
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"thumb")
    _backdate()

    call_command("delete_photo_album", album=child.pk)

    assert list(Album.objects.values_list("name", flat=True)) == ["Sommerfest"]
    assert list(Media.objects.all()) == [kept]
    assert _stored(kept) == [True, True, True]
    assert all(not any(_stored(media)) for media in doomed)
    assert not storage.exists(stray)
    assert list(AlbumDownload.objects.all()) == [untouched]
    assert storage.exists(untouched.archive_key)
    assert not storage.exists(whole_tree.archive_key) and not storage.exists(timeline.archive_key)
    assert not cached.exists()
    assert len(confirm) == 1


@pytest.mark.django_db
def test_deleting_a_folder_removes_every_album_in_it_and_nothing_else(
    make_resident: Callable[..., Resident], confirm: list[str]
) -> None:
    resident = make_resident()
    for name in ("Fest", "Tur"):
        album = Album.objects.create(folder="2019", name=name)
        _media(Album.objects.create(folder="2019", name="Dag", parent=album), resident)
    other = _media(Album.objects.create(folder="2020", name="Fest"), resident)
    _backdate()

    call_command("delete_photo_album", folder="2019")

    assert list(Album.objects.values_list("folder", "name")) == [("2020", "Fest")]
    assert list(Media.objects.all()) == [other]
    assert _stored(other) == [True, True, True]


@pytest.mark.django_db
def test_a_wrong_code_deletes_nothing(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _media(Album.objects.create(folder="2019", name="Fest"), make_resident())
    _backdate()
    monkeypatch.setattr("builtins.input", lambda prompt: "ABCDEFGH")

    with pytest.raises(CommandError, match="did not match"):
        call_command("delete_photo_album", folder="2019")

    assert Media.objects.get() == media
    assert _stored(media) == [True, True, True]


@pytest.mark.django_db
def test_the_code_is_random_eight_characters(
    make_resident: Callable[..., Resident], confirm: list[str]
) -> None:
    for folder in ("2018", "2019"):
        Album.objects.create(folder=folder, name="Fest")
    _backdate()

    call_command("delete_photo_album", folder="2018")
    call_command("delete_photo_album", folder="2019")

    codes = [_code(prompt) for prompt in confirm]
    assert all(len(code) == 8 and code.isalnum() for code in codes)
    assert codes[0] != codes[1]


@pytest.mark.django_db
@pytest.mark.parametrize("what", ["album", "media", "binned media"])
def test_refuses_anything_from_the_protected_date_on(
    make_resident: Callable[..., Resident], confirm: list[str], what: str
) -> None:
    root = Album.objects.create(folder="2026", name="Fest")
    child = Album.objects.create(folder="2026", name="Dag", parent=root)
    _media(child, make_resident(), binned=what == "binned media")
    _backdate()
    first_protected_moment = timezone.make_aware(datetime(2026, 10, 1))
    if what == "album":
        Album.objects.filter(pk=child.pk).update(created_at=first_protected_moment)
    else:
        Media.objects.update(added_at=first_protected_moment)

    with pytest.raises(CommandError, match=r"Refusing: .* from 2026-10-01 or later"):
        call_command("delete_photo_album", album=root.pk)

    assert Album.objects.count() == 2 and Media.objects.count() == 1
    assert not confirm


@pytest.mark.django_db
def test_the_protected_date_can_be_moved_but_not_into_the_future(
    make_resident: Callable[..., Resident], confirm: list[str], settings: object
) -> None:
    album = Album.objects.create(folder="2019", name="Fest")
    _media(album, make_resident())
    _backdate()

    settings.PHOTO_ALBUM_DELETE_PROTECTED_FROM = "2025-01-01"  # type: ignore[attr-defined]
    with pytest.raises(CommandError, match="Refusing"):
        call_command("delete_photo_album", album=album.pk)

    settings.PHOTO_ALBUM_DELETE_PROTECTED_FROM = (timezone.localdate() + timedelta(days=1)).isoformat()  # type: ignore[attr-defined]
    with pytest.raises(CommandError, match="in the future"):
        call_command("delete_photo_album", album=album.pk)

    settings.PHOTO_ALBUM_DELETE_PROTECTED_FROM = "1. oktober"  # type: ignore[attr-defined]
    with pytest.raises(CommandError, match="not a date"):
        call_command("delete_photo_album", album=album.pk)

    assert Media.objects.count() == 1 and not confirm

    settings.PHOTO_ALBUM_DELETE_PROTECTED_FROM = timezone.localdate().isoformat()  # type: ignore[attr-defined]
    call_command("delete_photo_album", album=album.pk)
    assert not Album.objects.exists()


@pytest.mark.django_db
def test_an_upload_while_the_prompt_waits_stops_the_delete(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2019", name="Fest")
    _media(album, resident)
    _backdate()

    def upload_then_confirm(prompt: str) -> str:
        _media(album, resident, "late.jpg")
        return _code(prompt)

    monkeypatch.setattr("builtins.input", upload_then_confirm)

    with pytest.raises(CommandError, match="changed while waiting"):
        call_command("delete_photo_album", album=album.pk)

    assert Media.objects.count() == 2


@pytest.mark.django_db
def test_an_unknown_album_or_empty_folder_is_an_error() -> None:
    with pytest.raises(CommandError, match="no album 999"):
        call_command("delete_photo_album", album=999)
    with pytest.raises(CommandError, match="no albums in folder"):
        call_command("delete_photo_album", folder="1999")
