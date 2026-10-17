import json
import pickle
from collections.abc import Callable
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from photo_album.models import Album, AlbumImport, Media
from residents.models import Resident, Role
from residents.views_admin import (
    JOB_SORTS,
    _add_photo_album_context,
    _child_jobs,
    _display_task_result,
    _format_runtime,
    _past_job_count,
    _past_jobs,
    _task_argument_id,
    _task_runtime,
)


def test_celery_requeues_work_when_a_worker_is_lost() -> None:
    assert settings.CELERY_TASK_ACKS_LATE is True
    assert settings.CELERY_TASK_REJECT_ON_WORKER_LOST is True


def test_legacy_pickled_task_result_is_displayed_safely() -> None:
    assert _display_task_result(pickle.dumps({"downloaded": 2, "ready": True})) == (
        '{\n  "downloaded": 2,\n  "ready": true\n}'
    )


def test_task_runtime_is_displayed_in_minutes_seconds_and_milliseconds() -> None:
    assert _format_runtime(timedelta(minutes=2, seconds=3, milliseconds=45)) == "2 min. 3 sek. 45 ms"


def test_task_runtime_normalizes_celery_and_broker_timezones() -> None:
    finished_at = datetime(2026, 9, 20, 15, 59, 34, 346184)
    submitted_at = datetime(2026, 9, 20, 17, 59, 30, 311044)

    assert _task_runtime(finished_at, submitted_at) == "0 min. 4 sek. 35 ms"


def test_task_runtime_reads_the_broker_timestamp_in_the_configured_zone_not_the_active_one() -> None:
    finished_at = datetime(2026, 9, 20, 15, 59, 34, 346184)
    submitted_at = datetime(2026, 9, 20, 17, 59, 30, 311044)

    with timezone.override("UTC"):
        assert _task_runtime(finished_at, submitted_at) == "0 min. 4 sek. 35 ms"


def test_task_context_reads_only_integer_positional_ids() -> None:
    assert _task_argument_id({"arguments": "(42,)"}) == 42
    assert _task_argument_id({"arguments": "('42',)"}) is None
    assert _task_argument_id({"arguments": ""}) is None
    assert _task_argument_id({"arguments": "(" * 5000 + ")" * 5000}) is None


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
    jobs: list[dict[str, object]] = [
        {"task": "photo_album.tasks.build_media_derivatives", "arguments": f"({media.pk},)"},
        {"task": "photo_album.tasks.process_album_import", "arguments": f"({album_import.pk},)"},
    ]

    _add_photo_album_context(jobs)

    assert jobs[0]["task_context"] == "Medie: sommer.jpg (album: 2026: Sommerfest)"
    assert jobs[1]["task_context"] == "ZIP: sommerfest.zip (mappe: 2026); albummer: 2026: Sommerfest"


def _mock_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetchall: list[tuple[object, ...]] | None = None,
    fetchone: object = None,
) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchall.return_value = fetchall or []
    cursor.fetchone.return_value = fetchone
    database_connection = MagicMock()
    database_connection.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr("residents.views_admin.connection", database_connection)
    return cursor


def test_past_jobs_filters_sorts_and_pages_in_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _mock_connection(monkeypatch)

    assert _past_jobs("FAILURE", "status_asc", include_children=False, limit=25, offset=50) == []

    sql, parameters = cursor.execute.call_args.args
    assert "DISTINCT ON (parsed.headers->>'id')" in sql
    assert "WHERE (%s = '' OR result.status = %s)" in sql
    assert "(%s::boolean OR message.parent_id IS NULL)" in sql
    assert "ORDER BY status ASC, date_done DESC NULLS LAST, task_id" in sql
    assert sql.rstrip().endswith("LIMIT %s OFFSET %s")
    assert "LIMIT 100" not in sql
    assert parameters == ["FAILURE", "FAILURE", False, 25, 50]


def test_every_sort_option_orders_in_sql() -> None:
    assert set(JOB_SORTS) == {
        "started_desc",
        "started_asc",
        "finished_desc",
        "finished_asc",
        "status_asc",
        "status_desc",
    }


