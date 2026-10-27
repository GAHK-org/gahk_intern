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
def test_only_albums_side_by_side_can_be_merged() -> None:
    fest, other_year = (
        Album.objects.create(folder="2014", name="Fest"),
        Album.objects.create(folder="2015", name="Fest"),
    )
    beside = Album.objects.create(folder="2014", name="Gallafest")
    under_fest = Album.objects.create(folder="2014", name="Aften", parent=fest)
    under_beside = Album.objects.create(folder="2014", name="Nat", parent=beside)

    for target, source in ((fest, other_year), (fest, under_beside), (under_fest, under_beside)):
        with pytest.raises(ValidationError, match="side om side"):
            merge.check(target, source)
    with pytest.raises(ValidationError, match="sig selv"):
        merge.check(fest, fest)
    merge.check(fest, beside)
    merge.check(under_fest, Album.objects.create(folder="2014", name="Morgen", parent=fest))


@pytest.mark.django_db
def test_both_albums_and_every_sub_album_merged_by_name_must_be_unlocked() -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    source.manually_locked_at = timezone.now()
    source.save(update_fields=["manually_locked_at"])
    with pytest.raises(ValidationError, match="Begge albummer"):
        merge.check(target, source)

    source.manually_locked_at = None
    source.save(update_fields=["manually_locked_at"])
    Album.objects.create(folder="2014", name="Aften", parent=target)
    namesake = Album.objects.create(folder="2014", name="aften", parent=source)
    moved_whole = Album.objects.create(folder="2014", name="Nat", parent=source)
    moved_whole.manually_locked_at = timezone.now()
    moved_whole.save(update_fields=["manually_locked_at"])
    merge.check(target, source)  # a locked sub-album that only moves is no obstacle

    namesake.manually_locked_at = timezone.now()
    namesake.save(update_fields=["manually_locked_at"])
    with pytest.raises(ValidationError, match="Underalbummet aften er låst"):
        merge.check(target, source)


# --- From the album page ------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_an_administrator_is_offered_the_merge_and_may_make_it(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    client.force_login(make_resident(email="foto@gahk.dk", roles=(Role.FOTO,)))
    assert "Flet med" not in client.get(reverse("photo_album:detail", args=[target.pk])).content.decode()
    response = client.post(reverse("photo_album:merge_album", args=[target.pk]), {"source": source.pk})
    assert response.status_code == 403

    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    page = client.get(reverse("photo_album:detail", args=[target.pk])).content.decode()
    assert "Flet med" in page
    assert f'<option value="{source.pk}">Sommerfest 2</option>' in page


@pytest.mark.django_db
def test_an_administrator_merges_from_the_album_page_and_is_told_how_it_went(
    client: Client,
    make_resident: Callable[..., Resident],
    uploader: Resident,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    same = jpeg_bytes()
    _item(target, "a.jpg", same, uploader)
    _item(source, "a.jpg", same, uploader)
    _item(source, "b.jpg", jpeg_bytes(), uploader)
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))

    with django_capture_on_commit_callbacks(execute=True):  # the merge runs eagerly in the tests
        response = client.post(reverse("photo_album:merge_album", args=[target.pk]), {"source": source.pk})

    assert response["Location"] == reverse("photo_album:detail", args=[target.pk])
    job = AlbumMerge.objects.get()
    assert (job.state, job.source_id, job.source_name) == (AlbumMergeState.READY, None, "Sommerfest 2")
    assert not Album.objects.filter(pk=source.pk).exists()
    page = client.get(response["Location"]).content.decode()
    assert "Sommerfest 2 er flettet ind i albummet: 1 medie flyttet hertil, 1 dublet gemt én gang." in page


@pytest.mark.django_db
def test_while_a_merge_runs_neither_album_takes_uploads_and_the_page_says_so(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    target, source = _albums("Sommerfest", "Sommerfest 2")
    beside = Album.objects.create(folder="2014", name="Julefrokost")
    under_source = Album.objects.create(folder="2014", name="Aften", parent=source)
    administrator = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    job = AlbumMerge.objects.create(
        target=target, source=source, source_name=source.name, requested_by=administrator
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
    assert "Sommerfest 2 flettes ind i" in page
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
        source_name=source.name,
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
        source_name=source.name,
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
