"""core.mail — every mail is stored, then delivered within the mail server's rate limit (#397)."""

import smtplib
from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.mail import EmailMessage, EmailMultiAlternatives, send_mail
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from core import mail as outbox
from core import tasks
from core.models import OutgoingMail

Status = OutgoingMail.Status
Kind = OutgoingMail.Kind


@pytest.fixture(autouse=True)
def _outbox(mail_stays_queued: None) -> Iterator[None]:
    """What production has: send_mail stores, deliver() sends through the (here locmem) real backend."""
    with override_settings(MAIL_RATE_LIMIT=3):
        cache.delete(tasks._RESUME_SCHEDULED)
        yield


def _queue(n: int) -> None:
    for i in range(n):
        send_mail(f"Mail {i}", "Hej", "autosvar@gahk.dk", [f"r{i}@gahk.dk"])


def _refuse_with(monkeypatch: pytest.MonkeyPatch, exc: Exception, *, only: str | None = None) -> None:
    real_send = EmailMessage.send

    def send(self: EmailMessage, *args: object, **kwargs: object) -> int:
        if only is None or self.to == [only]:
            raise exc
        return real_send(self, *args, **kwargs)

    monkeypatch.setattr(EmailMessage, "send", send)


@pytest.mark.django_db
def test_send_mail_only_stores_the_mail() -> None:
    assert send_mail("Hej", "Tekst", "autosvar@gahk.dk", ["a@gahk.dk"]) == 1

    assert mail.outbox == []
    assert OutgoingMail.objects.get().status == Status.QUEUED


@pytest.mark.django_db
def test_a_mail_is_stored_only_if_its_transaction_commits() -> None:
    with pytest.raises(RuntimeError), transaction.atomic():
        send_mail("Hej", "Tekst", "autosvar@gahk.dk", ["a@gahk.dk"])
        raise RuntimeError

    assert not OutgoingMail.objects.exists()


@pytest.mark.django_db
def test_the_mail_is_delivered_as_it_was_written() -> None:
    message = EmailMultiAlternatives(
        "Alumneregning", "Tekst", "Regnskabsgruppen <regnskab@gahk.dk>", ["a@gahk.dk"], cc=["c@gahk.dk"]
    )
    message.attach_alternative("<p>Tekst</p>", "text/html")
    message.attach("regning.pdf", b"%PDF-1.4 \x00\xff", "application/pdf")
    message.send()

    assert outbox.deliver().sent == 1

    [sent] = mail.outbox
    assert (sent.subject, sent.body, sent.from_email, sent.to, sent.cc) == (
        "Alumneregning",
        "Tekst",
        "Regnskabsgruppen <regnskab@gahk.dk>",
        ["a@gahk.dk"],
        ["c@gahk.dk"],
    )
    assert sent.alternatives == [("<p>Tekst</p>", "text/html")]  # type: ignore[attr-defined]
    assert sent.attachments == [("regning.pdf", b"%PDF-1.4 \x00\xff", "application/pdf")]
    stored = OutgoingMail.objects.get()
    assert stored.status == Status.SENT
    assert stored.payload == {}  # a reset link or a PDF is not kept once delivered


@pytest.mark.django_db
def test_every_mail_says_it_is_automatic_and_where_replies_go() -> None:
    """Third-party mail too (send_mail is how Django's password reset sends): replies go to
    DEFAULT_REPLY_TO, and out-of-office replies are not sent back at all."""
    send_mail("Nulstil kodeord", "Tekst", None, ["beboer@gahk.dk"])
    outbox.deliver()

    [sent] = mail.outbox
    assert sent.from_email == f"GAHK Intern <{settings.MAIL_SENDER}>"
    assert sent.reply_to == [settings.DEFAULT_REPLY_TO]
    headers = sent.message()
    assert (headers["Auto-Submitted"], headers["X-Auto-Response-Suppress"]) == ("auto-generated", "All")
    assert headers["Reply-To"] == settings.DEFAULT_REPLY_TO