def test_past_jobs_maps_rows_without_parsing_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    finished_at = datetime(2026, 9, 20, 15, 59, 34, 346184)
    submitted_at = datetime(2026, 9, 20, 17, 59, 30, 311044)
    _mock_connection(
        monkeypatch,
        fetchall=[
            (
                "task-id",
                "SUCCESS",
                finished_at,
                None,
                "celery@test",
                0,
                None,
                "celery",
                submitted_at,
                "oelkaelder.tasks.send_monthly_statements",
                "(1,)",
                "{}",
                "parent-id",
            ),
            ("orphan-id", "SUCCESS", None, None, None, None, "celery", None, None, None, None, None, None),
        ],
    )

    jobs = _past_jobs(include_children=True)

    assert jobs[0] == {
        "id": "task-id",
        "status": "SUCCESS",
        "finished_at": finished_at,
        "task": "oelkaelder.tasks.send_monthly_statements",
        "worker": "celery@test",
        "retries": 0,
        "queue": "celery",
        "submitted_at": submitted_at,
        "runtime": "0 min. 4 sek. 35 ms",
        "parent_id": "parent-id",
        "arguments": "(1,)",
        "keyword_arguments": "{}",
    }
    assert jobs[1]["task"] == "Ukendt opgave"
    assert jobs[1]["runtime"] is None
    assert jobs[1]["arguments"] == ""


@pytest.mark.django_db
def test_child_jobs_carry_their_arguments_so_photo_album_context_resolves(
    monkeypatch: pytest.MonkeyPatch, make_resident: Callable[..., Resident]
) -> None:
    album = Album.objects.create(folder="2026", name="Sommerfest")
    media = Media.objects.create(album=album, title="sommer.jpg", requested_by=make_resident())
    payload = json.dumps(
        {
            "headers": {
                "id": "child-id",
                "task": "photo_album.tasks.build_media_derivatives",
                "argsrepr": f"({media.pk},)",
            }
        }
    )
    cursor = _mock_connection(
        monkeypatch,
        fetchall=[("child-id", "SUCCESS", None, "photo_album.tasks.build_media_derivatives", None, payload)],
    )

    assert _child_jobs("parent-id") == [
        {
            "id": "child-id",
            "status": "SUCCESS",
            "task": "photo_album.tasks.build_media_derivatives",
            "arguments": f"({media.pk},)",
            "finished_at": None,
            "runtime": None,
            "task_context": "Medie: sommer.jpg (album: 2026: Sommerfest)",
        }
    ]
    assert cursor.execute.call_args.args[1] == ["parent-id", "parent-id"]


def test_child_jobs_keep_only_the_latest_message_per_task_and_prefilter_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _mock_connection(monkeypatch)

    assert _child_jobs("parent-id") == []

    sql = cursor.execute.call_args.args[0]
    assert "DISTINCT ON (result.task_id)" in sql
    assert "ORDER BY result.task_id, m.id DESC" in sql
    assert sql.index("position(%s in m.payload) > 0") < sql.index("'parent_id' = %s")


def test_past_job_count_honours_the_status_filter_when_children_are_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _mock_connection(monkeypatch, fetchone=(42,))

    assert _past_job_count("SUCCESS", include_children=True) == 42
    assert cursor.execute.call_args.args == (
        "SELECT COUNT(*) FROM celery_taskmeta WHERE (%s = '' OR status = %s)",
        ["SUCCESS", "SUCCESS"],
    )


def test_past_job_count_applies_the_parent_filter_when_children_are_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _mock_connection(monkeypatch, fetchone=(7,))

    assert _past_job_count("SUCCESS") == 7

    sql, parameters = cursor.execute.call_args.args
    assert "message.parent_id IS NULL" in sql
    assert parameters == ["SUCCESS", "SUCCESS", False]


@pytest.mark.django_db
def test_worker_jobs_requires_administrator(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident())

    assert client.get(reverse("siteadmin:worker_jobs")).status_code == 403


