"""The monthly ølkælder statement mail (oelkaelder.tasks.send_monthly_statements).

It went into the Celery PR with no test at all, which is how both of the bugs below survived: they
are content bugs, invisible to anything that only asserts "a mail was sent".
"""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from oelkaelder.models import Deposit, Shopper
from oelkaelder.tasks import send_monthly_statements
from residents.models import Resident


def _last_month_instant() -> timezone.datetime:
    """Some moment comfortably inside the previous calendar month."""
    first_this_month = timezone.localdate().replace(day=1)
    return timezone.make_aware(
        timezone.datetime.combine(first_this_month - timedelta(days=15), timezone.datetime.min.time())
    )


@pytest.mark.django_db
def test_the_statement_month_is_danish(make_resident: Callable[..., Resident]) -> None:
    """`f"{start:%B %Y}"` is strftime, which reads the process's C locale — nothing sets one, so
    every statement went out to a Danish house with an English month in the subject line."""
    shopper = Shopper.objects.create(resident=make_resident(email="buyer@gahk.dk"))
    Deposit.objects.create(shopper=shopper, amount_ore=10_000, created_at=_last_month_instant())

    send_monthly_statements.run()

    subject = mail.outbox[0].subject
    english = (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    )
    assert not any(month in subject for month in english), subject


@pytest.mark.django_db
def test_the_statement_reports_the_balance_at_the_end_of_its_own_month(
    make_resident: Callable[..., Resident],
) -> None:
    """It used to print `shopper.balance_ore`, which is the balance RIGHT NOW. A statement for last
    month, sent on the 1st, therefore quoted a figure that already included this month's activity —
    so its own four numbers did not add up, and the one people act on was the wrong one."""
    shopper = Shopper.objects.create(resident=make_resident(email="buyer@gahk.dk"))
    Deposit.objects.create(shopper=shopper, amount_ore=10_000, created_at=_last_month_instant())
    # Landed after the period the statement covers, so it must not appear in its closing balance.
    Deposit.objects.create(shopper=shopper, amount_ore=50_000, created_at=timezone.now())

    send_monthly_statements.run()

    body = mail.outbox[0].body
    assert "100,00 kr" in body  # the 10.000 øre deposited inside the month
    assert "600,00 kr" not in body  # not the 60.000 øre the shopper holds today


@pytest.mark.django_db
def test_one_failed_send_does_not_stop_the_run(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """send_mail raised straight out of the loop, so one bad address aborted the run and everyone
    after it went without a statement. Now it is logged per recipient and the run carries on."""
    from oelkaelder import tasks

    bad = Shopper.objects.create(resident=make_resident(email="bad@gahk.dk"))
    Shopper.objects.create(resident=make_resident(email="good@gahk.dk"))
    real_send_mail = tasks.send_mail

    def flaky_send_mail(subject: str, body: str, sender: str, recipients: list[str]) -> int:
        if recipients == ["bad@gahk.dk"]:
            raise OSError("550 mailbox unavailable")
        return real_send_mail(subject, body, sender, recipients)

    monkeypatch.setattr(tasks, "send_mail", flaky_send_mail)

    assert send_monthly_statements.run() == 1

    assert [m.to for m in mail.outbox] == [["good@gahk.dk"]]
    assert f"shopper#{bad.pk}" in caplog.text
    assert "1 failed" in caplog.text
