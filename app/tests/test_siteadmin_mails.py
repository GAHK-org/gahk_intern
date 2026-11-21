"""The mail page (core.views_admin): the outbox's mails, what waits and what did not go out."""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.mail import send_mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core import mail as outbox
from core.admin_summary import compose
from core.models import OutgoingMail
from residents.models import Resident, Role

Status = OutgoingMail.Status
Kind = OutgoingMail.Kind
LOCAL = ZoneInfo("Europe/Copenhagen")


@pytest.fixture(autouse=True)
def _queued(mail_stays_queued: None) -> None:
    """Mails stay where a test puts them."""


@pytest.fixture
def admin(client: Client, make_resident: Callable[..., Resident]) -> Client:
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    return client


def _mail(to: str, status: str = Status.QUEUED, kind: str = Kind.NORMAL, **fields: object) -> OutgoingMail:
    with outbox.kind(kind):
        send_mail(f"Til {to}", "Hemmeligt indhold", "autosvar@gahk.dk", [to])
    mail = OutgoingMail.objects.get(recipients=to)
    OutgoingMail.objects.filter(pk=mail.pk).update(status=status, **fields)
    mail.refresh_from_db()
    return mail


@pytest.mark.django_db
def test_the_mail_pages_are_for_administrators_only(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    mail = _mail("a@gahk.dk", Status.FAILED)
    client.force_login(make_resident())

    assert client.get(reverse("siteadmin:mails")).status_code == 403
    assert client.get(reverse("siteadmin:mail_detail", args=[mail.pk])).status_code == 403
    assert client.post(reverse("siteadmin:mail_send_again", args=[mail.pk])).status_code == 403
    assert client.get(reverse("siteadmin:mails_live")).status_code == 204


@pytest.mark.django_db
def test_the_page_lists_mails_but_never_what_they_say(admin: Client) -> None:
    _mail("sendt@gahk.dk", Status.SENT, sent_at=timezone.now())
    _mail("fejl@gahk.dk", Status.FAILED, error="550 No such user")
    _mail("venter@gahk.dk")

    text = admin.get(reverse("siteadmin:mails")).content.decode()

    for to in ("sendt@gahk.dk", "fejl@gahk.dk", "venter@gahk.dk"):
        assert to in text
    assert "550 No such user" in text
    assert "Hemmeligt indhold" not in text


@pytest.mark.django_db
def test_the_list_filters_by_status_kind_period_and_search(admin: Client) -> None:
    _mail("a@gahk.dk", Status.FAILED)
    _mail("b@gahk.dk", Status.EXPIRED, kind=Kind.BULK)
    _mail("c@gahk.dk", Status.UNKNOWN, kind=Kind.BULK)
    _mail("d@gahk.dk", Status.SENT, created_at=timezone.now() - timedelta(days=3))

    def listed(**query: str) -> set[str]:
        response = admin.get(reverse("siteadmin:mails"), query)
        return {row.mail.recipients for row in response.context["rows"]}

    assert listed(status="not_sent") == {"a@gahk.dk", "b@gahk.dk", "c@gahk.dk"}
    assert listed(status="not_sent", kind="bulk") == {"b@gahk.dk", "c@gahk.dk"}
    assert listed(status="sent") == {"d@gahk.dk"}
    assert listed(period="24h") == {"a@gahk.dk", "b@gahk.dk", "c@gahk.dk"}
    assert listed(q="c@gahk") == {"c@gahk.dk"}
    assert listed(status="nonsense") == {"a@gahk.dk", "b@gahk.dk", "c@gahk.dk", "d@gahk.dk"}


@pytest.mark.django_db
def test_the_top_shows_the_queue_the_rate_window_and_retries(admin: Client, settings: object) -> None:
    settings.MAIL_RATE_LIMIT = 2  # type: ignore[attr-defined]
    now = timezone.now()
    _mail("s1@gahk.dk", Status.SENT, sent_at=now, last_attempt_at=now)
    _mail("s2@gahk.dk", Status.SENT, sent_at=now, last_attempt_at=now)
    _mail("bulk@gahk.dk", kind=Kind.BULK)
    _mail("retry@gahk.dk", error="452 Mailbox full", attempts=1, next_attempt_at=now + timedelta(minutes=10))

    response = admin.get(reverse("siteadmin:mails_live"))

    text = response.content.decode()
    assert response.context["waiting"] == 2
    assert "2 / 2" in text
    assert "1 masseudsendelse venter" in text
    assert "Prøves igen (1)" in text and "452 Mailbox full" in text
    assert "<html" not in text


@pytest.mark.django_db
def test_a_failed_mail_is_sent_again_from_its_page(admin: Client) -> None:
    failed = _mail("a@gahk.dk", Status.FAILED, error="550 Sender refused")

    page = admin.get(reverse("siteadmin:mail_detail", args=[failed.pk])).content.decode()
    assert "Send igen" in page and "550 Sender refused" in page

    admin.post(reverse("siteadmin:mail_send_again", args=[failed.pk]))

    failed.refresh_from_db()
    assert (failed.status, failed.error) == (Status.QUEUED, "")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fields",
    [{"status": Status.SENT}, {"status": Status.FAILED, "payload": {}}, {"status": Status.EXPIRED}],
)
def test_a_mail_that_cannot_be_sent_again_offers_no_button_and_stays(admin: Client, fields: dict) -> None:
    mail = _mail("a@gahk.dk")
    OutgoingMail.objects.filter(pk=mail.pk).update(**fields)

    assert "Send igen" not in admin.get(reverse("siteadmin:mail_detail", args=[mail.pk])).content.decode()
    admin.post(reverse("siteadmin:mail_send_again", args=[mail.pk]))

    mail.refresh_from_db()
    assert mail.status == fields["status"]


@pytest.mark.django_db
def test_the_administration_page_counts_mails_not_sent_today(admin: Client) -> None:
    _mail("a@gahk.dk", Status.FAILED)

    text = admin.get(reverse("siteadmin:home")).content.decode()

    assert reverse("siteadmin:mails") in text
    assert "1 ikke sendt det seneste døgn" in text


@pytest.mark.django_db
def test_the_monthly_summary_counts_the_months_mail() -> None:
    for to, status, queued in [
        ("a@gahk.dk", Status.SENT, datetime(2026, 10, 3, 12, tzinfo=LOCAL)),
        ("b@gahk.dk", Status.FAILED, datetime(2026, 10, 9, 12, tzinfo=LOCAL)),
        ("c@gahk.dk", Status.EXPIRED, datetime(2026, 10, 31, 23, 30, tzinfo=LOCAL)),
        ("d@gahk.dk", Status.FAILED, datetime(2026, 11, 1, 0, 30, tzinfo=LOCAL)),  # November's
    ]:
        _mail(to, status, created_at=queued)

    _, body = compose(date(2026, 11, 1))

    assert "MAILS\nSendt: 1\nIkke sendt: 2\n  Fejlet: 1\n  Udløbet: 1\n" in body
    assert reverse("siteadmin:mails") + "?status=not_sent" in body
