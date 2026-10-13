"""The album-tree era pages: paging, sub-albums, bins, selections, downloads, views and imports."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from photo_album.models import (
    Album,
    AlbumDownload,
    AlbumDownloadState,
    AlbumImport,
    AlbumImportState,
    DownloadVariant,
    Media,
    MediaStatus,
    MediaView,
)
from photo_album.services import build_derivatives, delete, upload_media
from photo_album.views import PAGE_SIZE
from residents.models import Resident, Role

from .media_samples import image_upload, jpeg_bytes

START = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def _media(album: Album, resident: Resident, *, at: datetime | None = None, **fields: object) -> Media:
    defaults: dict[str, object] = {
        "title": "photo",
        "original": "a",
        "high_definition": "b",
        "thumbnail": "c",
        "status": MediaStatus.APPROVED,
        "captured_at": at,
    }
    return Media.objects.create(album=album, requested_by=resident, **{**defaults, **fields})


def _ids(content: str) -> list[int]:
    return [int(pk) for pk in re.findall(r'data-media-id="(\d+)"', content)]


def _series(album: Album, resident: Resident, count: int) -> list[Media]:
    """`count` items one day apart, returned newest first — the order the pages show them in."""
    items = [_media(album, resident, at=START + timedelta(days=day), title=f"p{day}") for day in range(count)]
    return items[::-1]


# --- Paging -------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_album_pages_continue_older_and_newer_from_a_tile(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    items = _series(album, resident, PAGE_SIZE + 5)
    client.force_login(resident)
    url = reverse("photo_album:detail", args=[album.pk])

    first = client.get(url).content.decode()
    assert _ids(first) == [item.pk for item in items[:PAGE_SIZE]]
    assert f"after={items[PAGE_SIZE - 1].pk}" in first

    older = client.get(url, {"after": items[PAGE_SIZE - 1].pk})
    assert older.templates[0].name == "photo_album/_media_tiles.html"
    assert _ids(older.content.decode()) == [item.pk for item in items[PAGE_SIZE:]]

    newer = client.get(url, {"before": items[PAGE_SIZE].pk})
    assert _ids(newer.content.decode()) == [item.pk for item in items[:PAGE_SIZE]]


@pytest.mark.django_db
def test_a_deep_link_opens_the_page_around_that_item(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    items = _series(album, resident, PAGE_SIZE * 3)
    target = items[PAGE_SIZE + 10]
    client.force_login(resident)

    content = client.get(
        reverse("photo_album:detail", args=[album.pk]), {"media": target.pk}
    ).content.decode()

    ids = _ids(content)
    assert target.pk in ids
    # Half a page of newer items above it, so the reader can scroll either way.
    assert ids.index(target.pk) == PAGE_SIZE // 2
    assert f"before={ids[0]}" in content
    assert f"after={ids[-1]}" in content


@pytest.mark.django_db
def test_an_unknown_paging_anchor_is_a_404_not_a_leak(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """A pending item of someone else is not in the reader's scope, so it cannot anchor a page."""
    resident = make_resident()
    other = make_resident(email="other@gahk.dk")
    album = Album.objects.create(folder="2024", name="Fest")
    hidden = _media(album, other, at=START, status=MediaStatus.PENDING)
    client.force_login(resident)

    url = reverse("photo_album:detail", args=[album.pk])
    assert client.get(url, {"after": hidden.pk}).status_code == 404
    assert client.get(url, {"before": "nonsense"}).status_code == 404


