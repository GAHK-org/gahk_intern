"""Demo mail for `manage.py seed_demo`: an outbox (core.mail) with a mail of every kind and status.

Every mail is made by the code that sends it in real life — a password reset through the reset form,
GAHK-konto's monthly statements, the ølkælder's debt warnings, the admissions auto-reply, the
administrators' monthly summary — so subject, recipients, kind and lifetime are what the app stores
(and, on a branch that adds a sender name or Reply-To, what it adds). Then each is given the state the deliverer
(core.mail.deliver) would have left it in, at a time relative to now: content emptied once sent or
expired, an error text as smtplib words it, attempts counted the way deliver counts them.

What it shows:

  * GAHK-konto's monthly statements for last month (signed PDFs; the ølkælder seed's history reaches
    back into it), a bulk send under way: sent in batches of MAIL_RATE_LIMIT, the last one two minutes ago, so the window is full and the rest wait for it — with one recipient's mailbox
    full (waiting again) and one address gone (failed);
  * password resets (interactive): one sent at once, one greylisted and waiting again, one given up on
    while one.com throttled the account;
  * debt warnings (normal): sent, refused with its content and lifetime left (so "Send igen" works),
    one being sent right now, one cut off mid-send (unknown), one expired during a mail-server outage,
    and an old failure whose content the daily purge has emptied;
  * an admissions auto-reply for each application of the last 90 days, and this month's admin summary,
    so the period filters (24 hours, 7 and 30 days, all) each pick a different set.

Never sent, here or later by itself:
  * nothing is delivered while seeding: queued inside core.mail.without_delivery, no delivery job is
    started, and the states are written directly;
  * the mails left queued (the held bulk, the two waiting again) are sent by deliver_mail's next run
    once due — so they are only seeded when MAIL_DELIVERY_BACKEND reaches nobody (dev's console
    default, locmem, file, dummy). With a real backend they would go to the residents' gahk.dk
    addresses, so the seed leaves them out;
  * the rate limit: the seeded mails keep its window full for about four minutes after seeding, so a
    bulk mail you send yourself straight after waits that long (behind the three held ones); every
    other kind is never held. Long enough to see the page's "masseudsendelser venter", short enough
    not to stand in the way.

The statements really go out on the 1st; they are seeded as under way now because the rate limit
only holds mail in the five minutes after a batch. Alumneregninger, the other bulk mail,
are left out: their mail is made from e-conomic, and an AlumneregningSend row would mark a period as
sent to that resident, so a send you try yourself would skip them (and dev only logs them anyway,
ALUMNEREGNING_SEND_MAIL).
"""

from __future__ import annotations

import random
import smtplib
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from residents.models import Account

from . import mail as outbox
from .models import OutgoingMail

Status = OutgoingMail.Status

# Backends that deliver to nobody: a mail left queued may be "sent" through them by the next run.
_DELIVER_NOWHERE = frozenset(
    f"django.core.mail.backends.{name}.EmailBackend" for name in ("console", "locmem", "filebased", "dummy")
)
HELD_BULK = 3  # bulk mails left waiting for the rate window
LAST_BATCH_AGO = timedelta(minutes=2)
# How deliver() paces bulk mail: a full batch, then a wait until the newest leaves the window.
BATCH_GAP = settings.MAIL_RATE_WINDOW + timedelta(seconds=5)


def seed(residents: list[Account], now: datetime, rng: random.Random) -> int:
    """Returns how many mails it seeded."""
    may_wait = settings.MAIL_DELIVERY_BACKEND in _DELIVER_NOWHERE
    _bulk_overview(now, may_wait)
    _resets(residents, now, may_wait)
    _debt_warnings(now, rng)
    _admissions_replies(now, rng)
    _admin_summary(now)
    return OutgoingMail.objects.count()


# ---- the mails ----


def _bulk_overview(now: datetime, may_wait: bool) -> None:
    from credit import statements

    year, month = statements.previous_month(timezone.localdate(now))
    mails = _queued(lambda: statements.send_month(year, month))
    limit = settings.MAIL_RATE_LIMIT
    # Held back only when the batch before them fills the window; with fewer shoppers than that, the
    # deliverer would have sent them all.
    held = HELD_BULK if may_wait and len(mails) - HELD_BULK >= limit else 0
    tried = mails[: len(mails) - held]  # the rest stay as queue() stored them: queued, due, held
    # Batches counted back from the last, which is full; the first is what was left over.
    batches = [tried[max(end - limit, 0) : end] for end in range(len(tried), 0, -limit)][::-1]
    first_batch_at = now - LAST_BATCH_AGO - (len(batches) - 1) * BATCH_GAP
    for mail in mails:
        _at(mail, first_batch_at - timedelta(seconds=5))

    outcomes: dict[int, Callable[[OutgoingMail, datetime], None]] = {}
    if len(tried) >= 2:
        # An address that is gone fails outright, and the run goes on with the rest.
        outcomes[batches[0][-1].pk] = lambda m, at: _failed(
            m, at, _refused(m, 550, "5.1.1 Recipient address rejected")
        )
        if may_wait:
            # A full mailbox is about the one recipient: tried again after its kind's wait.
            outcomes[batches[-1][0].pk] = lambda m, at: _retrying(
                m, 1, at, _refused(m, 452, "4.2.2 Mailbox full")
            )
    for number, batch in enumerate(batches):
        for i, mail in enumerate(batch):
            outcomes.get(mail.pk, _sent)(mail, first_batch_at + number * BATCH_GAP + timedelta(seconds=i))


