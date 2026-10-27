import logging
from datetime import date

from celery import shared_task

from . import alumneregning, economic

logger = logging.getLogger(__name__)


@shared_task
def send_alumneregninger_task(date_from_iso: str, date_to_iso: str) -> None:
    """Runs the actual send in the background — a page load blocking for the duration of hundreds of
    e-conomic calls and SMTP sends, inside a single HTTP request, does not survive a proxy/browser
    timeout partway through. regnskab.views.send_alumneregninger_view queues this instead of calling
    send_alumneregninger directly; regnskab.alumneregning.AlumneregningSend is the status/audit trail,
    since the results no longer exist only in a rendered response.

    Catches EconomicAuthError specifically: send_alumneregninger already logs it before re-raising (to
    abort the batch rather than report the same failure once per remaining alumne), and there is no
    HTTP response left for a background task to show it on — letting it propagate would just mark this
    an unhandled Celery task failure, noisier than the log line already written."""
    try:
        alumneregning.send_alumneregninger(date.fromisoformat(date_from_iso), date.fromisoformat(date_to_iso))
    except economic.EconomicAuthError:
        logger.error("alumneregning task: aborted — see the error logged by send_alumneregninger above")