@pytest.mark.django_db
def test_a_mail_keeps_its_own_reply_to_and_headers() -> None:
    EmailMessage(
        "Alumneregning",
        "Tekst",
        outbox.sender("GAHK Regnskabsgruppen"),
        ["alumne@gahk.dk"],
        reply_to=["regnskab@gahk.dk"],
        headers={"Auto-Submitted": "auto-replied"},
    ).send()
    outbox.deliver()

    [sent] = mail.outbox
    assert sent.from_email == f"GAHK Regnskabsgruppen <{settings.MAIL_SENDER}>"
    assert sent.reply_to == ["regnskab@gahk.dk"]
    assert sent.message()["Auto-Submitted"] == "auto-replied"


@pytest.mark.django_db
def test_no_more_bulk_mail_than_the_rate_limit_goes_out_in_a_window() -> None:
    with outbox.kind(Kind.BULK):
        _queue(5)

    result = outbox.deliver()

    assert result.sent == 3
    assert result.resume_in is not None and timedelta(minutes=4) < result.resume_in <= timedelta(minutes=5)
    assert OutgoingMail.objects.filter(status=Status.QUEUED).count() == 2

    OutgoingMail.objects.update(last_attempt_at=timezone.now() - timedelta(minutes=6))  # the window passes
    assert outbox.deliver().sent == 2


@pytest.mark.django_db
def test_other_mail_is_never_held_by_the_rate_limit() -> None:
    _queue(5)

    result = outbox.deliver()

    assert result.sent == 5
    assert result.resume_in is None


@pytest.mark.django_db
def test_other_mail_goes_out_before_bulk_queued_earlier() -> None:
    with outbox.kind(Kind.BULK):
        send_mail("Alumneregning", "Tekst", "regnskab@gahk.dk", ["alumne@gahk.dk"])
    send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["beboer@gahk.dk"])

    outbox.deliver()

    assert [m.subject for m in mail.outbox] == ["Nulstil kodeord", "Alumneregning"]


@pytest.mark.django_db
def test_a_password_reset_during_a_monthly_send_goes_out_at_once() -> None:
    """And counts against the bulk mail's room, so together they stay below one.com's limit."""
    with outbox.kind(Kind.BULK):
        _queue(4)

    assert outbox.deliver().sent == 3  # MAIL_RATE_LIMIT
    send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["beboer@gahk.dk"])
    result = outbox.deliver()

    assert result.sent == 1
    assert mail.outbox[-1].subject == "Nulstil kodeord"
    assert result.resume_in is not None
    assert OutgoingMail.objects.filter(kind=Kind.BULK, status=Status.QUEUED).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "refusal",
    [
        smtplib.SMTPRecipientsRefused({"r0@gahk.dk": (451, b"4.7.1 [R2] Too many mails")}),
        smtplib.SMTPDataError(550, b"Too many mails received from x within the last 5 minutes"),
    ],
)
def test_a_throttle_stops_delivery_and_keeps_the_mail_queued(
    monkeypatch: pytest.MonkeyPatch, refusal: Exception
) -> None:
    """Whatever the code: the throttle is the account's, not the mail's, and it is not failed for it."""
    _queue(2)
    _refuse_with(monkeypatch, refusal)

    result = outbox.deliver()

    assert result.sent == 0
    assert result.failed == []
    assert result.resume_in == outbox.THROTTLE_WAIT
    first = OutgoingMail.objects.get(recipients="r0@gahk.dk")
    assert (first.status, first.attempts) == (Status.QUEUED, 0)
    assert first.next_attempt_at > timezone.now() + timedelta(minutes=5)
    assert OutgoingMail.objects.get(recipients="r1@gahk.dk").last_attempt_at is None  # not tried


@pytest.mark.django_db
def test_a_full_mailbox_is_retried_for_that_mail_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue(2)
    _refuse_with(
        monkeypatch, smtplib.SMTPRecipientsRefused({"r0@gahk.dk": (452, b"Mailbox full")}), only="r0@gahk.dk"
    )

    result = outbox.deliver()

    assert result.sent == 1
    full = OutgoingMail.objects.get(recipients="r0@gahk.dk")
    assert (full.status, full.attempts) == (Status.QUEUED, 1)
    assert full.next_attempt_at > timezone.now()


