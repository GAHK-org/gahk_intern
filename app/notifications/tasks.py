from celery import shared_task

from . import services


@shared_task
def purge_expired_notifications() -> int:
    """Delete notifications whose TTL ran out more than 30 days ago (services.purge_expired)."""
    return services.purge_expired()
