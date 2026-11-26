from celery import shared_task

from . import locks, operations, preview, shared
from .structure import ensure_structure


@shared_task(name="drive.tasks.ensure_structure")
def ensure_structure_task() -> None:
    """Daily: a folder for every embedsgruppe, and the next period's and year's from LEAD before."""
    ensure_structure()


@shared_task(name="drive.tasks.empty_bin")
def empty_bin_task() -> None:
    """Daily: remove for good what has been in the bin for BIN_DAYS."""
    operations.empty_bin()


@shared_task(name="drive.tasks.auto_lock")
def auto_lock_task() -> None:
    """Daily: lock files and folders that have had nothing new for a year."""
    locks.auto_lock()


@shared_task(name="drive.tasks.shared_expiry")
def shared_expiry_task() -> None:
    """Daily: warn the moderators of Delte mapper about to expire, and delete or archive expired ones."""
    shared.run_expiry()


@shared_task(name="drive.tasks.build_image_preview")
def build_image_preview(key: str) -> bool:
    """An image's viewer copy and thumbnail, queued on "thumbnails" when it is uploaded."""
    return preview.build(key)


@shared_task(name="drive.tasks.queue_stranded_previews")
def queue_stranded_previews() -> int:
    """Nightly: image previews still waiting an hour after they were asked for are queued again."""
    return preview.queue_stranded()
