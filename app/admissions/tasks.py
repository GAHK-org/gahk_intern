import logging

from celery import shared_task
from django.core.management import call_command

from .models import Application
from .services import send_auto_reply

logger = logging.getLogger(__name__)


@shared_task
def purge_expired_applications() -> None:
    """Enforce the one-year application retention policy."""
    call_command("purge_applications")


@shared_task
def send_application_auto_reply(application_id: int) -> None:
    """The applicant's "we got it" mail, queued once when an application is first stored."""
    application = Application.objects.filter(pk=application_id).first()
    if application is None:
        logger.info("Application %s is gone; no auto-reply", application_id)
        return
    send_auto_reply(application)
