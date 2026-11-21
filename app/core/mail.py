"""The outbox every mail goes through (#397).

one.com refuses everything from the account for a while after ~25 mails in 5 minutes ("451 4.7.1
[R2] Too many mails", "Too many mails received from x within the last 5 minutes"), and the app sends
from one account: a month's alumneregninger or ølkælder overviews used to run into it, and whatever
else was sent meanwhile — a password reset, a welcome mail — was lost with them.

So EMAIL_BACKEND is OutboxBackend, which only stores each message as an OutgoingMail, in the caller's
transaction, and starts delivery once that commits. deliver() sends what is due through
MAIL_DELIVERY_BACKEND (the real one), by the rules of each mail's kind (KINDS): interactive mail —
someone is waiting for it — first, and also at once from the process that queued it; bulk mail last,
and only while fewer than MAIL_RATE_LIMIT mails went out within MAIL_RATE_WINDOW. A throttle or a
server that is down stops delivery with the mail still queued; a refusal that may pass later is
retried with a growing wait; a refusal outright fails the mail, and a mail not sent within its
lifetime expires. Either fails the job, so it shows on the worker-jobs page instead of only in the log.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import functools
import logging
import smtplib
import threading
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.base import MIMEBase
from typing import Any, Literal

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.db import connection, transaction
from django.db.models import Case, IntegerField, QuerySet, Value, When
from django.utils import timezone

from .models import OutgoingMail

logger = logging.getLogger(__name__)

Kind = OutgoingMail.Kind
Status = OutgoingMail.Status


@dataclass(frozen=True)
class KindRules:
    """How a kind of mail is delivered. retry_waits: how long attempt n waits after a refusal that may
    pass later (a full mailbox), the last repeating — until the mail's lifetime runs out."""

    order: int  # lower goes first
    rate_limited: bool  # held to MAIL_RATE_LIMIT per MAIL_RATE_WINDOW
    send_at_once: bool  # also sent from the process that queued it, not only by the worker
    lifetime: timedelta  # the default; kind(lifetime=...) sets a mail's own
    retry_waits: tuple[timedelta, ...]

    def retry_wait(self, attempts: int) -> timedelta:
        return self.retry_waits[min(max(attempts, 1), len(self.retry_waits)) - 1]


_MINUTE = timedelta(minutes=1)
_SLOW_RETRIES = tuple(n * 10 * _MINUTE for n in range(1, 7))  # 10, 20, … 60 minutes, then hourly

KINDS: dict[str, KindRules] = {
    Kind.INTERACTIVE: KindRules(
        order=0,
        rate_limited=False,
        send_at_once=True,
        lifetime=timedelta(hours=1),
        retry_waits=(_MINUTE, 2 * _MINUTE, 5 * _MINUTE, 10 * _MINUTE),
    ),
    Kind.NORMAL: KindRules(
        order=1, rate_limited=False, send_at_once=False, lifetime=timedelta(days=1), retry_waits=_SLOW_RETRIES
    ),
    Kind.BULK: KindRules(
        order=2, rate_limited=True, send_at_once=False, lifetime=timedelta(days=3), retry_waits=_SLOW_RETRIES
    ),
}
_SENT_AT_ONCE = frozenset(name for name, rules in KINDS.items() if rules.send_at_once)
_KIND_ORDER = Case(
    *(When(kind=name, then=Value(rules.order)) for name, rules in KINDS.items()), output_field=IntegerField()
)

# A server-wide refusal: 421 closes the connection, 451 is one.com's throttle. Other 4xx — 450 mailbox
# busy, 452 mailbox full — are about the one recipient, and are retried for just that mail.
THROTTLE_CODES = frozenset({421, 451})
THROTTLE_WAIT = timedelta(minutes=6)
# A server that is unreachable or refuses our login: nothing will get through, so stop and try again.
SERVER_DOWN_WAIT = timedelta(minutes=5)
# A server down for a few minutes is not worth a failed job every 5 minutes; mail held up this long is.
SERVER_DOWN_REPORT_AFTER = timedelta(minutes=30)
# A mail still "sending" after this was being sent by a job that died (killed past its time limit, a
# deploy). Whether it went out is unknown, so it is not sent again by itself: two alumneregninger are
# worse than one that has to be checked. It becomes UNKNOWN, which someone can send again on purpose.
SENDING_LOST_AFTER = timedelta(minutes=20)
LOST_ERROR = "Afbrudt under afsendelse — ukendt om den kom frem."
EXPIRED_ERROR = "Ikke sendt i tide, så den er opgivet."
# Sent, failed and expired mails are kept this long, then deleted (purge).
RETENTION = timedelta(days=90)
# pg_try_advisory_lock key: one sender at a time, so two can't both fill the same window.
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


