"""The worker-jobs admin page (residents.views_admin), read from the broker's and the result
backend's own tables.

Those tables belong to kombu and Celery, not to Django, so a fresh test database does not have
them. The `celery_tables` fixture builds them to the shape the running database has, both clocks
included: kombu's `timestamp` naive in settings.TIME_ZONE, Celery's `date_done` naive in UTC. Most
of what is worth pinning here — which rows a filter picks, which clock a time is read in — is
exactly what a mocked cursor would assume rather than test.
"""

import json
import pickle
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.db import connection
from django.http import QueryDict
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask

from core.models import WorkerJobLog
from core.templatetags.ui_extras import ago, ahead
from photo_album.models import Album, AlbumImport, Media
from residents.models import Resident, Role
from residents.views_admin import (
    Job,
    JobFilters,
    _activity,
    _add_photo_album_context,
    _child_jobs,
    _display_task_result,
    _format_duration,
    _history,
    _history_counts,
    _job_details,
    _nice_ceiling,
    _plan,
    _queue,
    _running,
    _task_argument_id,
    _task_types,
)

DERIVATIVES = "photo_album.tasks.build_media_derivatives"
IMPORT = "photo_album.tasks.process_album_import"
NOTIFY = "core.tasks.send_admin_dummy_notification"
LOCAL = ZoneInfo(settings.TIME_ZONE)


def test_celery_requeues_work_when_a_worker_is_lost() -> None:
    assert settings.CELERY_TASK_ACKS_LATE is True
    assert settings.CELERY_TASK_REJECT_ON_WORKER_LOST is True


def test_legacy_pickled_task_result_is_displayed_safely() -> None:
    assert _display_task_result(pickle.dumps({"downloaded": 2, "ready": True})) == (
        '{\n  "downloaded": 2,\n  "ready": true\n}'
    )


@pytest.mark.parametrize(
    ("duration", "shown"),
    [
        (timedelta(milliseconds=45), "45 ms"),
        (timedelta(seconds=3, milliseconds=240), "3,2 sek."),
        (timedelta(minutes=2, seconds=3, milliseconds=45), "2 min. 3 sek."),
        (timedelta(hours=1, minutes=5, seconds=9), "1 t. 5 min."),
        (timedelta(seconds=-2), "0 ms"),
        (None, ""),
    ],
)
def test_durations_are_shown_in_the_largest_units_that_matter(duration: timedelta | None, shown: str) -> None:
    assert _format_duration(duration) == shown


def test_task_context_reads_only_integer_positional_ids() -> None:
    assert _task_argument_id("(42,)") == 42
    assert _task_argument_id("('42',)") is None
    assert _task_argument_id("") is None
    assert _task_argument_id("(" * 5000 + ")" * 5000) is None


def test_a_moment_ago_is_not_zero_minutes_ago() -> None:
    now = timezone.now()

    assert ago(now - timedelta(seconds=20), now) == "under et minut"
    assert ago(now - timedelta(minutes=3), now) == "3\xa0minutter"  # Django's own, non-breaking
    assert ahead(now + timedelta(seconds=20), now) == "under et minut"


def test_the_chart_scale_is_a_round_number() -> None:
    assert [_nice_ceiling(n) for n in (0, 1, 3, 7, 10, 11, 140)] == [1, 1, 5, 10, 10, 20, 200]


