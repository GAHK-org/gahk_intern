"""Alerting on the status checks (core.alerts): e-mails and notifications on a change, and the dead
man's switch ping."""

import logging
from collections.abc import Callable
from unittest import mock

import pytest
from django.core import mail

from core import alerts, health
from core.celery_health import TASK_LIFECYCLE_LOGGERS
from core.models import HealthCheckState
from notifications.models import Notification
from residents.models import Role

S3_DOWN = health.Check("S3", health.FAIL, "Bucket gahk: 403 Forbidden")
S3_UP = health.Check("S3", health.OK, "Bucket gahk svarer (40 ms).")


@pytest.fixture
def admin(make_resident: Callable) -> object:
    return make_resident(email="drift@gahk.dk", roles=[Role.ADMINISTRATOR])


@pytest.mark.django_db
def test_one_failing_run_tells_nobody(admin: object) -> None:
    alerts.update([S3_DOWN])
    assert mail.outbox == []
    assert not Notification.objects.exists()


@pytest.mark.django_db
def test_the_administrators_are_told_once_when_a_check_keeps_failing(admin: object) -> None:
    for _ in range(alerts.ALERT_AFTER_RUNS + 2):
        alerts.update([S3_DOWN])

    assert [m.subject for m in mail.outbox] == ["GAHK drift: S3 fejler"]
    assert mail.outbox[0].to == ["drift@gahk.dk"]
    assert "403 Forbidden" in mail.outbox[0].body
    assert "/admin/status" in mail.outbox[0].body
    assert Notification.objects.filter(recipient=admin, title="GAHK drift: S3 fejler").count() == 1


@pytest.mark.django_db
def test_recovery_is_reported_and_resets_the_count(admin: object) -> None:
    for _ in range(alerts.ALERT_AFTER_RUNS):
        alerts.update([S3_DOWN])
    alerts.update([S3_UP])

    assert [m.subject for m in mail.outbox] == ["GAHK drift: S3 fejler", "GAHK drift: S3 virker igen"]
    state = HealthCheckState.objects.get(name="S3")
    assert (state.alerted, state.failing_runs) == (False, 0)


@pytest.mark.django_db
def test_a_flapping_check_alerts_nobody(admin: object) -> None:
    for _ in range(4):
        alerts.update([S3_DOWN])
        alerts.update([S3_UP])
    assert mail.outbox == []


@pytest.mark.django_db
def test_health_alert_emails_overrides_who_is_mailed(admin: object, settings: object) -> None:
    settings.HEALTH_ALERT_EMAILS = ["vagt@example.org"]  # type: ignore[attr-defined]
    for _ in range(alerts.ALERT_AFTER_RUNS):
        alerts.update([S3_DOWN])
    assert mail.outbox[0].to == ["vagt@example.org"]


def _pinged(settings: object, url: str, alerted: list[HealthCheckState]) -> list:
    settings.HEALTHCHECK_PING_URL = url  # type: ignore[attr-defined]
    with mock.patch.object(alerts.urllib.request, "urlopen") as urlopen:
        alerts.ping_monitor(alerted)
    return [call.args[0] for call in urlopen.call_args_list]


def test_no_ping_without_a_url(settings: object) -> None:
    assert _pinged(settings, "", []) == []


def test_an_http_url_is_refused(settings: object) -> None:
    assert _pinged(settings, "http://hc-ping.com/abc", []) == []


def test_all_good_is_a_plain_ping(settings: object) -> None:
    (request,) = _pinged(settings, "https://hc-ping.com/abc", [])
    assert (request.full_url, request.get_method()) == ("https://hc-ping.com/abc", "GET")


def test_a_failing_check_pings_fail_with_its_name(settings: object) -> None:
    state = HealthCheckState(name="S3", detail="Bucket gahk: 403 Forbidden")
    (request,) = _pinged(settings, "https://hc-ping.com/abc", [state])
    assert (request.full_url, request.get_method()) == ("https://hc-ping.com/abc/fail", "POST")
    assert request.data == b"S3: Bucket gahk: 403 Forbidden"


def test_a_ping_that_fails_does_not_raise(settings: object) -> None:
    settings.HEALTHCHECK_PING_URL = "https://hc-ping.com/abc"  # type: ignore[attr-defined]
    with mock.patch.object(alerts.urllib.request, "urlopen", side_effect=OSError("network down")):
        alerts.ping_monitor([])


@pytest.mark.django_db
def test_run_checks_and_pings(settings: object) -> None:
    with (
        mock.patch.object(health, "system_status", return_value=[S3_UP]),
        mock.patch.object(alerts, "ping_monitor") as ping,
    ):
        assert alerts.run() == 0
    ping.assert_called_once_with([])


def test_the_check_health_task_is_kept_out_of_the_worker_log() -> None:
    record = logging.LogRecord("celery.app.trace", logging.INFO, __file__, 1, "Task succeeded", None, None)
    record.data = {"name": "core.tasks.check_health", "id": "abc"}
    for logger in TASK_LIFECYCLE_LOGGERS:
        assert not logging.getLogger(logger).filter(record)


def test_queued_up_health_checks_expire_instead_of_running_in_a_burst() -> None:
    from django.conf import settings
    from django_celery_beat.schedulers import ModelEntry

    entry = settings.CELERY_BEAT_SCHEDULE["check-health"]
    expire = ModelEntry._unpack_options(**entry["options"])["expire_seconds"]
    assert expire is not None
    assert 0 < expire < entry["schedule"]