@dataclass(frozen=True)
class _Sending:
    kind: str
    lifetime: timedelta | None


_NORMAL = _Sending(Kind.NORMAL, None)
_sending: contextvars.ContextVar[_Sending | None] = contextvars.ContextVar("core.mail.sending", default=None)


@contextlib.contextmanager
def kind(mail_kind: str, *, lifetime: timedelta | None = None) -> Iterator[None]:
    """Mail queued inside this is of `mail_kind` (KINDS) instead of normal — `with kind(Kind.BULK):`
    around a month's alumneregninger. `lifetime` replaces the kind's own, for a mail whose content
    expires sooner, like a password-reset link."""
    token = _sending.set(_Sending(Kind(mail_kind), lifetime))
    try:
        yield
    finally:
        _sending.reset(token)


_without_delivery: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "core.mail.without_delivery", default=False
)


@contextlib.contextmanager
def without_delivery() -> Iterator[None]:
    """Mail queued inside this is stored, but no delivery is started for it: seed_demo's demo outbox
    (core.demo), whose mails it then gives the state the deliverer would have. deliver_mail's periodic
    run still sends whatever is left queued and due."""
    token = _without_delivery.set(True)
    try:
        yield
    finally:
        _without_delivery.reset(token)


def queue(message: EmailMessage) -> OutgoingMail:
    """Store the message for delivery, and set it as message.outgoing_mail — how a caller that
    tracks delivery (regnskab.alumneregning) finds its row. Delivery starts once the caller's
    transaction commits; if it rolls back, so does the mail."""
    sending = _sending.get() or _NORMAL
    now = timezone.now()
    mail = OutgoingMail.objects.create(
        subject=message.subject[:998],
        recipients=", ".join(message.recipients()),
        payload=serialize(message),
        kind=sending.kind,
        next_attempt_at=now,
        expires_at=now + (sending.lifetime or KINDS[sending.kind].lifetime),
    )
    message.outgoing_mail = mail  # type: ignore[attr-defined]
    if not _without_delivery.get():
        _schedule_delivery(sending.kind)
    return mail


def _schedule_delivery(mail_kind: str | None) -> None:
    """Start delivery once the caller's transaction commits. tests/conftest.py replaces this with
    delivering there and then, as CELERY_TASK_ALWAYS_EAGER does for jobs."""
    transaction.on_commit(functools.partial(_start_delivery, mail_kind))


def _start_delivery(mail_kind: str | None = None) -> None:
    if mail_kind is not None and KINDS[mail_kind].send_at_once:
        # Not daemon: an exiting process (a management command) waits for the send to finish rather
        # than cutting it off, which would leave the mail UNKNOWN.
        threading.Thread(target=_send_at_once, name="core.mail.send_at_once").start()
    _queue_deliver_mail()


def _queue_deliver_mail() -> None:
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


def quota_wait(now: datetime) -> timedelta | None:
    """None if a rate-limited (bulk) mail may go out now: fewer than MAIL_RATE_LIMIT mails of any kind
    went out within the window. Otherwise how long until the window has room for a full batch again —
    waiting for one slot at a time would run the job once per mail. Other kinds go out at once,
    whatever the count, in the room this leaves below one.com's limit."""
    attempted = OutgoingMail.objects.filter(last_attempt_at__gt=now - settings.MAIL_RATE_WINDOW)
    if attempted.count() < settings.MAIL_RATE_LIMIT:
        return None
    newest: datetime = attempted.order_by("-last_attempt_at").values_list("last_attempt_at", flat=True)[0]  # type: ignore[assignment]
    return newest + settings.MAIL_RATE_WINDOW - now


def held_up_by_server(now: datetime) -> bool:
    """Whether queued mail has waited longer than SERVER_DOWN_REPORT_AFTER: with the server down, a
    deliver_mail run reports it only then."""
    return OutgoingMail.objects.filter(
        status=Status.QUEUED, created_at__lt=now - SERVER_DOWN_REPORT_AFTER
    ).exists()


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


def _due(now: datetime, kinds: Collection[str] | None = None) -> QuerySet[OutgoingMail]:
    """Queued mail due now, in the order it goes out: by kind (KINDS' order), then oldest first."""
    due = OutgoingMail.objects.filter(status=Status.QUEUED, next_attempt_at__lte=now, expires_at__gt=now)
    if kinds is not None:
        due = due.filter(kind__in=kinds)
    return due.annotate(kind_order=_KIND_ORDER).order_by("kind_order", "next_attempt_at", "pk")