@pytest.mark.django_db
def test_photo_album_tasks_show_the_affected_media_and_import(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Sommerfest")
    media = Media.objects.create(album=album, title="sommer.jpg", requested_by=resident)
    album_import = AlbumImport.objects.create(
        requested_by=resident,
        folder="2026",
        archive_name="sommerfest.zip",
        album_ids=[album.pk],
    )
    jobs = [
        Job(id="a", task=DERIVATIVES, arguments=f"({media.pk},)"),
        Job(id="b", task=IMPORT, arguments=f"({album_import.pk},)"),
    ]

    _add_photo_album_context(jobs)

    assert jobs[0].task_context == "Medie: sommer.jpg (album: 2026: Sommerfest)"
    assert jobs[1].task_context == "ZIP: sommerfest.zip (mappe: 2026); albummer: 2026: Sommerfest"


def test_a_job_is_named_by_its_function_under_its_app() -> None:
    job = Job(id="x", task=DERIVATIVES)

    assert (job.name, job.app) == ("build_media_derivatives", "photo_album")
    assert (Job(id="y", task="celery.chord_unlock").app, Job(id="z", task="loose").app) == ("celery", "")


@pytest.mark.django_db
def test_schedules_are_described_in_words_where_they_have_a_usual_shape() -> None:
    def crontab(**fields: str) -> PeriodicTask:
        return PeriodicTask(crontab=CrontabSchedule(**fields))

    daily = crontab(minute="20", hour="3")
    monthly = crontab(minute="10", hour="4", day_of_month="1")
    quarterly = crontab(minute="5-59/15")
    odd = crontab(minute="0", hour="4", day_of_week="1")
    twice = crontab(minute="0", hour="8,16")
    thrice = crontab(minute="30", hour="6,12,18")
    interval = PeriodicTask(interval=IntervalSchedule(every=10, period=IntervalSchedule.MINUTES))
    hourly = PeriodicTask(interval=IntervalSchedule(every=1, period=IntervalSchedule.HOURS))

    assert _plan(daily) == "Hver dag kl. 03:20"
    assert _plan(monthly) == "Den 1. i måneden kl. 04:10"
    assert _plan(quarterly) == "Hvert 15. minut, fra :05"
    assert _plan(odd) == "0 4 * * 1"
    assert _plan(twice) == "Hver dag kl. 08:00 og 16:00"
    assert _plan(thrice) == "Hver dag kl. 06:30, 12:30 og 18:30"
    assert _plan(interval) == "Hvert 10. minut"
    assert _plan(hourly) == "Hver time"


# ---- against the tables ----

CELERY_TABLES = """
    CREATE TABLE kombu_queue (id serial PRIMARY KEY, name varchar(200));
    CREATE TABLE kombu_message (
        id serial PRIMARY KEY, visible boolean, timestamp timestamp without time zone,
        payload text NOT NULL, version smallint, queue_id integer
    );
    CREATE TABLE celery_taskmeta (
        id serial PRIMARY KEY, task_id varchar(155) UNIQUE, status varchar(50), result bytea,
        date_done timestamp without time zone, traceback text, name varchar(155), args bytea,
        kwargs bytea, worker varchar(155), retries integer, queue varchar(155)
    );
    INSERT INTO kombu_queue (name) VALUES ('celery'), ('thumbnails');
"""
CELERY, THUMBNAILS = 1, 2


@pytest.fixture
def celery_tables(db: None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(CELERY_TABLES)


def _publish(
    task_id: str,
    *,
    task: str = DERIVATIVES,
    queue: int = CELERY,
    sent_at: datetime | None = None,
    taken_at: datetime | None = None,
    parent_id: str | None = None,
    args: str = "()",
) -> None:
    """A broker message. Untaken (visible, no timestamp) unless `taken_at` says when a worker took
    it — stored, like kombu does, as the worker's local wall-clock time."""
    headers = {"id": task_id, "task": task, "argsrepr": args, "kwargsrepr": "{}"}
    if sent_at:
        headers["sent_at"] = sent_at.isoformat()
    if parent_id:
        headers["parent_id"] = parent_id
    taken = taken_at.astimezone(LOCAL).replace(tzinfo=None) if taken_at else None
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kombu_message (visible, timestamp, payload, version, queue_id) VALUES (%s, %s, %s, 1, %s)",
            [taken is None, taken, json.dumps({"body": "", "headers": headers}), queue],
        )


def _result(
    task_id: str,
    done: datetime,
    status: str = "SUCCESS",
    *,
    task: str = DERIVATIVES,
    traceback: str | None = None,
) -> None:
    """A result row, with `date_done` naive UTC as Celery stores it."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO celery_taskmeta (task_id, status, date_done, name, worker, retries, queue, traceback)"
            " VALUES (%s, %s, %s, %s, 'celery@test', 0, 'celery', %s)",
            [task_id, status, done.astimezone(UTC).replace(tzinfo=None), task, traceback],
        )


def _record(
    task_id: str,
    started_at: datetime,
    *,
    sent_at: datetime | None = None,
    finished_at: datetime | None = None,
    parent_id: str = "",
    task: str = DERIVATIVES,
    state: str = "SUCCESS",
    arguments: str = "()",
) -> WorkerJobLog:
    return WorkerJobLog.objects.create(
        task_id=task_id,
        task_name=task,
        state=state,
        sent_at=sent_at,
        started_at=started_at,
        finished_at=finished_at,
        parent_id=parent_id,
        arguments=arguments,
    )


def _admin(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))


# The screenshot in the bug report: sent 20:05:01, finished 18:05:01 — the same second, in two zones.
SENT = datetime(2026, 10, 14, 18, 5, 1, tzinfo=UTC)


@pytest.mark.usefixtures("celery_tables")
def test_a_job_never_finishes_before_it_was_sent_whichever_clock_stored_it(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """kombu's timestamp is local wall-clock time and Celery's date_done is UTC. Read as stored,
    a job taken at 20:05:01 local finished at 18:05:01."""
    _publish("old-job", taken_at=SENT)  # a job from before the worker's own record
    _result("old-job", SENT + timedelta(milliseconds=400))
    _admin(client, make_resident)

    details = _job_details("old-job", timezone.now())
    page = client.get(reverse("siteadmin:worker_job_detail", args=["old-job"])).content.decode()

    assert details is not None
    job, _ = details
    assert job.started_at == SENT
    assert job.finished_at == SENT + timedelta(milliseconds=400)
    assert job.ran == "400 ms"
    assert "20:05:01" in page
    assert "18:05:01" not in page


@pytest.mark.usefixtures("celery_tables")
def test_the_sent_time_outlives_the_broker_message() -> None:
    """The broker row is swept after a day; the worker's record keeps when the job was sent."""
    _result("swept", SENT + timedelta(seconds=5))
    _record("swept", SENT + timedelta(seconds=2), sent_at=SENT, finished_at=SENT + timedelta(seconds=5))

    (job,) = _history(JobFilters(), timezone.now(), limit=25, offset=0)

    assert (job.sent_at, job.started_at, job.finished_at) == (
        SENT,
        SENT + timedelta(seconds=2),
        SENT + timedelta(seconds=5),
    )
    assert (job.waited, job.ran) == ("2,0 sek.", "3,0 sek.")


@pytest.mark.usefixtures("celery_tables")
def test_a_job_that_failed_before_it_started_takes_its_sent_time_from_the_broker() -> None:
    """No worker started it, so there is no record — a worker lost on receipt, or an annulled job.
    Its message is still in the broker for a day, with the stamp."""
    _publish("never-ran", sent_at=SENT, taken_at=SENT, parent_id="parent", args="(7,)")
    _result("never-ran", SENT + timedelta(seconds=1), "FAILURE")

    (job,) = _history(JobFilters(include_children=True), timezone.now(), limit=25, offset=0)

    assert (job.sent_at, job.arguments, job.parent_id) == (SENT, "(7,)", "parent")


def test_a_task_is_measured_against_its_own_time_limit_even_if_the_web_process_never_sent_one() -> None:
    from photo_album.tasks import ZIP_TASK_TIME_LIMIT

    job = Job(id="x", task="photo_album.tasks.process_album_import")
    job.run(timezone.now())

    assert job.time_limit == timedelta(seconds=ZIP_TASK_TIME_LIMIT)


@pytest.mark.usefixtures("celery_tables")
def test_the_history_filters_by_type_status_period_and_parent_in_sql() -> None:
    now = timezone.now()
    for index in range(3):
        _result(f"thumb-{index}", now - timedelta(hours=index))
        _record(f"thumb-{index}", now - timedelta(hours=index, seconds=1), parent_id="import")
    _result("import", now - timedelta(days=3), task=IMPORT)
    _record("import", now - timedelta(days=3, minutes=5), task=IMPORT)
    _result("broken", now - timedelta(days=40), "FAILURE", task=NOTIFY)
    _record("broken", now - timedelta(days=40), task=NOTIFY, state="FAILURE")

    def ids(**filters: Any) -> list[str]:  # noqa: ANN401
        return [job.id for job in _history(JobFilters(**filters), now, limit=25, offset=0)]

    assert ids() == ["import", "broken"]
    assert ids(include_children=True) == ["thumb-0", "thumb-1", "thumb-2", "import", "broken"]
    assert ids(include_children=True, task=DERIVATIVES, period="24h") == ["thumb-0", "thumb-1", "thumb-2"]
    assert ids(period="7d") == ["import"]
    assert ids(status="FAILURE") == ["broken"]
    assert ids(sort="oldest") == ["broken", "import"]
    assert ids(sort="slowest", include_children=True)[0] == "import"
    assert _history_counts(JobFilters(include_children=True)) == {"SUCCESS": 4, "FAILURE": 1}
    assert _history_counts(JobFilters(status="FAILURE")) == {"SUCCESS": 1, "FAILURE": 1}, (
        "status is not counted under itself"
    )


@pytest.mark.usefixtures("celery_tables")
def test_the_history_pages_through_the_database_rather_than_a_fixed_window() -> None:
    now = timezone.now()
    for index in range(60):
        _result(f"job-{index:02d}", now - timedelta(minutes=index))

    pages = [_history(JobFilters(), now, limit=25, offset=offset) for offset in (0, 25, 50)]

    assert [len(page) for page in pages] == [25, 25, 10]
    assert pages[2][-1].id == "job-59"


def test_filters_only_take_what_is_offered() -> None:
    filters = JobFilters.from_query(QueryDict("status=DROP&period=1y&sort=name;--&type=x&children=1"))

    assert filters == JobFilters(task="x", include_children=True)
    assert JobFilters.from_query(QueryDict("status=FAILURE&period=24h&sort=slowest")) == JobFilters(
        status="FAILURE", period="24h", sort="slowest"
    )


@pytest.mark.usefixtures("celery_tables")
def test_the_queue_is_grouped_by_queue_and_type_in_the_order_it_will_be_taken() -> None:
    for index in range(7):
        _publish(f"thumb-{index}", queue=THUMBNAILS, sent_at=SENT + timedelta(seconds=index))
    _publish("mail", task=NOTIFY, sent_at=SENT + timedelta(minutes=1))
    _publish("legacy", task=NOTIFY)  # sent before messages carried their sent time
    _publish("taken", sent_at=SENT, taken_at=SENT)  # a worker has this one

    queue = _queue()

    assert [(group.queue, group.task, group.count) for group in queue.groups] == [
        ("thumbnails", DERIVATIVES, 7),
        ("celery", NOTIFY, 2),
    ]
    assert [job.id for job in queue.groups[0].jobs] == [f"thumb-{index}" for index in range(5)]
    assert queue.groups[0].more == 2
    assert queue.total == 9
    assert queue.oldest_sent_at == SENT
    assert queue.groups[1].jobs[1].sent_at is None


@pytest.mark.usefixtures("celery_tables")
def test_a_payload_with_escaped_characters_does_not_break_the_page() -> None:
    """kombu's JSON escapes quotes and non-ASCII. The old `payload::bytea` cast read those
    backslashes as bytea escapes and failed the whole query, which the page showed as empty."""
    _publish("quoted", args="('Fællesspisning \"for alle\"',)")

    (group,) = _queue().groups

    assert group.jobs[0].arguments == "('Fællesspisning \"for alle\"',)"


@pytest.mark.usefixtures("celery_tables")
def test_running_jobs_are_measured_against_their_time_limit() -> None:
    now = timezone.now()
    _result("fresh", now - timedelta(minutes=2), "STARTED", task="some.quick_task")
    _record(
        "fresh",
        now - timedelta(minutes=2),
        sent_at=now - timedelta(minutes=3),
        state="STARTED",
        task="some.quick_task",
    )
    _result("dead", now - timedelta(hours=3), "STARTED", task="some.quick_task")

    dead, fresh = _running(now)  # oldest first

    assert fresh.time_limit == timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)
    assert (fresh.running_for, fresh.waited, fresh.over_time_limit) == (
        "2 min. 0 sek.",
        "1 min. 0 sek.",
        False,
    )
    assert fresh.limit_used == 13
    assert dead.over_time_limit
    assert dead.limit_used == 100


