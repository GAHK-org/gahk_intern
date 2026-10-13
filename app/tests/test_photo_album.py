import subprocess
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path

import imageio_ffmpeg
import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile, File
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import ExifTags, Image

from photo_album import access
from photo_album.models import Album, AlbumImport, AlbumImportState, DerivativeState, Media, MediaStatus
from photo_album.services import (
    MAX_DERIVATIVE_ATTEMPTS,
    build_derivatives,
    delete,
    extract_captured_at,
    extract_metadata,
    pending_derivatives,
    purge_expired,
    reject,
    upload_media,
)
from photo_album.storage import get_photo_album_storage
from photo_album.tasks import process_album_import
from residents.models import Resident, Role

from .media_samples import image_upload, jpeg_bytes, mp4_bytes, png_bytes, video_upload


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
def test_album_create_and_zip_import_offer_existing_or_new_folders(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    Album.objects.create(folder="2025", name="Eksisterende")
    client.force_login(administrator)

    create_content = client.get(reverse("photo_album:create_album")).content.decode()
    import_content = client.get(reverse("photo_album:import_zip")).content.decode()

    assert '<select id="folder-choice"' in create_content
    assert '<select id="folder-choice"' in import_content
    assert '<option value="2025">2025</option>' in create_content
    assert '<option value="2025">2025</option>' in import_content
    assert '<option value="__new__">Ny mappe...</option>' in create_content
    assert '<option value="__new__">Ny mappe...</option>' in import_content
    assert 'name="folder" type="text"' in create_content
    assert 'name="folder" type="text"' in import_content
    assert 'name="name"' in create_content


def _zip_upload(*members: tuple[str, bytes], name: str = "foo.zip") -> SimpleUploadedFile:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for member, content in members:
            archive.writestr(member, content)
    return SimpleUploadedFile(name, payload.getvalue(), content_type="application/zip")


def _import(
    client: Client, make_resident: Callable[..., Resident], *members: tuple[str, bytes], name: str = "foo.zip"
) -> AlbumImport:
    """Post a ZIP as an administrator and run the worker task on it, as Celery would."""
    client.force_login(
        make_resident(email=f"importer-{AlbumImport.objects.count()}@gahk.dk", roles=(Role.ADMINISTRATOR,))
    )
    response = client.post(
        reverse("photo_album:import_zip"), {"folder": "2026", "archive": _zip_upload(*members, name=name)}
    )
    assert response.status_code == 302
    album_import = AlbumImport.objects.latest("pk")
    process_album_import(album_import.pk)
    album_import.refresh_from_db()
    return album_import


def _tree() -> list[tuple[str, str | None]]:
    """Every album as (name, parent name), parents first."""
    return [
        (album.name, album.parent.name if album.parent else None)
        for album in Album.objects.select_related("parent").order_by("depth", "name")
    ]


@pytest.mark.django_db
def test_only_photo_group_or_administrator_can_import_zip_albums(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    client.force_login(resident)

    response = client.post(
        reverse("photo_album:import_zip"),
        {"folder": "2026", "archive": _zip_upload(("one.jpg", jpeg_bytes()))},
    )

    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_token_import_uses_system_resident() -> None:
    csrf_client = Client(enforce_csrf_checks=True)
    response = csrf_client.post(
        reverse("photo_album:system_import_zip"),
        {"folder": "2026", "archive": _zip_upload(("one.jpg", jpeg_bytes()), ("sub/two.jpg", jpeg_bytes()))},
        HTTP_AUTHORIZATION="Bearer migration-token",
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 202
    album_import = AlbumImport.objects.get()
    assert album_import.requested_by.full_name == "System"
    assert process_album_import(album_import.pk)
    assert {media.requested_by.full_name for media in Media.objects.all()} == {"System"}

    # The Dropbox archive is history: it arrives locked, sub-albums included.
    root = Album.objects.get(parent__isnull=True)
    assert root.manually_locked_at is not None
    assert all(album.is_locked() for album in Album.objects.all())
    status = csrf_client.get(response.json()["statusUrl"], HTTP_AUTHORIZATION="Bearer migration-token").json()
    assert status["state"] == "ready"
    assert status["locked"] is True


@pytest.mark.django_db
def test_zip_import_builds_an_album_tree_named_after_the_zip_and_its_folders(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    album_import = _import(
        client,
        make_resident,
        ("root.jpg", jpeg_bytes()),
        ("bar/nested.png", png_bytes()),
        ("bar/baz/deep.jpg", jpeg_bytes()),
        ("bar/baz/clip.mp4", mp4_bytes()),
    )

    assert album_import.state == AlbumImportState.READY
    assert _tree() == [("foo", None), ("bar", "foo"), ("baz", "bar")]
    assert {album.folder for album in Album.objects.all()} == {"2026"}
    placement = {media.filename: media.album.name for media in Media.objects.select_related("album")}
    assert placement == {"root.jpg": "foo", "nested.png": "bar", "deep.jpg": "baz", "clip.mp4": "baz"}
    assert all(media.status == MediaStatus.APPROVED and media.sha256 for media in Media.objects.all())
    root = Album.objects.get(name="foo")
    assert album_import.album_ids[0] == root.pk
    assert root.subtree_media().count() == 4
    assert not root.is_locked()  # only the system import locks what it brings in


@pytest.mark.django_db
def test_zip_import_names_the_root_after_the_first_folder_that_holds_files(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A ZIP of a folder wraps it once more, and a ZIP of Downloads/… more than once."""
    _import(
        client,
        make_resident,
        ("Downloads/Mathildefest/photo.jpg", jpeg_bytes()),
        ("Downloads/Mathildefest/Lørdag/nested.jpg", jpeg_bytes()),
    )

    assert _tree() == [("Mathildefest", None), ("Lørdag", "Mathildefest")]


@pytest.mark.django_db
def test_zip_import_stops_descending_at_a_folder_with_several_sub_folders(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    _import(
        client,
        make_resident,
        ("Sommer/Fredag/one.jpg", jpeg_bytes()),
        ("Sommer/Lørdag/two.jpg", jpeg_bytes()),
    )

    assert _tree() == [("Sommer", None), ("Fredag", "Sommer"), ("Lørdag", "Sommer")]


@pytest.mark.django_db
def test_zip_import_returns_status_and_result_urls_for_the_progress_uploader(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    client.force_login(administrator)

    response = client.post(
        reverse("photo_album:import_zip"),
        {"folder": "2026", "archive": _zip_upload(("one.jpg", jpeg_bytes()))},
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["statusUrl"].endswith("/status")
    assert payload["resultUrl"].startswith(f"{reverse('photo_album:import_zip')}?job=")


@pytest.mark.django_db
def test_zip_import_ignores_and_logs_small_macos_metadata(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    album_import = _import(
        client,
        make_resident,
        ("Mathildefest/photo.jpg", jpeg_bytes()),
        ("__MACOSX/Mathildefest/._photo.jpg", b"resource fork"),
        ("Mathildefest/.AppleDouble/photo.jpg", b"resource fork"),
        ("Mathildefest/._another.jpg", b"resource fork"),
        ("Mathildefest/.DS_Store", b"finder"),
    )

    assert album_import.state == AlbumImportState.READY
    assert _tree() == [("Mathildefest", None)]
    assert Media.objects.count() == 1
    assert len(album_import.skipped) == 4
    assert all("macOS-metadata (ignoreret)" in line for line in album_import.skipped)
    completed = client.get(f"{reverse('photo_album:import_zip')}?job={album_import.token}")
    assert "macOS-metadata" in completed.content.decode()


@pytest.mark.django_db
def test_zip_import_refuses_macos_metadata_over_16_kb(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Above 16 KB it is not a resource fork any more — perhaps a real photo that lost its name."""
    album_import = _import(
        client,
        make_resident,
        ("photo.jpg", jpeg_bytes()),
        ("._photo.jpg", b"x" * (16 * 1024 + 1)),
    )

    assert album_import.state == AlbumImportState.FAILED
    assert album_import.errors == ["._photo.jpg: ligner macOS-metadata, men er større end 16 KB."]
    assert not Album.objects.exists()
    assert not Media.objects.exists()


@pytest.mark.django_db
def test_zip_import_skips_macos_metadata_of_exactly_16_kb_and_a_ds_store_of_any_size(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A big album's .DS_Store is easily past the limit, and nothing real is called that."""
    album_import = _import(
        client,
        make_resident,
        ("Fest/photo.jpg", jpeg_bytes()),
        ("Fest/._photo.jpg", b"x" * (16 * 1024)),
        ("Fest/.DS_Store", b"x" * (1024 * 1024)),
    )

    assert album_import.state == AlbumImportState.READY
    assert sorted(album_import.skipped) == [
        "Fest/.DS_Store: macOS-metadata (ignoreret)",
        "Fest/._photo.jpg: macOS-metadata (ignoreret)",
    ]
    assert Media.objects.count() == 1


@pytest.mark.django_db
def test_zip_import_is_refused_whole_with_every_reason_listed(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    album_import = _import(
        client,
        make_resident,
        ("good.jpg", jpeg_bytes()),
        ("bar/notes.txt", b"not media"),
        ("bar/broken.jpg", b"not a jpeg"),
        ("a/b/c/d/e/too-deep.jpg", jpeg_bytes()),
        ("Fest/one.jpg", jpeg_bytes()),
        ("fest/two.jpg", jpeg_bytes()),
    )

    assert album_import.state == AlbumImportState.FAILED
    assert album_import.error == "ZIP-filen blev afvist. Intet er importeret."
    errors = "\n".join(album_import.errors)
    assert "bar/notes.txt: Filtypen er ikke understøttet" in errors
    assert "bar/broken.jpg: Billedet kunne ikke læses" in errors
    assert "a/b/c/d/e/too-deep.jpg: mapperne ligger dybere end 5 niveauer." in errors
    assert "store/små bogstaver" in errors
    assert not Album.objects.exists()
    assert not Media.objects.exists()
    assert not album_import.archive


@pytest.mark.django_db
def test_zip_import_allows_five_levels(client: Client, make_resident: Callable[..., Resident]) -> None:
    album_import = _import(
        client, make_resident, ("top.jpg", jpeg_bytes()), ("a/b/c/d/deepest.jpg", jpeg_bytes())
    )

    assert album_import.state == AlbumImportState.READY
    assert Media.objects.get(filename="deepest.jpg").album.depth == 4


@pytest.mark.django_db
def test_zip_import_refuses_a_root_album_name_already_in_the_folder(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    Album.objects.create(folder="2026", name="FOO")

    album_import = _import(client, make_resident, ("one.jpg", jpeg_bytes()))

    assert album_import.state == AlbumImportState.FAILED
    assert album_import.errors == ['Der findes allerede et album med navnet "foo" i 2026.']
    assert Album.objects.count() == 1


def _system_import(
    *members: tuple[str, bytes], name: str = "Fest.zip", album: str = ""
) -> tuple[AlbumImport, dict[str, object]]:
    """Post a ZIP to the import script's endpoint, run the worker task on it and fetch its status."""
    client = Client()
    response = client.post(
        reverse("photo_album:system_import_zip"),
        {"folder": "2026", "album": album, "archive": _zip_upload(*members, name=name)},
        HTTP_AUTHORIZATION="Bearer migration-token",
        HTTP_ACCEPT="application/json",
    )
    assert response.status_code == 202
    album_import = AlbumImport.objects.latest("pk")
    process_album_import(album_import.pk)
    album_import.refresh_from_db()
    status = client.get(response.json()["statusUrl"], HTTP_AUTHORIZATION="Bearer migration-token").json()
    return album_import, status


def _unlock(name: str) -> None:
    Album.objects.filter(name=name).update(manually_locked_at=None, unlocked_at=timezone.now())


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_system_import_adds_only_new_files_to_an_album_of_the_same_name() -> None:
    """The Dropbox script sends what an earlier attempt left out to the album that attempt created."""
    first, second = jpeg_bytes(), jpeg_bytes()
    _system_import(("Fest/first.jpg", first), ("Fest/Lørdag/IMG_0001.JPG", second))
    _unlock("Fest")

    album_import, status = _system_import(
        ("Fest/first.jpg", first),  # already there: left out
        ("Fest/LØRDAG/IMG_0001.JPG", jpeg_bytes()),  # the same name, another photo: kept beside it
        ("Fest/Søndag/new.png", png_bytes()),  # a folder the first attempt did not have
        name="fest.zip",
    )

    assert album_import.state == AlbumImportState.READY
    assert album_import.merged and status["merged"] is True
    assert album_import.skipped == ["Fest/first.jpg: findes allerede i albummet (ignoreret)"]
    assert _tree() == [("Fest", None), ("Lørdag", "Fest"), ("Søndag", "Fest")]
    placement = sorted(Media.objects.values_list("album__name", "filename"))
    assert placement == [
        ("Fest", "first.jpg"),
        ("Lørdag", "IMG_0001.JPG"),
        ("Lørdag", "IMG_0001.JPG"),
        ("Søndag", "new.png"),
    ]
    root = Album.objects.get(name="Fest")
    assert album_import.album_ids[0] == root.pk
    assert root.is_locked() and status["locked"] is True  # locked again, like everything it imports


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_system_import_with_an_album_name_takes_the_paths_as_relative_to_that_album() -> None:
    """A retry of files from one sub-album must not become an album named after that sub-album."""
    _system_import(("Fest/first.jpg", jpeg_bytes()), ("Fest/Lørdag/second.jpg", jpeg_bytes()))
    _unlock("Fest")

    album_import, status = _system_import(("Lørdag/late.jpg", jpeg_bytes()), name="retry.zip", album="Fest")

    assert album_import.state == AlbumImportState.READY
    assert status["merged"] is True
    assert _tree() == [("Fest", None), ("Lørdag", "Fest")]
    assert Media.objects.get(filename="late.jpg").album.name == "Lørdag"

    # Without one, the same archive is named after the ZIP — or here, the folder it descends into.
    album_import, _ = _system_import(("Lørdag/late.jpg", jpeg_bytes()), name="retry.zip")
    assert ("Lørdag", None) in _tree()


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_system_import_refuses_to_add_to_a_locked_album() -> None:
    """The import never lifts a lock; Fotogruppen unlocks the album on purpose first."""
    _system_import(("Fest/first.jpg", jpeg_bytes()), ("Fest/Lørdag/second.jpg", jpeg_bytes()))
    album_import, _ = _system_import(("Fest/new.jpg", jpeg_bytes()))

    assert album_import.state == AlbumImportState.FAILED
    assert album_import.errors == ['Albummet "Fest" er låst. Lås det op, før der tilføjes flere filer.']

    # A locked sub-album refuses what would land in it, not the rest of the album.
    _unlock("Fest")
    Album.objects.filter(name="Lørdag").update(manually_locked_at=timezone.now())
    refused, _ = _system_import(("Fest/new.jpg", jpeg_bytes()), ("Fest/Lørdag/late.jpg", jpeg_bytes()))
    accepted, _ = _system_import(("Fest/new.jpg", jpeg_bytes()), ("Fest/Søndag/late.jpg", jpeg_bytes()))

    assert refused.errors == ['Albummet "Lørdag" er låst. Lås det op, før der tilføjes flere filer.']
    assert accepted.state == AlbumImportState.READY
    assert Media.objects.count() == 4


@pytest.mark.django_db
def test_zip_import_skips_exact_duplicates_but_keeps_same_named_different_files(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Two phones both write IMG_0001.JPG; only a byte-for-byte copy of the same file is dropped."""
    same = jpeg_bytes()
    with pytest.warns(UserWarning, match="Duplicate name"):
        album_import = _import(
            client,
            make_resident,
            ("IMG_0001.JPG", same),
            ("IMG_0001.JPG", same),
            ("IMG_0001.JPG", jpeg_bytes()),
            ("copy.jpg", same),
        )

    assert album_import.state == AlbumImportState.READY
    assert album_import.skipped == ["IMG_0001.JPG: dublet af IMG_0001.JPG (ignoreret)"]
    assert sorted(Media.objects.values_list("filename", flat=True)) == [
        "IMG_0001.JPG",
        "IMG_0001.JPG",
        "copy.jpg",
    ]


def test_zip_member_names_are_decoded_whichever_encoding_the_tool_used() -> None:
    """Without the UTF-8 flag Python reads names as CP437 — right for old DOS archives, wrong for Finder."""
    from photo_album.services import _member_name

    finder = zipfile.ZipInfo("Påske/Bål.jpg".encode().decode("cp437"))
    dos = zipfile.ZipInfo("Påske/Bål.jpg".encode("cp437").decode("cp437"))
    flagged = zipfile.ZipInfo("Påske/Bål.jpg")
    flagged.flag_bits |= 0x800

    assert finder.flag_bits & 0x800 == 0
    assert _member_name(finder) == "Påske/Bål.jpg"
    assert _member_name(dos) == "Påske/Bål.jpg"
    assert _member_name(flagged) == "Påske/Bål.jpg"


@pytest.mark.django_db
def test_zip_import_that_fails_while_storing_leaves_nothing_behind(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    from photo_album import services
    from photo_album.storage import get_photo_album_storage

    real_store_upload = services.store_upload
    stored: list[str] = []

    def failing_store_upload(**kwargs: object) -> services.UploadOutcome:
        if stored:
            raise OSError("bucket went away")
        outcome = real_store_upload(**kwargs)  # type: ignore[arg-type]
        assert outcome.media is not None
        stored.append(outcome.media.original.name)
        return outcome

    monkeypatch.setattr(services, "store_upload", failing_store_upload)

    with pytest.raises(OSError, match="bucket went away"):
        _import(client, make_resident, ("one.jpg", jpeg_bytes()), ("two.jpg", jpeg_bytes()))

    album_import = AlbumImport.objects.get()
    assert album_import.state == AlbumImportState.FAILED
    assert not Album.objects.exists()
    assert not Media.objects.exists()
    assert stored
    assert not get_photo_album_storage().exists(stored[0])


@pytest.mark.django_db
def test_normal_upload_is_pending_and_only_visible_to_requester(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    requester = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(requester)
    response = client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {"uploads": image_upload("x.jpg")},
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
                image_upload("one.jpg"),
                image_upload("two.jpg"),
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
    assert "data-album-upload-dialog" in content
    assert "data-album-upload-progress" in content


@pytest.mark.django_db
def test_upload_returns_json_for_the_queued_browser_uploader(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)

    response = client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {"uploads": [_image()]},
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 201
    assert response.json() == {"uploaded": 1, "skipped": [], "warnings": []}


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


def _animated_gif(size: tuple[int, int], loop: int | None = 0) -> bytes:
    from PIL import Image

    frames = [Image.new("RGB", size, colour) for colour in ("red", "green", "blue")]
    output = BytesIO()
    options: dict[str, object] = {"duration": [100, 200, 300]}
    if loop is not None:
        options["loop"] = loop
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], **options)
    return output.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize(("size", "loop"), [((200, 100), 0), ((2400, 1200), 0), ((2400, 1200), None)])
def test_an_animated_gif_stays_animated_in_high_definition(
    make_resident: Callable[..., Resident], size: tuple[int, int], loop: int | None
) -> None:
    from PIL import Image

    content = _animated_gif(size, loop)
    media = upload_media(
        album=Album.objects.create(folder="2026", name="Fest"),
        uploaded_file=SimpleUploadedFile("dance.gif", content, content_type="image/gif"),
        resident=make_resident(),
        approved=True,
    )
    assert build_derivatives(media)

    assert media.high_definition.name.endswith(".gif")
    assert media.thumbnail.name.endswith(".jpg")
    with Image.open(media.high_definition) as high_definition:
        assert high_definition.format == "GIF"
        assert high_definition.n_frames == 3
        assert max(high_definition.size) == min(max(size), 1600)
        assert high_definition.info.get("loop") == loop
        durations = []
        for index in range(3):
            high_definition.seek(index)
            durations.append(high_definition.info["duration"])
        assert durations == [100, 200, 300]
    if max(size) <= 1600:
        with media.high_definition.open("rb") as stored:
            assert stored.read() == content
    with Image.open(media.thumbnail) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert max(thumbnail.size) <= 320


@pytest.mark.django_db
def test_a_still_gif_gets_jpeg_derivatives(make_resident: Callable[..., Resident]) -> None:
    from PIL import Image

    source = BytesIO()
    Image.new("RGB", (200, 100), "red").save(source, format="GIF")
    media = upload_media(
        album=Album.objects.create(folder="2026", name="Fest"),
        uploaded_file=SimpleUploadedFile("still.gif", source.getvalue(), content_type="image/gif"),
        resident=make_resident(),
        approved=True,
    )
    assert build_derivatives(media)
    assert media.high_definition.name.endswith(".jpg")


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
    assert (
        first_media.original.name == f"photo-album/{first_album.pk}/{first_media.uuid}/original/same-name.jpg"
    )
    assert second_media.original.name == (
        f"photo-album/{second_album.pk}/{second_media.uuid}/original/same-name.jpg"
    )
    assert first_media.filename == second_media.filename == "same-name.jpg"


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
        uploaded_file=image_upload("first.jpg"),
        resident=resident,
        approved=True,
    )
    upload_media(
        album=album,
        uploaded_file=video_upload("second.mp4"),
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
        assert archive.read("first.jpg") == Media.objects.get(filename="first.jpg").original.read()
        assert archive.read("second.mp4") == mp4_bytes()


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
        uploaded_file=image_upload("visible.jpg"),
        resident=resident,
        approved=True,
    )
    upload_media(
        album=album,
        uploaded_file=image_upload("pending.jpg"),
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
        uploaded_file=image_upload("x.jpg"),
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
        uploaded_file=image_upload("x.jpg"),
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
        uploaded_file=image_upload("x.jpg"),
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

    response = client.get("/photo-album/999999/thumbnail/nope.jpg")

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
    uploaded_file = image_upload("nested.jpg")

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
    # `.update()` skips `Media.save`, which is what keeps `sort_at` in step — so set it here too.
    Media.objects.filter(pk=captured.pk).update(added_at=timezone.make_aware(datetime(2026, 9, 1)))
    Media.objects.filter(pk=uploaded.pk).update(
        added_at=timezone.make_aware(datetime(2026, 9, 14)),
        sort_at=timezone.make_aware(datetime(2026, 9, 14)),
    )
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
        {"uploads": image_upload("x.jpg")},
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
    assert 'src="/thumbnail.jpg"' in content
    assert "Fra album: Fest" in content
    # The viewer-sized file is not in the grid any more; it arrives when the item is opened.
    assert "/high-definition.jpg" not in content
    detail = client.get(reverse("photo_album:media_detail", args=[media.pk])).json()
    assert detail["full"] == "/high-definition.jpg"
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
    # Back to the album's bin, where the resident was working — not out to the album itself.
    assert response.url == reverse("photo_album:album_bin", args=[album.pk])
    assert media.album_id == album.pk
    assert media.deleted_at is None


@pytest.mark.django_db
def test_album_with_media_can_be_renamed_but_not_deleted(
    make_resident: Callable[..., Resident],
) -> None:
    """Album names are folder names in downloads, so renaming is allowed; only the lock stops it."""
    from photo_album.services import rename_album

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    Media.objects.create(
        album=album, title="photo", original="a", high_definition="b", thumbnail="c", requested_by=resident
    )
    rename_album(album, "Nyt navn")
    album.refresh_from_db()
    assert album.name == "Nyt navn"
    with pytest.raises(ValidationError, match="tomt"):
        album.delete()


# --- Regression tests for the PR review findings -------------------------------------------------
#
# Each of these fails against the code as it was. Several of the bugs below survived the original
# suite precisely because the existing tests asserted the buggy behaviour, so they are spelled out
# here in terms of what a resident or a manager actually sees.


def _image(name: str = "photo.jpg") -> SimpleUploadedFile:
    return image_upload(name)


def _video(name: str = "clip.mp4") -> SimpleUploadedFile:
    return video_upload(name)


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
    """Transcoding in the request outran the 60 s request timeout and lost the upload."""
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
    corrupt = SimpleUploadedFile("clip.mp4", b"not a video", content_type="video/mp4")
    media = upload_media(album=album, uploaded_file=corrupt, resident=resident)

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

    # One more than before the album tree: the page now lists the album's sub-albums. One more again
    # for the album's tags (a root album has no ancestors to inherit any from, so no query for those),
    # and one for the notification bell's unseen count, which base.html draws on every page.
    with django_assert_num_queries(11):  # type: ignore[operator]
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


def test_exif_text_ends_at_its_first_nul() -> None:
    """Old cameras pad EXIF strings with NULs and garbage, which Postgres refuses in JSON."""
    image = Image.new("RGB", (8, 8))
    exif = image.getexif()
    exif[271] = "PENTAX\x00junk"
    exif.get_ifd(ExifTags.IFD.Exif)[36867] = "2001:06:15 12:00:00\x00\x00\x01"
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    photo = File(buffer, name="photo.jpg")

    assert extract_metadata(photo) == {"Oprindelig dato": "2001:06:15 12:00:00", "Kamera": "PENTAX"}
    assert extract_captured_at(photo) == timezone.make_aware(datetime(2001, 6, 15, 12, 0, 0))


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

    # Can create albums, approve submissions, and reach an album's bin — none of which a plain
    # resident can. The combined bin of every album is for administrators only.
    assert client.post(reverse("photo_album:approve", args=[media.pk])).status_code == 302
    assert client.get(reverse("photo_album:album_bin", args=[album.pk])).status_code == 200
    assert client.get(reverse("photo_album:bin")).status_code == 403
    assert (
        client.post(
            reverse("photo_album:create_album"), {"folder": "2026", "name": "Julefrokost"}
        ).status_code
        == 302
    )

    media.refresh_from_db()
    assert media.status == MediaStatus.APPROVED

    client.force_login(uploader)
    assert client.get(reverse("photo_album:album_bin", args=[album.pk])).status_code == 403
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


# --- what the Celery review turned up ------------------------------------------------------------


@pytest.mark.django_db
def test_a_video_stored_without_a_content_type_is_still_treated_as_a_video(
    make_resident: Callable[..., Resident],
) -> None:
    """Several browsers send NO content type for .mov, and `check_media_upload` accepts that when
    the extension vouches for the file — so the row is stored with content_type "".

    `build_derivatives` asked `is_video(media)`, and a Media has no `.name`, so the test collapsed
    to the content type and said "image". `image_variants` then returned None, the unsupported-source
    branch copied the raw video bytes into BOTH derivative fields, and the item was marked READY —
    putting a .mov inside the grid's <img>.
    """
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    upload = SimpleUploadedFile("clip.mov", b"bytes", content_type="")

    media = upload_media(album=album, uploaded_file=upload, resident=resident)
    # Uploads now take their type from the extension; rows stored before that still have "".
    assert media.content_type == "video/quicktime"
    Media.objects.filter(pk=media.pk).update(content_type="")
    media.refresh_from_db()

    assert media.derivative_state == DerivativeState.PENDING
    # False because these bytes are not a real video — the point is that it took the VIDEO path and
    # failed honestly, rather than the image path and "succeeding" with the original copied over.
    assert build_derivatives(media) is False
    assert not media.thumbnail
    assert not media.high_definition
    assert media.derivative_state != DerivativeState.READY


@pytest.mark.django_db
def test_a_claimed_media_row_is_not_rebuilt_by_a_duplicate_dispatch(
    make_resident: Callable[..., Resident],
) -> None:
    """A recovery dispatch must not duplicate a build that already holds the media claim."""
    from photo_album.tasks import build_media_derivatives

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    media = upload_media(album=album, uploaded_file=_video(), resident=resident)

    build_media_derivatives.run(media.pk)
    media.refresh_from_db()
    assert media.derivative_attempts == 1
    assert media.derivative_started_at is not None

    assert build_media_derivatives.run(media.pk) is False

    media.refresh_from_db()
    assert media.derivative_attempts == 1


@pytest.mark.django_db
def test_an_expired_claim_lets_another_worker_pick_the_row_up(
    make_resident: Callable[..., Resident],
) -> None:
    """The claim is a timestamp precisely so a worker that DIES holding one strands nothing."""
    from photo_album.models import DERIVATIVE_CLAIM_TTL
    from photo_album.tasks import build_media_derivatives

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    media = upload_media(album=album, uploaded_file=_video(), resident=resident)
    Media.objects.filter(pk=media.pk).update(
        derivative_started_at=timezone.now() - DERIVATIVE_CLAIM_TTL - timedelta(minutes=1)
    )

    build_media_derivatives.run(media.pk)

    media.refresh_from_db()
    assert media.derivative_attempts == 1


@pytest.mark.django_db
def test_process_pending_media_skips_held_rows_and_reports_what_it_dispatched(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old return value re-queried AFTER dispatching, so it reported whatever was still pending
    at that instant — racing the worker it had just started."""
    from photo_album import tasks

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    held = upload_media(album=album, uploaded_file=_video("held.mp4"), resident=resident)
    free = upload_media(album=album, uploaded_file=_video("free.mp4"), resident=resident)
    Media.objects.filter(pk=held.pk).update(derivative_started_at=timezone.now())

    dispatched: list[int] = []
    monkeypatch.setattr(tasks.build_media_derivatives, "delay", dispatched.append)

    assert tasks.process_pending_media.run() == 1
    assert dispatched == [free.pk]


@pytest.mark.django_db
def test_a_download_whose_worker_never_returned_is_closed_out(
    make_resident: Callable[..., Resident],
) -> None:
    """A worker killed outright runs no handler, so the row kept BUILDING and completed_at NULL —
    which `purge_expired_downloads` filters on, and which detail.html answers with a five-second
    reload. The page reloaded itself forever and the row was never collected."""
    from photo_album.models import ALBUM_DOWNLOAD_STALE_AFTER, AlbumDownload, AlbumDownloadState
    from photo_album.tasks import fail_stalled_downloads

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    stalled = AlbumDownload.objects.create(
        album=album, requested_by=resident, media_ids=[], state=AlbumDownloadState.BUILDING
    )
    fresh = AlbumDownload.objects.create(
        album=album, requested_by=resident, media_ids=[], state=AlbumDownloadState.BUILDING
    )
    AlbumDownload.objects.filter(pk=stalled.pk).update(
        created_at=timezone.now() - ALBUM_DOWNLOAD_STALE_AFTER - timedelta(minutes=1)
    )

    assert fail_stalled_downloads.run() == 1

    stalled.refresh_from_db()
    fresh.refresh_from_db()
    assert stalled.state == AlbumDownloadState.FAILED
    # Set so `purge_expired_downloads`, which filters on it, can eventually collect the row.
    assert stalled.completed_at is not None
    assert fresh.state == AlbumDownloadState.BUILDING


@pytest.mark.django_db
def test_a_second_download_click_does_not_queue_a_second_archive(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The button stays on the page while the build runs, and a whole-album ZIP is slow enough to
    invite a second click — which used to queue a second complete archive and leave a second object
    in the bucket, while the page only ever shows the newest row anyway."""
    from photo_album.models import AlbumDownload

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    upload_media(
        album=album,
        uploaded_file=image_upload("first.jpg"),
        resident=resident,
        approved=True,
    )
    client.force_login(resident)
    url = reverse("photo_album:download_album", args=[album.pk])

    client.post(url)
    client.post(url)

    assert AlbumDownload.objects.filter(album=album, requested_by=resident).count() == 1


@pytest.mark.django_db
def test_albums_loaded_with_deferred_fields_still_track_moves_and_folder_changes() -> None:
    """`.only()` used to send Album.__init__ into endless reloads of the fields it remembers."""
    root = Album.objects.create(folder="2026", name="Fest")
    child = Album.objects.create(name="Lørdag", parent=root)

    assert Album.objects.only("pk", "name").get(pk=child.pk).name == "Lørdag"

    deferred_root = Album.objects.only("pk", "name").get(pk=root.pk)
    deferred_root.folder = "2025"
    deferred_root.save()
    child.refresh_from_db()
    assert child.folder == "2025"

    deferred_child = Album.objects.only("pk", "name").get(pk=child.pk)
    deferred_child.parent = Album.objects.create(folder="2025", name="Andet")
    with pytest.raises(ValidationError, match="flyttes"):
        deferred_child.save()


# --- Uploads, duplicates, views and download layout ---------------------------------------------


@pytest.mark.django_db
def test_form_upload_refuses_a_file_that_is_not_what_its_name_says(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A .jpg that does not decode used to be stored and then copied over as its own thumbnail."""
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2026", name="Fest")
    client.force_login(administrator)

    response = client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {"uploads": [SimpleUploadedFile("x.jpg", b"not-an-image", content_type="image/jpeg")]},
        follow=True,
    )

    assert not Media.objects.exists()
    assert "Billedet kunne ikke læses" in response.content.decode()


@pytest.mark.django_db
def test_store_upload_keeps_same_named_files_and_skips_exact_copies(
    make_resident: Callable[..., Resident],
) -> None:
    from photo_album.services import store_upload

    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")
    other = Album.objects.create(folder="2026", name="Andet")
    same = jpeg_bytes()

    first = store_upload(album=album, uploaded_file=image_upload("IMG_0001.JPG", same), resident=resident)
    again = store_upload(album=album, uploaded_file=image_upload("IMG_0001.JPG", same), resident=resident)
    different = store_upload(album=album, uploaded_file=image_upload("IMG_0001.JPG"), resident=resident)
    elsewhere = store_upload(album=other, uploaded_file=image_upload("IMG_0001.JPG", same), resident=resident)

    assert first.media is not None
    assert again.media is None
    assert again.duplicate == first.media
    assert different.media is not None
    assert different.media.original.name != first.media.original.name
    assert elsewhere.media is not None
    assert elsewhere.also_in == [album]
    assert Media.objects.filter(filename="IMG_0001.JPG").count() == 3


@pytest.mark.django_db
def test_store_upload_refuses_a_locked_album(make_resident: Callable[..., Resident]) -> None:
    from photo_album.services import store_upload

    album = Album.objects.create(folder="2026", name="Fest", manually_locked_at=timezone.now())
    child = Album.objects.create(name="Lørdag", parent=album)

    with pytest.raises(ValueError, match="låst"):
        store_upload(album=child, uploaded_file=image_upload(), resident=make_resident())


@pytest.mark.django_db
def test_video_capture_time_comes_from_the_container_metadata(make_resident: Callable[..., Resident]) -> None:
    media = upload_media(
        album=Album.objects.create(folder="2026", name="Fest"),
        uploaded_file=video_upload(),
        resident=make_resident(),
        approved=True,
    )

    assert build_derivatives(media)

    media.refresh_from_db()
    assert media.captured_at == datetime(2021, 6, 5, 14, 30, tzinfo=UTC)
    assert media.sort_at == media.captured_at


@pytest.mark.django_db
def test_views_count_unique_viewers_and_repeat_views_outside_ten_minutes(
    make_resident: Callable[..., Resident],
) -> None:
    from photo_album.models import MediaView
    from photo_album.services import record_view

    resident = make_resident()
    other = make_resident(email="other@gahk.dk")
    media = Media.objects.create(
        album=Album.objects.create(folder="2026", name="Fest"),
        original="a",
        requested_by=resident,
        status=MediaStatus.APPROVED,
    )
    start = timezone.now()

    assert record_view(media, resident, now=start)
    assert not record_view(media, resident, now=start + timedelta(minutes=9))
    assert record_view(media, resident, now=start + timedelta(minutes=20))
    assert record_view(media, other, now=start)

    views = MediaView.objects.filter(media=media)
    assert views.count() == 2
    assert views.get(resident=resident).count == 2
    assert sum(view.count for view in views) == 3


@pytest.mark.django_db
def test_archive_mirrors_the_album_tree_and_numbers_clashing_names(
    make_resident: Callable[..., Resident],
) -> None:
    from photo_album.models import AlbumDownload
    from photo_album.tasks import RENAMED_NOTICE_NAME, archive_entries, build_album_download

    resident = make_resident()
    root = Album.objects.create(folder="2026", name="Fest")
    child = Album.objects.create(name="Lørdag", parent=root)
    ids = [
        upload_media(album=album, uploaded_file=image_upload(name), resident=resident, approved=True).pk
        for album, name in [(root, "IMG_1.JPG"), (root, "img_1.jpg"), (child, "IMG_1.JPG"), (child, "b.jpg")]
    ]

    download = AlbumDownload.objects.create(album=root, name=root.name, requested_by=resident, media_ids=ids)
    entries, renamed = archive_entries(download)

    assert sorted(name for name, _, _ in entries) == [
        "IMG_1_01.JPG",
        "Lørdag/IMG_1.JPG",
        "Lørdag/b.jpg",
        "img_1_02.jpg",
    ]
    assert sorted(renamed) == [("IMG_1.JPG", "IMG_1_01.JPG"), ("img_1.jpg", "img_1_02.jpg")]

    selection = AlbumDownload.objects.create(requested_by=resident, media_ids=ids[2:])
    assert sorted(name for name, _, _ in archive_entries(selection)[0]) == [
        "2026/Fest/Lørdag/IMG_1.JPG",
        "2026/Fest/Lørdag/b.jpg",
    ]

    assert build_album_download.run(download.pk)
    download.refresh_from_db()
    with get_photo_album_storage().open(download.archive_key) as stored, zipfile.ZipFile(stored) as archive:
        notice = archive.read(RENAMED_NOTICE_NAME).decode()
    assert "IMG_1.JPG  ->  IMG_1_01.JPG" in notice
    assert RENAMED_NOTICE_NAME.startswith("_")


@pytest.mark.django_db
def test_backfill_hashes_fills_in_rows_stored_before_hashing(make_resident: Callable[..., Resident]) -> None:
    import hashlib

    content = jpeg_bytes()
    media = upload_media(
        album=Album.objects.create(folder="2026", name="Fest"),
        uploaded_file=image_upload("old.jpg", content),
        resident=make_resident(),
    )
    Media.objects.filter(pk=media.pk).update(sha256="")

    call_command("backfill_photo_album_hashes", stdout=StringIO())

    media.refresh_from_db()
    assert media.sha256 == hashlib.sha256(content).hexdigest()


@pytest.mark.django_db
def test_nightly_hash_backfill_stops_at_its_budget(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    from datetime import timedelta

    from photo_album import tasks

    album = Album.objects.create(folder="2026", name="Fest")
    resident = make_resident()
    contents = [jpeg_bytes((index, 0, 0)) for index in range(3)]
    ids = [
        upload_media(album=album, uploaded_file=image_upload(f"{index}.jpg", content), resident=resident).pk
        for index, content in enumerate(contents)
    ]
    Media.objects.update(sha256="")

    monkeypatch.setattr(tasks, "HASH_BACKFILL_BUDGET", timedelta(0))
    assert tasks.backfill_media_hashes.run() == 0

    monkeypatch.setattr(tasks, "HASH_BACKFILL_BUDGET", timedelta(minutes=20))
    assert tasks.backfill_media_hashes.run() == 3
    hashes = dict(Media.objects.values_list("pk", "sha256"))
    assert [hashes[pk] for pk in ids] == [hashlib.sha256(content).hexdigest() for content in contents]
    assert tasks.backfill_media_hashes.run() == 0
