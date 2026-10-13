from celery import shared_task
from django.core.management import call_command


@shared_task
def purge_orphaned_images() -> None:
    """Remove unreferenced opslagstavle image uploads."""
    call_command("purge_notices")
