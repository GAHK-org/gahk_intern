"""The administrators' monthly summary (core.admin_summary, core.tasks.send_admin_monthly_summary).

The job half reads Celery's result table, which a fresh test database does not have; RESULT_TABLE
builds the columns it reads, with `date_done` naive UTC as Celery stores it, so the month boundary
is tested in the clock the table really holds rather than assumed by a mock.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core import mail
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from core.admin_summary import compose, previous_month
from core.tasks import WORKER_JOB_RETENTION, send_admin_monthly_summary
from residents.models import AdminAccessGrant, Resident, Role

LOCAL = ZoneInfo(settings.TIME_ZONE)
RESULT_TABLE = """
    CREATE TABLE celery_taskmeta (
        id serial PRIMARY KEY, task_id varchar(155) UNIQUE, status varchar(50),
        date_done timestamp without time zone, name varchar(155)
    )
"""
DERIVATIVES = "photo_album.tasks.build_media_derivatives"
ENCODE = "gahkflix.tasks.process_video_file"


def _result(done: datetime | None, status: str = "SUCCESS", task: str = DERIVATIVES) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO celery_taskmeta (task_id, status, date_done, name)"
            " VALUES (gen_random_uuid()::text, %s, %s, %s)",
            [status, done.astimezone(UTC).replace(tzinfo=None) if done else None, task],
        )


def test_the_summary_is_of_the_calendar_month_before() -> None:
    assert previous_month(date(2026, 11, 1)) == (date(2026, 10, 1), date(2026, 11, 1))
    assert previous_month(date(2026, 1, 20)) == (date(2025, 12, 1), date(2026, 1, 1))


@pytest.mark.django_db
def test_jobs_are_counted_in_the_local_month_they_ended() -> None:
    with connection.cursor() as cursor:
        cursor.execute(RESULT_TABLE)
    # 00:30 on 1 October in Copenhagen is still 30 September in UTC, and belongs to October.
    _result(datetime(2026, 10, 1, 0, 30, tzinfo=LOCAL))
    _result(datetime(2026, 9, 30, 23, 30, tzinfo=LOCAL))  # September
    _result(datetime(2026, 11, 1, 0, 10, tzinfo=LOCAL))  # November
    _result(datetime(2026, 10, 14, 12, tzinfo=LOCAL))
    _result(datetime(2026, 10, 15, 2, tzinfo=LOCAL), "FAILURE", ENCODE)
    _result(datetime(2026, 10, 15, 3, tzinfo=LOCAL), "FAILURE")
    _result(datetime(2026, 10, 16, 2, tzinfo=LOCAL), "FAILURE", ENCODE)
    _result(datetime(2026, 10, 20, tzinfo=LOCAL), "REVOKED")
    _result(None, "STARTED")  # still running: it has not ended in any month

    subject, body = compose(date(2026, 11, 1))

    assert subject == "Månedsoversigt for oktober 2026"
    assert "Kørt: 5\n  Gennemført: 2\n  Fejlet: 3\nAnnulleret: 1" in body
    assert f"Fejlede job efter type:\n  {ENCODE}: 2\n  {DERIVATIVES}: 1" in body
    assert reverse("siteadmin:worker_jobs") + "?status=FAILURE" in body


@pytest.mark.django_db
def test_a_quiet_month_lists_no_failures() -> None:
    with connection.cursor() as cursor:
        cursor.execute(RESULT_TABLE)
    _result(datetime(2026, 10, 14, 12, tzinfo=LOCAL))

    _, body = compose(date(2026, 11, 1))

    assert "Kørt: 1\n  Gennemført: 1\n  Fejlet: 0\n" in body
    assert "Annulleret" not in body and "Fejlede job" not in body


@pytest.mark.django_db
def test_without_a_result_table_the_summary_says_so() -> None:
    """A result backend other than the database has no table to count, and the access half is
    still worth sending."""
    _, body = compose(date(2026, 11, 1))

    assert "Ingen jobdata" in body
    assert "ADGANG" in body


@pytest.mark.django_db
def test_everyone_with_django_admin_access_gets_who_holds_access(
    make_resident: Callable[..., Resident],
) -> None:
    superuser = make_resident(email="su@gahk.dk", first_name="Super", last_name="Bruger", is_superuser=True)
    approved = make_resident(email="adm@gahk.dk", first_name="Anna", roles=(Role.ADMINISTRATOR,))
    start, _ = previous_month(timezone.localdate())
    AdminAccessGrant.objects.filter(resident=approved).update(
        status=AdminAccessGrant.Status.APPROVED,
        decided_by=superuser,
        decided_at=timezone.make_aware(datetime.combine(start + timedelta(days=2), time(12))),
    )
    make_resident(email="ny@gahk.dk", first_name="Nynne", roles=(Role.ADMINISTRATOR,))  # pending
    make_resident(email="foto@gahk.dk", first_name="Frida", last_name="Foto", roles=(Role.FOTO,))
    make_resident(email="gone@gahk.dk", first_name="Gorm", roles=(Role.FOTO,), is_active=False)
    mail.outbox.clear()

    assert send_admin_monthly_summary.run() == 1

    (message,) = mail.outbox
    assert message.to == ["adm@gahk.dk", "su@gahk.dk"]  # not the pending administrator, nor Fotogruppen
    assert "Superbrugere (1):\n  Super Bruger <su@gahk.dk>" in message.body
    assert "Anna Beboer <adm@gahk.dk> – Django admin: godkendt" in message.body
    assert "Nynne Beboer <ny@gahk.dk> – Django admin: afventer godkendelse" in message.body
    assert "Fotogruppen: Frida Foto\n" in message.body
    assert "Gorm" not in message.body
    assert f"  3. {date_format(start, 'F')}: Anna Beboer godkendt af Super Bruger" in message.body


@pytest.mark.django_db
def test_nothing_is_sent_without_anyone_to_send_it_to(make_resident: Callable[..., Resident]) -> None:
    make_resident(email="ny@gahk.dk", roles=(Role.ADMINISTRATOR,))  # pending: no access yet
    mail.outbox.clear()

    assert send_admin_monthly_summary.run() == 0
    assert mail.outbox == []


def test_the_summary_goes_out_on_the_first_in_the_day() -> None:
    entry = settings.CELERY_BEAT_SCHEDULE["email-admin-monthly-summary"]

    assert entry["task"] == "core.tasks.send_admin_monthly_summary"
    assert entry["schedule"].day_of_month == {1}
    assert entry["schedule"].hour == {9}


def test_successful_jobs_are_kept_past_the_end_of_any_month() -> None:
    """The purge on the 1st runs before the summary, 31 days after the 1st of a 31-day month began
    (plus the hour or two Copenhagen is ahead of UTC). Kept only 30, a day of the month was gone."""
    assert WORKER_JOB_RETENTION >= timedelta(days=32)
