"""What happens after an import has committed: a job that cannot be queued must not undo it."""

import zipfile
from collections.abc import Callable
from io import BytesIO, StringIO
from typing import Any

import pytest
from django.core.files.base import File
from django.core.management import call_command

from photo_album import services, tasks
from photo_album.models import Album, DerivativeState, Media
from photo_album.services import import_zip_album, upload_media
from photo_album.storage import get_photo_album_storage
from residents.models import Resident

from .media_samples import image_upload, jpeg_bytes


def _archive(*names: str) -> File:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name in names:
            archive.writestr(name, jpeg_bytes())
    payload.seek(0)
    return File(payload, name="Fest.zip")


def _broker_down(media: Media) -> None:
    raise RuntimeError('prepared statement "_pg3_3" already exists')


@pytest.mark.django_db(transaction=True)
def test_a_job_that_cannot_be_queued_does_not_undo_a_committed_import(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callbacks that queue the derivative jobs run after the COMMIT. One raised — the broker's
    connection shared across a fork — and the import's cleanup, taking it for a failed import,
    deleted every original it had just committed: albums and rows in place, no files."""
    monkeypatch.setattr(tasks, "queue_derivatives", _broker_down)

    result = import_zip_album(
        archive=_archive("one.jpg", "sub/two.jpg"), folder="2006", resident=make_resident()
    )

    assert result.added == 2
    storage = get_photo_album_storage()
    for media in Media.objects.all():
        assert storage.exists(media.original.name)
        # Waiting for the nightly process_pending_media to queue what could not be queued now.
        assert media.derivative_state == DerivativeState.PENDING


@pytest.mark.django_db(transaction=True)
def test_an_error_after_the_commit_never_deletes_what_was_committed(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cleanup itself, for any callback that does raise: it only undoes an import that did not
    commit."""
    monkeypatch.setattr(tasks, "queue_derivatives", _broker_down)
    on_commit = services.transaction.on_commit

    def not_robust(func: Callable[[], Any], robust: bool = False) -> None:
        on_commit(func)

    monkeypatch.setattr(services.transaction, "on_commit", not_robust)

    with pytest.raises(RuntimeError, match="already exists"):
        import_zip_album(archive=_archive("one.jpg"), folder="2006", resident=make_resident())

    media = Media.objects.get()
    assert get_photo_album_storage().exists(media.original.name)


def test_a_forked_worker_process_gets_broker_connections_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """kombu caches its SQLAlchemy engines on the class, so a forked pool process inherited the main
    process's open connections, and the two collided on psycopg's prepared statements."""
    from celery.signals import worker_process_init
    from kombu.transport.sqlalchemy import Channel

    import config.celery  # noqa: F401 — connects the handler

    disposed: list[bool] = []

    class Engine:
        def dispose(self, close: bool = True) -> None:
            disposed.append(close)

    monkeypatch.setattr(Channel, "_engines", {"sqla+postgresql://broker": (Engine(), None)})

    worker_process_init.send(sender=None)

    # A fresh pool for this process; the parent's connections left open for the parent.
    assert disposed == [False]


@pytest.mark.django_db
def test_media_without_an_original_are_found_and_removed_with_the_albums_they_empty(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    storage = get_photo_album_storage()
    broken_root = Album.objects.create(folder="2006", name="Fest")
    broken_sub = Album.objects.create(folder="2006", name="Aften", parent=broken_root)
    mixed = Album.objects.create(folder="2006", name="Sommer")
    other_year = Album.objects.create(folder="2005", name="Fest")

    def upload(album: Album, name: str, *, lose_original: bool) -> Media:
        media = upload_media(album=album, uploaded_file=image_upload(name, jpeg_bytes()), resident=resident)
        if lose_original:
            storage.delete(media.original.name)
        return media

    upload(broken_root, "a.jpg", lose_original=True)
    upload(broken_sub, "b.jpg", lose_original=True)
    kept = upload(mixed, "c.jpg", lose_original=False)
    upload(mixed, "d.jpg", lose_original=True)
    elsewhere = upload(other_year, "e.jpg", lose_original=True)

    listed = StringIO()
    call_command("check_photo_album_originals", "--year", "2006", stdout=listed)
    assert "3 of 4 media item(s) have no original." in listed.getvalue()
    assert Media.objects.count() == 5

    call_command("check_photo_album_originals", "--year", "2006", "--delete", stdout=StringIO())

    assert set(Media.objects.values_list("pk", flat=True)) == {kept.pk, elsewhere.pk}
    # The tree that held nothing else is gone; the album with a good photo in it stays.
    assert set(Album.objects.values_list("name", "folder")) == {("Sommer", "2006"), ("Fest", "2005")}