@pytest.mark.django_db
def test_timeline_spans_albums_and_jumps_to_a_year(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    first = Album.objects.create(folder="2023", name="Gammel")
    second = Album.objects.create(folder="2025", name="Ny")
    old = _media(first, resident, at=datetime(2023, 6, 1, tzinfo=UTC))
    new = _media(second, resident, at=datetime(2025, 6, 1, tzinfo=UTC))
    client.force_login(resident)
    url = reverse("photo_album:timeline")

    everything = client.get(url).content.decode()
    assert _ids(everything) == [new.pk, old.pk]
    assert 'data-year-link="2025"' in everything
    assert 'data-year-link="2023"' in everything

    year = client.get(url, {"year": 2023}).content.decode()
    assert _ids(year) == [old.pk]
    # The newer year is still reachable by scrolling up.
    assert f"before={old.pk}" in year


# --- Sub-albums ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_sub_albums_are_listed_and_can_be_shown_with_their_media(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    in_root = _media(root, resident, at=START)
    in_child = _media(child, resident, at=START + timedelta(days=1))
    client.force_login(resident)
    url = reverse("photo_album:detail", args=[root.pk])

    own = client.get(url).content.decode()
    assert reverse("photo_album:detail", args=[child.pk]) in own
    assert _ids(own) == [in_root.pk]

    everything = client.get(url, {"alle": "1"}).content.decode()
    assert _ids(everything) == [in_child.pk, in_root.pk]

    child_page = client.get(reverse("photo_album:detail", args=[child.pk])).content.decode()
    assert "Sommerfest" in child_page  # the breadcrumb back up


@pytest.mark.django_db
def test_sub_albums_can_be_created_one_level_below_a_root_only(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    photographer = make_resident(roles=(Role.FOTO,))
    root = Album.objects.create(folder="2024", name="Sommerfest")
    client.force_login(photographer)

    response = client.post(reverse("photo_album:create_sub_album", args=[root.pk]), {"name": "Fredag"})

    child = Album.objects.get(parent=root)
    assert response.url == reverse("photo_album:detail", args=[child.pk])
    assert (child.name, child.depth, child.folder) == ("Fredag", 1, "2024")
    assert (
        client.post(reverse("photo_album:create_sub_album", args=[child.pk]), {"name": "Nat"}).status_code
        == 403
    )


@pytest.mark.django_db
def test_sibling_names_must_differ_regardless_of_case(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    photographer = make_resident(roles=(Role.FOTO,))
    root = Album.objects.create(folder="2024", name="Sommerfest")
    Album.objects.create(name="Fredag", parent=root)
    client.force_login(photographer)

    response = client.post(
        reverse("photo_album:create_sub_album", args=[root.pk]), {"name": "FREDAG"}, follow=True
    )

    assert Album.objects.filter(parent=root).count() == 1
    assert list(response.context["messages"])


@pytest.mark.django_db
def test_plain_residents_cannot_create_sub_albums(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    root = Album.objects.create(folder="2024", name="Sommerfest")
    client.force_login(make_resident())

    response = client.post(reverse("photo_album:create_sub_album", args=[root.pk]), {"name": "Fredag"})

    assert response.status_code == 403


@pytest.mark.django_db
def test_albums_can_be_renamed_until_they_are_locked(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    photographer = make_resident(roles=(Role.FOTO,))
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    client.force_login(photographer)

    client.post(reverse("photo_album:rename_album", args=[child.pk]), {"name": "Fredag aften"})
    child.refresh_from_db()
    assert child.name == "Fredag aften"

    root.manually_locked_at = timezone.now()
    root.save(update_fields=["manually_locked_at"])

    # The lock on the root covers its sub-album too.
    response = client.post(reverse("photo_album:rename_album", args=[child.pk]), {"name": "Lørdag"})
    assert response.status_code == 403
    child.refresh_from_db()
    assert child.name == "Fredag aften"


@pytest.mark.django_db
def test_unlocking_an_album_locked_by_its_parent_names_the_parent(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    photographer = make_resident(roles=(Role.FOTO,))
    root = Album.objects.create(folder="2024", name="Sommerfest", manually_locked_at=timezone.now())
    child = Album.objects.create(name="Fredag", parent=root)
    client.force_login(photographer)

    response = client.post(reverse("photo_album:unlock_album", args=[child.pk]), follow=True)

    assert any("Sommerfest" in str(message) for message in response.context["messages"])
    root.refresh_from_db()
    assert root.manually_locked_at is not None


@pytest.mark.django_db
def test_a_sub_album_can_be_locked_without_its_parent(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    photographer = make_resident(roles=(Role.FOTO,))
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    client.force_login(photographer)

    client.post(reverse("photo_album:lock_album", args=[child.pk]))

    assert Album.objects.get(pk=child.pk).is_locked()
    assert not Album.objects.get(pk=root.pk).is_locked()


@pytest.mark.django_db
def test_deleting_a_sub_album_returns_to_its_parent(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    photographer = make_resident(roles=(Role.FOTO,))
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    client.force_login(photographer)

    response = client.post(reverse("photo_album:delete_album", args=[child.pk]))

    assert response.url == reverse("photo_album:detail", args=[root.pk])
    assert not Album.objects.filter(pk=child.pk).exists()


# --- Bins ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_album_bin_holds_its_subtree_and_nothing_else(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    elsewhere = Album.objects.create(folder="2024", name="Julefrokost")
    in_child = _media(child, administrator, title="i-fredag")
    other = _media(elsewhere, administrator, title="i-jul")
    delete(in_child, administrator)
    delete(other, administrator)
    client.force_login(administrator)

    album_bin = client.get(reverse("photo_album:album_bin", args=[root.pk])).content.decode()
    assert "i-fredag" in album_bin
    assert "Sommerfest / Fredag" in album_bin
    assert "i-jul" not in album_bin

    everything = client.get(reverse("photo_album:bin")).content.decode()
    assert "i-fredag" in everything
    assert "i-jul" in everything


# --- Selections ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_selection_approves_only_pending_items(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    photographer = make_resident(roles=(Role.FOTO,))
    uploader = make_resident(email="beboer@gahk.dk")
    album = Album.objects.create(folder="2024", name="Fest")
    pending = _media(album, uploader, status=MediaStatus.PENDING)
    approved = _media(album, uploader)
    client.force_login(photographer)

    response = client.post(
        reverse("photo_album:selection"),
        {"action": "approve", "media": [pending.pk, approved.pk], "album": album.pk},
    )

    assert response.url == reverse("photo_album:detail", args=[album.pk])
    pending.refresh_from_db()
    assert pending.status == MediaStatus.APPROVED


@pytest.mark.django_db
def test_selection_approval_is_for_fotogruppen_only(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    pending = _media(album, resident, status=MediaStatus.PENDING)
    client.force_login(resident)

    response = client.post(reverse("photo_album:selection"), {"action": "approve", "media": [pending.pk]})

    assert response.status_code == 403


@pytest.mark.django_db
def test_selection_delete_touches_only_what_the_resident_may_remove(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    other = make_resident(email="other@gahk.dk")
    album = Album.objects.create(folder="2024", name="Fest")
    mine = _media(album, resident, status=MediaStatus.PENDING)
    theirs = _media(album, other)
    client.force_login(resident)

    client.post(reverse("photo_album:selection"), {"action": "delete", "media": [mine.pk, theirs.pk]})

    assert not Media.objects.filter(pk=mine.pk).exists()  # an own pending upload is withdrawn
    theirs.refresh_from_db()
    assert theirs.deleted_at is None


@pytest.mark.django_db
def test_selection_download_queues_only_visible_items(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    other = make_resident(email="other@gahk.dk")
    album = Album.objects.create(folder="2024", name="Fest")
    visible = _media(album, resident)
    hidden = _media(album, other, status=MediaStatus.PENDING)
    client.force_login(resident)

    response = client.post(
        reverse("photo_album:selection"),
        {"action": "download", "variant": "hd", "media": [visible.pk, hidden.pk], "album": album.pk},
    )

    assert response.url == reverse("photo_album:downloads")
    download = AlbumDownload.objects.get()
    assert download.media_ids == [visible.pk]
    assert download.variant == DownloadVariant.HIGH_DEFINITION
    assert download.album_id == album.pk


@pytest.mark.django_db
def test_an_empty_selection_or_unknown_action_is_refused(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    media = _media(album, resident)
    client.force_login(resident)

    assert client.post(reverse("photo_album:selection"), {"action": "download"}).status_code == 302
    assert not AlbumDownload.objects.exists()
    assert (
        client.post(reverse("photo_album:selection"), {"action": "x", "media": [media.pk]}).status_code == 400
    )


# --- Downloads ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_album_download_covers_the_subtree_and_is_not_queued_twice(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    first = _media(root, resident)
    second = _media(child, resident)
    client.force_login(resident)
    url = reverse("photo_album:download_album", args=[root.pk])

    client.post(url, {"variant": "original"})
    client.post(url, {"variant": "original"})
    client.post(url, {"variant": "hd"})

    downloads = list(AlbumDownload.objects.order_by("pk"))
    assert len(downloads) == 2
    assert downloads[0].media_ids == sorted([first.pk, second.pk])
    assert [download.variant for download in downloads] == ["original", "hd"]


@pytest.mark.django_db
def test_album_download_can_leave_the_sub_albums_out(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    own = _media(root, resident)
    _media(child, resident)
    client.force_login(resident)

    page = client.get(reverse("photo_album:detail", args=[root.pk])).content.decode()
    assert 'name="sub_albums" value="0"' in page
    assert (
        'name="sub_albums" value="0"'
        not in client.get(reverse("photo_album:detail", args=[child.pk])).content.decode()
    )

    client.post(
        reverse("photo_album:download_album", args=[root.pk]), {"variant": "original", "sub_albums": "0"}
    )
    assert AlbumDownload.objects.get().media_ids == [own.pk]

    response = client.post(reverse("photo_album:download_album", args=[child.pk]), {"sub_albums": "0"})
    assert response.status_code == 302
    assert AlbumDownload.objects.count() == 2

    empty = Album.objects.create(folder="2024", name="Tom")
    _media(Album.objects.create(name="Kun her", parent=empty), resident)
    response = client.post(
        reverse("photo_album:download_album", args=[empty.pk]), {"sub_albums": "0"}, follow=True
    )
    assert "ingen medier" in response.content.decode()
    assert AlbumDownload.objects.count() == 2


@pytest.mark.django_db
def test_an_expired_ready_download_is_queued_afresh(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    media = _media(album, resident)
    AlbumDownload.objects.create(
        album=album,
        name="Fest",
        requested_by=resident,
        media_ids=[media.pk],
        state=AlbumDownloadState.READY,
        completed_at=timezone.now() - timedelta(days=30),
    )
    client.force_login(resident)

    client.post(reverse("photo_album:download_album", args=[album.pk]))

    assert AlbumDownload.objects.count() == 2


@pytest.mark.django_db
def test_an_unknown_download_variant_is_refused(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    _media(album, resident)
    client.force_login(resident)

    response = client.post(reverse("photo_album:download_album", args=[album.pk]), {"variant": "raw"})

    assert response.status_code == 400


@pytest.mark.django_db
def test_downloads_page_lists_only_the_residents_own(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    other = make_resident(email="other@gahk.dk")
    AlbumDownload.objects.create(name="Mit album", requested_by=resident, media_ids=[1])
    AlbumDownload.objects.create(name="Andres album", requested_by=other, media_ids=[1])
    client.force_login(resident)

    content = client.get(reverse("photo_album:downloads")).content.decode()

    assert "Mit album" in content
    assert "Andres album" not in content


# --- The viewer ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_views_are_counted_once_per_window_and_reported_unique_and_total(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    other = make_resident(email="other@gahk.dk")
    album = Album.objects.create(folder="2024", name="Fest")
    media = _media(album, resident)
    url = reverse("photo_album:record_view", args=[media.pk])
    client.force_login(resident)

    assert client.post(url).json() == {"counted": True, "uniqueViews": 1, "totalViews": 1}
    assert client.post(url).json() == {"counted": False, "uniqueViews": 1, "totalViews": 1}

    MediaView.objects.update(last_viewed_at=timezone.now() - timedelta(minutes=11))
    assert client.post(url).json() == {"counted": True, "uniqueViews": 1, "totalViews": 2}

    client.force_login(other)
    assert client.post(url).json() == {"counted": True, "uniqueViews": 2, "totalViews": 3}
    assert client.get(url).status_code == 405


@pytest.mark.django_db
def test_views_of_hidden_items_are_refused(client: Client, make_resident: Callable[..., Resident]) -> None:
    uploader = make_resident()
    stranger = make_resident(email="stranger@gahk.dk")
    album = Album.objects.create(folder="2024", name="Fest")
    media = _media(album, uploader, status=MediaStatus.PENDING)
    client.force_login(stranger)

    assert client.post(reverse("photo_album:record_view", args=[media.pk])).status_code == 403
    assert not MediaView.objects.exists()


@pytest.mark.django_db
def test_viewer_details_include_album_path_link_counts_and_map(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    media = _media(child, resident, filename="IMG_0001.JPG", metadata={"Sted": "55.676100, 12.568300"})
    client.force_login(resident)

    payload = client.get(reverse("photo_album:media_detail", args=[media.pk])).json()

    assert payload["album"] == "Sommerfest / Fredag"
    assert payload["albumUrl"] == reverse("photo_album:detail", args=[child.pk])
    assert payload["filename"] == "IMG_0001.JPG"
    assert payload["mapUrl"].startswith("https://www.openstreetmap.org/?mlat=55.676100&mlon=12.568300")
    assert (payload["uniqueViews"], payload["totalViews"]) == (0, 0)


@pytest.mark.django_db
def test_a_nonsense_location_gets_no_map_link(client: Client, make_resident: Callable[..., Resident]) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    media = _media(album, resident, metadata={"Sted": "nowhere"})
    client.force_login(resident)

    assert client.get(reverse("photo_album:media_detail", args=[media.pk])).json()["mapUrl"] == ""


@pytest.mark.django_db
def test_serve_media_refuses_a_key_that_borrows_another_items_uuid(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """The uuid finds the row; the rest of the key must still be one of that row's own files."""
    resident = make_resident()
    album = Album.objects.create(folder="2024", name="Fest")
    media = upload_media(album=album, uploaded_file=image_upload("x.jpg"), resident=resident, approved=True)
    assert build_derivatives(media)
    client.force_login(resident)

    assert client.get(media.thumbnail.url).status_code == 200
    forged = media.thumbnail.url.rsplit("/", 1)[0] + "/other.jpg"
    assert client.get(forged).status_code == 404


# --- Uploads ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_uploading_the_same_file_twice_skips_it_and_says_so(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2024", name="Fest")
    other = Album.objects.create(folder="2024", name="Andet")
    client.force_login(administrator)
    content = jpeg_bytes()

    def send(target: Album) -> dict[str, object]:
        response = client.post(
            reverse("photo_album:upload", args=[target.pk]),
            {"uploads": [image_upload("IMG_0001.JPG", content)]},
            HTTP_ACCEPT="application/json",
        )
        return response.json()

    assert send(album)["uploaded"] == 1
    again = send(album)
    assert again["uploaded"] == 0
    assert len(again["skipped"]) == 1  # type: ignore[arg-type]
    elsewhere = send(other)
    assert elsewhere["uploaded"] == 1
    assert "Fest" in elsewhere["warnings"][0]  # type: ignore[index]
    assert Media.objects.count() == 2


@pytest.mark.django_db
def test_upload_keeps_the_browsers_file_modified_time(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album = Album.objects.create(folder="2024", name="Fest")
    client.force_login(administrator)
    modified = datetime(2024, 7, 1, 18, 30, tzinfo=UTC)

    client.post(
        reverse("photo_album:upload", args=[album.pk]),
        {"uploads": [image_upload("a.jpg")], "last_modified": [str(int(modified.timestamp() * 1000))]},
        HTTP_ACCEPT="application/json",
    )

    media = Media.objects.get()
    assert media.file_modified_at == modified
    assert media.sort_at == modified


# --- The import script's endpoints --------------------------------------------------------------


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_IMPORT_TOKEN="migration-token")
def test_system_albums_lists_root_albums_of_a_folder(make_resident: Callable[..., Resident]) -> None:
    resident = make_resident()
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    Album.objects.create(folder="2023", name="Gammel")
    _media(child, resident)
    client = Client()
    url = reverse("photo_album:system_albums")

    assert client.get(url, {"folder": "2024"}).status_code == 403
    response = client.get(url, {"folder": "2024"}, HTTP_AUTHORIZATION="Bearer migration-token")

    payload = response.json()
    assert payload["albums"] == [{"id": root.pk, "name": "Sommerfest", "media": 1}]
    assert ".jpg" in payload["rules"]["imageExtensions"] and ".avi" in payload["rules"]["videoExtensions"]
    assert payload["rules"]["maxSubAlbumLevels"] == 4
    assert payload["rules"]["macosMetadataMaxBytes"] == 16 * 1024
    assert payload["rules"]["macosMetadataAnySize"] == [".DS_Store"]


@pytest.mark.django_db
def test_import_status_reports_every_reason_a_zip_was_refused(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    album_import = AlbumImport.objects.create(
        requested_by=administrator,
        folder="2024",
        archive_name="fest.zip",
        state=AlbumImportState.FAILED,
        error="ZIP-filen blev afvist. Intet er importeret.",
        errors=["a.txt: ikke et billede", "b.txt: ikke et billede"],
    )
    url = reverse("photo_album:import_zip_status", args=[album_import.token])

    assert client.get(url).status_code == 401
    client.force_login(administrator)
    payload = client.get(url).json()

    assert payload["state"] == AlbumImportState.FAILED
    assert payload["errors"] == ["a.txt: ikke et billede", "b.txt: ikke et billede"]
    page = client.get(f"{reverse('photo_album:import_zip')}?job={album_import.token}").content.decode()
    assert "b.txt: ikke et billede" in page


@pytest.mark.django_db
def test_import_status_lists_the_created_tree(client: Client, make_resident: Callable[..., Resident]) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    root = Album.objects.create(folder="2024", name="Sommerfest")
    child = Album.objects.create(name="Fredag", parent=root)
    album_import = AlbumImport.objects.create(
        requested_by=administrator,
        folder="2024",
        archive_name="Sommerfest.zip",
        state=AlbumImportState.READY,
        album_ids=[root.pk, child.pk],
    )
    client.force_login(administrator)

    payload = client.get(reverse("photo_album:import_zip_status", args=[album_import.token])).json()

    assert [album["path"] for album in payload["albums"]] == ["Sommerfest", "Sommerfest / Fredag"]
