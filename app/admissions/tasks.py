from celery import shared_task
from django.core.management import call_command


@shared_task
def purge_expired_applications() -> None:
    """Enforce the one-year application retention policy."""
    call_command("purge_applications")