@pytest.mark.django_db
def test_worker_jobs_shows_job_data_and_active_schedules(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    interval, _ = IntervalSchedule.objects.get_or_create(every=10, period=IntervalSchedule.MINUTES)
    PeriodicTask.objects.create(
        name="Process media", task="photo_album.tasks.process_pending_media", interval=interval
    )
    monkeypatch.setattr(
        "residents.views_admin._queued_jobs",
        lambda *_: [
            {
                "id": "queued-task-id",
                "task": "photo_album.tasks.build_media_derivatives",
                "queue": "celery",
                "timestamp": None,
                "eta": None,
            }
        ],
    )
    monkeypatch.setattr(
        "residents.views_admin._past_jobs",
        lambda *_, **__: [
            {
                "id": "completed-task-id",
                "task": "oelkaelder.tasks.send_monthly_statements",
                "status": "SUCCESS",
                "finished_at": None,
                "worker": "celery@test",
                "retries": 0,
                "queue": "celery",
                "submitted_at": None,
                "runtime": None,
                "task_context": "Medie: sommer.jpg (album: 2026: Sommerfest)",
                "arguments": "(42,)",
                "keyword_arguments": "{}",
            }
        ],
    )
    monkeypatch.setattr("residents.views_admin._past_job_count", lambda *_: 1)
    client.force_login(administrator)

    response = client.get(reverse("siteadmin:worker_jobs"))

    assert response.status_code == 200
    assert "photo_album.tasks.build_media_derivatives" in response.content.decode()
    assert "oelkaelder.tasks.send_monthly_statements" in response.content.decode()
    assert "(42,)" in response.content.decode()
    assert "Medie: sommer.jpg" in response.content.decode()
    assert "Seneste jobs (1 overordnede jobs)" in response.content.decode()
    assert 'value="finished_desc" selected' in response.content.decode()
    assert "Process media" in response.content.decode()
    assert reverse("siteadmin:worker_job_detail", args=["completed-task-id"]) in response.content.decode()


@pytest.mark.django_db
def test_worker_jobs_can_include_child_jobs(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr("residents.views_admin._queued_jobs", list)
    monkeypatch.setattr(
        "residents.views_admin._past_jobs",
        lambda status, sort, include_children, **_: calls.append((status, sort, include_children)) or [],
    )
    monkeypatch.setattr("residents.views_admin._past_job_count", lambda *_: 0)
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))

    response = client.get(reverse("siteadmin:worker_jobs"), {"children": "1"})

    assert response.status_code == 200
    assert calls == [("", "finished_desc", True)]
    assert "Vis kun overordnede jobs" in response.content.decode()


@pytest.mark.django_db
def test_worker_jobs_form_keeps_the_sub_job_toggle(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("residents.views_admin._queued_jobs", list)
    monkeypatch.setattr("residents.views_admin._past_jobs", lambda *_, **__: [])
    monkeypatch.setattr("residents.views_admin._past_job_count", lambda *_: 0)
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))

    with_children = client.get(reverse("siteadmin:worker_jobs"), {"children": "1"}).content.decode()
    without_children = client.get(reverse("siteadmin:worker_jobs")).content.decode()

    assert '<input type="hidden" name="children" value="1">' in with_children
    assert 'name="children"' not in without_children


@pytest.mark.django_db
def test_worker_jobs_pages_through_the_database_rather_than_a_fixed_window(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("residents.views_admin._queued_jobs", list)
    monkeypatch.setattr("residents.views_admin._past_jobs", lambda *_, **kwargs: calls.append(kwargs) or [])
    monkeypatch.setattr("residents.views_admin._past_job_count", lambda *_: 60)
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))

    response = client.get(reverse("siteadmin:worker_jobs"), {"page": "3"})

    assert calls == [{"limit": 25, "offset": 50}]
    assert "Side 3 / 3" in response.content.decode()
    assert "Seneste jobs (60 overordnede jobs)" in response.content.decode()


@pytest.mark.django_db
def test_worker_job_detail_shows_result_metadata(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    monkeypatch.setattr(
        "residents.views_admin._job_details",
        lambda task_id: {
            "id": task_id,
            "task": "photo_album.tasks.build_album_download",
            "status": "FAILURE",
            "submitted_at": None,
            "finished_at": None,
            "runtime": "0:00:03",
            "children": [
                {
                    "id": "child-task-id",
                    "task": "photo_album.tasks.build_media_derivatives",
                    "status": "SUCCESS",
                    "finished_at": None,
                    "runtime": "0 min. 1 sek. 2 ms",
                }
            ],
            "result": "download failed",
            "traceback": "Traceback (most recent call last):\\nExampleError",
        },
    )
    client.force_login(administrator)

    response = client.get(reverse("siteadmin:worker_job_detail", args=["task-id"]))

    assert response.status_code == 200
    assert "build_album_download" in response.content.decode()
    assert "0:00:03" in response.content.decode()
    assert "Underopgaver (1)" in response.content.decode()
    assert reverse("siteadmin:worker_job_detail", args=["child-task-id"]) in response.content.decode()
    assert "ExampleError" in response.content.decode()
    assert "Jobbet har ikke logget noget" in response.content.decode()


@pytest.mark.django_db
def test_worker_job_detail_returns_not_found_for_unknown_job(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("residents.views_admin._job_details", lambda task_id: None)
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))

    assert client.get(reverse("siteadmin:worker_job_detail", args=["missing-task-id"])).status_code == 404


