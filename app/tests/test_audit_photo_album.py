"""What the photo album leaves in the audit trail (photo_album.auditing)."""

import zipfile
from collections.abc import Callable
from datetime import timedelta
from io import BytesIO
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from photo_album import tasks
from photo_album.models import Album, AlbumImport, AlbumMerge, Media, MediaStatus
from photo_album.services import purge_expired, upload_media
from residents.models import Resident, Role

from .audit_support import actions, changes, events
from .media_samples import image_upload, jpeg_bytes

pytestmark = pytest.mark.django_db


@pytest.fixture
def fotogruppen(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="foto@gahk.dk", first_name="Freja", roles=(Role.FOTO,))


@pytest.fixture
def administrator(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="admin@gahk.dk", first_name="Ada", roles=(Role.ADMINISTRATOR,))


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _subject(event: AuditEvent) -> tuple[str, int | None]:
    entity = event.entities.get(role="subject")
    return entity.kind, entity.entity_id


def test_an_album_from_creation_to_deletion_is_in_the_trail(fotogruppen: Resident) -> None:
    client = _client(fotogruppen)
    client.post(reverse("photo_album:create_album"), {"folder": "2026", "name": "Fest"})
    album = Album.objects.get()
    client.post(reverse("photo_album:rename_album", args=[album.pk]), {"name": "Sommerfest"})
    client.post(reverse("photo_album:rename_album", args=[album.pk]), {"name": "Sommerfest"})  # as it was
    client.post(reverse("photo_album:album_tags", args=[album.pk]), {"action": "add", "name": "sommer"})
    client.post(reverse("photo_album:album_tags", args=[album.pk]), {"action": "remove", "name": "sommer"})
    client.post(reverse("photo_album:lock_album", args=[album.pk]))
    client.post(reverse("photo_album:unlock_album", args=[album.pk]))
    client.post(reverse("photo_album:delete_album", args=[album.pk]))

    assert actions("photo_album.") == [
        "photo_album.album.created",
        "photo_album.album.renamed",  # once: the second rename changed nothing
        "photo_album.tag.added",
        "photo_album.tag.removed",
        "photo_album.album.locked",
        "photo_album.album.unlocked",
        "photo_album.album.deleted",
    ]
    assert all(event.actor_id == fotogruppen.pk for event in events("photo_album."))
    assert all(_subject(event) == ("photo_album.album", album.pk) for event in events("photo_album."))
    created = events("photo_album.album.created").get()
    assert changes(created) == {"folder": [None, "2026"], "name": [None, "Fest"]}  # not its path or depth
    assert changes(events("photo_album.album.renamed").get()) == {"name": ["Fest", "Sommerfest"]}
    tagged = events("photo_album.tag.added").get()
    tag = tagged.entities.get(kind="photo_album.tag")
    assert (tag.name, tag.operation, tag.role) == ("sommer", "created", "tag")
    assert changes(tagged)["tags"] == [[], [tag.entity_id]]
    assert changes(events("photo_album.tag.removed").get())["tags"] == [[tag.entity_id], []]
    locked_at = changes(events("photo_album.album.locked").get())["manually_locked_at"]
    assert locked_at[0] is None and locked_at[1]
    assert events("photo_album.album.deleted").get().entities.get(role="subject").operation == "deleted"