@pytest.mark.usefixtures("celery_tables")
def test_task_types_count_every_status_and_what_waits() -> None:
    now = timezone.now()
    _result("ok", now - timedelta(hours=2))
    _record("ok", now - timedelta(hours=2, seconds=4), finished_at=now - timedelta(hours=2))
    _result("failed", now - timedelta(hours=1), "FAILURE")
    _result("revoked", now - timedelta(days=2), "REVOKED")
    _publish("waiting", task=NOTIFY)

    queue = _queue()
    notify, derivatives = _task_types(queue)  # by name

    assert (derivatives.task, derivatives.succeeded, derivatives.failed, derivatives.other) == (
        DERIVATIVES,
        1,
        1,
        1,
    )
    assert (derivatives.last_status, derivatives.average) == ("FAILURE", "4,0 sek.")
    assert (notify.task, notify.total, notify.queued) == (NOTIFY, 0, 1)


@pytest.mark.usefixtures("celery_tables")
def test_the_activity_chart_counts_each_hour_of_the_last_day() -> None:
    now = timezone.now()
    hour = now.replace(minute=0, second=0, microsecond=0)
    for seconds in (0, 1, 2):
        _result(f"this-hour-{seconds}", hour + timedelta(seconds=seconds))
    _result("last-hour", hour - timedelta(minutes=30), "FAILURE")
    _result("too-old", now - timedelta(hours=30))

    activity = _activity(now)

    assert len(activity.buckets) == 24
    assert (activity.succeeded, activity.failed) == (3, 1)
    assert activity.scale == 5
    assert activity.buckets[-1].succeeded_height == pytest.approx(60)


