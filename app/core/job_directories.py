"""Temporary directories for worker jobs that a killed job cannot leave behind for long.

A `TemporaryDirectory` is removed when its `with` block ends, including by an exception — but not
when the process is killed: the OOM killer, the hard time limit and a container restart all end it
mid-block, and whatever it had on disk stays in /tmp. A restart keeps the container's /tmp, and so
does Celery replacing a lost pool process, so only a redeploy (a new container) ever cleared it.
In November 2026 seven killed video encodes left 18 GB there and filled the server's disk.

`job_directory` is a `TemporaryDirectory` that holds a lock while it exists: an exclusive `flock` on
a file inside it, which the kernel releases the moment the process ends, however it ends.
`remove_abandoned_job_directories` deletes exactly the directories whose lock it can take, so it
never touches one a running job (or a command started by hand) is still using, however long that
job runs. A lock, not the process id in the name: an id is reused, a released lock is not mistaken.

The directory is made under a staging name and renamed to its real one only once the lock is held,
so the sweep never sees a directory between its creation and its lock. One killed in exactly that
moment is left empty under the staging name, and is removed by age instead; so is a directory whose
lock file is gone, which only its owner's own removal, under way or killed midway, leaves.
"""

import fcntl
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PREFIX = "gahk-job-"
STAGING_PREFIX = ".gahk-job-staging-"
LOCK_NAME = ".lock"
# A directory with no lock to test (a staging one, or one whose lock file its owner's removal already
# took) is left this long after its last change. Either lasts moments while its owner lives.
UNLOCKED_ABANDONED_AFTER = 60 * 60  # seconds


@contextmanager
def job_directory(kind: str, *, root: Path | None = None) -> Iterator[Path]:
    """A new directory for one job's working files, removed when the block ends.

    `kind` names the job in the directory's name (`gahk-job-<kind>-…`), so a look at /tmp tells
    what left it.
    """
    root = root or Path(tempfile.gettempdir())
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=root))
    lock = os.open(staging / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        directory = root / f"{PREFIX}{kind}-{staging.name.removeprefix(STAGING_PREFIX)}"
        staging.rename(directory)
        try:
            yield directory
        finally:
            # Not fatal: a directory that could not be removed is unlocked once `lock` closes, and
            # the next sweep removes it.
            shutil.rmtree(directory, ignore_errors=True)
    finally:
        os.close(lock)
        if staging.exists():  # the rename failed
            shutil.rmtree(staging, ignore_errors=True)


@dataclass
class Removed:
    directories: int = 0
    bytes: int = 0


def remove_abandoned_job_directories(root: Path | None = None) -> Removed:
    """Delete the job directories under `root` (the temp dir) that no living process holds.

    Safe to run at any moment, in any process, beside any number of running jobs.
    """
    root = root or Path(tempfile.gettempdir())
    removed = Removed()
    try:
        entries = list(root.iterdir())
    except OSError:
        logger.warning("Could not list %s for abandoned job directories.", root, exc_info=True)
        return removed
    for entry in entries:
        try:
            if entry.name.startswith(PREFIX) and entry.is_dir() and not entry.is_symlink():
                _remove_if_unlocked(entry, removed)
            elif entry.name.startswith(STAGING_PREFIX) and entry.is_dir() and not entry.is_symlink():
                _remove_if_old(entry, removed)
        except OSError:
            logger.warning("Could not remove the abandoned job directory %s.", entry, exc_info=True)
    if removed.directories:
        logger.warning(
            "Removed %d abandoned job director%s (%s bytes) from %s, left by killed jobs.",
            removed.directories,
            "y" if removed.directories == 1 else "ies",
            f"{removed.bytes:,}",
            root,
        )
    return removed


def _remove_if_unlocked(directory: Path, removed: Removed) -> None:
    try:
        lock = os.open(directory / LOCK_NAME, os.O_RDONLY)
    except FileNotFoundError:
        _remove_if_old(directory, removed)  # its owner's rmtree has begun; it may still be running
        return
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # a living process holds it
        # Held by this sweep now, so no one else can be using it while it goes.
        _remove(directory, removed)
    finally:
        os.close(lock)


def _remove_if_old(directory: Path, removed: Removed) -> None:
    if time.time() - directory.stat().st_mtime > UNLOCKED_ABANDONED_AFTER:
        _remove(directory, removed)


def _remove(directory: Path, removed: Removed) -> None:
    size = sum(
        path.stat().st_size for path in directory.rglob("*") if path.is_file() and not path.is_symlink()
    )
    shutil.rmtree(directory)
    removed.directories += 1
    removed.bytes += size
