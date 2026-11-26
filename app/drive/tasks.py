from celery import shared_task

from . import locks, operations
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