def test_an_upload_of_several_files_is_one_event_and_their_conversion_is_none(
    make_resident: Callable[..., Resident],
    fotogruppen: Resident,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    anna = make_resident(email="anna@gahk.dk", first_name="Anna")
    album = Album.objects.create(folder="2026", name="Fest")
    with django_capture_on_commit_callbacks(execute=True):  # the thumbnails, built by the worker
        _client(anna).post(
            reverse("photo_album:upload", args=[album.pk]),
            {"uploads": [image_upload("a.jpg"), image_upload("b.jpg", jpeg_bytes((0, 0, 255)))]},
        )
    assert Media.objects.exclude(thumbnail="").count() == 2  # converted, and nothing in the trail of it

    [uploaded] = events("photo_album.")
    assert (uploaded.action, uploaded.actor_id, uploaded.details) == (
        "photo_album.media.uploaded",
        anna.pk,
        {"count": 2},
    )
    assert _subject(uploaded) == ("photo_album.album", album.pk)
    files = uploaded.entities.filter(kind="photo_album.media", operation="created")
    assert sorted(files.values_list("name", flat=True)) == ["a.jpg", "b.jpg"]
    a = files.get(name="a.jpg")
    fields = {change.field: change.after for change in a.changes.all()}
    assert fields["status"] == MediaStatus.PENDING and fields["requested_by"] == anna.pk
    assert "sha256" not in fields and "thumbnail" not in fields  # the worker's, not hers

    # Fotogruppen approves both at once: one event, with both items.
    _client(fotogruppen).post(
        reverse("photo_album:selection"),
        {"action": "approve", "media": list(Media.objects.values_list("pk", flat=True)), "album": album.pk},
    )
    approved = events("photo_album.media.approved").get()
    assert (approved.actor_id, approved.details) == (fotogruppen.pk, {"count": 2})
    assert _subject(approved) == ("photo_album.album", album.pk)
    item = approved.entities.get(kind="photo_album.media", name="a.jpg")
    status = item.changes.get(field="status")
    assert (status.before, status.after) == (MediaStatus.PENDING, MediaStatus.APPROVED)


def test_binning_restoring_and_the_nightly_purge(
    make_resident: Callable[..., Resident], fotogruppen: Resident
) -> None:
    anna = make_resident(email="anna@gahk.dk")
    album = Album.objects.create(folder="2026", name="Fest")
    kept = upload_media(album=album, uploaded_file=image_upload("kept.jpg"), resident=anna, approved=True)
    gone = upload_media(album=album, uploaded_file=image_upload("gone.jpg"), resident=anna, approved=True)
    client = _client(fotogruppen)

    client.post(reverse("photo_album:delete", args=[kept.pk]))
    client.post(reverse("photo_album:restore", args=[kept.pk]))
    client.post(reverse("photo_album:delete", args=[gone.pk]))
    Media.objects.filter(pk=gone.pk).update(deleted_at=timezone.now() - timedelta(days=31))
    assert purge_expired() == 1

    assert actions("photo_album.") == [
        "photo_album.media.binned",
        "photo_album.media.restored",
        "photo_album.media.binned",
        "photo_album.media.purged",
    ]
    binned = events("photo_album.media.binned").first()
    assert binned is not None and binned.actor_id == fotogruppen.pk
    assert _subject(binned) == ("photo_album.media", kept.pk)
    assert changes(binned)["deleted_by"] == [None, fotogruppen.pk]
    assert changes(events("photo_album.media.restored").get())["deleted_by"] == [fotogruppen.pk, None]
    purged = events("photo_album.media.purged").get()
    assert (purged.actor_id, purged.details) == (None, {"count": 1})
    entity = purged.entities.get()
    assert (entity.kind, entity.entity_id, entity.name, entity.operation) == (
        "photo_album.media",
        gone.pk,
        "gone.jpg",
        "deleted",
    )
    assert purge_expired() == 0
    assert events("photo_album.media.purged").count() == 1  # a night with nothing to purge says nothing


def _zip(*members: tuple[str, bytes]) -> SimpleUploadedFile:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for member, content in members:
            archive.writestr(member, content)
    return SimpleUploadedFile("Sommerfest.zip", payload.getvalue(), content_type="application/zip")


def test_an_import_is_asked_for_by_a_person_and_stored_by_the_worker_in_one_event(
    administrator: Resident,
) -> None:
    _client(administrator).post(
        reverse("photo_album:import_zip"),
        {
            "folder": "2026",
            "archive": _zip(("one.jpg", jpeg_bytes()), ("Aften/two.jpg", jpeg_bytes((9, 9, 9)))),
        },
    )
    album_import = AlbumImport.objects.get()
    tasks.process_album_import.apply(args=(album_import.pk,))  # as the worker runs it

    assert actions("photo_album.") == ["photo_album.import.requested", "photo_album.import.completed"]
    requested = events("photo_album.import.requested").get()
    assert requested.actor_id == administrator.pk
    assert _subject(requested) == ("photo_album.import", album_import.pk)
    assert changes(requested)["archive_name"] == [None, "Sommerfest.zip"]

    completed = events("photo_album.import.completed").get()
    assert (completed.actor_id, completed.source) == (None, AuditEvent.Source.WORKER)
    assert completed.details == {"added": 2, "left_out": 0, "merged": False}
    assert _subject(completed) == ("photo_album.import", album_import.pk)
    assert completed.entities.get(role="requested_by").entity_id == administrator.pk
    created = completed.entities.filter(operation="created")
    assert sorted(created.filter(kind="photo_album.album").values_list("name", flat=True)) == [
        "Aften (2026)",
        "Sommerfest (2026)",
    ]
    assert sorted(created.filter(kind="photo_album.media").values_list("name", flat=True)) == [
        "one.jpg",
        "two.jpg",
    ]


def test_a_refused_import_says_why(administrator: Resident) -> None:
    _client(administrator).post(
        reverse("photo_album:import_zip"), {"folder": "2026", "archive": _zip(("notes.txt", b"hej"))}
    )
    album_import = AlbumImport.objects.get()
    tasks.process_album_import.apply(args=(album_import.pk,))

    refused = events("photo_album.import.refused").get()
    assert refused.actor_id is None
    assert refused.details["reasons"] == AlbumImport.objects.get().errors
    assert not events("photo_album.import.completed").exists()


def test_a_merge_is_asked_for_by_an_administrator_and_carried_out_by_the_worker(
    administrator: Resident,
    make_resident: Callable[..., Resident],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    anna = make_resident(email="anna@gahk.dk")
    target = Album.objects.create(folder="2014", name="Sommerfest")
    source = Album.objects.create(folder="2014", name="Sommerfest 2")
    moved = upload_media(album=source, uploaded_file=image_upload("b.jpg"), resident=anna, approved=True)

    with django_capture_on_commit_callbacks(execute=True):  # the merge runs eagerly
        _client(administrator).post(
            reverse("photo_album:merge_album", args=[source.pk]), {"target": target.pk}
        )

    assert actions("photo_album.") == ["photo_album.merge.requested", "photo_album.merge.completed"]
    job = AlbumMerge.objects.get()
    requested = events("photo_album.merge.requested").get()
    assert requested.actor_id == administrator.pk
    assert _subject(requested) == ("photo_album.merge", job.pk)
    completed = events("photo_album.merge.completed").get()
    assert completed.actor_id is None
    assert _subject(completed) == ("photo_album.album", target.pk)
    assert completed.entities.get(role="request").entity_id == job.pk
    gone = completed.entities.get(role="merged")
    assert (gone.entity_id, gone.operation) == (source.pk, "deleted")
    assert changes(completed, "photo_album.media")["album"] == [source.pk, target.pk]
    assert completed.details["moved"] == 1
    assert Media.objects.get(pk=moved.pk).album_id == target.pk


def test_the_workers_bookkeeping_leaves_no_trail(
    make_resident: Callable[..., Resident], django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    album = Album.objects.create(folder="2026", name="Fest")
    media = upload_media(album=album, uploaded_file=image_upload("a.jpg"), resident=make_resident())
    Media.objects.filter(pk=media.pk).update(sha256="", size=None, capture_date_checked=False)
    with django_capture_on_commit_callbacks(execute=True):
        tasks.build_media_derivatives.apply(args=(media.pk,))
        tasks.backfill_media_hashes.apply()
        tasks.settle_media_dates.apply()
        tasks.fail_stalled_imports.apply()
        tasks.fail_stalled_merges.apply()
        tasks.purge_expired_downloads.apply()
    media.refresh_from_db()
    assert media.thumbnail and media.sha256 and media.capture_date_checked  # all of it done
    assert not AuditEvent.objects.exists()


@override_settings(
    PHOTO_ALBUM_IMPORT_TOKEN="migration-token", PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL="system@gahk.dk"
)
def test_the_import_script_is_the_actor_of_what_it_asks_for() -> None:
    """It calls in with a token, not a login: its own resident, System, is who did it."""
    token = {"HTTP_AUTHORIZATION": "Bearer migration-token"}
    client = Client()
    client.post(
        reverse("photo_album:system_import_zip"),
        {"folder": "2014", "archive": _zip(("one.jpg", jpeg_bytes()))},
        **token,
    )
    tasks.process_album_import.apply(args=(AlbumImport.objects.get().pk,))
    client.post(
        reverse("photo_album:system_rename_album"),
        {"folder": "2014", "album": "Sommerfest", "name": "Sommerfesten"},
        **token,
    )

    system = Resident.objects.get(email="system@gahk.dk")
    assert events("photo_album.import.requested").get().actor_id == system.pk
    renamed = events("photo_album.album.renamed").get()
    assert renamed.actor_id == system.pk
    assert changes(renamed) == {"name": ["Sommerfest", "Sommerfesten"]}
