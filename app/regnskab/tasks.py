import logging
from datetime import date, timedelta

from celery import shared_task
from django.utils import timezone

from . import alumneregning, economic
from .models import AlumneregningBatch

logger = logging.getLogger(__name__)

# one.com counts the last 5 minutes.
THROTTLE_WAIT = timedelta(minutes=6)
# How long a run has before resume_alumneregninger takes it for lost and starts another: past
# CELERY_TASK_TIME_LIMIT. A run that is in fact still going costs nothing but a wasted pass, since
# each alumne is claimed before they are mailed (alumneregning._claim).
RUN_LEASE = timedelta(minutes=20)
# Runs started again before giving up: three hours of throttle waits, about 750 mails at one.com's rate.
MAX_ATTEMPTS = 30
GAVE_UP_DETAIL = (
    "Ikke sendt: mailserveren tog ikke imod mails i flere timer, så afsendelsen blev opgivet. "
    "Send perioden igen fra regnskabssiden for at prøve de resterende."
)


def queue_alumneregninger(date_from: date, date_to: date) -> None:
    """Start (or restart) the send of a period's alumneregninger — what the send button does."""
    AlumneregningBatch.objects.update_or_create(
        date_from=date_from,
        date_to=date_to,
        defaults={"resume_at": timezone.now() + RUN_LEASE, "throttled": False, "attempts": 0},
    )
    send_alumneregninger_task.delay(date_from.isoformat(), date_to.isoformat())


@shared_task
def send_alumneregninger_task(date_from_iso: str, date_to_iso: str) -> None:
    """Runs the actual send in the background — a page load blocking for the duration of hundreds of
    e-conomic calls and SMTP sends, inside a single HTTP request, does not survive a proxy/browser
    timeout partway through. regnskab.views.send_alumneregninger_view queues this (queue_alumneregninger)
    instead of calling send_alumneregninger directly; regnskab.alumneregning.AlumneregningSend is the
    status/audit trail, since the results no longer exist only in a rendered response.

    When the mail server throttles, the batch's resume_at is moved to after the wait and
    resume_alumneregninger runs it again from there — not self.retry, whose delayed message the worker
    holds in memory, so a deploy in the meantime would lose the rest of the send without a trace.

    Catches EconomicAuthError specifically: send_alumneregninger already logs it before re-raising (to
    abort the batch rather than report the same failure once per remaining alumne), and there is no
    HTTP response left for a background task to show it on — letting it propagate would just mark this
    an unhandled Celery task failure, noisier than the log line already written."""
    date_from, date_to = date.fromisoformat(date_from_iso), date.fromisoformat(date_to_iso)
    batch = AlumneregningBatch.objects.filter(date_from=date_from, date_to=date_to)
    batch.update(resume_at=timezone.now() + RUN_LEASE, throttled=False)
    try:
        alumneregning.send_alumneregninger(date_from, date_to)
    except alumneregning.MailThrottled as exc:
        resume_at = timezone.now() + THROTTLE_WAIT
        batch.update(resume_at=resume_at, throttled=True)
        logger.info(
            "alumneregning task: mail server throttling (%s); the rest go out from %s", exc, resume_at
        )
        return
    except economic.EconomicAuthError:
        logger.error("alumneregning task: aborted — see the error logged by send_alumneregninger above")
    # Not over a throttle another run of the period hit meanwhile: whoever it left waiting is still
    # to send, and this run may have passed them while that one had them claimed.
    batch.filter(throttled=False).update(resume_at=None)


@shared_task
def resume_alumneregninger() -> None:
    """Runs again every send whose resume_at has passed (AlumneregningBatch): one waiting out the mail
    server, or one whose run was lost. After MAX_ATTEMPTS it fails whoever is still waiting instead,
    and logs it, so the status page stops promising them a mail."""
    now = timezone.now()
    for batch in AlumneregningBatch.objects.filter(resume_at__lte=now):
        # Conditional on resume_at, so two sweeps at once start it only once.
        if not AlumneregningBatch.objects.filter(pk=batch.pk, resume_at=batch.resume_at).update(
            resume_at=now + RUN_LEASE, attempts=batch.attempts + 1
        ):
            continue
        if batch.attempts >= MAX_ATTEMPTS:
            failed = alumneregning.fail_the_rest(batch.date_from, batch.date_to, GAVE_UP_DETAIL)
            AlumneregningBatch.objects.filter(pk=batch.pk).update(resume_at=None, throttled=False)
            logger.error(
                "alumneregning %s: gave up after %d attempts; %d alumne(r) marked failed",
                batch,
                batch.attempts,
                failed,
            )
            continue
        send_alumneregninger_task.delay(batch.date_from.isoformat(), batch.date_to.isoformat())