def _resets(residents: list[Account], now: datetime, may_wait: bool) -> None:
    emails = {r.email for r in residents}

    if "beboer@gahk.dk" in emails:
        # The usual case: sent at once by the web process, a second after the form was posted.
        for mail in _queued(lambda: _reset("beboer@gahk.dk")):
            _at(mail, now - timedelta(minutes=3))
            _sent(mail, mail.created_at + timedelta(seconds=1))

    if may_wait and "oel@gahk.dk" in emails:
        # A receiving server greylisting a first-time sender: two tries, the next after its kind's wait.
        for mail in _queued(lambda: _reset("oel@gahk.dk")):
            _at(mail, now - timedelta(minutes=6))
            greylisted = _refused(mail, 450, "4.7.1 Greylisted, please try again later")
            _retrying(mail, 2, now - timedelta(minutes=1), greylisted)

    if "ak@gahk.dk" in emails:
        # one.com throttling the account until the reset's hour ran out. A throttle is not the mail's
        # fault, so deliver never counted an attempt against it.
        for mail in _queued(lambda: _reset("ak@gahk.dk")):
            _at(mail, now - timedelta(days=3, hours=2))
            throttled = smtplib.SMTPDataError(
                451, b"4.7.1 [R2] Too many mails received from you within the last 5 minutes"
            )
            _expired(
                mail, attempts=0, last_attempt_at=mail.expires_at - timedelta(minutes=4), error=throttled
            )


def _debt_warnings(now: datetime, rng: random.Random) -> None:
    from oelkaelder.models import Shopper
    from oelkaelder.services import balances, send_debt_warnings

    def sent(mail: OutgoingMail) -> None:
        # Normal mail goes out on the worker's next run.
        _at(mail, now - timedelta(hours=2))
        _sent(mail, mail.created_at + timedelta(seconds=4))

    def failed(mail: OutgoingMail) -> None:
        # Refused outright, hours ago: its content and lifetime are left, so "Send igen" works.
        _at(mail, now - timedelta(hours=6))
        _failed(
            mail,
            mail.created_at + timedelta(seconds=3),
            _refused(mail, 554, "5.7.1 Message rejected as spam"),
        )

    def sending(mail: OutgoingMail) -> None:
        # Being sent this very moment. Still "sending" 20 minutes on, deliver_mail calls it unknown.
        _at(mail, now - timedelta(minutes=1))
        _set(mail, status=Status.SENDING, attempts=1, last_attempt_at=now - timedelta(seconds=10))

    def unknown(mail: OutgoingMail) -> None:
        # Cut off mid-send (a deploy): whether it arrived is unknown, so only a person sends it again.
        _at(mail, now - timedelta(hours=3))
        _set(
            mail,
            status=Status.UNKNOWN,
            attempts=1,
            last_attempt_at=mail.created_at + timedelta(seconds=5),
            error=outbox.LOST_ERROR,
        )

    def expired_in_outage(mail: OutgoingMail) -> None:
        # The mail server refusing our login for longer than a normal mail's day: not the mail's fault,
        # so no attempt is counted, and it expires with the server's last answer.
        _at(mail, now - timedelta(days=10))
        down = smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")
        _expired(mail, attempts=0, last_attempt_at=mail.expires_at - timedelta(minutes=3), error=down)

    def failed_and_purged(mail: OutgoingMail) -> None:
        # Failed long ago: its lifetime is over, so the daily purge has emptied it — no "Send igen".
        _at(mail, now - timedelta(days=12))
        _failed(
            mail,
            mail.created_at + timedelta(seconds=3),
            _refused(mail, 552, "5.2.2 Mailbox full, over quota"),
        )
        _set(mail, payload={})

    cases = [sent, failed, sending, unknown, expired_in_outage, failed_and_purged]
    # The shoppers deepest in debt, each by the purchase that took them down through the 0 kr. warning
    # (oelkaelder 0004).
    active = list(Shopper.objects.filter(active=True).select_related("resident"))
    balance = balances(active)
    shoppers = sorted(active, key=lambda s: (balance[s.pk], s.pk))
    for shopper, case in zip(shoppers, cases, strict=False):
        new_ore = -rng.randint(1, 40) * 100
        for mail in _queued(partial(send_debt_warnings, shopper, new_ore + 5000, new_ore)):
            case(mail)


