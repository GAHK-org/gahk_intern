from celery import shared_task
from django.core.management import call_command


@shared_task
def apply_monthly_assessment() -> None:
    """Apply this month's idempotent AK assessment."""
    call_command("ak_monthly_assessment")
