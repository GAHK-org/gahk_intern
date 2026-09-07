"""Reaching the archive's objects, which the media storage deliberately cannot do.

`STORAGES["default"]` is `core.storage.MediaS3Storage`, pinned to `location="media"`. That prefix is
a security boundary, not a namespace: it is what makes `/media/../backups/...` raise instead of
resolving, and what keeps one bucket safe to share with the database dumps (DEPLOY.md 4d). So Arkiv
must not borrow it - a storage that could reach `arkiv/` could reach `backups/` too, and the whole
argument for the prefix would be gone.

Arkiv also has no FileField to hang a Storage on. Keys are derived from a hash, never from a name,
so Django's Storage API buys nothing here and would mostly get in the way. What is actually needed
is four operations, and this module is those four against two backends.

**Downloads always route through a Django view**, never a presigned URL in the page. That differs
from the media path, and the reason is what Arkiv holds: a presigned URL to a Regnskabsgruppen
document is a bearer token for that document, valid for its lifetime and forwardable to anyone. For
opslag images that is an acceptable trade for cheap caching; for a folder whose entire promise is
"only this embedsgruppe", it is not. The view re-checks access on every request, and the presigned
URL it redirects to is short-lived and never rendered into HTML.
"""

import shutil
from pathlib import Path
from typing import BinaryIO, Protocol

from django.conf import settings
from django.core.files.storage import storages

from core.storage import MediaS3Storage

# Short. The URL is a redirect target, followed immediately by the browser that asked for it, so it
# only has to survive one hop - unlike the media redirects, which are cached for 15 minutes.
DOWNLOAD_TTL = 300


class ArchiveStore(Protocol):
    """The four things Arkiv needs from a bucket."""

    def exists(self, key: str) -> bool: ...

    def save(self, key: str, fileobj: BinaryIO) -> None: ...

    def delete(self, key: str) -> None: ...

    def head(self, key: str) -> tuple[int, str] | None:
        """`(size, content_type)` from the store itself, or None. The commit step's real check."""
        ...

    def list_keys(self, prefix: str) -> set[str]:
        """Every key under `prefix`. One listing instead of a HEAD per object."""
        ...

    def download_url(self, key: str, *, filename: str, content_type: str) -> str | None:
        """A short-lived URL to redirect to, or None when the bytes must be streamed instead."""
        ...


class S3ArchiveStore:
    """Production. Talks to the bucket directly, with no location prefix of its own.

    Keys already carry `arkiv/`, so nothing here prepends anything - which also means nothing here
    can be tricked into reading `media/` or `backups/` by a crafted key, since every key it is given
    is built by `models.object_key` out of a 64-character hash.
    """

    def __init__(self, storage: MediaS3Storage) -> None:
        self._bucket = storage.bucket
        self._client = storage.connection.meta.client
        self._bucket_name = storage.bucket_name

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket_name, Key=key)
        except ClientError:
            return False
        return True

    def save(self, key: str, fileobj: BinaryIO) -> None:
        self._bucket.upload_fileobj(fileobj, key)

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket_name, Key=key)

    def head(self, key: str) -> tuple[int, str] | None:
        from botocore.exceptions import ClientError

        try:
            meta = self._client.head_object(Bucket=self._bucket_name, Key=key)
        except ClientError:
            return None
        return int(meta["ContentLength"]), str(meta.get("ContentType", ""))

    def list_keys(self, prefix: str) -> set[str]:
        """Every key under `prefix`, paginated by boto3's collection manager.

        For the Dropbox import this replaces one HEAD per file with one listing per run: at a few
        hundred thousand objects that is the difference between hours of round trips and a couple of
        minutes. S3 charges per request, so it is cheaper in money too.
        """
        return {obj.key for obj in self._bucket.objects.filter(Prefix=prefix)}

    def presigned_post(
        self,
        key: str,
        *,
        fields: dict[str, object],
        conditions: list[object],
        expires_in: int,
    ) -> dict[str, object]:
        """A form policy the browser POSTs the file to.

        POST rather than a presigned PUT, for one reason: only POST carries
        `content-length-range`, so the size limit is enforced by Hetzner before the bytes are
        accepted rather than by us after they are already stored and billed.
        """
        return self._client.generate_presigned_post(
            Bucket=self._bucket_name,
            Key=key,
            Fields=dict(fields),
            Conditions=list(conditions),
            ExpiresIn=expires_in,
        )

    def download_url(self, key: str, *, filename: str, content_type: str) -> str:
        """Presigned GET that hands back the resident's filename.

        The key is a hash with no extension, so without ResponseContentDisposition the browser would
        save `9f86d081...` with no idea what it is. Quoting the filename matters: Danish names have
        spaces and aeoeaa in them.
        """
        params = {
            "Bucket": self._bucket_name,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{_ascii_fallback(filename)}"; '
            f"filename*=UTF-8''{_rfc5987(filename)}",
        }
        if content_type:
            params["ResponseContentType"] = content_type
        return self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=DOWNLOAD_TTL)


class LocalArchiveStore:
    """Dev and CI, where there are no credentials and no bucket.

    Objects live under MEDIA_ROOT/arkiv/... so `task dev` can exercise the whole feature offline.
    `download_url` returns None, which tells the view to stream the file instead of redirecting -
    the same two-branch shape as core.media.serve_media, and for the same reason: a code path that
    only works in production is a code path no test covers.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        return self._root / key

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def save(self, key: str, fileobj: BinaryIO) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as out:
            shutil.copyfileobj(fileobj, out)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def head(self, key: str) -> tuple[int, str] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        # No stored content type on disk; the caller falls back to the name-derived guess.
        return path.stat().st_size, ""

    def list_keys(self, prefix: str) -> set[str]:
        base = self._root / prefix
        if not base.is_dir():
            return set()
        return {p.relative_to(self._root).as_posix() for p in base.rglob("*") if p.is_file()}

    def download_url(self, key: str, *, filename: str, content_type: str) -> None:
        return None

    def path(self, key: str) -> Path:
        """Only the local backend has one; the streaming branch of the download view uses it."""
        return self._path(key)


def get_store() -> ArchiveStore:
    """The store for the configured backend.

    Resolved per call rather than cached at import: `override_settings` in tests swaps STORAGES, and
    a module-level singleton would hold the first one forever.
    """
    storage = storages["default"]
    if isinstance(storage, MediaS3Storage):
        return S3ArchiveStore(storage)
    return LocalArchiveStore(Path(settings.MEDIA_ROOT))


def _ascii_fallback(filename: str) -> str:
    """A filename old clients can parse: ASCII, no quotes, never empty."""
    cleaned = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return cleaned or "download"


def _rfc5987(filename: str) -> str:
    """Percent-encoded UTF-8 for the `filename*` parameter, which is what modern browsers read."""
    from urllib.parse import quote

    return quote(filename, safe="")
