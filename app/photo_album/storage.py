"""Where photo-album originals/derivatives live: bucket key "photo-album/…", never "media/…".

Deliberately its own storage rather than `STORAGES["default"]`. `core.storage.MediaS3Storage` is
pinned to `location="media"` and its `.url()` deliberately returns a Django-owned `/media/<name>`
path — because for THAT storage, the URL is a database value: cms.Page.background_image, CMS bodies
and opslag Markdown all embed `/media/...` directly, so it can never point straight at the bucket
(core/storage.py has the full argument).

None of that applies to a photo-album item — nothing here is written into another row's body or
CharField. But `.url()` still does NOT return a presigned bucket URL directly: that URL is a bearer
token, good for an hour with no further check, and photo_album.access has a real rule a bare token
can't enforce (`visible_media`: a PENDING upload is visible only to its uploader and Fotogruppen). A
copied or logged link would leak it to anyone for as long as the signature lives.

So `.url()` returns a site-relative `/fotoalbum-media/<name>` path instead, resolved by
`photo_album.views.serve_media` — same shape as `core.media.serve_media`, but re-checking
`photo_album.access`'s own per-item rule on every request instead of just "is logged in", and
redirecting to a freshly presigned URL from `signed_url()` below rather than streaming. Falls back
to local disk only because the test suite does: tests/conftest.py's autouse fixture overrides
`STORAGES["default"]` to plain FileSystemStorage for the whole suite so pytest never touches a real
bucket, and this follows that same signal (see `_build_storage` below) rather than keeping its own
copy of the guard. There is no supported way to run this app for real — dev, CI outside pytest,
staging, production — without S3_BUCKET configured.
"""

from typing import Any, cast
from urllib.parse import urljoin

from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage, storages
from django.dispatch import receiver
from django.test.signals import setting_changed
from django.utils.encoding import filepath_to_uri
from django.utils.functional import LazyObject, empty
from storages.utils import clean_name

from core.storage import MediaS3Storage, PublicEndpointS3Storage

# The URL prefix photo_album.views.serve_media is mounted at (config/urls.py) and the local-disk
# fallback's base_url both use this, so the two backends produce identical URL shapes — the same
# invariant core.storage.MediaS3Storage keeps for "media", just for a route the test suite also hits.
MEDIA_URL_PREFIX = "fotoalbum-media"


class PhotoAlbumS3Storage(PublicEndpointS3Storage):
    """S3 for the bytes; `.url()` is a site-relative path, `signed_url()` the real bucket URL.

    See the module docstring for why `.url()` does not return the presigned URL directly here.
    """

    def url(
        self,
        name: str,
        parameters: dict[str, Any] | None = None,
        expire: int | None = None,
        http_method: str | None = None,
    ) -> str:
        """`/fotoalbum-media/<name>`, reproducing FileSystemStorage.url() — see MediaS3Storage.url()
        for why this has to quote with filepath_to_uri rather than an f-string."""
        url = filepath_to_uri(name)
        if url is not None:
            url = url.lstrip("/")
        return urljoin(f"/{MEDIA_URL_PREFIX}/", url)

    def signed_url(
        self, name: str, expire: int | None = None, parameters: dict[str, Any] | None = None
    ) -> str:
        """The real, presigned bucket URL. Only `photo_album.views.serve_media` and
        `download_original` may call this — anywhere else is a bearer token leaking into stored
        content or a log line, exactly what `.url()` above exists to avoid.
        """
        normalized = self._normalize_name(clean_name(name))
        params = dict(parameters) if parameters else {}
        params["Bucket"] = self.bucket_name
        params["Key"] = normalized
        if expire is None:
            expire = self.querystring_expire
        return self.public_connection.meta.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expire
        )


def _build_storage() -> FileSystemStorage | PhotoAlbumS3Storage:
    """S3 exactly when `STORAGES["default"]` is too — see the module docstring.

    Piggy-backing on that decision (rather than reading settings.S3_BUCKET directly) is what makes
    tests safe for free: tests/conftest.py forces STORAGES["default"] to FileSystemStorage for the
    whole suite so a developer's real bucket in app/.env is never touched, and this follows it there
    without needing its own copy of that guard. Outside the test suite STORAGES["default"] is always
    core.storage.MediaS3Storage (config/settings.py has no local-disk fallback of its own any more),
    so this only ever resolves to FileSystemStorage under pytest.
    """
    if isinstance(storages["default"], MediaS3Storage):
        return PhotoAlbumS3Storage(**settings.PHOTO_ALBUM_S3_OPTIONS)
    return FileSystemStorage(location=str(settings.MEDIA_ROOT), base_url=f"/{MEDIA_URL_PREFIX}/")


class _ConfiguredPhotoAlbumStorage(LazyObject):
    """The same trick `django.core.files.storage.default_storage` plays, for this second storage.

    `photo_album.models.Media`'s FileFields hold a permanent reference to THIS object (not to
    whatever `_build_storage()` returns), so overriding STORAGES in a test invalidates it the same
    way `_reset` below does — see the settings_changed receiver underneath.
    """

    def _setup(self) -> None:
        self._wrapped: object = _build_storage()


photo_album_storage = _ConfiguredPhotoAlbumStorage()


def get_photo_album_storage() -> Storage:
    """What `models.Media`'s FileFields actually pass as `storage=`.

    A callable, not `photo_album_storage` itself: `FileField.deconstruct()` special-cases a callable
    storage by serializing a REFERENCE to it, exactly like it does for `default_storage`. Passing
    the LazyObject directly has no such exemption, so `makemigrations` would resolve it there and
    then — baking whichever backend and credentials happen to be configured on the machine that ran
    it straight into a migration file. Always returns the same object, so this changes nothing about
    the dynamic behaviour described above; it only changes what deconstruct() sees.

    Typed as `Storage`, not `_ConfiguredPhotoAlbumStorage`: that is what FileField's `storage=`
    expects, and it is true at runtime — a LazyObject proxies every attribute access to whichever
    concrete Storage `_build_storage()` returned — even though it is not the literal class mypy can
    see through.
    """
    return cast(Storage, photo_album_storage)


@receiver(setting_changed)
def _reset_photo_album_storage(*, setting: str, **kwargs: object) -> None:
    """Mirrors django.test.signals.storages_changed, which only knows about `default_storage`."""
    if setting == "STORAGES":
        photo_album_storage._wrapped = empty