@pytest.mark.usefixtures("celery_tables")
def test_a_job_page_lists_its_sub_jobs_with_those_still_queued() -> None:
    now = timezone.now()
    _result("child-done", now)
    _record("child-done", now - timedelta(seconds=1), parent_id="parent")
    _record("child-running", now, parent_id="parent", state="STARTED")
    _result("child-running", now, "STARTED")
    _publish("child-waiting", parent_id="parent", sent_at=now)
    _publish("unrelated", parent_id="someone-else", sent_at=now)

    children, counts = _child_jobs("parent")

    assert [(child.id, child.status) for child in children] == [
        ("child-done", "SUCCESS"),
        ("child-running", "STARTED"),
        ("child-waiting", "PENDING"),
    ]
    assert counts == {"SUCCESS": 1, "STARTED": 1, "PENDING": 1}


@pytest.mark.usefixtures("celery_tables")
def test_a_job_page_puts_the_three_sources_together(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    _publish("job", sent_at=SENT, taken_at=SENT + timedelta(seconds=1), parent_id="parent")
    _result("job", SENT + timedelta(seconds=3), "FAILURE", traceback="Traceback …\nExampleError")
    _record(
        "job",
        SENT + timedelta(seconds=1),
        sent_at=SENT,
        finished_at=SENT + timedelta(seconds=3),
        parent_id="parent",
    )
    _admin(client, make_resident)

    page = client.get(reverse("siteadmin:worker_job_detail", args=["job"])).content.decode()

    assert "build_media_derivatives" in page
    assert "Fejlet" in page
    assert "ventede 1,0 sek." in page
    assert "kørte 2,0 sek." in page
    assert reverse("siteadmin:worker_job_detail", args=["parent"]) in page
    assert "ExampleError" in page
    assert "Jobbet har ikke logget noget" in page


@pytest.mark.django_db
def test_a_job_page_shows_a_job_known_only_from_its_record(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    """Without the Celery tables (another broker, or a result already purged) the record is enough."""
    _record("only-a-record", SENT, finished_at=SENT + timedelta(seconds=1), task=IMPORT)
    _admin(client, make_resident)

    response = client.get(reverse("siteadmin:worker_job_detail", args=["only-a-record"]))

    assert response.status_code == 200
    assert "process_album_import" in response.content.decode()
    assert client.get(reverse("siteadmin:worker_job_detail", args=["missing"])).status_code == 404


@pytest.mark.django_db
def test_worker_jobs_requires_administrator(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident())

    assert client.get(reverse("siteadmin:worker_jobs")).status_code == 403
    assert client.get(reverse("siteadmin:worker_job_detail", args=["x"])).status_code == 403


@pytest.mark.django_db
def test_the_live_poll_answers_nobody_but_an_administrator_with_anything_to_swap_in(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    live = reverse("siteadmin:worker_jobs_live")

    assert client.get(live).status_code == 204
    client.force_login(make_resident())
    assert client.get(live).status_code == 204
    client.force_login(make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,)))
    response = client.get(live)
    assert response.status_code == 200
    assert "Kører nu" in response.content.decode()
    assert "<html" not in response.content.decode()


@pytest.mark.usefixtures("celery_tables")
def test_the_dashboard_shows_every_part(client: Client, make_resident: Callable[..., Resident]) -> None:
    now = timezone.now()
    _result("done", now - timedelta(minutes=5))
    _record(
        "done",
        now - timedelta(minutes=5, seconds=2),
        sent_at=now - timedelta(minutes=6),
        finished_at=now - timedelta(minutes=5),
    )
    _result("failed", now - timedelta(minutes=30), "FAILURE", task=NOTIFY)
    _result("running", now - timedelta(seconds=30), "STARTED", task=IMPORT)
    _publish("waiting", queue=THUMBNAILS, sent_at=now - timedelta(minutes=1))
    crontab = CrontabSchedule.objects.create(minute="20", hour="3", timezone=settings.TIME_ZONE)
    PeriodicTask.objects.create(name="Purge expired applications", task=NOTIFY, crontab=crontab)
    _admin(client, make_resident)

    page = client.get(reverse("siteadmin:worker_jobs")).content.decode()

    assert f'hx-get="{reverse("siteadmin:worker_jobs_live")}"' in page
    for part in (
        "I kø",
        "Kører nu",
        "Fejlet, 24 timer",
        "Afsluttede jobs pr. time",
        "Opgavetyper (3)",
        "Historik",
        "Planlagte opgaver (1)",
    ):
        assert part in page
    assert "process_album_import" in page  # running
    assert "thumbnails" in page  # queued
    assert "Hver dag kl. 03:20" in page
    assert f"{timezone.localtime(now - timedelta(minutes=6)):%H:%M:%S}" in page  # sent, from the record
    for pill in ("Alle <b>3</b>", "Gennemført <b>1</b>", "Fejlet <b>1</b>", "Kører <b>1</b>"):
        assert pill in page
    assert reverse("siteadmin:worker_job_detail", args=["done"]) in page


@pytest.mark.django_db
def test_the_dashboard_is_empty_rather_than_broken_without_the_celery_tables(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    _admin(client, make_resident)

    response = client.get(reverse("siteadmin:worker_jobs"), {"type": "nothing.here", "status": "FAILURE"})

    assert response.status_code == 200
    assert "Køen er tom." in response.content.decode()
    assert '<option value="nothing.here" selected>' in response.content.decode()
