from celery import shared_task
from django.core.management import call_command


@shared_task
def purge_expired_events() -> None:
    """Delete events past their retention period."""
    call_command("purge_events")


@shared_task
def remind_rsvp_deadlines() -> None:
    """Send RSVP reminders for deadlines within the next 24 hours."""
    call_command("remind_rsvp_deadlines")
