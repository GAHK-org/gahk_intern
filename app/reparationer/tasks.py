from celery import shared_task
from django.core.management import call_command


@shared_task
def archive_finished_repairs() -> None:
    """Archive repair tickets that have been completed for over 30 days."""
    call_command("archive_finished_repairs")
