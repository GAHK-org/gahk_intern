from celery import shared_task

from . import operations
from .structure import ensure_structure


@shared_task(name="drive.tasks.ensure_structure")
def ensure_structure_task() -> None:
    """Daily: a folder for every embedsgruppe, and the next period's and year's from LEAD before."""
    ensure_structure()


@shared_task(name="drive.tasks.empty_bin")
def empty_bin_task() -> None:
    """Daily: remove for good what has been in the bin for BIN_DAYS."""
    operations.empty_bin()
