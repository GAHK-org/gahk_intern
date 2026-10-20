"""/healthz and /health.

  * /healthz — liveness, for the Docker healthcheck Coolify reads. It only says this process is
    serving requests and touches nothing else, on purpose: Traefik stops routing to a container
    Docker calls unhealthy, so if it checked the workers, a crashed `beat` would take the whole
    site offline — and on a deploy the new `web` would wait for workers that are themselves
    waiting for its migrations.
  * /health — the whole stack, for an external uptime monitor: the database, the S3 bucket, and a
    fresh heartbeat (core/heartbeat.py) from every process in HEALTH_PROCESSES. 200 when all pass,
    503 otherwise, with a JSON body naming which check failed. Failure reasons go to the log, not
    the body — the endpoint is public and an exception message can carry hosts and keys.

Both are answered here, first in MIDDLEWARE, rather than from the URLconf: Docker probes
http://127.0.0.1:8000, which is not in production's ALLOWED_HOSTS, and nothing after this —
sessions, the front-page visit counter — has any business running for a probe.
"""

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import boto3
from botocore.config import Config
from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone

from .heartbeat import HEARTBEAT_MAX_AGE

logger = logging.getLogger(__name__)

# Fail fast: a monitor that times out learns nothing about which part is down.
_S3_CONFIG = {"connect_timeout": 3, "read_timeout": 3, "retries": {"max_attempts": 1}}


def check_database() -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return {"ok": True}


def _s3_client() -> Any:  # noqa: ANN401 — a boto3 S3 client
    # PHOTO_ALBUM_S3_OPTIONS rather than STORAGES["default"]: the same bucket and credentials, and
    # it stays S3 under the test settings, which swap the default storage for the local disk.
    options = settings.PHOTO_ALBUM_S3_OPTIONS
    return boto3.session.Session().client(
        "s3",
        endpoint_url=options["endpoint_url"],
        region_name=options["region_name"],
        aws_access_key_id=options["access_key"],
        aws_secret_access_key=options["secret_key"],
        config=Config(**_S3_CONFIG, s3={"addressing_style": options["addressing_style"]}),
    )


def check_storage() -> dict[str, Any]:
    _s3_client().head_bucket(Bucket=settings.PHOTO_ALBUM_S3_OPTIONS["bucket_name"])
    return {"ok": True}


def check_heartbeats() -> dict[str, dict[str, Any]]:
    from .models import ServiceHeartbeat

    names = settings.HEALTH_PROCESSES
    seen = dict(ServiceHeartbeat.objects.filter(name__in=names).values_list("name", "beat_at"))
    now = timezone.now()
    results: dict[str, dict[str, Any]] = {}
    for name in names:
        if name not in seen:
            results[name] = {"ok": False, "age_seconds": None}
            continue
        age = now - seen[name]
        results[name] = {
            "ok": age <= timedelta(seconds=HEARTBEAT_MAX_AGE),
            "age_seconds": int(age.total_seconds()),
        }
    return results


def _guarded(name: str, check: Callable[[], dict[str, Any]], failed: dict[str, Any]) -> dict[str, Any]:
    try:
        return check()
    except Exception:
        logger.exception("Health check %s failed", name)
        return failed


def health() -> tuple[bool, dict[str, Any]]:
    """(all ok, per-check results). Workers and beat are keyed by name, beside database/storage."""
    unknown = {name: {"ok": False, "age_seconds": None} for name in settings.HEALTH_PROCESSES}
    checks: dict[str, Any] = {
        "database": _guarded("database", check_database, {"ok": False}),
        "storage": _guarded("storage", check_storage, {"ok": False}),
    }
    # The heartbeats are rows in that database; with it down, every process is unknown.
    checks.update(_guarded("heartbeats", check_heartbeats, unknown) if checks["database"]["ok"] else unknown)
    return all(result["ok"] for result in checks.values()), checks


class HealthCheckMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path == "/healthz":
            return HttpResponse("ok", content_type="text/plain")
        if request.path == "/health":
            ok, checks = health()
            response = JsonResponse(
                {"status": "ok" if ok else "fail", "checks": checks}, status=200 if ok else 503
            )
            response["Cache-Control"] = "no-store"
            return response
        return self.get_response(request)
