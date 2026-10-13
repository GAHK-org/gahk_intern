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
so a redeploy starts warm. Every failure here falls back to the redirect rather than failing the
request: the cache is an optimisation, never the only way to a picture.
"""

import logging
import os
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.conf import settings

from .models import Media
from .services import is_video_media

logger = logging.getLogger(__name__)

THUMBNAIL = "thumbnail"
HIGH_DEFINITION = "high-definition"
# Evicting walks a whole directory, so it runs after this many new files rather than after each.
EVICT_EVERY = 200
# Eviction stops at this share of the budget, so the next few misses do not start another walk.
EVICT_TO = 0.9

_lock = threading.Lock()
_written: dict[str, int] = {}
_evicting: set[str] = set()


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
    target = root / variant / key
    try:
        os.utime(target)  # a hit: marks it recently served, which is what eviction orders by
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("photo album cache: cannot touch %s", target)
        return None
    else:
        return target
    try:
        _fetch(media, key, target)
    except Exception:
        logger.exception("photo album cache: cannot fetch %s", key)
        return None
    _note_write(root / variant, variant)
    return target


def _fetch(media: Media, key: str, target: Path) -> None:
    """Copy `key` from the bucket to `target` — whole or not at all, so a reader never sees half a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, prefix=".partial-", delete=False) as partial:
        try:
            with media.thumbnail.storage.open(key, "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    partial.write(chunk)
        except BaseException:
            Path(partial.name).unlink(missing_ok=True)
            raise
    Path(partial.name).replace(target)  # atomic: two requests racing for one key both end up correct


def _note_write(directory: Path, variant: str) -> None:
    with _lock:
        _written[variant] = _written.get(variant, 0) + 1
        # The first write after start-up evicts too: the volume outlives the process and its count.
        if _written[variant] % EVICT_EVERY != 1 or variant in _evicting:
            return
        _evicting.add(variant)
    threading.Thread(target=_evict_in_background, args=(directory, variant), daemon=True).start()


def _evict_in_background(directory: Path, variant: str) -> None:
    try:
        evict(directory, _budget(variant))
    except Exception:
        logger.exception("photo album cache: eviction failed in %s", directory)
    finally:
        with _lock:
            _evicting.discard(variant)


def evict(directory: Path, budget: int) -> int:
    """Delete the least recently served files until `directory` is back under `budget`. Returns bytes freed."""
    files: list[tuple[float, int, Path]] = []
    total = 0
    stale_partial = time.time() - 3600
    for path in directory.rglob("*"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_file():
            continue
        if path.name.startswith(".partial-"):
            # Left by a process that died mid-copy; a live copy is minutes old at most.
            if stat.st_mtime < stale_partial:
                path.unlink(missing_ok=True)
            continue
        files.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size
    if total <= budget:
        return 0
    freed = 0
    for _mtime, size, path in sorted(files):
        if total - freed <= budget * EVICT_TO:
            break
        path.unlink(missing_ok=True)
        freed += size
    logger.info("photo album cache: evicted %d MB from %s", freed // (1024 * 1024), directory)
    return freed
