"""The outbox every mail goes through (#397).

one.com refuses everything from the account for a while after ~25 mails in 5 minutes ("451 4.7.1
[R2] Too many mails", "Too many mails received from x within the last 5 minutes"), and the app sends
from one account: a month's alumneregninger or ølkælder overviews used to run into it, and whatever
else was sent meanwhile — a password reset, a welcome mail — was lost with them.

So EMAIL_BACKEND is OutboxBackend, which only stores each message as an OutgoingMail, in the caller's
transaction, and queues core.tasks.deliver_mail once that commits. deliver() sends what is due, in
order, through MAIL_DELIVERY_BACKEND (the real one), never more than MAIL_RATE_LIMIT within
MAIL_RATE_WINDOW. A throttle or a server that is down stops it with the mail still queued; a refusal
that may pass later is retried with a growing wait; a refusal outright fails the mail, and the job
with it, so it shows on the worker-jobs page instead of only in the log.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import logging
import smtplib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.base import MIMEBase
from typing import Any, Literal

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.db import connection, transaction
from django.utils import timezone

from .models import OutgoingMail

logger = logging.getLogger(__name__)

Status = OutgoingMail.Status

# A server-wide refusal: 421 closes the connection, 451 is one.com's throttle. Other 4xx — 450 mailbox
# busy, 452 mailbox full — are about the one recipient, and are retried for just that mail.
THROTTLE_CODES = frozenset({421, 451})
THROTTLE_WAIT = timedelta(minutes=6)
# A server that is unreachable or refuses our login: nothing will get through, so stop and try again.
SERVER_DOWN_WAIT = timedelta(minutes=5)
# A refusal for one recipient that may pass later: attempt n waits n × this, so 8 attempts span 6 hours.
RETRY_WAIT = timedelta(minutes=10)
MAX_ATTEMPTS = 8
# A mail still "sending" after this was being sent by a job that died (killed past its time limit, a
# deploy). Whether it went out is unknown, so it is failed rather than sent again: two alumneregninger
# are worse than one that has to be checked.
SENDING_LOST_AFTER = timedelta(minutes=20)
LOST_ERROR = "Afbrudt under afsendelse — ukendt om den kom frem."
# pg_try_advisory_lock key: one deliver() at a time, so two can't both fill the same window.
_LOCK_KEY = 397_000_001

Verdict = Literal["throttled", "server_down", "retry", "failed"]


# ---- storing ----


def serialize(message: EmailMessage) -> dict[str, Any]:
    """What it takes to build the message again in another process (deliver)."""
    attachments = []
    for attachment in message.attachments:
        if isinstance(attachment, MIMEBase):
            raise TypeError(
                "core.mail cannot queue a MIMEBase attachment; attach filename, content, mimetype"
            )
        filename, content, mimetype = attachment
        if isinstance(content, bytes):
            attachments.append(
                {"filename": filename, "mimetype": mimetype, "base64": base64.b64encode(content).decode()}
            )
        else:
            attachments.append({"filename": filename, "mimetype": mimetype, "text": content})
    return {
        "subject": message.subject,
        "body": message.body,
        "from_email": message.from_email,
        "to": list(message.to),
        "cc": list(message.cc),
        "bcc": list(message.bcc),
        "reply_to": list(message.reply_to),
        "headers": dict(message.extra_headers),
        "content_subtype": message.content_subtype,
        "alternatives": [[content, mimetype] for content, mimetype in getattr(message, "alternatives", [])],
        "attachments": attachments,
    }


def deserialize(payload: dict[str, Any]) -> EmailMultiAlternatives:
    message = EmailMultiAlternatives(
        subject=payload["subject"],
        body=payload["body"],
        from_email=payload["from_email"],
        to=payload["to"],
        cc=payload["cc"],
        bcc=payload["bcc"],
        reply_to=payload["reply_to"],
        headers=payload["headers"],
        alternatives=[tuple(alternative) for alternative in payload["alternatives"]],
    )
    message.content_subtype = payload["content_subtype"]
    for attachment in payload["attachments"]:
        content = base64.b64decode(attachment["base64"]) if "base64" in attachment else attachment["text"]
        message.attach(attachment["filename"], content, attachment["mimetype"])
    return message


_bulk = contextvars.ContextVar("core.mail.bulk", default=False)


@contextlib.contextmanager
def bulk() -> Iterator[None]:
    """Mail queued inside this is bulk — the same mail to many, like a month's alumneregninger. It
    goes out after any other due mail, and only within MAIL_BULK_LIMIT of the window, so a password
    reset never waits behind a monthly send."""
    token = _bulk.set(True)
    try:
        yield
    finally:
        _bulk.reset(token)


def queue(message: EmailMessage) -> OutgoingMail:
    """Store the message for delivery, and set it as message.outgoing_mail — how a caller that
    tracks delivery (regnskab.alumneregning) finds its row. Delivery is queued once the caller's
    transaction commits; if it rolls back, so does the mail."""
    mail = OutgoingMail.objects.create(
        subject=message.subject[:998],
        recipients=", ".join(message.recipients()),
        payload=serialize(message),
        bulk=_bulk.get(),
    )
    message.outgoing_mail = mail  # type: ignore[attr-defined]
    transaction.on_commit(_start_delivery)
    return mail


def _start_delivery() -> None:
    from .tasks import deliver_mail

    try:
        deliver_mail.delay()
    except Exception:  # the broker down: the mail is stored, and the periodic run sends it
        logger.exception("Could not queue deliver_mail; the next periodic run will send the mail.")


class OutboxBackend(BaseEmailBackend):
    """EMAIL_BACKEND: send_mail and EmailMessage.send() store the mail instead of sending it."""

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        count = 0
        for message in email_messages:
            if not message.recipients():
                continue
            queue(message)
            count += 1
        return count


# ---- delivering ----


def _replies(exc: Exception) -> list[tuple[int, str]]:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return [(code, _text(msg)) for code, msg in exc.recipients.values()]
    if isinstance(exc, smtplib.SMTPResponseException):
        return [(exc.smtp_code, _text(exc.smtp_error))]
    return []


def _text(msg: bytes | str) -> str:
    return msg.decode(errors="replace") if isinstance(msg, bytes) else msg


def classify(exc: Exception) -> Verdict:
    """What a failed send means for the mail — and, for a throttle or a server down, for every mail."""
    replies = _replies(exc)
    # By the text too: whichever code one.com gives it, it is the account that is refused, not the mail.
    if replies and all(code in THROTTLE_CODES or "too many" in text.lower() for code, text in replies):
        return "throttled"
    if isinstance(
        exc, smtplib.SMTPAuthenticationError | smtplib.SMTPConnectError | smtplib.SMTPServerDisconnected
    ):
        return "server_down"
    if replies:
        return "retry" if all(400 <= code < 500 for code, _text in replies) else "failed"
    if isinstance(exc, OSError):  # socket errors, timeouts, refused connections — smtplib's included
        return "server_down"
    return "failed"


def quota_wait(now: datetime, *, bulk: bool = False) -> timedelta | None:
    """None if a mail may go out now; otherwise how long until the window has room for a full batch
    again — waiting for one slot at a time would run the job once per mail. A bulk mail must also
    fit within MAIL_BULK_LIMIT, which keeps the rest of the window free for everything else."""
    attempted = OutgoingMail.objects.filter(last_attempt_at__gt=now - settings.MAIL_RATE_WINDOW)
    limits = [(attempted, settings.MAIL_RATE_LIMIT)]
    if bulk:
        limits.append((attempted.filter(bulk=True), settings.MAIL_BULK_LIMIT))
    waits = []
    for counted, limit in limits:
        if counted.count() >= limit:
            newest: datetime = counted.order_by("-last_attempt_at").values_list("last_attempt_at", flat=True)[
                0
            ]  # type: ignore[assignment]
            waits.append(newest + settings.MAIL_RATE_WINDOW - now)
    return max(waits, default=None)


@contextlib.contextmanager
def _exclusive() -> Iterator[bool]:
    """Whether this process got the delivery lock. Postgres only; elsewhere (SQLite tests) always."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_LOCK_KEY])
        got = bool(cursor.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [_LOCK_KEY])


