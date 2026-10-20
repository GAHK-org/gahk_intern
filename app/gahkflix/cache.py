"""A local disk copy of GAHKFlix's pictures — thumbnails, banners and candidate frames.

A home page is a few dozen cards and a banner, every one of them an access check here and then, without
a copy, a redirect to the bucket. With one, the bytes come off this box's disk (core.disk_cache).
Only pictures: the renditions are streamed by the browser with Range requests straight from the
bucket, and the originals are only ever downloaded.

Under MEDIA_CACHE_DIR/gahkflix/, with its own budget (GAHKFLIX_CACHE_MB). Off when MEDIA_CACHE_DIR is
empty, as it is in the test suite.
"""

from pathlib import Path

from django.conf import settings

from core import disk_cache

from .storage import get_video_storage


def _directory() -> Path | None:
    root = getattr(settings, "MEDIA_CACHE_DIR", "")
    return Path(root) / "gahkflix" if root else None


def cached_picture(key: str) -> Path | None:
    directory = _directory()
    if directory is None or not key:
        return None
    return disk_cache.cached_copy(
        directory, key, get_video_storage(), settings.GAHKFLIX_CACHE_MB * 1024 * 1024
    )


def discard(key: str) -> None:
    directory = _directory()
    if directory is not None and key:
        disk_cache.discard(directory, key)
