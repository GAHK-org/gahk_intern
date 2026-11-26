from celery import shared_task

from .structure import ensure_structure


@shared_task(name="drive.tasks.ensure_structure")
def ensure_structure_task() -> None:
    """Daily: a folder for every embedsgruppe, and the next period's and year's from LEAD before."""
    ensure_structure()
