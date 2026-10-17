"""Where GAHKFlix's files live: bucket keys "gahkflix/<video uuid>/…", in the photo album's bucket.

The same storage class as the photo album (photo_album.storage.PhotoAlbumS3Storage) and for the same
reasons: no "media/" location of its own, and never a presigned URL rendered into a page. Every
file is reached through a view in gahkflix.views that re-checks who may see the video first, then
redirects to a short-lived signed URL (or, for the small images, serves a local disk copy).

One thing the photo album does not need: the browser uploads the ORIGINAL straight to the bucket,
through a presigned POST policy, as Arkiv does (arkiv/uploads.py explains why a multi-gigabyte
video cannot go through the app server). `presigned_post` and `head` are the two calls that takes.
Under pytest there is no bucket, and both return None so the views fall back to posting the file
through Django — the one path tests can cover.
"""

from typing import Any, cast

from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage, storages
from django.dispatch import receiver
from django.test.signals import setting_changed
from django.utils.functional import LazyObject, empty

from core.storage import MediaS3Storage
from photo_album.storage import PhotoAlbumS3Storage

# How long a signed URL for a stream stays good. Longer than the photo album's hour: the browser
# keeps reading a film through the URL it was redirected to, and a two-hour revy that outlives its
# signature fails mid-scene. The player also re-asks for a fresh one when a read is refused.
STREAM_URL_TTL = 6 * 60 * 60
# How long the browser has to START an upload. S3 checks the policy when the request arrives, so a
# slow upload that began in time still completes.
UPLOAD_POLICY_TTL = 60 * 60


def _build_storage() -> FileSystemStorage | PhotoAlbumS3Storage:
    """S3 exactly when STORAGES["default"] is (see photo_album.storage._build_storage)."""
    if isinstance(storages["default"], MediaS3Storage):
        return PhotoAlbumS3Storage(**settings.PHOTO_ALBUM_S3_OPTIONS)
    return FileSystemStorage(location=str(settings.MEDIA_ROOT), base_url="/")


class _ConfiguredVideoStorage(LazyObject):
    def _setup(self) -> None:
        self._wrapped: object = _build_storage()


video_storage = _ConfiguredVideoStorage()


def get_video_storage() -> Storage:
    """A callable for FileField(storage=…), so migrations store a reference rather than a backend."""
    return cast(Storage, video_storage)


def s3_storage() -> PhotoAlbumS3Storage | None:
    """The bucket-backed storage, or None under pytest. (A LazyObject proxies isinstance.)"""
    return (
        cast(PhotoAlbumS3Storage, video_storage) if isinstance(video_storage, PhotoAlbumS3Storage) else None
    )


def presigned_post(key: str, max_bytes: int, content_type: str) -> dict[str, Any] | None:
    """A form policy the browser POSTs the file to, or None when there is no bucket.

    `content-length-range` is what stops a forty-gigabyte upload before it is stored and billed;
    the commit step's HEAD then checks what actually arrived.
    """
    storage = s3_storage()
    if storage is None:
        return None
    fields: dict[str, Any] = {"key": key}
    conditions: list[Any] = [["content-length-range", 1, max_bytes], {"key": key}]
    if content_type:
        fields["Content-Type"] = content_type
        conditions.append({"Content-Type": content_type})
    client = storage.public_connection.meta.client
    return dict(
        client.generate_presigned_post(
            Bucket=storage.bucket_name,
            Key=key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=UPLOAD_POLICY_TTL,
        )
    )


def stored_size(key: str) -> int | None:
    """The size the storage itself reports for `key`, or None if it has nothing there."""
    storage = s3_storage()
    if storage is not None:
        from botocore.exceptions import ClientError

        try:
            meta = storage.connection.meta.client.head_object(Bucket=storage.bucket_name, Key=key)
        except ClientError:
            return None
        return int(meta["ContentLength"])
    local = get_video_storage()
    return local.size(key) if local.exists(key) else None


def signed_url(
    key: str, *, expire: int | None = None, parameters: dict[str, Any] | None = None
) -> str | None:
    """A presigned GET for `key`, or None when the bytes must be streamed instead (pytest)."""
    storage = s3_storage()
    if storage is None:
        return None
    return storage.signed_url(key, expire=expire, parameters=parameters)


@receiver(setting_changed)
def _reset_video_storage(*, setting: str, **kwargs: object) -> None:
    if setting == "STORAGES":
        video_storage._wrapped = empty
