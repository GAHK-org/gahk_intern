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

import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def _range_reader(key: str) -> tuple[int, Callable[[int, int], Iterator[bytes]]]:
    """The size of `key`, and a way to read bytes `start`–`end` of it, from whichever storage holds it."""
    storage = s3_storage()
    if storage is not None:
        client = storage.connection.meta.client  # a botocore client may be shared between threads
        bucket = storage.bucket_name
        size = int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])

        def read_s3(start: int, end: int) -> Iterator[bytes]:
            body = client.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end}")["Body"]
            try:
                yield from body.iter_chunks(1024 * 1024)
            finally:
                body.close()  # back to the pool, or it runs dry after a few dozen

        return size, read_s3
    path = get_video_storage().path(key)
    with open(path, "rb") as handle:  # noqa: PTH123
        size = handle.seek(0, 2)

    def read_local(start: int, end: int) -> Iterator[bytes]:
        with open(path, "rb") as handle:  # noqa: PTH123
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0 and (chunk := handle.read(min(1024 * 1024, remaining))):
                remaining -= len(chunk)
                yield chunk

    return size, read_local


_RANGE = re.compile(r"bytes=(\d+)-(\d*)")


@contextmanager
def loopback_url(key: str) -> Iterator[str]:
    """A http://127.0.0.1 address serving the stored file `key`, byte ranges and all, while in use.

    For ffmpeg, which seeks with range requests and so reads only the parts of a film it needs. Not
    a signed URL to the bucket: the static ffmpeg binary imageio-ffmpeg ships cannot resolve host
    names (a static build has no NSS), so it reaches neither `s3` in `task dev` nor the bucket's
    host in production — "Failed to resolve hostname". The loopback needs no resolving, and this
    process fetches each range with boto3, which can.
    """
    size, read = _range_reader(key)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            match = _RANGE.fullmatch(self.headers.get("Range", "").strip())
            start = int(match[1]) if match else 0
            end = min(int(match[2]), size - 1) if match and match[2] else size - 1
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206 if match else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if match:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            chunks = read(start, end)
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # ffmpeg had what it needed and hung up, as it does on every seek
            finally:
                chunks.close()  # type: ignore[attr-defined]

        def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="gahkflix-loopback", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/video.mp4"
    finally:
        server.shutdown()
        server.server_close()


@receiver(setting_changed)
def _reset_video_storage(*, setting: str, **kwargs: object) -> None:
    if setting == "STORAGES":
        video_storage._wrapped = empty
