"""Where photo-album originals/derivatives live: bucket key "photo-album/…", never "media/…".

Deliberately its own storage rather than `STORAGES["default"]`. `core.storage.MediaS3Storage` is
pinned to `location="media"` and its `.url()` deliberately returns a Django-owned `/media/<name>`
path — because for THAT storage, the URL is a database value: cms.Page.background_image, CMS bodies
and opslag Markdown all embed `/media/...` directly, so it can never point straight at the bucket
(core/storage.py has the full argument).

None of that applies to a photo-album item. Nothing here is written into another row's body or
CharField — a media item's URL is read fresh out of the database (via the FileField) on every
request, never copied into stored HTML or Markdown. So `.url()` below returns a presigned bucket URL
directly, with no Django hop at all: no `/media/` redirect, and — the reason it matters for this
app in particular — no request of gunicorn's ever streams a photo, still less a hundreds-of-MB video,
through this process on its way from S3 to a browser.

Falls back to local disk when there is no bucket (dev/CI, mirroring `STORAGES["default"]`'s own
fallback and switched by the exact same signal: whether `storages["default"]` is
`core.storage.MediaS3Storage`). Same physical files as before — still under
`MEDIA_ROOT/photo-album/…` — served at `/photo-album/…` by `photo_album.views.serve_local_media`
instead of through `/media/`.
"""

from typing import Any, cast

from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage, storages
from django.dispatch import receiver
from django.test.signals import setting_changed
from django.utils.functional import LazyObject, empty
from storages.utils import clean_name

from core.storage import MediaS3Storage, PublicEndpointS3Storage


class PhotoAlbumS3Storage(PublicEndpointS3Storage):
    """S3 for the bytes; `.url()` is always a presigned bucket URL, never a site-relative path.

    See the module docstring for why that is safe here and is not for `core.storage.MediaS3Storage`.
    """

    def url(
        self,
        name: str,
        parameters: dict[str, Any] | None = None,
        expire: int | None = None,
        http_method: str | None = None,
    ) -> str:
        normalized = self._normalize_name(clean_name(name))
        params = dict(parameters) if parameters else {}
        params["Bucket"] = self.bucket_name
        params["Key"] = normalized
        if expire is None:
            expire = self.querystring_expire
        return self.public_connection.meta.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expire, HttpMethod=http_method
        )


def _build_storage() -> FileSystemStorage | PhotoAlbumS3Storage:
    """S3 exactly when `STORAGES["default"]` is too — see the module docstring.

    Piggy-backing on that decision (rather than reading settings.S3_BUCKET directly) is what makes
    tests safe for free: tests/conftest.py forces STORAGES["default"] to FileSystemStorage for the
    whole suite so a developer's real bucket in app/.env is never touched, and this follows it there
    without needing its own copy of that guard.
    """
    if isinstance(storages["default"], MediaS3Storage):
        return PhotoAlbumS3Storage(**settings.PHOTO_ALBUM_S3_OPTIONS)
    return FileSystemStorage(location=str(settings.MEDIA_ROOT), base_url="/")


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
