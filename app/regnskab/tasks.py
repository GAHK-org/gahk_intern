import logging
from datetime import date

from celery import Task, shared_task

from . import alumneregning

logger = logging.getLogger(__name__)

# e-conomic's rate limit is per minute; three tries spread over a few minutes see it through.
ECONOMIC_RETRY_COUNTDOWN = 2 * 60


class AlumneregningerFailed(Exception):
    """Raised at the end of a send in which someone could not be billed, so the job shows as failed
    on the worker-jobs page rather than done (#397). Who and why is on the status page."""


@shared_task(bind=True, max_retries=3)
def send_alumneregninger_task(self: Task, date_from_iso: str, date_to_iso: str) -> int:
    """Runs the actual send in the background — a page load blocking for the duration of hundreds of
    e-conomic calls, inside a single HTTP request, does not survive a proxy/browser timeout partway
    through. regnskab.views.send_alumneregninger_view queues this instead of calling
    send_alumneregninger directly; regnskab.alumneregning.AlumneregningSend is the status/audit trail,
    since the results no longer exist only in a rendered response.

    It makes the statements and queues their mails; core.mail delivers them, within the mail server's
    rate limit, and retries each until it goes through. The job itself is tried again when e-conomic's
    rate limit failed someone (the rerun skips whoever is billed), and fails if anyone is left failed
    — as it does on EconomicAuthError, already logged by send_alumneregninger. Returns how many it
    billed."""
    date_from, date_to = date.fromisoformat(date_from_iso), date.fromisoformat(date_to_iso)
    results = alumneregning.send_alumneregninger(date_from, date_to)
    failed = [r for r in results if r.outcome == "failed"]
    if any(r.retryable for r in failed) and self.request.retries < self.max_retries:
        logger.info("alumneregning task: %d failed on e-conomic's rate limit; trying again", len(failed))
        raise self.retry(countdown=ECONOMIC_RETRY_COUNTDOWN)
    if failed:
        raise AlumneregningerFailed(
            f"{len(failed)} af {len(results)} alumneregninger kunne ikke laves: "
            + "; ".join(f"{r.resident.full_name}: {r.detail}" for r in failed)
        )
    return len(results)
