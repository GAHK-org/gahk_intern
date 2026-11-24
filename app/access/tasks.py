"""Just after midnight: a list made ahead takes effect on the 1st, so the groups follow the date too."""

from celery import shared_task

from .roles import sync


@shared_task
def sync_roles() -> int:
    return sync()
