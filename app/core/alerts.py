"""Alerts on the status checks (core.health), for a site that runs on a single server.

A server cannot report its own death, so this has two halves:

  * OUTSIDE: every run pings a dead man's switch service (HEALTHCHECK_PING_URL, e.g. Healthchecks.io).
    That service alerts when the pings STOP — the server, beat or the default worker gone — and when
    a ping says /fail. Nothing is exposed: the server only makes outgoing requests, and the ping
    carries the names of failing checks, nothing personal.
  * INSIDE: the administrators get an e-mail and a system notification when a check starts failing,
    and again when it recovers. That covers what does not take the server down: the database past
    its limit, S3 failing, a worker gone quiet.

A check must fail ALERT_AFTER_RUNS runs in a row before anyone is told, so one slow S3 answer does
not wake anybody. Recovery is reported on the first good run.
"""

import logging
import urllib.request

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils import timezone

from residents.models import Resident, Role, active_period

from . import health
from .models import HealthCheckState

logger = logging.getLogger(__name__)

ALERT_AFTER_RUNS = 2  # with the 5-minute schedule: failing for about ten minutes
PING_TIMEOUT_SECONDS = 10


def administrators() -> QuerySet[Resident]:
    """This month's administrators and the superusers: who is told."""
    year, month = active_period()
    return Resident.objects.filter(
        Q(is_superuser=True)
        | Q(
            role_assignments__role=Role.ADMINISTRATOR,
            role_assignments__year=year,
            role_assignments__month=month,
        ),
        is_active=True,
    ).distinct()


def _status_url() -> str:
    return settings.SITE_URL.rstrip("/") + reverse("siteadmin:status")


def _tell(subject: str, body: str, *, failing: bool) -> None:
    from notifications import categories
    from notifications import services as notifications
    from notifications.categories import Importance

    admins = administrators()
    recipients = settings.HEALTH_ALERT_EMAILS or sorted(set(admins.values_list("email", flat=True)))
    if recipients:
        send_mail(
            subject, f"{body}\n\nDriftsstatus: {_status_url()}", settings.DEFAULT_FROM_EMAIL, recipients
        )
    notifications.notify(
        admins,
        category=categories.SYSTEM,
        importance=Importance.HOEJ if failing else Importance.NORMAL,
        title=subject,
        body=body,
        url=reverse("siteadmin:status"),
    )


def update(checks: list[health.Check]) -> list[HealthCheckState]:
    """Record this run's results; tell the administrators about every check that changed. Returns
    the checks currently alerted as failing."""
    now = timezone.now()
    states = {s.name: s for s in HealthCheckState.objects.filter(name__in=[c.name for c in checks])}
    alerted: list[HealthCheckState] = []
    for check in checks:
        state = states.get(check.name) or HealthCheckState(name=check.name, changed_at=now)
        if check.status == health.FAIL:
            state.failing_runs += 1
            state.detail = check.detail[:300]
            if not state.alerted and state.failing_runs >= ALERT_AFTER_RUNS:
                state.alerted, state.changed_at = True, now
                _tell(f"GAHK drift: {check.name} fejler", check.detail, failing=True)
        else:
            if state.alerted:
                state.alerted, state.changed_at = False, now
                _tell(f"GAHK drift: {check.name} virker igen", check.detail, failing=False)
            state.failing_runs = 0
            state.detail = check.detail[:300]
        state.save()
        if state.alerted:
            alerted.append(state)
    return alerted


def ping_monitor(alerted: list[HealthCheckState]) -> None:
    """Tell the dead man's switch this run happened, and whether anything is failing. A no-op
    without HEALTHCHECK_PING_URL (dev, tests)."""
    url = settings.HEALTHCHECK_PING_URL
    if not url:
        return
    if not url.startswith("https://"):
        logger.warning("HEALTHCHECK_PING_URL must be https; not pinging.")
        return
    # https only, checked above, so no file: or custom scheme can reach urlopen.
    if alerted:
        body = "\n".join(f"{s.name}: {s.detail}" for s in alerted).encode()
        fail_url = f"{url.rstrip('/')}/fail"
        request = urllib.request.Request(fail_url, data=body, method="POST")  # noqa: S310  # nosec B310
    else:
        request = urllib.request.Request(url)  # noqa: S310  # nosec B310
    try:
        with urllib.request.urlopen(request, timeout=PING_TIMEOUT_SECONDS):  # noqa: S310  # nosec B310
            pass
    except Exception:  # the monitor alerts on the missing ping, which is the right outcome anyway
        logger.warning("Could not ping the health monitor.", exc_info=True)


def run() -> int:
    alerted = update(health.system_status())
    ping_monitor(alerted)
    return len(alerted)
