import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from residents.models import Resident, RoleAssignment, active_period


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "access_levels: accounts get their real access level, not the tests' default resident"
    )


@pytest.fixture(autouse=True)
def _accounts_live_here(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """An account a test makes is a resident (access.levels), as it was before the levels.

    make_resident puts nobody on the alumneliste, and a thousand tests that are about something else
    would otherwise each have to set up residency months. Saving an account works its level out
    (access.signals), with this rule. The kitchen account stays the kitchen.
    Tests about the levels themselves are marked `access_levels` and get the real rule.
    """
    if request.node.get_closest_marker("access_levels"):
        return
    from access import levels
    from access.policy import AccessLevel

    # What a person's residency months make of them (the kitchen, superusers, role holders and an
    # override are decided before this is asked).
    monkeypatch.setattr(levels, "from_months", lambda *_months: AccessLevel.RESIDENT)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every transactional test starts from the database's seeded contents (serialized rollback).

    A `transaction=True` test ends by flushing every table, which also deletes the rows data
    migrations seed (the 12 AK schedule months, Fotogruppen, the ølkælder warnings). Serialized
    rollback reloads them at the start of the next transactional test; it also makes pytest-django
    snapshot them at setup, which `_restore_seed_data_after_the_run` puts back at the end.
    """
    for item in items:
        marker = item.get_closest_marker("django_db")
        if marker and marker.kwargs.get("transaction") and not marker.kwargs.get("serialized_rollback"):
            item.add_marker(
                pytest.mark.django_db(*marker.args, **marker.kwargs, serialized_rollback=True), append=False
            )


@pytest.fixture(scope="session", autouse=True)
def _restore_seed_data_after_the_run(request: pytest.FixtureRequest) -> Iterator[None]:
    """Leave the test database as the run found it, for the next `--reuse-db` run.

    pytest-django runs transactional tests last, and the last one ends with a flush, so the kept
    database would otherwise start the next run without its seeded rows. A database already
    flushed by a run from before this fixture needs one run with `--create-db`.
    """
    transactional = any(
        (marker := item.get_closest_marker("django_db")) and marker.kwargs.get("transaction")
        for item in request.session.items
    )
    if not transactional:
        yield
        return
    request.getfixturevalue("django_db_setup")
    yield
    from django.db import connection

    contents = getattr(connection, "_test_serialized_contents", None)
    if contents:
        with request.getfixturevalue("django_db_blocker").unblock():
            connection.creation.deserialize_db_from_string(contents)


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(settings: object, tmp_path: Path) -> None:
    """Neutralise the developer's app/.env for the whole suite.

    config/settings.py calls `load_dotenv(BASE_DIR / ".env")`, so anything a developer puts there to
    exercise a real integration also reaches `task test`. That has already caused two different
    kinds of confusion, and neither announced itself as an environment problem:

      * S3_BUCKET — several tests upload files and several assert that deleting a row deletes the
        file, so the suite would write to and then delete objects in the PRODUCTION bucket.
      * GAHK_WEB_KEY — with a key set, every content write in a test would try to publish to the
        developer's gahk_web devserver (or gahk.dk itself), and sync tests would talk to it.
      * S3_PUBLIC_ENDPOINT_URL — a developer's `task dev` value (the S3 container's published port) would
        otherwise reach every test that builds its own MediaS3Storage/PhotoAlbumS3Storage with a
        different (fake) endpoint_url, making core.storage.PublicEndpointS3Storage presign against a
        host that has nothing to do with the test's own fixture bucket.

    Autouse and unconditional, because in every case the symptom points somewhere other than the
    cause, and no individual test should have to remember. Anything else added to .env that changes
    behaviour rather than merely configuring an address belongs here too.
    """
    settings.STORAGES = {  # type: ignore[attr-defined]
        **settings.STORAGES,  # type: ignore[attr-defined]
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    # Files a test saves go to its own directory, never the developer's app/media: that is the dev
    # data, and the Docker dev container leaves root-owned folders in it that tests cannot write to.
    settings.MEDIA_ROOT = tmp_path / "media"  # type: ignore[attr-defined]
    settings.GAHK_WEB_KEY = ""  # type: ignore[attr-defined]
    settings.S3_PUBLIC_ENDPOINT_URL = ""  # type: ignore[attr-defined]
    # A developer's cache directory would otherwise collect test files and serve them across tests;
    # the cache tests (test_photo_album_cache.py) turn it on with their own tmp_path.
    settings.PHOTO_ALBUM_CACHE_DIR = ""  # type: ignore[attr-defined]
    settings.MEDIA_CACHE_DIR = ""  # type: ignore[attr-defined]  # likewise; test_attachment_cache.py
    # Off by default under DEBUG, so a dev .env would decide whether the suite's alumneregninger reach
    # Django's test outbox; tests always take the real path (the test runner's locmem backend sends nothing).
    settings.ALUMNEREGNING_SEND_MAIL = True  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _mail_through_the_outbox(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every mail a test sends goes through core.mail's outbox, as in production: Django's test runner
    sets EMAIL_BACKEND to locmem, which would send it straight past. The outbox then delivers it there
    and then, through locmem (so `mail.outbox` has it), the way CELERY_TASK_ALWAYS_EAGER runs jobs;
    a test of the queue itself asks for `mail_stays_queued` instead."""
    from core import mail as outbox

    settings.EMAIL_BACKEND = "core.mail.OutboxBackend"  # type: ignore[attr-defined]
    settings.MAIL_DELIVERY_BACKEND = "django.core.mail.backends.locmem.EmailBackend"  # type: ignore[attr-defined]
    settings.MAIL_RATE_LIMIT = 1000  # type: ignore[attr-defined]  # no test sends that much bulk mail by accident
    monkeypatch.setattr(outbox, "_schedule_delivery", lambda mail_kind: outbox.deliver())


@pytest.fixture
def mail_stays_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queued mail waits to be delivered — by the test calling core.mail.deliver(), as deliver_mail
    would — instead of going out as it is queued (_mail_through_the_outbox)."""
    from core import mail as outbox

    monkeypatch.setattr(outbox, "_schedule_delivery", lambda mail_kind: None)


@pytest.fixture
def make_resident(db: None) -> Callable[[str, str, bool, tuple], Resident]:
    def _make(
        email: str = "r@gahk.dk",
        password: str = "hemmelig",
        legacy: bool = False,
        roles: tuple = (),
        **extra: object,
    ) -> Resident:
        r = Resident(
            email=email,
            first_name=extra.pop("first_name", "Test"),
            last_name=extra.pop("last_name", "Beboer"),
            **extra,
        )
        if legacy:
            r.password = "gahk_sha256$$" + hashlib.sha256(password.encode()).hexdigest()
        else:
            r.set_password(password)
        r.save()
        year, month = active_period()
        for role in roles:
            RoleAssignment.objects.create(resident=r, role=role, year=year, month=month)
        if roles:
            Resident.objects.filter(id=r.id).update(is_staff=True)
        return r

    return _make
