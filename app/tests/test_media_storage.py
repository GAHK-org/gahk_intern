"""Media storage: the /media/ URL invariant, and the checks that refuse to let it be broken.

The whole S3 migration rests on one property — `storage.url(name)` keeps returning
`/media/<name>` — because MEDIA_URL is a prefix of content stored in the database:
cms.Page.background_image holds the URL string outright, CMS bodies embed <img src="/media/...">,
and opslag bodies embed the same in Markdown. Break it and nothing raises: live CMS images stop
resolving, and the next edit of an existing opslag releases its images for purge_notices to delete a
day later.

core/storage.py carries the full argument. These tests are the enforcement, together with the
round-trip pair in tests/test_markdown.py.
"""

from pathlib import Path

import pytest
from django.core.files.storage import FileSystemStorage

from core.checks import check_media_storage_url, check_media_url
from core.storage import MediaS3Storage


def s3_storage() -> MediaS3Storage:
    """Configured the way config/settings.py configures it, minus anything that would hit network.

    Constructing this makes no request; boto3 defers the client until a call needs one.
    """
    return MediaS3Storage(
        bucket_name="test-bucket",
        access_key="k",
        secret_key="s",
        endpoint_url="https://fsn1.your-objectstorage.com",
        region_name="fsn1",
        signature_version="s3v4",
        addressing_style="virtual",
    )


# --- the URL contract ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "opslag/2026/09/a.jpg",
        "profile_pictures/portraet.png",
        "cms/2019/03/logo.gif",
        "roomimages/2026/vaerelse_42.webp",
        "opslag/2026/09/Skærmbillede_2026-09-04.png",
    ],
)
def test_the_s3_backend_returns_the_same_url_as_the_filesystem_one(name: str) -> None:
    """Byte-identical, not merely "also under /media/".

    The two backends coexist — dev and CI run on FileSystemStorage, prod on S3 — and whichever one
    is active is what writes URLs into Notice bodies and Page.background_image. Any divergence makes
    those two environments produce content the other cannot resolve.
    """
    assert s3_storage().url(name) == FileSystemStorage().url(name)


def test_the_url_is_site_relative_and_carries_no_signature() -> None:
    """The failure this is here to catch is a presigned URL reaching stored content.

    A signature expires within the hour; written into a post body or into Page.background_image it
    is then wrong permanently, and there is no way to tell from the row that it ever worked.
    """
    url = s3_storage().url("opslag/2026/09/a.jpg")

    assert url == "/media/opslag/2026/09/a.jpg"
    assert "X-Amz" not in url
    assert "your-objectstorage.com" not in url


def test_url_ignores_the_presigning_arguments_it_accepts() -> None:
    """They exist to keep the Storage interface intact; honouring them would leak signatures."""
    storage = s3_storage()

    assert storage.url("a.jpg", expire=60) == storage.url("a.jpg") == "/media/a.jpg"


# --- the checks ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("media_url", "expected"),
    [
        ("/media/", []),
        ("/uploads/", ["core.E007"]),
        # The one that matters: the obvious way to serve media from a bucket, and the one that
        # silently unlinks every historic image.
        ("https://test-bucket.fsn1.your-objectstorage.com/", ["core.E007"]),
    ],
)
def test_media_url_must_stay_slash_media(settings: object, media_url: str, expected: list[str]) -> None:
    settings.MEDIA_URL = media_url  # type: ignore[attr-defined]

    assert [e.id for e in check_media_url(None)] == expected


def test_the_storage_check_passes_on_the_default_filesystem_backend() -> None:
    """Dev and CI configuration must not trip the check that guards prod."""
    assert check_media_storage_url(None) == []


def test_the_storage_check_rejects_a_backend_that_returns_bucket_urls(settings: object) -> None:
    """core.E008 catches what core.E007 cannot: MEDIA_URL correct, but STORAGES['default'] pointed
    at plain storages.backends.s3.S3Storage instead of core.storage.MediaS3Storage."""
    settings.STORAGES = {  # type: ignore[attr-defined]
        **settings.STORAGES,  # type: ignore[attr-defined]
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": "test-bucket",
                "access_key": "k",
                "secret_key": "s",
                "endpoint_url": "https://fsn1.your-objectstorage.com",
                "region_name": "fsn1",
                "signature_version": "s3v4",
                "addressing_style": "virtual",
            },
        },
    }

    errors = check_media_storage_url(None)

    assert [e.id for e in errors] == ["core.E008"]
    assert "MediaS3Storage" in (errors[0].hint or "")