@dataclass
class Delivery:
    """What one deliver() did. resume_in: when there is more to send, but not yet."""

    sent: int = 0
    failed: list[OutgoingMail] = field(default_factory=list)  # FAILED, and UNKNOWN (cut off)
    expired: list[OutgoingMail] = field(default_factory=list)
    resume_in: timedelta | None = None
    server_error: str = ""


def deliver() -> Delivery:
    """Send the queued mail that is due until it is all sent or the rate limit, a throttle or a server
    down stops it (resume_in). Does nothing if another sender holds the lock: that one checks for due
    mail again once it lets go, so nothing queued meanwhile is left waiting for Beat."""
    result = Delivery()
    while True:
        with _exclusive() as got:
            if not got:
                return result
            _give_up_on_lost_and_expired(result)
            if _send_due(result):
                return result
        # After letting go of the lock: a mail queued while this run was finishing had its own run
        # turned away by the lock, so this run is the one to send it.
        if not _due(timezone.now()).exists():
            break
    upcoming = (
        OutgoingMail.objects.filter(status=Status.QUEUED)
        .order_by("next_attempt_at")
        .values_list("next_attempt_at", flat=True)
        .first()
    )
    if upcoming is not None:
        # At least a second: a wait of nothing would not be scheduled at all (core.tasks.deliver_mail).
        result.resume_in = max(upcoming - timezone.now(), timedelta(seconds=1))
    return result


def _give_up_on_lost_and_expired(result: Delivery) -> None:
    now = timezone.now()
    for lost in OutgoingMail.objects.filter(
        status=Status.SENDING, last_attempt_at__lt=now - SENDING_LOST_AFTER
    ):
        lost.status, lost.error = Status.UNKNOWN, LOST_ERROR
        lost.save(update_fields=["status", "error"])
        result.failed.append(lost)
        logger.error(
            "Mail %s to %s was being sent by a job that died: %s", lost.pk, lost.recipients, LOST_ERROR
        )
    for mail in OutgoingMail.objects.filter(status=Status.QUEUED, expires_at__lte=now):
        _expire(mail, result)


def _send_due(result: Delivery) -> bool:
    """Send due mail until none is left (False) or something stops it for now (True, resume_in set)."""
    while True:
        now = timezone.now()
        mail = _due(now).first()
        if mail is None:
            return False
        if KINDS[mail.kind].rate_limited:
            result.resume_in = quota_wait(now)
            if result.resume_in is not None:
                return True
        _claim(mail, now)
        try:
            _send(mail)
        except Exception as exc:
            if _settle_failure(mail, exc, result):
                return True
            continue
        _mark_sent(mail)
        result.sent += 1


def _claim(mail: OutgoingMail, now: datetime) -> None:
    mail.status, mail.attempts, mail.last_attempt_at = Status.SENDING, mail.attempts + 1, now
    mail.save(update_fields=["status", "attempts", "last_attempt_at"])


def _send(mail: OutgoingMail) -> None:
    message = deserialize(mail.payload)
    message.connection = get_connection(settings.MAIL_DELIVERY_BACKEND)
    message.send()


def _mark_sent(mail: OutgoingMail) -> None:
    mail.status, mail.sent_at, mail.payload, mail.error = Status.SENT, timezone.now(), {}, ""
    mail.save(update_fields=["status", "sent_at", "payload", "error"])


def _expire(mail: OutgoingMail, result: Delivery) -> None:
    mail.status, mail.payload = Status.EXPIRED, {}
    mail.error = f"{EXPIRED_ERROR} Sidste fejl: {mail.error}" if mail.error else EXPIRED_ERROR
    mail.save(update_fields=["status", "payload", "error", "attempts"])
    result.expired.append(mail)
    logger.error(
        "Mail %s to %s expired after %d attempt(s): %s", mail.pk, mail.recipients, mail.attempts, mail.error
    )