@pytest.mark.django_db
def test_a_refusal_outright_fails_the_mail_and_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue(2)
    _refuse_with(
        monkeypatch, smtplib.SMTPRecipientsRefused({"r0@gahk.dk": (550, b"No such user")}), only="r0@gahk.dk"
    )

    with pytest.raises(tasks.MailNotDelivered, match=r"1 sendt\. 1 fejlede: r0@gahk\.dk"):
        tasks.deliver_mail()

    assert OutgoingMail.objects.get(recipients="r0@gahk.dk").status == Status.FAILED
    assert [m.to for m in mail.outbox] == [["r1@gahk.dk"]]


@pytest.mark.django_db
def test_a_mail_that_keeps_being_refused_for_now_expires_with_its_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _queue(1)
    OutgoingMail.objects.update(expires_at=timezone.now() + timedelta(minutes=5))  # less than a retry wait
    _refuse_with(monkeypatch, smtplib.SMTPRecipientsRefused({"r0@gahk.dk": (450, b"Mailbox busy")}))

    with pytest.raises(tasks.MailNotDelivered, match=r"1 udløb: r0@gahk\.dk"):
        tasks.deliver_mail()

    expired = OutgoingMail.objects.get()
    assert expired.status == Status.EXPIRED
    assert expired.payload == {}
    assert expired.error.startswith(outbox.EXPIRED_ERROR) and "Mailbox busy" in expired.error


@pytest.mark.django_db
def test_a_server_down_for_a_moment_neither_fails_the_job_nor_the_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _queue(1)
    _refuse_with(monkeypatch, ConnectionRefusedError("refused"))

    assert tasks.deliver_mail() == 0

    assert OutgoingMail.objects.get().status == Status.QUEUED


@pytest.mark.django_db
def test_a_server_down_long_enough_to_hold_mail_up_fails_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue(1)
    OutgoingMail.objects.update(
        created_at=timezone.now() - outbox.SERVER_DOWN_REPORT_AFTER - timedelta(minutes=1)
    )
    _refuse_with(monkeypatch, ConnectionRefusedError("refused"))

    with pytest.raises(tasks.MailNotDelivered, match="Mailserveren har ikke svaret"):
        tasks.deliver_mail()

    assert OutgoingMail.objects.get().status == Status.QUEUED


@pytest.mark.django_db
def test_a_mail_left_sending_by_a_dead_job_is_unknown_not_sent_twice() -> None:
    _queue(1)
    OutgoingMail.objects.update(
        status=Status.SENDING,
        last_attempt_at=timezone.now() - outbox.SENDING_LOST_AFTER - timedelta(minutes=1),
    )

    result = outbox.deliver()

    assert [(m.status, m.error) for m in result.failed] == [(Status.UNKNOWN, outbox.LOST_ERROR)]
    assert OutgoingMail.objects.get().payload != {}  # kept, so it can be sent again on purpose
    assert mail.outbox == []


# ---- kinds and lifetimes ----


@pytest.mark.django_db
def test_mail_goes_out_by_kind_interactive_first_bulk_last() -> None:
    with outbox.kind(Kind.BULK):
        send_mail("Alumneregning", "Tekst", "regnskab@gahk.dk", ["alumne@gahk.dk"])
    send_mail("Auto-svar", "Tekst", "autosvar@gahk.dk", ["ansoeger@gahk.dk"])
    with outbox.kind(Kind.INTERACTIVE):
        send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["beboer@gahk.dk"])

    outbox.deliver()

    assert [m.subject for m in mail.outbox] == ["Nulstil kodeord", "Auto-svar", "Alumneregning"]


@pytest.mark.django_db
def test_a_mail_lives_as_long_as_its_kind_unless_given_a_lifetime_of_its_own() -> None:
    send_mail("Auto-svar", "Tekst", "autosvar@gahk.dk", ["a@gahk.dk"])
    with outbox.kind(Kind.INTERACTIVE, lifetime=timedelta(minutes=30)):
        send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["b@gahk.dk"])

    lifetimes = {m.kind: m.expires_at - m.created_at for m in OutgoingMail.objects.all()}

    assert abs(lifetimes[Kind.NORMAL] - timedelta(days=1)) < timedelta(seconds=1)
    assert abs(lifetimes[Kind.INTERACTIVE] - timedelta(minutes=30)) < timedelta(seconds=1)