def _admissions_replies(now: datetime, rng: random.Random) -> None:
    """The "vi har modtaget din henvendelse" of each application still within the outbox's retention,
    queued as gahk.dk's form reached intern, and sent on the worker's next run."""
    from admissions.models import Application
    from admissions.services import send_auto_reply

    for application in Application.objects.filter(submitted_at__gt=now - outbox.RETENTION).order_by("pk"):
        for mail in _queued(partial(send_auto_reply, application)):
            queued_at = application.submitted_at + timedelta(minutes=rng.randint(1, 5))
            _at(mail, min(queued_at, now - timedelta(minutes=1)))
            _sent(mail, mail.created_at + timedelta(seconds=rng.randint(2, 30)))


def _admin_summary(now: datetime) -> None:
    """Sent on the 1st at 09:05 (CELERY_BEAT_SCHEDULE) to everyone with Django-admin access."""
    from .tasks import send_admin_monthly_summary

    local = timezone.localtime(now)
    at = local.replace(day=1, hour=9, minute=5, second=0, microsecond=0)
    if at > local:  # seeded on the 1st before then: last month's
        at = (at - timedelta(days=1)).replace(day=1)
    for mail in _queued(send_admin_monthly_summary):
        _at(mail, at)
        _sent(mail, at + timedelta(seconds=2))


def _reset(email: str) -> None:
    from residents.forms import ResidentPasswordResetForm

    form = ResidentPasswordResetForm({"email": email})
    if form.is_valid():
        site = urlsplit(settings.SITE_URL)
        form.save(domain_override=site.netloc, use_https=site.scheme == "https")


# ---- the deliverer's states (core.mail) ----


def _queued(send: Callable[[], object]) -> list[OutgoingMail]:
    """The mails `send` queued through the real path (send_mail → OutboxBackend → core.mail.queue),
    with no delivery started for them."""
    before = OutgoingMail.objects.aggregate(last=Max("pk"))["last"] or 0
    with outbox.without_delivery():
        send()
    return list(OutgoingMail.objects.filter(pk__gt=before).order_by("pk"))


def _at(mail: OutgoingMail, created_at: datetime) -> None:
    """Move the mail to when it was queued, keeping the lifetime queue() gave it."""
    shift = created_at - mail.created_at
    _set(
        mail,
        created_at=created_at,
        next_attempt_at=mail.next_attempt_at + shift,
        expires_at=mail.expires_at + shift,
    )


def _sent(mail: OutgoingMail, attempt_at: datetime) -> None:
    # _claim + _mark_sent: the content goes once delivered.
    _set(
        mail,
        status=Status.SENT,
        attempts=mail.attempts + 1,
        last_attempt_at=attempt_at,
        sent_at=attempt_at + timedelta(seconds=1),
        payload={},
        error="",
    )


def _retrying(mail: OutgoingMail, attempts: int, attempt_at: datetime, error: str) -> None:
    # _settle_failure's "retry": queued again after its kind's wait for this attempt, content kept.
    _set(
        mail,
        status=Status.QUEUED,
        attempts=attempts,
        last_attempt_at=attempt_at,
        next_attempt_at=attempt_at + outbox.KINDS[mail.kind].retry_wait(attempts),
        error=error,
    )


def _failed(mail: OutgoingMail, attempt_at: datetime, error: str) -> None:
    # _settle_failure's "failed": the content stays until its lifetime ends (purge), for "Send igen".
    _set(mail, status=Status.FAILED, attempts=mail.attempts + 1, last_attempt_at=attempt_at, error=error)


def _expired(mail: OutgoingMail, *, attempts: int, last_attempt_at: datetime, error: Exception) -> None:
    # _expire: given up on, content emptied, the last error kept after the reason.
    _set(
        mail,
        status=Status.EXPIRED,
        attempts=attempts,
        last_attempt_at=last_attempt_at,
        payload={},
        error=f"{outbox.EXPIRED_ERROR} Sidste fejl: {error}",
    )


def _refused(mail: OutgoingMail, code: int, text: str) -> str:
    """The error as deliver stores it: str() of smtplib's exception for a refused recipient."""
    recipient = mail.recipients.split(",")[0].strip()
    return str(smtplib.SMTPRecipientsRefused({recipient: (code, text.encode())}))


def _set(mail: OutgoingMail, **fields: object) -> None:
    OutgoingMail.objects.filter(pk=mail.pk).update(**fields)
    for name, value in fields.items():
        setattr(mail, name, value)