def test_the_storage_check_accepts_our_own_backend(settings: object) -> None:
    settings.STORAGES = {  # type: ignore[attr-defined]
        **settings.STORAGES,  # type: ignore[attr-defined]
        "default": {
            "BACKEND": "core.storage.MediaS3Storage",
            "OPTIONS": {
                "bucket_name": "test-bucket",
                "access_key": "k",
                "secret_key": "s",
                "endpoint_url": "https://fsn1.your-objectstorage.com",
                "region_name": "fsn1",
                "signature_version": "s3v4",
                "addressing_style": "virtual",
            },
        },
    }

    assert check_media_storage_url(None) == []


# --- the /media/ route ----------------------------------------------------------------------------
#
# The filesystem branch is covered by tests/test_features.py::test_media_files_are_served_in_prod,
# which predates this module and is deliberately left untouched: it is the parity oracle proving the
# route swap changed nothing for the environment CI actually runs in.

S3_STORAGES_SETTING = {
    "BACKEND": "core.storage.MediaS3Storage",
    "OPTIONS": {
        "bucket_name": "test-bucket",
        "access_key": "k",
        "secret_key": "s",
        "endpoint_url": "https://fsn1.your-objectstorage.com",
        "region_name": "fsn1",
        "signature_version": "s3v4",
        "addressing_style": "virtual",
    },
}


@pytest.fixture
def on_s3(settings: object) -> None:
    """Point STORAGES["default"] at the bucket backend, overriding the autouse safety fixture.

    Nothing here reaches the network: presigning is local HMAC over the credentials above.
    """
    settings.STORAGES = {  # type: ignore[attr-defined]
        **settings.STORAGES,  # type: ignore[attr-defined]
        "default": S3_STORAGES_SETTING,
    }


@pytest.fixture
def resident_client(make_resident: object) -> object:
    """A logged-in client. Everything outside PUBLIC_PREFIXES needs one now — see the gate tests."""
    from django.test import Client

    client = Client()
    client.force_login(make_resident(email="beboer@gahk.dk"))  # type: ignore[operator,arg-type]
    return client


@pytest.mark.django_db
def test_media_redirects_to_a_presigned_url_when_the_bytes_are_in_the_bucket(
    on_s3: None, resident_client: object
) -> None:
    response = resident_client.get("/media/opslag/2026/09/a.jpg")  # type: ignore[attr-defined]

    assert response.status_code == 302
    target = response.headers["Location"]
    assert target.startswith("https://test-bucket.fsn1.your-objectstorage.com/")
    assert "X-Amz-Signature" in target


@pytest.mark.django_db
def test_the_redirect_is_cacheable_but_not_by_a_shared_cache(on_s3: None, resident_client: object) -> None:
    """`private` keeps one resident's signature out of another's response, and the max-age must
    stay below the signature's own lifetime or a cached 302 outlives the URL it names."""
    from core.media import PRESIGN_TTL, REDIRECT_MAX_AGE

    response = resident_client.get("/media/opslag/2026/09/a.jpg")  # type: ignore[attr-defined]

    assert response.headers["Cache-Control"] == f"private, max-age={REDIRECT_MAX_AGE}"
    assert response.headers["Vary"] == "Cookie"
    assert REDIRECT_MAX_AGE < PRESIGN_TTL, "a cached redirect must never outlive its signature"


@pytest.mark.django_db
def test_a_traversal_attempt_can_never_name_a_key_outside_the_media_prefix(
    on_s3: None, resident_client: object
) -> None:
    """Two guards in series, and this pins the outcome rather than which one fired.

    `_clean` collapses the dots first, so `../../etc/passwd` becomes the ordinary (nonexistent) name
    `etc/passwd` — which means the storage's own safe_join guard is now unreachable from a URL and
    survives only as defence in depth. What must hold either way is that no request can produce a
    signature for a key outside `media/`, which is what keeps the database backups in the same
    bucket out of reach.
    """
    response = resident_client.get("/media/../../etc/passwd")  # type: ignore[attr-defined]

    assert response.status_code in (302, 404)
    if response.status_code == 302:
        target = response.headers["Location"]
        assert "/media/etc/passwd?" in target
        assert "/etc/passwd?" not in target.replace("/media/etc/passwd?", "")