@pytest.mark.django_db
def test_a_mail_still_queued_past_its_lifetime_is_given_up_on() -> None:
    _queue(1)
    OutgoingMail.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    with pytest.raises(tasks.MailNotDelivered, match="1 udløb"):
        tasks.deliver_mail()

    assert OutgoingMail.objects.get().status == Status.EXPIRED
    assert mail.outbox == []


@pytest.mark.django_db
def test_a_throttle_that_outlasts_a_mails_lifetime_expires_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue(1)
    OutgoingMail.objects.update(expires_at=timezone.now() + timedelta(minutes=2))  # less than THROTTLE_WAIT
    _refuse_with(monkeypatch, smtplib.SMTPRecipientsRefused({"r0@gahk.dk": (451, b"Too many mails")}))

    result = outbox.deliver()

    assert [m.status for m in result.expired] == [Status.EXPIRED]
    assert OutgoingMail.objects.get().attempts == 0  # a throttle is not the mail's attempt


@pytest.mark.django_db
def test_an_interactive_mail_is_retried_within_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    with outbox.kind(Kind.INTERACTIVE):
        send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["r0@gahk.dk"])
    _refuse_with(monkeypatch, smtplib.SMTPRecipientsRefused({"r0@gahk.dk": (452, b"Mailbox full")}))

    outbox.deliver()

    waiting = OutgoingMail.objects.get()
    assert waiting.status == Status.QUEUED
    assert waiting.next_attempt_at - timezone.now() <= timedelta(minutes=1)


# ---- not left waiting ----