@dataclass
class Delivery:
    """What one deliver() did. resume_in: when there is more to send, but not yet."""

    sent: int = 0
    failed: list[OutgoingMail] = field(default_factory=list)
    resume_in: timedelta | None = None
    server_error: str = ""


def deliver() -> Delivery:
    """Send the queued mail that is due, oldest first, until it is all sent or the rate limit, a
    throttle or a server down stops it (resume_in). Does nothing if another deliver() is running."""
    result = Delivery()
    with _exclusive() as got:
        if not got:
            return result
        now = timezone.now()
        for lost in OutgoingMail.objects.filter(
            status=Status.SENDING, last_attempt_at__lt=now - SENDING_LOST_AFTER
        ):
            lost.status, lost.error = Status.FAILED, LOST_ERROR
            lost.save(update_fields=["status", "error"])
            result.failed.append(lost)
            logger.error(
                "Mail %s to %s was being sent by a job that died: %s", lost.pk, lost.recipients, LOST_ERROR
            )

        while True:
            now = timezone.now()
            mail = (
                OutgoingMail.objects.filter(status=Status.QUEUED, next_attempt_at__lte=now)
                .order_by("bulk", "next_attempt_at", "pk")  # everything else before bulk
                .first()
            )
            if mail is None:
                break
            result.resume_in = quota_wait(now, bulk=mail.bulk)
            if result.resume_in is not None:
                break
            mail.status, mail.attempts, mail.last_attempt_at = Status.SENDING, mail.attempts + 1, now
            mail.save(update_fields=["status", "attempts", "last_attempt_at"])
            try:
                message = deserialize(mail.payload)
                message.connection = get_connection(settings.MAIL_DELIVERY_BACKEND)
                message.send()
            except Exception as exc:
                if _settle_failure(mail, exc, result):
                    break
                continue
            mail.status, mail.sent_at, mail.payload, mail.error = Status.SENT, timezone.now(), {}, ""
            mail.save(update_fields=["status", "sent_at", "payload", "error"])
            result.sent += 1
        if result.resume_in is None:
            upcoming = (
                OutgoingMail.objects.filter(status=Status.QUEUED)
                .order_by("next_attempt_at")
                .values_list("next_attempt_at", flat=True)
                .first()
            )
            if upcoming is not None:
                result.resume_in = max(upcoming - timezone.now(), timedelta(0))
    return result


