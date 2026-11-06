"""A size-bounded local disk copy of files in object storage, for the files read often enough that
the extra round trip to the bucket shows.

The mechanics only; WHAT is worth caching is the caller's call. photo_album.cache keeps the album's
thumbnails and HD images, and core.media keeps the image attachments of live Den Hurtige messages
and recent opslag, and the profile thumbnails every avatar draws. Each gives this module its own
directory and byte budget, and nothing here knows what is in them.

  * A copy is fetched on the first ask and is whole or absent. It is written to a `.partial-` file
    and renamed into place, so a reader never sees half a file and two requests racing for one key
    both end up correct.
  * A hit touches the file. Eviction orders by mtime, so "least recently served" is just the oldest.
  * Every EVICT_EVERY new copies in a directory, a background thread trims it back under EVICT_TO of
    its budget. The first write after start-up evicts too: the volume outlives the process and its
    count.

NO INVALIDATION, because every caller caches only keys whose content never changes: the bucket
refuses to overwrite (file_overwrite=False), so a new file is always a new key. A key whose file is
deleted is never asked for again and ages out, and a deletion the caller wants gone sooner can
`discard` it.

Every failure here returns None, and callers fall back to the bucket: the cache is an optimisation,
never the only way to a file.
"""

import logging
import os
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.files.storage import Storage

logger = logging.getLogger(__name__)

# Evicting walks a whole directory, so it runs after this many new files rather than after each.
EVICT_EVERY = 200
# Eviction stops at this share of the budget, so the next few misses do not start another walk.
EVICT_TO = 0.9

_lock = threading.Lock()
_written: dict[Path, int] = {}
_evicting: set[Path] = set()


def cached_copy(directory: Path, key: str, storage: Storage, budget: int) -> Path | None:
    """The local copy of `key` from `storage`, under `directory`, fetching it on the first ask.

    None when the copy could not be made; the caller then serves `key` as it would without a cache.
    `key` must already be a clean storage name (core.media._clean), so it cannot climb out of
    `directory`.
    """
    target = directory / key
    try:
        os.utime(target)  # a hit: marks it recently served, which is what eviction orders by
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("disk cache: cannot touch %s", target)
        return None
    else:
        return target
    try:
        _fetch(storage, key, target)
    except Exception:
        logger.exception("disk cache: cannot fetch %s", key)
        return None
    _note_write(directory, budget)
    return target


def discard(directory: Path, key: str) -> None:
    """Drop the copy of `key`, if there is one. For a file deleted from the bucket that should not
    linger on this disk until eviction gets to it."""
    try:
        (directory / key).unlink(missing_ok=True)
    except OSError:
        logger.warning("disk cache: cannot discard %s", key, exc_info=True)


def _fetch(storage: Storage, key: str, target: Path) -> None:
    """Copy `key` from `storage` to `target`, whole or not at all."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, prefix=".partial-", delete=False) as partial:
        try:
            with storage.open(key, "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    partial.write(chunk)
        except BaseException:
            Path(partial.name).unlink(missing_ok=True)
            raise
    Path(partial.name).replace(target)  # atomic: two requests racing for one key both end up correct


def _note_write(directory: Path, budget: int) -> None:
    with _lock:
        _written[directory] = _written.get(directory, 0) + 1
        if _written[directory] % EVICT_EVERY != 1 or directory in _evicting:
            return
        _evicting.add(directory)
    threading.Thread(target=_evict_in_background, args=(directory, budget), daemon=True).start()


def _evict_in_background(directory: Path, budget: int) -> None:
    try:
        evict(directory, budget)
    except Exception:
        logger.exception("disk cache: eviction failed in %s", directory)
    finally:
        with _lock:
            _evicting.discard(directory)


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
    logger.info("disk cache: evicted %d MB from %s", freed // (1024 * 1024), directory)
    return freed