def _settle_failure(mail: OutgoingMail, exc: Exception, result: Delivery) -> bool:
    """Put the mail back, expire it or fail it, by classify(). Returns whether to stop sending for now."""
    verdict = classify(exc)
    now = timezone.now()
    mail.error = str(exc)
    if verdict in ("throttled", "server_down"):
        # Not the mail's fault: the attempt is not counted against it.
        wait = THROTTLE_WAIT if verdict == "throttled" else SERVER_DOWN_WAIT
        mail.attempts -= 1
        _retry_or_expire(mail, now + wait, result)
        result.resume_in = wait
        if verdict == "server_down":
            result.server_error = str(exc)
        logger.warning("Mail %s not sent (%s, waiting %s): %s", mail.pk, verdict, wait, exc)
        return True
    if verdict == "retry":
        _retry_or_expire(mail, now + KINDS[mail.kind].retry_wait(mail.attempts), result)
        logger.warning(
            "Mail %s to %s refused for now, attempt %d: %s", mail.pk, mail.recipients, mail.attempts, exc
        )
        return False
    mail.status = Status.FAILED
    mail.save(update_fields=["status", "error"])
    result.failed.append(mail)
    logger.error("Mail %s to %s failed after %d attempt(s): %s", mail.pk, mail.recipients, mail.attempts, exc)
    return False


def _retry_or_expire(mail: OutgoingMail, next_attempt_at: datetime, result: Delivery) -> None:
    if next_attempt_at >= mail.expires_at:
        _expire(mail, result)
        return
    mail.status, mail.next_attempt_at = Status.QUEUED, next_attempt_at
    mail.save(update_fields=["status", "attempts", "next_attempt_at", "error"])


# ---- sending at once ----


def _send_at_once() -> None:
    """Send the due mail of the kinds sent at once (a password reset), from this process: no waiting
    for a worker busy with something else, and in a thread of its own, so the request that queued it
    does not wait for the mail server either. One try each — a mail that does not go through is put
    back for deliver_mail, which retries it by its kind's rules and reports a failure as a failed job."""
    handed_over = False
    try:
        while True:
            with _exclusive() as got:
                if not got:  # a deliver() is running: it sends this mail first, and checks again after
                    return
                handed_over = _send_due_at_once()
            if handed_over or not _due(timezone.now(), _SENT_AT_ONCE).exists():
                break
    except Exception:
        logger.exception("Sending mail at once failed; deliver_mail sends it instead.")
        handed_over = True
    finally:
        # This thread's own connection: under the ASGI pool it is only handed back when closed. (Never
        # inside a transaction in its own thread; a test calling this directly is in its test's.)
        if not connection.in_atomic_block:
            connection.close()
    if handed_over:
        _queue_deliver_mail()


def _send_due_at_once() -> bool:
    """Returns whether a mail was put back for deliver_mail."""
    while True:
        now = timezone.now()
        mail = _due(now, _SENT_AT_ONCE).first()
        if mail is None:
            return False
        _claim(mail, now)
        try:
            _send(mail)
        except Exception as exc:
            mail.status, mail.attempts, mail.error = Status.QUEUED, mail.attempts - 1, str(exc)
            mail.save(update_fields=["status", "attempts", "error"])
            logger.warning("Mail %s not sent at once; deliver_mail will retry it: %s", mail.pk, exc)
            return True
        _mark_sent(mail)


# ---- after the fact ----


def can_send_again(mail: OutgoingMail, now: datetime | None = None) -> bool:
    """A failed or cut-off mail can be sent again while it still has its content and its lifetime."""
    now = now or timezone.now()
    return mail.status in (Status.FAILED, Status.UNKNOWN) and bool(mail.payload) and mail.expires_at > now


def send_again(mails: Collection[OutgoingMail]) -> int:
    """Queue these failed or cut-off mails again, as they were (can_send_again). Returns how many."""
    now = timezone.now()
    ids = [mail.pk for mail in mails if can_send_again(mail, now)]
    count = OutgoingMail.objects.filter(pk__in=ids).update(
        status=Status.QUEUED, attempts=0, next_attempt_at=now, error=""
    )
    if count:
        _schedule_delivery(None)
    return count


def purge(now: datetime | None = None) -> tuple[int, int]:
    """Empty the content of failed and cut-off mails whose lifetime has run out — it may hold a reset
    link or a statement, and such a mail can no longer be sent again — and delete mails older than
    RETENTION that are done with. Returns (emptied, deleted)."""
    now = now or timezone.now()
    emptied = (
        OutgoingMail.objects.filter(status__in=[Status.FAILED, Status.UNKNOWN], expires_at__lte=now)
        .exclude(payload={})
        .update(payload={})
    )
    deleted, _ = (
        OutgoingMail.objects.filter(created_at__lt=now - RETENTION)
        .exclude(status__in=[Status.QUEUED, Status.SENDING])
        .delete()
    )
    return emptied, deleted