def _settle_failure(mail: OutgoingMail, exc: Exception, result: Delivery) -> bool:
    """Put the mail back or fail it, by classify(). Returns whether to stop sending for now."""
    verdict = classify(exc)
    now = timezone.now()
    mail.error = str(exc)
    if verdict in ("throttled", "server_down"):
        # Not the mail's fault: back in the queue, the attempt not counted against it.
        wait = THROTTLE_WAIT if verdict == "throttled" else SERVER_DOWN_WAIT
        mail.status, mail.attempts, mail.next_attempt_at = Status.QUEUED, mail.attempts - 1, now + wait
        mail.save(update_fields=["status", "attempts", "next_attempt_at", "error"])
        result.resume_in = wait
        if verdict == "server_down":
            result.server_error = str(exc)
        logger.warning("Mail %s not sent (%s, waiting %s): %s", mail.pk, verdict, wait, exc)
        return True
    if verdict == "retry" and mail.attempts < MAX_ATTEMPTS:
        mail.status, mail.next_attempt_at = Status.QUEUED, now + RETRY_WAIT * mail.attempts
        mail.save(update_fields=["status", "next_attempt_at", "error"])
        logger.warning(
            "Mail %s to %s refused for now, attempt %d: %s", mail.pk, mail.recipients, mail.attempts, exc
        )
        return False
    mail.status = Status.FAILED
    mail.save(update_fields=["status", "error"])
    result.failed.append(mail)
    logger.error("Mail %s to %s failed after %d attempt(s): %s", mail.pk, mail.recipients, mail.attempts, exc)
    return False