BROKER_AND_RESULT_TABLES = """
    CREATE TABLE kombu_queue (id serial PRIMARY KEY, name varchar(200));
    CREATE TABLE kombu_message (
        id serial PRIMARY KEY, visible boolean, timestamp timestamp without time zone,
        payload text, version smallint, queue_id integer
    );
    CREATE TABLE celery_taskmeta (
        id serial PRIMARY KEY, task_id varchar(155) UNIQUE, status varchar(50),
        date_done timestamp without time zone, name varchar(155), worker varchar(155),
        retries integer, queue varchar(155)
    );
    INSERT INTO kombu_queue (name) VALUES ('celery');
"""


def _publish(
    task_id: str, parent_id: str | None = None, args: str = "()", delivered: str = "2026-09-20 17:59:30"
) -> None:
    headers = {"id": task_id, "task": "photo_album.tasks.build_media_derivatives", "argsrepr": args}
    if parent_id:
        headers["parent_id"] = parent_id
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kombu_message (visible, timestamp, payload, version, queue_id)"
            " VALUES (false, %s, %s, 1, 1)",
            [delivered, json.dumps({"headers": headers})],
        )


def _store_result(task_id: str, status: str = "SUCCESS", done: str = "2026-09-20 15:59:34") -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO celery_taskmeta (task_id, status, date_done, name) VALUES (%s, %s, %s, %s)",
            [task_id, status, done, "photo_album.tasks.build_media_derivatives"],
        )


@pytest.fixture
def broker_tables(db: None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(BROKER_AND_RESULT_TABLES)


def test_child_jobs_collapse_retries_and_ignore_unrelated_messages(broker_tables: None) -> None:
    _publish("parent")
    _store_result("parent")
    for delivery in ("2026-09-20 17:59:00", "2026-09-20 17:59:10", "2026-09-20 17:59:30"):
        _publish("child", parent_id="parent", delivered=delivery)
    _store_result("child")
    _publish("other-child", parent_id="someone-else")
    _store_result("other-child")

    children = _child_jobs("parent")

    assert [child["id"] for child in children] == ["child"]
    assert children[0]["runtime"] == "0 min. 4 sek. 0 ms"


def test_past_jobs_hide_children_before_paging_so_a_fan_out_cannot_crowd_out_parents(
    broker_tables: None,
) -> None:
    _publish("old-parent")
    _store_result("old-parent", done="2026-09-01 10:00:00")
    for index in range(120):
        _publish(f"child-{index}", parent_id="old-parent")
        _store_result(f"child-{index}", done=f"2026-09-20 15:{index // 60:02d}:{index % 60:02d}")

    assert _past_job_count() == 1
    assert [job["id"] for job in _past_jobs(include_children=False)] == ["old-parent"]
    assert _past_job_count(include_children=True) == 121
    assert len(_past_jobs(include_children=True, limit=25, offset=100)) == 21


def test_past_jobs_sort_options_reach_the_whole_table_and_retries_do_not_duplicate(
    broker_tables: None,
) -> None:
    _publish("failed-long-ago")
    _store_result("failed-long-ago", status="FAILURE", done="2026-01-01 08:00:00")
    for index in range(110):
        _publish(f"ok-{index}")
        _store_result(f"ok-{index}", done=f"2026-09-20 15:{index // 60:02d}:{index % 60:02d}")
    for delivery in ("2026-09-20 17:00:00", "2026-09-20 17:30:00"):
        _publish("retried", delivered=delivery)
    _store_result("retried", done="2026-09-20 16:00:00")

    by_status = _past_jobs(sort="status_asc", include_children=True, limit=1)
    by_age = _past_jobs(sort="finished_asc", include_children=True, limit=1)
    everything = _past_jobs(include_children=True, limit=500)

    assert by_status[0]["id"] == "failed-long-ago"
    assert by_age[0]["id"] == "failed-long-ago"
    assert [job["id"] for job in everything].count("retried") == 1
    assert len(everything) == 112
    assert _past_job_count(status="FAILURE") == 1