@pytest.mark.django_db
def test_a_missing_file_on_the_filesystem_backend_is_a_404(resident_client: object) -> None:
    """The autouse fixture keeps us on FileSystemStorage here — the pre-migration branch."""
    response = resident_client.get(  # type: ignore[attr-defined]
        "/media/opslag/2026/09/definitely-not-there.jpg"
    )

    assert response.status_code == 404


# --- the key prefix (core.E009) -------------------------------------------------------------------
#
# An empty `location` is not untidiness. django-storages resolves names with
# safe_join(location, name): with a prefix, climbing out of it raises; with none there is nothing to
# climb out of, so safe_join silently collapses the dots. /media/ is public, so that turns this view
# into an unauthenticated read primitive for every key in the bucket.


def test_the_prefix_is_applied_even_when_options_omit_it() -> None:
    """MediaS3Storage defaults `location`, so forgetting it in OPTIONS is not a security hole."""
    from core.storage import MEDIA_PREFIX

    assert s3_storage().location == MEDIA_PREFIX


def test_climbing_out_of_the_prefix_raises_rather_than_resolving() -> None:
    """The property the whole shared-bucket decision rests on: media and the database backups live
    in one bucket, so `/media/../backups/...` must not resolve."""
    from django.core.exceptions import SuspiciousOperation

    storage = s3_storage()

    assert storage._normalize_name("opslag/a.jpg") == "media/opslag/a.jpg"
    with pytest.raises(SuspiciousOperation):
        storage._normalize_name("../backups/db/2026-09-04.dump")


def test_an_explicitly_emptied_prefix_is_a_check_error(settings: object) -> None:
    """A class default cannot stop `"location": ""` in OPTIONS; core.E009 can."""
    from core.checks import check_media_storage_prefix

    settings.STORAGES = {  # type: ignore[attr-defined]
        **settings.STORAGES,  # type: ignore[attr-defined]
        "default": {
            **S3_STORAGES_SETTING,
            "OPTIONS": {**S3_STORAGES_SETTING["OPTIONS"], "location": ""},
        },
    }

    assert [e.id for e in check_media_storage_prefix(None)] == ["core.E009"]


def test_the_prefix_check_is_silent_on_the_filesystem_backend() -> None:
    """FileSystemStorage confines itself to MEDIA_ROOT; it has no prefix concept to get wrong."""
    from core.checks import check_media_storage_prefix

    assert check_media_storage_prefix(None) == []


# --- deletion waits for the commit (core.files) ----------------------------------------------------
#
# Neither of these may use `django_db(transaction=True)`. It would work, but TransactionTestCase
# semantics truncate the tables afterwards WITHOUT restoring data created by migrations, so the next
# test to rely on a migration-seeded row (tests/test_ak.py's twelve-month schedule,
# tests/test_oelkaelder_admin.py's warning) fails depending on the random order pytest picks. The
# deferral is observable without it.


