"""A local disk copy of the grid's thumbnails and the viewer's HD images, in front of the bucket.

Without it every tile is a round trip to this server (the access check in views.serve_media) and
then a second one to S3 through a presigned redirect; with it the second disappears and the bytes
come from the box's own disk. Only derivatives are cached: originals are large, rarely fetched and
stay redirects, and so do HD videos, whose players need Range requests that FileResponse does not do.

Safe to keep forever because a derivative's key never changes meaning: services.build_derivatives
writes a rebuild under NEW keys and the bucket refuses to overwrite (file_overwrite=False). So the
cache needs no invalidation, only a size bound — each variant has its own budget, and once a
directory passes it the least recently served files go first. A key whose row is gone is never
served again (serve_media looks the row up before it asks here), and simply ages out.

Off unless PHOTO_ALBUM_CACHE_DIR is set; production points it at a volume (docker-compose.prod.yml)
so a redeploy starts warm, and the dev servers set it too (docker-compose.yml, Taskfile.yml). Every failure here falls back to the redirect rather than failing the
request: the cache is an optimisation, never the only way to a picture.

The fetching, touching and evicting are core.disk_cache, shared with the attachment cache in
core.media; this module only decides which keys go in which directory.
"""

from pathlib import Path

from django.conf import settings

from core.disk_cache import cached_copy, evict

from .models import Media
from .services import is_video_media

__all__ = ["HIGH_DEFINITION", "THUMBNAIL", "cached_file", "evict"]

THUMBNAIL = "thumbnail"
HIGH_DEFINITION = "high-definition"


def _root() -> Path | None:
    configured = getattr(settings, "PHOTO_ALBUM_CACHE_DIR", "")
    return Path(configured) if configured else None


def _budget(variant: str) -> int:
    megabytes = (
        settings.PHOTO_ALBUM_CACHE_THUMBNAIL_MB if variant == THUMBNAIL else settings.PHOTO_ALBUM_CACHE_HD_MB
    )
    return int(megabytes) * 1024 * 1024


def _variant(media: Media, key: str) -> str | None:
    if key == media.thumbnail.name:
        return THUMBNAIL
    if key == media.high_definition.name and not is_video_media(media):
        return HIGH_DEFINITION
    return None


def cached_file(media: Media, key: str) -> Path | None:
    """The local copy of `key`, one of `media`'s derivatives, fetching it on the first ask.

    None when caching is off, when `key` is not something this cache keeps, or when the copy could
    not be made — the caller then redirects to the bucket as it would without a cache.
    """
    root = _root()
    variant = _variant(media, key) if root is not None and key else None
    if root is None or variant is None:
        return None
    return cached_copy(root / variant, key, media.thumbnail.storage, _budget(variant))
