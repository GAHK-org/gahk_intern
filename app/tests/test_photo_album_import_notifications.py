"""Whoever started an import hears when it fails — they may have closed the page that was waiting."""

import zipfile
from collections.abc import Callable
from datetime import timedelta
from io import BytesIO

import pytest
from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone

from notifications import categories
from notifications.models import Notification
from photo_album import notify
from photo_album.models import JOB_QUEUED_TOO_LONG, JOB_STALLED_AFTER, AlbumImport, AlbumImportState
from photo_album.tasks import fail_stalled_imports, process_album_import
from residents.models import Resident

from .media_samples import jpeg_bytes


def _zip(*members: tuple[str, bytes]) -> bytes:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
    return payload.getvalue()


def _import(resident: Resident, content: bytes, **fields: object) -> AlbumImport:
    album_import = AlbumImport.objects.create(
        requested_by=resident, folder="2026", archive_name="Sommerfest.zip", **fields
    )
    album_import.archive.save("Sommerfest.zip", ContentFile(content), save=True)
    return album_import


def _notification(resident: Resident) -> Notification:
    return Notification.objects.get(recipient=resident)


@pytest.mark.django_db
def test_a_refused_import_tells_its_resident_why_and_where_to_see_the_rest(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album_import = _import(resident, _zip(("notes.txt", b"not a photo"), ("other.doc", b"nor this")))

    assert process_album_import(album_import.pk) is False

    notice = _notification(resident)
    assert notice.category == categories.SYSTEM
    assert notice.title == "Importen af Sommerfest.zip mislykkedes"
    assert notice.body.startswith("ZIP-filen blev afvist, så intet er importeret: ")
    assert "Og 1 andre ting" in notice.body
    assert notice.url.endswith(f"?job={album_import.token}")  # the import page lists every reason


@pytest.mark.django_db
def test_an_import_that_crashes_tells_its_resident(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(**_: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("photo_album.tasks.import_zip_album", crash)
    resident = make_resident()
    album_import = _import(resident, _zip(("one.jpg", jpeg_bytes())))

    with pytest.raises(RuntimeError, match="disk full"):
        process_album_import(album_import.pk)

    assert _notification(resident).body == notify.CRASHED


@pytest.mark.django_db
def test_an_import_whose_worker_died_tells_its_resident(make_resident: Callable[..., Resident]) -> None:
    resident = make_resident()
    album_import = _import(resident, b"zip", state=AlbumImportState.BUILDING, task_id="first-run")

    assert process_album_import.apply(args=(album_import.pk,), task_id="first-run").get() is False

    assert _notification(resident).body == notify.INTERRUPTED


@pytest.mark.django_db
def test_the_sweep_tells_a_stopped_import_from_one_that_never_started(
    make_resident: Callable[..., Resident],
) -> None:
    """One that never started can still be revived if its message turns up — its notice says so."""
    stopped_by, waiting_for = make_resident(email="a@gahk.dk"), make_resident(email="b@gahk.dk")
    _import(
        stopped_by,
        b"zip",
        state=AlbumImportState.BUILDING,
        progressed_at=timezone.now() - JOB_STALLED_AFTER - timedelta(minutes=1),
    )
    queued = _import(waiting_for, b"zip")
    AlbumImport.objects.filter(pk=queued.pk).update(
        created_at=timezone.now() - JOB_QUEUED_TOO_LONG - timedelta(minutes=1)
    )

    assert fail_stalled_imports.run() == 2

    assert _notification(stopped_by).body == notify.INTERRUPTED
    assert _notification(waiting_for).body == notify.NEVER_STARTED


@pytest.mark.django_db
@override_settings(PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL="system@gahk.dk")
def test_nobody_is_told_about_the_import_scripts_own_imports_nor_about_one_that_worked(
    make_resident: Callable[..., Resident],
) -> None:
    system = make_resident(email="system@gahk.dk")
    resident = make_resident(email="r@gahk.dk")
    refused = _import(system, _zip(("notes.txt", b"not a photo")))
    worked = _import(resident, _zip(("one.jpg", jpeg_bytes())))

    assert process_album_import(refused.pk) is False
    assert process_album_import(worked.pk) is True

    assert not Notification.objects.exists()


@pytest.mark.django_db
def test_a_notification_that_cannot_be_sent_does_not_change_the_imports_outcome(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> None:
        raise RuntimeError("push service down")

    monkeypatch.setattr(notify, "import_failed", broken)
    album_import = _import(make_resident(), _zip(("notes.txt", b"not a photo")))

    assert process_album_import(album_import.pk) is False

    album_import.refresh_from_db()
    assert album_import.state == AlbumImportState.FAILED