@pytest.mark.django_db
def test_mail_queued_while_a_run_finishes_is_sent_by_that_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its own run is turned away by the lock the finishing run still holds; the finishing run checks
    again once it lets go, so the mail does not wait for Beat."""
    _queue(1)
    real_send_due = outbox._send_due
    calls = []

    def send_due(result: outbox.Delivery) -> bool:
        stopped = real_send_due(result)
        if not calls:
            send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["late@gahk.dk"])
        calls.append(1)
        return stopped

    monkeypatch.setattr(outbox, "_send_due", send_due)

    result = outbox.deliver()

    assert result.sent == 2
    assert [m.to for m in mail.outbox] == [["r0@gahk.dk"], ["late@gahk.dk"]]


@pytest.mark.django_db
def test_mail_due_in_no_time_still_schedules_the_next_run() -> None:
    _queue(1)
    OutgoingMail.objects.update(next_attempt_at=timezone.now() + timedelta(milliseconds=500))

    result = outbox.deliver()

    assert result.resume_in is not None and result.resume_in >= timedelta(seconds=1)


# ---- sending at once ----


@pytest.mark.django_db
def test_only_interactive_mail_is_also_sent_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[object] = []
    queued: list[None] = []

    class Thread:
        def __init__(self, target: object, name: str) -> None:
            self.target = target

        def start(self) -> None:
            started.append(self.target)

    monkeypatch.setattr(outbox.threading, "Thread", Thread)
    monkeypatch.setattr(outbox, "_queue_deliver_mail", lambda: queued.append(None))

    outbox._start_delivery(Kind.NORMAL)
    outbox._start_delivery(Kind.BULK)
    assert (started, len(queued)) == ([], 2)

    outbox._start_delivery(Kind.INTERACTIVE)
    assert (started, len(queued)) == ([outbox._send_at_once], 3)  # the worker's run as the fallback


@pytest.mark.django_db
def test_sending_at_once_sends_interactive_mail_only() -> None:
    _queue(1)
    with outbox.kind(Kind.INTERACTIVE):
        send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["beboer@gahk.dk"])

    outbox._send_at_once()

    assert [m.subject for m in mail.outbox] == ["Nulstil kodeord"]
    assert OutgoingMail.objects.get(kind=Kind.NORMAL).status == Status.QUEUED


@pytest.mark.django_db
def test_a_mail_not_sent_at_once_is_handed_to_the_worker_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    with outbox.kind(Kind.INTERACTIVE):
        send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["beboer@gahk.dk"])
    _refuse_with(monkeypatch, smtplib.SMTPRecipientsRefused({"beboer@gahk.dk": (550, b"No such user")}))
    queued: list[None] = []
    monkeypatch.setattr(outbox, "_queue_deliver_mail", lambda: queued.append(None))

    outbox._send_at_once()

    waiting = OutgoingMail.objects.get()
    assert (waiting.status, waiting.attempts) == (Status.QUEUED, 0)  # deliver_mail judges and reports it
    assert "No such user" in waiting.error
    assert queued == [None]


# ---- after the fact ----


@pytest.mark.django_db
def test_a_failed_mail_can_be_sent_again_while_it_has_content_and_lifetime() -> None:
    _queue(3)
    OutgoingMail.objects.update(status=Status.FAILED, error="550")
    OutgoingMail.objects.filter(recipients="r1@gahk.dk").update(payload={})  # replaced by a rerun
    OutgoingMail.objects.filter(recipients="r2@gahk.dk").update(expires_at=timezone.now())

    assert outbox.send_again(list(OutgoingMail.objects.all())) == 1

    again = OutgoingMail.objects.get(recipients="r0@gahk.dk")
    assert (again.status, again.error) == (Status.QUEUED, "")


@pytest.mark.django_db
def test_purge_empties_failed_mail_past_its_lifetime_and_deletes_old_mail() -> None:
    _queue(4)
    now = timezone.now()
    OutgoingMail.objects.filter(recipients="r0@gahk.dk").update(status=Status.FAILED, expires_at=now)
    OutgoingMail.objects.filter(recipients="r1@gahk.dk").update(status=Status.FAILED)  # can still be resent
    old = now - outbox.RETENTION - timedelta(days=1)
    OutgoingMail.objects.filter(recipients="r2@gahk.dk").update(status=Status.SENT, created_at=old)
    OutgoingMail.objects.filter(recipients="r3@gahk.dk").update(created_at=old)  # still queued: kept

    assert outbox.purge(now) == (1, 1)

    payloads = dict(OutgoingMail.objects.values_list("recipients", "payload"))
    assert payloads["r0@gahk.dk"] == {}
    assert payloads["r1@gahk.dk"] != {}
    assert "r2@gahk.dk" not in payloads
    assert "r3@gahk.dk" in payloads


# ---- configuration ----


def test_the_outbox_being_bypassed_is_warned_about(settings: object) -> None:
    from core.checks import check_mail_outbox

    assert check_mail_outbox(None) == []
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"  # type: ignore[attr-defined]
    assert [message.id for message in check_mail_outbox(None)] == ["core.W001"]


@pytest.mark.django_db
def test_check_mail_server_sends_past_the_outbox() -> None:
    from django.core.management import call_command

    call_command("check_mail_server", "you@example.com", "--from", "bierkeller@gahk.dk")

    [sent] = mail.outbox
    assert (sent.to, sent.from_email) == (["you@example.com"], "bierkeller@gahk.dk")
    assert not OutgoingMail.objects.exists()


@pytest.mark.django_db
def test_check_mail_server_fails_with_the_servers_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.core.management import CommandError, call_command

    _refuse_with(monkeypatch, smtplib.SMTPSenderRefused(550, b"not authorized to send on behalf of", "x"))

    with pytest.raises(CommandError, match="not authorized"):
        call_command("check_mail_server", "you@example.com")


# ---- who sends what ----


@pytest.mark.django_db
def test_a_password_reset_is_interactive_and_lives_half_as_long_as_its_link(
    client: object, make_resident: Callable, settings: object
) -> None:
    from django.urls import reverse

    make_resident(email="beboer@gahk.dk")

    client.post(reverse("password_reset"), {"email": "beboer@gahk.dk"})  # type: ignore[attr-defined]

    reset = OutgoingMail.objects.get()
    assert reset.kind == Kind.INTERACTIVE
    half = timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT) / 2  # type: ignore[attr-defined]
    assert abs(reset.expires_at - reset.created_at - half) < timedelta(seconds=1)


@pytest.mark.django_db
def test_a_welcome_mail_is_interactive(make_resident: Callable, rf: object) -> None:
    from residents.views import _send_welcome_email

    resident = make_resident(email="ny@gahk.dk")

    assert _send_welcome_email(rf.get("/"), resident)  # type: ignore[attr-defined]

    assert OutgoingMail.objects.get().kind == Kind.INTERACTIVE
