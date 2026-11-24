import logging
from datetime import date

from celery import Task, shared_task

from audit import trail

from . import alumneregning

logger = logging.getLogger(__name__)

# e-conomic's rate limit is per minute; three tries spread over a few minutes see it through.
ECONOMIC_RETRY_COUNTDOWN = 2 * 60


class AlumneregningerFailed(Exception):
    """Raised at the end of a send in which someone's statement could not be made, so the job shows as
    failed on the worker-jobs page rather than done (#397). Who and why is on the status page."""


@shared_task(bind=True, max_retries=3)
def send_alumneregninger_task(self: Task, date_from_iso: str, date_to_iso: str) -> int:
    """Runs the actual send in the background — a page load blocking for the duration of hundreds of
    e-conomic calls, inside a single HTTP request, does not survive a proxy/browser timeout partway
    through. regnskab.views.send_alumneregninger_view queues this instead of calling
    send_alumneregninger directly; regnskab.alumneregning.AlumneregningSend is the status/audit trail,
    since the results no longer exist only in a rendered response.

    It makes the statements and queues their mails; core.mail delivers them, within the mail server's
    rate limit, and retries each until it goes through. The job itself is tried again when e-conomic's
    rate limit failed someone (the rerun skips whoever was sent theirs), and fails if anyone is left
    failed — as it does on EconomicAuthError, already logged by send_alumneregninger. Returns how many
    statements it made."""
    date_from, date_to = date.fromisoformat(date_from_iso), date.fromisoformat(date_to_iso)
    results = alumneregning.send_alumneregninger(date_from, date_to)
    failed = [r for r in results if r.outcome == "failed"]
    # Recorded after the run rather than around it: an event is one transaction, and each alumne's
    # statement commits (and its mail goes) on its own. Every run, retries too, says whom it billed
    # and whom it could not; a run with nobody left to bill records nothing.
    if results:
        trail.record(
            "regnskab.alumneregninger.made",
            actor=None,
            details={
                "date_from": date_from_iso,
                "date_to": date_to_iso,
                "made": len(results) - len(failed),
                "failed": len(failed),
            },
            involve=[(r.resident, "billed" if r.outcome == "sent" else "failed") for r in results],
        )
    if any(r.retryable for r in failed) and self.request.retries < self.max_retries:
        logger.info("alumneregning task: %d failed on e-conomic's rate limit; trying again", len(failed))
        raise self.retry(countdown=ECONOMIC_RETRY_COUNTDOWN)
    if failed:
        raise AlumneregningerFailed(
            f"{len(failed)} af {len(results)} alumneregninger kunne ikke laves: "
            + "; ".join(f"{r.resident.full_name}: {r.detail}" for r in failed)
        )
    return len(results)
