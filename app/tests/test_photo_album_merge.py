"""Merging one album into another beside it (photo_album.merge)."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from photo_album import access, merge
from photo_album.dashboard import measure_storage
from photo_album.models import (
    Album,
    AlbumDownload,
    AlbumImport,
    AlbumImportState,
    AlbumMerge,
    AlbumMergeState,
    FeaturedMedia,
    FileTimeSource,
    Media,
    MediaStatus,
    MediaView,
    Tag,
    locked_album_ids,
)
from photo_album.services import store_upload
from photo_album.storage import get_photo_album_storage
from photo_album.tasks import fail_stalled_merges, process_album_merge
from residents.models import Resident, Role

from .media_samples import jpeg_bytes

SIDECAR_TIME = datetime(2014, 6, 21, 18, 30, tzinfo=UTC)


@pytest.fixture
def uploader(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="foto@gahk.dk")


def _item(
    album: Album, name: str, content: bytes, by: Resident, *, sidecar: bytes = b"", **fields: object
) -> Media:
    media = store_upload(
        album=album,
        uploaded_file=SimpleUploadedFile(name, content),
        resident=by,
        approved=True,
        sidecar=sidecar,
    ).media
    assert media is not None
    if fields:
        Media.objects.filter(pk=media.pk).update(**fields)
        media.refresh_from_db()
    return media


def _albums(*names: str, folder: str = "2014") -> list[Album]:
    return [Album.objects.create(folder=folder, name=name) for name in names]


# --- What moves where ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_everything_moves_into_the_album_that_keeps_its_name(uploader: Resident) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    kept = _item(target, "a.jpg", jpeg_bytes(), uploader)
    moved = _item(source, "b.jpg", jpeg_bytes(), uploader)
    saturday = Album.objects.create(folder="2014", name="Lørdag", parent=source)
    on_saturday = _item(saturday, "c.jpg", jpeg_bytes(), uploader)
    sunday_here = Album.objects.create(folder="2014", name="søndag", parent=target)
    sunday_there = Album.objects.create(folder="2014", name="Søndag", parent=source)
    evening = Album.objects.create(folder="2014", name="Aften", parent=sunday_there)
    on_sunday = _item(sunday_there, "d.jpg", jpeg_bytes(), uploader)
    in_the_evening = _item(evening, "e.jpg", jpeg_bytes(), uploader)
    target.tags.add(Tag.objects.create(name="gf"))
    source.tags.add(Tag.objects.create(name="fest"))

    result = merge.merge_albums(target, source)

    assert not Album.objects.filter(pk__in=[source.pk, sunday_there.pk]).exists()
    target.refresh_from_db()
    assert target.name == "Sommerfest"
    assert {tag.name for tag in target.tags.all()} == {"gf", "fest"}
    where = dict(Media.objects.values_list("pk", "album_id"))
    assert where == {
        kept.pk: target.pk,
        moved.pk: target.pk,
        on_saturday.pk: saturday.pk,
        on_sunday.pk: sunday_here.pk,
        in_the_evening.pk: evening.pk,
    }
    # The sub-album with no namesake moved whole, its subtree with it; the other was merged.
    saturday.refresh_from_db()
    evening.refresh_from_db()
    assert (saturday.parent_id, saturday.path, saturday.depth) == (
        target.pk,
        f"{target.path}{saturday.pk}/",
        1,
    )
    assert (evening.parent_id, evening.path, evening.depth) == (
        sunday_here.pk,
        f"{sunday_here.path}{evening.pk}/",
        2,
    )
    assert set(target.subtree_media()) == {kept, moved, on_saturday, on_sunday, in_the_evening}
    # Counted one by one: the items of a sub-album moved whole come with it.
    assert (result.moved, result.duplicates, result.albums_merged, result.albums_moved) == (2, 0, 1, 2)


@pytest.mark.django_db
def test_downloads_and_imports_of_the_merged_album_now_name_the_one_it_went_into(
    uploader: Resident,
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    download = AlbumDownload.objects.create(album=source, requested_by=uploader, name="Sommerfest 2")
    album_import = AlbumImport.objects.create(
        requested_by=uploader,
        folder="2014",
        archive_name="x.zip",
        state=AlbumImportState.READY,
        album_ids=[source.pk],
    )

    merge.merge_albums(target, source)

    download.refresh_from_db()
    album_import.refresh_from_db()
    assert download.album_id == target.pk
    assert album_import.album_ids == [target.pk]


# --- Duplicates ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_item_already_there_byte_for_byte_is_kept_once_whatever_its_name(
    uploader: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    same = jpeg_bytes()
    there = _item(target, "IMG_0001.JPG", same, uploader)
    copy = _item(source, "IMG_0001 kopi.JPG", same, uploader)
    namesake = _item(source, "IMG_0001.JPG", jpeg_bytes(), uploader)  # same name, other bytes

    with django_capture_on_commit_callbacks(execute=True):
        result = merge.merge_albums(target, source)

    assert set(target.media.all()) == {there, namesake}
    assert not get_photo_album_storage().exists(copy.original.name)
    assert get_photo_album_storage().exists(there.original.name)
    assert (result.moved, result.duplicates) == (1, 1)


@pytest.mark.django_db
def test_an_original_never_hashed_is_hashed_to_be_compared(uploader: Resident) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    same = jpeg_bytes()
    there = _item(target, "a.jpg", same, uploader, sha256="", size=None)
    _item(source, "a.jpg", same, uploader)

    assert merge.merge_albums(target, source).duplicates == 1

    there.refresh_from_db()
    assert there.sha256 and there.size == len(same)


@pytest.mark.django_db
def test_the_copy_kept_gets_the_sidecar_and_its_date_when_only_the_other_had_one(
    uploader: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    same = jpeg_bytes()
    there = _item(target, "a.jpg", same, uploader)
    _item(
        source,
        "a.jpg",
        same,
        uploader,
        sidecar=b"the old server's record",
        file_modified_at=SIDECAR_TIME,
        file_time_source=FileTimeSource.SIDECAR,
    )

    with django_capture_on_commit_callbacks(execute=True):
        result = merge.merge_albums(target, source)

    there.refresh_from_db()
    assert list(target.media.all()) == [there]
    with there.sidecar.open("rb") as sidecar:
        assert sidecar.read() == b"the old server's record"
    assert (there.file_modified_at, there.file_time_source) == (SIDECAR_TIME, FileTimeSource.SIDECAR)
    assert (result.duplicates, result.sidecars_kept) == (1, 1)


@pytest.mark.django_db
def test_the_copy_kept_keeps_its_own_sidecar_when_the_other_has_none(uploader: Resident) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    same = jpeg_bytes()
    there = _item(target, "a.jpg", same, uploader, sidecar=b"record")
    _item(source, "a.jpg", same, uploader)

    result = merge.merge_albums(target, source)

    assert list(target.media.all()) == [there]
    assert (result.duplicates, result.sidecars_kept) == (1, 0)


@pytest.mark.django_db
def test_copies_with_identical_sidecars_are_duplicates_and_with_different_ones_are_not(
    uploader: Resident,
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    first, second = jpeg_bytes(), jpeg_bytes()
    _item(target, "a.jpg", first, uploader, sidecar=b"21 June")
    _item(source, "a.jpg", first, uploader, sidecar=b"21 June")
    _item(target, "b.jpg", second, uploader, sidecar=b"21 June")
    other_record = _item(source, "b.jpg", second, uploader, sidecar=b"3 September")

    result = merge.merge_albums(target, source)

    assert target.media.count() == 3
    assert target.media.filter(pk=other_record.pk).exists()
    assert (result.moved, result.duplicates) == (1, 1)


@pytest.mark.django_db
def test_the_copy_kept_is_the_one_that_shows(uploader: Resident) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    first, second = jpeg_bytes(), jpeg_bytes()
    binned = _item(target, "a.jpg", first, uploader, deleted_at=timezone.now())
    live = _item(source, "a.jpg", first, uploader)
    waiting = _item(target, "b.jpg", second, uploader, status=MediaStatus.PENDING)
    approved = _item(source, "b.jpg", second, uploader)

    merge.merge_albums(target, source)

    assert set(target.media.all()) == {live, approved}
    assert not Media.objects.filter(pk__in=[binned.pk, waiting.pk]).exists()


@pytest.mark.django_db
def test_the_copy_kept_has_the_tags_views_and_dashboard_days_of_both(
    uploader: Resident, make_resident: Callable[..., Resident]
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    same = jpeg_bytes()
    there = _item(target, "a.jpg", same, uploader)
    copy = _item(source, "a.jpg", same, uploader)
    there.tags.add(Tag.objects.create(name="kage"))
    copy.tags.add(Tag.objects.create(name="dans"))
    both, only_copy = make_resident(email="both@gahk.dk"), make_resident(email="copy@gahk.dk")
    MediaView.objects.create(media=there, resident=both, count=2)
    MediaView.objects.create(media=copy, resident=both, count=3)
    MediaView.objects.create(media=copy, resident=only_copy, count=1)
    FeaturedMedia.objects.create(date=date(2026, 1, 1), position=0, media=copy)

    merge.merge_albums(target, source)

    assert {tag.name for tag in there.tags.all()} == {"kage", "dans"}
    assert dict(MediaView.objects.filter(media=there).values_list("resident__email", "count")) == {
        "both@gahk.dk": 5,
        "copy@gahk.dk": 1,
    }
    assert FeaturedMedia.objects.get().media_id == there.pk


# --- Which albums ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_any_album_in_the_folder_can_be_merged_but_not_into_one_of_its_own_sub_albums() -> None:
    fest, other_year = (
        Album.objects.create(folder="2014", name="Fest"),
        Album.objects.create(folder="2015", name="Fest"),
    )
    gallafest = Album.objects.create(folder="2014", name="Gallafest")
    evening = Album.objects.create(folder="2014", name="Aften", parent=fest)
    night = Album.objects.create(folder="2014", name="Nat", parent=gallafest)
    late = Album.objects.create(folder="2014", name="Sent", parent=night)

    with pytest.raises(ValidationError, match="samme mappe"):
        merge.plan(fest, other_year)
    with pytest.raises(ValidationError, match="ind i sig selv"):
        merge.plan(fest, fest)
    for target, source in ((evening, fest), (late, gallafest), (late, night)):
        with pytest.raises(ValidationError, match="ind i et af sine egne underalbummer"):
            merge.plan(target, source)
    for target, source in (
        (fest, gallafest),  # root into root
        (evening, night),  # sub-album into sub-album
        (fest, night),  # sub-album into another root
        (evening, gallafest),  # root into sub-album
        (gallafest, late),  # sub-album into its own root
    ):
        merge.plan(target, source)


@pytest.mark.django_db
def test_both_albums_and_every_sub_album_merged_by_name_must_be_unlocked() -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    source.manually_locked_at = timezone.now()
    source.save(update_fields=["manually_locked_at"])
    with pytest.raises(ValidationError, match="Begge albummer"):
        merge.plan(target, source)

    source.manually_locked_at = None
    source.save(update_fields=["manually_locked_at"])
    Album.objects.create(folder="2014", name="Aften", parent=target)
    namesake = Album.objects.create(folder="2014", name="aften", parent=source)
    moved_whole = Album.objects.create(folder="2014", name="Nat", parent=source)
    moved_whole.manually_locked_at = timezone.now()
    moved_whole.save(update_fields=["manually_locked_at"])
    merge.plan(target, source)  # a locked sub-album that only moves is no obstacle

    namesake.manually_locked_at = timezone.now()
    namesake.save(update_fields=["manually_locked_at"])
    with pytest.raises(ValidationError, match="Underalbummet aften er låst"):
        merge.plan(target, source)


@pytest.mark.django_db
def test_a_sub_album_merged_into_its_own_root_is_flattened_into_it(uploader: Resident) -> None:
    """The import descends through folders holding one folder, but not when that folder sits beside
    files: "Generalforsamlingen 2006" holding photos and "Generalforsamlingen 2006" again."""
    root = Album.objects.create(folder="2006", name="Generalforsamlingen 2006")
    nested = Album.objects.create(folder="2006", name="Generalforsamlingen 2006", parent=root)
    again = Album.objects.create(folder="2006", name="generalforsamlingen 2006", parent=nested)
    beside = Album.objects.create(folder="2006", name="Fest", parent=root)
    in_root = _item(root, "a.jpg", jpeg_bytes(), uploader)
    in_nested = _item(nested, "b.jpg", jpeg_bytes(), uploader)

    result = merge.merge_albums(root, nested)

    assert not Album.objects.filter(pk=nested.pk).exists()
    assert set(root.media.all()) == {in_root, in_nested}
    again.refresh_from_db()
    # Its own namesake below it is not the album it merges into, but one more level, moved up.
    assert (again.parent_id, again.path, again.depth) == (root.pk, f"{root.path}{again.pk}/", 1)
    assert set(root.children.all()) == {beside, again}
    assert (result.albums_merged, result.albums_moved) == (0, 1)


@pytest.mark.django_db
def test_a_sub_album_of_another_root_moves_up_with_everything_below_it(uploader: Resident) -> None:
    fest = Album.objects.create(folder="2014", name="Fest")
    gallafest = Album.objects.create(folder="2014", name="Gallafest")
    night = Album.objects.create(folder="2014", name="Nat", parent=gallafest)
    late = Album.objects.create(folder="2014", name="Sent", parent=night)
    later = Album.objects.create(folder="2014", name="Senere", parent=late)
    item = _item(later, "a.jpg", jpeg_bytes(), uploader)

    merge.merge_albums(fest, night)

    late.refresh_from_db()
    later.refresh_from_db()
    assert (late.parent_id, late.path, late.depth) == (fest.pk, f"{fest.path}{late.pk}/", 1)
    assert (later.path, later.depth) == (f"{fest.path}{late.pk}/{later.pk}/", 2)
    assert set(fest.subtree_media()) == {item}
    assert not gallafest.subtree().exclude(pk=gallafest.pk).exists()


@pytest.mark.django_db
def test_nothing_may_end_up_deeper_than_five_levels() -> None:
    deep = Album.objects.create(folder="2014", name="Fest")
    for name in ("1", "2", "3"):
        deep = Album.objects.create(folder="2014", name=name, parent=deep)
    gallafest = Album.objects.create(folder="2014", name="Gallafest")
    night = Album.objects.create(folder="2014", name="Nat", parent=gallafest)
    Album.objects.create(folder="2014", name="Sent", parent=night)

    merge.plan(deep, night)  # Sent moves to depth 4, the deepest there is
    with pytest.raises(ValidationError, match="Underalbummet Nat ville komme til at ligge for dybt"):
        merge.plan(deep, gallafest)  # Nat to depth 4, and Sent below it to 5


@pytest.mark.django_db
def test_the_locks_of_a_whole_folder_are_known_in_one_query(
    uploader: Resident, django_assert_num_queries: Callable[..., Any]
) -> None:
    fest, gallafest = _albums("Fest", "Gallafest")
    evening = Album.objects.create(folder="2014", name="Aften", parent=fest)
    night = Album.objects.create(folder="2014", name="Nat", parent=gallafest)
    _item(night, "a.jpg", jpeg_bytes(), uploader, added_at=timezone.now() - timedelta(days=200))
    _item(evening, "b.jpg", jpeg_bytes(), uploader)
    albums = list(Album.objects.filter(folder="2014"))

    with django_assert_num_queries(1):
        locked = locked_album_ids(albums)

    assert locked == {album.pk for album in Album.objects.filter(folder="2014") if album.is_locked()}
    assert locked == {gallafest.pk, night.pk}


# --- From the album page ------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_an_administrator_is_offered_the_merge_and_may_make_it(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    evening = Album.objects.create(folder="2014", name="Aften", parent=source)
    locked = Album.objects.create(folder="2014", name="Julefrokost", manually_locked_at=timezone.now())
    url = reverse("photo_album:merge_album", args=[target.pk])
    client.force_login(make_resident(email="foto@gahk.dk", roles=(Role.FOTO,)))
    assert (
        "Flet et album ind"
        not in client.get(reverse("photo_album:detail", args=[target.pk])).content.decode()
    )
    assert client.get(url, {"source": source.pk}).status_code == 403
    assert client.post(url, {"source": source.pk}).status_code == 403

    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    page = client.get(reverse("photo_album:detail", args=[target.pk])).content.decode()
    assert "Det valgte album flettes ind i <strong>Sommerfest</strong> og forsvinder." in page
    assert (
        f'<option value="{locked.pk}" disabled>Julefrokost (låst)</option>'
        f'<option value="{source.pk}">Sommerfest 2</option>'
        f'<option value="{evening.pk}">Sommerfest 2 › Aften</option></select>'
    ) in page
    # From a sub-album, everything in the folder but the albums above it.
    page = client.get(reverse("photo_album:detail", args=[evening.pk])).content.decode()
    assert f'<option value="{target.pk}">Sommerfest</option>' in page
    assert f'<option value="{source.pk}">' not in page


@pytest.mark.django_db
def test_an_administrator_sees_what_will_happen_merges_and_is_told_how_it_went(
    client: Client,
    make_resident: Callable[..., Resident],
    uploader: Resident,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    Album.objects.create(folder="2014", name="Aften", parent=target)
    Album.objects.create(folder="2014", name="aften", parent=source)
    Album.objects.create(folder="2014", name="Nat", parent=source)
    same = jpeg_bytes()
    _item(target, "a.jpg", same, uploader)
    _item(source, "a.jpg", same, uploader)
    _item(source, "b.jpg", jpeg_bytes(), uploader)
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    url = reverse("photo_album:merge_album", args=[target.pk])

    confirm = client.get(url, {"source": source.pk}).content.decode()

    assert AlbumMerge.objects.count() == 0  # nothing yet
    assert "<h1>Flet Sommerfest 2 ind i Sommerfest</h1>" in confirm
    assert "Flettes ind og forsvinder</p>\n\t\t\t<h2>Sommerfest 2</h2>" in confirm
    assert "Beholdes</p>\n\t\t\t<h2>Sommerfest</h2>" in confirm
    assert "<p>2 medier og 2 underalbummer</p>" in confirm
    assert (
        "Underalbummet Sommerfest 2 › aften flettes ind i Sommerfest › Aften, som har samme navn." in confirm
    )
    assert "Underalbummet Nat flyttes ind under Sommerfest." in confirm
    assert "Sommerfest beholder sit navn og sin adresse." in confirm
    assert ">Flet Sommerfest 2 ind i Sommerfest</button>" in confirm

    with django_capture_on_commit_callbacks(execute=True):  # the merge runs eagerly in the tests
        response = client.post(url, {"source": source.pk})

    assert response["Location"] == reverse("photo_album:detail", args=[target.pk])
    job = AlbumMerge.objects.get()
    assert (job.state, job.source_id, job.source_path) == (AlbumMergeState.READY, None, "Sommerfest 2")
    assert not Album.objects.filter(pk=source.pk).exists()
    page = client.get(response["Location"]).content.decode()
    assert (
        "<strong>Sommerfest 2</strong> er flettet ind i <strong>Sommerfest</strong>: 1 medie flyttet hertil, "
        "1 dublet gemt én gang, 1 underalbum flettet med et af samme navn, 1 underalbum flyttet hertil."
    ) in page


@pytest.mark.django_db
def test_an_album_merged_into_one_of_the_same_name_is_told_apart_by_its_path(
    client: Client,
    make_resident: Callable[..., Resident],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    root = Album.objects.create(folder="2006", name="Generalforsamling")
    nested = Album.objects.create(folder="2006", name="Generalforsamling", parent=root)
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    url = reverse("photo_album:merge_album", args=[root.pk])

    confirm = client.get(url, {"source": nested.pk}).content.decode()

    assert "<h1>Flet Generalforsamling › Generalforsamling ind i Generalforsamling</h1>" in confirm
    assert "Bagefter slettes Generalforsamling › Generalforsamling." in confirm

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"source": nested.pk})
    page = client.get(response["Location"]).content.decode()

    assert AlbumMerge.objects.get().source_path == "Generalforsamling › Generalforsamling"
    assert (
        "<strong>Generalforsamling › Generalforsamling</strong> er flettet ind i <strong>Generalforsamling</strong>"
        in page
    )


@pytest.mark.django_db
def test_albums_that_cannot_be_merged_are_turned_back_before_the_confirmation(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    target = Album.objects.create(folder="2014", name="Sommerfest")
    other_year = Album.objects.create(folder="2015", name="Sommerfest")
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))

    response = client.get(
        reverse("photo_album:merge_album", args=[target.pk]), {"source": other_year.pk}, follow=True
    )

    assert response.redirect_chain == [(reverse("photo_album:detail", args=[target.pk]), 302)]
    assert "Kun albummer i samme mappe kan flettes." in response.content.decode()


@pytest.mark.django_db
def test_while_a_merge_runs_neither_album_takes_uploads_and_the_page_says_so(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    beside = Album.objects.create(folder="2014", name="Julefrokost")
    under_source = Album.objects.create(folder="2014", name="Aften", parent=source)
    administrator = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    job = AlbumMerge.objects.create(
        target=target, source=source, source_path=source.name, requested_by=administrator
    )
    client.force_login(make_resident(email="r@gahk.dk"))
    request = client.get(reverse("photo_album:detail", args=[beside.pk])).wsgi_request

    assert [access.can_upload(request, album) for album in (target, source, under_source, beside)] == [
        False,
        False,
        False,
        True,
    ]
    page = client.get(reverse("photo_album:detail", args=[source.pk])).content.decode()
    assert "<strong>Sommerfest 2</strong> flettes ind i" in page
    assert "Sommerfest beholdes, og Sommerfest 2 forsvinder, når fletningen er færdig." in page
    assert f'hx-get="{reverse("photo_album:merge_status", args=[job.pk])}"' in page

    with pytest.raises(ValidationError, match="allerede en fletning i gang i mappen 2014"):
        merge.request_merge(target, beside, by=administrator)

    AlbumMerge.objects.filter(pk=job.pk).update(state=AlbumMergeState.READY)
    status = client.get(reverse("photo_album:merge_status", args=[job.pk]))
    assert status["HX-Redirect"] == reverse("photo_album:detail", args=[target.pk])


# --- The worker -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_merge_refused_by_the_time_it_runs_fails_with_why_and_changes_nothing(
    make_resident: Callable[..., Resident], uploader: Resident
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    item = _item(source, "a.jpg", jpeg_bytes(), uploader)
    job = AlbumMerge.objects.create(
        target=target,
        source=source,
        source_path=source.name,
        requested_by=make_resident(email="admin@gahk.dk"),
    )
    target.manually_locked_at = timezone.now()  # locked after the merge was asked for
    target.save(update_fields=["manually_locked_at"])

    assert process_album_merge.delay(job.pk).get() is False

    job.refresh_from_db()
    assert (job.state, job.error) == (
        AlbumMergeState.FAILED,
        "Begge albummer skal være låst op, før de kan flettes.",
    )
    item.refresh_from_db()
    assert item.album_id == source.pk


@pytest.mark.django_db
def test_a_merge_no_worker_is_coming_back_to_is_failed(make_resident: Callable[..., Resident]) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    job = AlbumMerge.objects.create(
        target=target,
        source=source,
        source_path=source.name,
        requested_by=make_resident(email="admin@gahk.dk"),
    )
    AlbumMerge.objects.filter(pk=job.pk).update(
        state=AlbumMergeState.BUILDING, progressed_at=timezone.now() - timedelta(hours=1)
    )

    assert fail_stalled_merges() == 1

    job.refresh_from_db()
    assert (job.state, job.error) == (
        AlbumMergeState.FAILED,
        "Fletningen blev afbrudt undervejs. Intet er flettet.",
    )


@pytest.mark.django_db
def test_the_storage_measured_after_a_merge_counts_moved_files_in_their_new_album(
    uploader: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    with django_capture_on_commit_callbacks(execute=True):  # the derivatives, built eagerly
        _item(target, "a.jpg", jpeg_bytes(), uploader)
        moved = _item(source, "b.jpg", jpeg_bytes(), uploader)
    assert moved.original.name.startswith(f"photo-album/{source.pk}/")

    merge.merge_albums(target, source)
    report = measure_storage()

    assert set(report.by_album) == {str(target.pk)}