@pytest.mark.django_db
def test_a_rolled_back_delete_leaves_the_file_alone(tmp_path: object, settings: object) -> None:
    """The reason core.files defers to transaction.on_commit.

    post_delete fires *inside* the transaction Django wraps around delete(). If an enclosing atomic
    block then rolls back, the row comes back — and with an immediate unlink its file is already
    gone, leaving a live row pointing at nothing. Against a local disk that window is microseconds;
    against a bucket it is a network round trip, and opslagstavle, residents and rooms all delete
    inside atomic blocks.

    The assertion INSIDE the block is the one that pins the behaviour down: an implementation that
    unlinks in the receiver fails there, before the rollback is even reached.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.db import transaction

    from cms.models import CmsImage

    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    image = CmsImage.objects.create(file=SimpleUploadedFile("x.png", b"pngbytes", "image/png"))
    stored = tmp_path / image.file.name  # type: ignore[operator]
    assert stored.is_file()

    class Rollback(Exception):
        pass

    with pytest.raises(Rollback), transaction.atomic():
        CmsImage.objects.filter(pk=image.pk).delete()
        assert stored.is_file(), "the file went before the delete was committed"
        raise Rollback

    assert CmsImage.objects.filter(pk=image.pk).exists(), "the row should have come back"
    assert stored.is_file(), "the file was deleted for a row that still exists"


@pytest.mark.django_db
def test_a_storage_failure_is_logged_and_does_not_break_the_delete(
    tmp_path: object,
    settings: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    django_capture_on_commit_callbacks: object,
) -> None:
    """A DELETE over the network routinely fails, and there is no useful recovery: the row is gone
    and the transaction has committed. Raising would turn a Hetzner blip into a 500 on the Den
    Hurtige feed, which purges expired posts on every load. The object leaks instead, and
    audit_media is what finds leaks."""
    import logging

    from django.core.files.storage import FileSystemStorage
    from django.core.files.uploadedfile import SimpleUploadedFile

    from cms.models import CmsImage

    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    image = CmsImage.objects.create(file=SimpleUploadedFile("x.png", b"pngbytes", "image/png"))

    def boom(self: object, name: str) -> None:
        raise OSError("bucket unreachable")

    monkeypatch.setattr(FileSystemStorage, "delete", boom)

    with caplog.at_level(logging.WARNING, logger="core.files"):
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            image.delete()

    assert not CmsImage.objects.filter(pk=image.pk).exists()
    assert "could not delete" in caplog.text


# --- the authentication gate ----------------------------------------------------------------------
#
# /media/ used to be public by URL, deliberately, as the legacy /public/ images were. It is not any
# more: everything except the cms/ prefix now needs a session. cms/ is the exception because the
# CMS toolbar's uploads are embedded in Page/NewsItem/Event bodies that the logged-out front page
# renders — see PUBLIC_PREFIXES for how that list was derived.

GATED = [
    "profile_pictures/IMG_1234.jpg",
    "roomimages/2026/vaerelse.jpg",
    "opslag/2026/09/fest.jpg",
    "quick_posts/2026/09/kaffe.jpg",
    "quick_comments/2026/09/svar.jpg",
    "begivenheder/2026/09/plakat.jpg",
    "oel/tuborg.png",
    "public/image/intern/roomimages/112/skab/image.jpg",
]


def store(media_tmp: Path, name: str) -> None:
    path = media_tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"pngbytes")


@pytest.fixture
def media_tmp(settings: object, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path  # type: ignore[attr-defined]
    return tmp_path


@pytest.mark.django_db
@pytest.mark.parametrize("name", GATED)
def test_a_logged_out_stranger_cannot_read_internal_media(media_tmp: Path, name: str) -> None:
    """The behaviour this change exists for. Names like profile_pictures/IMG_1234.jpg carry no date
    and no random suffix, so they are guessable rather than merely obscure."""
    from django.test import Client

    store(media_tmp, name)

    response = Client().get(f"/media/{name}")

    assert response.status_code == 302
    assert response.headers["Location"].startswith(settings_login_url())
    assert "Cache-Control" not in response.headers, "an anonymous redirect must never be cached"


@pytest.mark.django_db
@pytest.mark.parametrize("name", GATED)
def test_a_logged_in_resident_can_read_internal_media(
    media_tmp: Path, make_resident: object, name: str
) -> None:
    from django.test import Client

    store(media_tmp, name)
    resident = make_resident(email="beboer@gahk.dk")  # type: ignore[operator]
    client = Client()
    client.force_login(resident)  # type: ignore[arg-type]

    response = client.get(f"/media/{name}")

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"pngbytes"


@pytest.mark.django_db
def test_cms_images_stay_readable_without_a_session(media_tmp: Path) -> None:
    """The public site embeds these in Page/NewsItem/Event bodies. Gating them blanks the front
    page, which is why PUBLIC_PREFIXES exists at all."""
    from django.test import Client

    store(media_tmp, "cms/2026/08/logo.jpg")

    response = Client().get("/media/cms/2026/08/logo.jpg")

    assert response.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize(
    "attempt",
    [
        "/media/cms/../profile_pictures/IMG_1234.jpg",
        "/media/cms/./../profile_pictures/IMG_1234.jpg",
        "/media/cms/../../media/profile_pictures/IMG_1234.jpg",
    ],
)
def test_the_public_prefix_cannot_be_used_to_reach_a_gated_one(media_tmp: Path, attempt: str) -> None:
    """THE BYPASS. A prefix check on the raw path would pass every one of these: the string starts
    with "cms/", and collapsing the dots lands back INSIDE the media root, so the storage's own
    safe_join guard never fires either. Normalising first is what makes the check mean anything."""
    from django.test import Client

    store(media_tmp, "profile_pictures/IMG_1234.jpg")

    response = Client().get(attempt)

    assert response.status_code in (302, 404), "a gated file was served to an anonymous client"
    if response.status_code == 302:
        assert response.headers["Location"].startswith(settings_login_url())


@pytest.mark.django_db
def test_a_backslash_path_is_refused(media_tmp: Path) -> None:
    """posixpath does not treat "\\" as a separator but a filesystem backend on Windows does, so it
    is a second spelling of the same trick."""
    from django.test import Client

    assert Client().get(r"/media/cms\..\profile_pictures/x.jpg").status_code == 404


def settings_login_url() -> str:
    from django.conf import settings

    return settings.LOGIN_URL


# --- the production backend guard (core.E010) -------------------------------------------------------
#
# S3_BUCKET being the only switch is what made the migration safe: unset it and the app was exactly
# as before. That stopped being true the day the prod `media` volume was emptied — now an unset
# bucket serves nothing and writes uploads to a disk nobody backs up, silently, because falling back
# is what the setting is designed to do.


@pytest.mark.parametrize(
    ("debug", "bucket", "allow", "expected"),
    [
        (True, "", False, []),  # dev and CI: the normal case, never nagged
        (False, "gahk", False, []),  # production as it now runs
        (False, "", True, []),  # staging, or rehearsing the rollback, said out loud
        (False, "", False, ["core.E010"]),  # the silent disaster
    ],
)
def test_production_refuses_to_fall_back_to_local_media(
    settings: object, debug: bool, bucket: str, allow: bool, expected: list[str]
) -> None:
    from core.checks import check_media_backend_in_production

    settings.DEBUG = debug  # type: ignore[attr-defined]
    settings.S3_BUCKET = bucket  # type: ignore[attr-defined]
    settings.ALLOW_LOCAL_MEDIA = allow  # type: ignore[attr-defined]

    assert [e.id for e in check_media_backend_in_production(None)] == expected


def test_the_guard_names_the_escape_hatch(settings: object) -> None:
    """A check that blocks a deploy has to say how to proceed, or the next person sets DEBUG=1."""
    from core.checks import check_media_backend_in_production

    settings.DEBUG = False  # type: ignore[attr-defined]
    settings.S3_BUCKET = ""  # type: ignore[attr-defined]
    settings.ALLOW_LOCAL_MEDIA = False  # type: ignore[attr-defined]

    hint = check_media_backend_in_production(None)[0].hint or ""

    assert "ALLOW_LOCAL_MEDIA=1" in hint
    assert "S3_BUCKET" in hint


# --- conditional GET (the dev-server regression) ----------------------------------------------------
#
# django.views.static.serve - which core.media.serve_media replaced - answered If-Modified-Since with
# a 304 and set Last-Modified. The first version of serve_media did neither, and the symptom was not
# an error: every image on a page re-downloaded in full on every load. On alumnelisten that is ~124
# profile pictures of up to a megabyte each through a single-threaded dev server, which reads as
# "the site got slow" rather than as a caching bug.


@pytest.mark.django_db
def test_a_served_file_carries_last_modified(media_tmp: Path) -> None:
    from django.test import Client

    store(media_tmp, "cms/logo.jpg")

    response = Client().get("/media/cms/logo.jpg")

    assert response.status_code == 200
    assert response.headers.get("Last-Modified"), "without this the browser cannot revalidate"


@pytest.mark.django_db
def test_an_unchanged_file_revalidates_to_304(media_tmp: Path) -> None:
    """The assertion that would have caught it: a 200 here is a full re-send."""
    from django.test import Client
    from django.utils.http import http_date

    store(media_tmp, "cms/logo.jpg")
    mtime = (media_tmp / "cms" / "logo.jpg").stat().st_mtime

    response = Client().get("/media/cms/logo.jpg", HTTP_IF_MODIFIED_SINCE=http_date(mtime))

    assert response.status_code == 304


@pytest.mark.django_db
def test_a_modified_file_is_sent_again(media_tmp: Path) -> None:
    """No max-age, deliberately: an image replaced in place during development must not be stale."""
    from django.test import Client
    from django.utils.http import http_date

    store(media_tmp, "cms/logo.jpg")
    long_ago = http_date(0)

    response = Client().get("/media/cms/logo.jpg", HTTP_IF_MODIFIED_SINCE=long_ago)

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"pngbytes"
