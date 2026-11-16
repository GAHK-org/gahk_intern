"""core.mail — every mail is stored, then delivered within the mail server's rate limit (#397)."""

import smtplib
from collections.abc import Iterator
from datetime import timedelta

import pytest
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


@pytest.fixture(autouse=True)
def _outbox() -> Iterator[None]:
    """What production has: send_mail stores, deliver() sends through the (here locmem) real backend."""
    with override_settings(
        EMAIL_BACKEND="core.mail.OutboxBackend",
        MAIL_DELIVERY_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        MAIL_RATE_LIMIT=3,
    ):
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
def test_no_more_bulk_mail_than_the_rate_limit_goes_out_in_a_window() -> None:
    with outbox.bulk():
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
    with outbox.bulk():
        send_mail("Alumneregning", "Tekst", "regnskab@gahk.dk", ["alumne@gahk.dk"])
    send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["beboer@gahk.dk"])

    outbox.deliver()

    assert [m.subject for m in mail.outbox] == ["Nulstil kodeord", "Alumneregning"]


@pytest.mark.django_db
def test_a_password_reset_during_a_monthly_send_goes_out_at_once() -> None:
    """And counts against the bulk mail's room, so together they stay below one.com's limit."""
    with outbox.bulk():
        _queue(4)

    assert outbox.deliver().sent == 3  # MAIL_RATE_LIMIT
    send_mail("Nulstil kodeord", "Tekst", "autosvar@gahk.dk", ["beboer@gahk.dk"])
    result = outbox.deliver()

    assert result.sent == 1
    assert mail.outbox[-1].subject == "Nulstil kodeord"
    assert result.resume_in is not None
    assert OutgoingMail.objects.filter(bulk=True, status=Status.QUEUED).count() == 1


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
def test_a_mail_that_keeps_being_refused_for_now_fails_in_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue(1)
    OutgoingMail.objects.update(attempts=outbox.MAX_ATTEMPTS - 1)
    _refuse_with(monkeypatch, smtplib.SMTPRecipientsRefused({"r0@gahk.dk": (450, b"Mailbox busy")}))

    assert len(outbox.deliver().failed) == 1


@pytest.mark.django_db
def test_a_server_that_is_down_fails_the_job_but_no_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue(1)
    _refuse_with(monkeypatch, ConnectionRefusedError("refused"))

    with pytest.raises(tasks.MailNotDelivered, match="Mailserveren svarer ikke"):
        tasks.deliver_mail()

    assert OutgoingMail.objects.get().status == Status.QUEUED


@pytest.mark.django_db
def test_a_mail_left_sending_by_a_dead_job_is_failed_not_sent_twice() -> None:
    _queue(1)
    OutgoingMail.objects.update(
        status=Status.SENDING,
        last_attempt_at=timezone.now() - outbox.SENDING_LOST_AFTER - timedelta(minutes=1),
    )

    result = outbox.deliver()

    assert [m.error for m in result.failed] == [outbox.LOST_ERROR]
    assert mail.outbox == []
