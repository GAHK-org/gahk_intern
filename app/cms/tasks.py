"""Keeping gahk.dk in step with intern. Every exchange is started here; gahk.dk never calls intern.

  * publish_gahk_web — after every content save (cms.services.publish_on_commit), on "Udgiv nu",
    and whenever sync finds gahk.dk on another revision: build the snapshot, upload the images
    gahk.dk lacks, publish.
  * sync_gahk_web — every 2 minutes: import the old site's images the content still points at
    (cms.legacy_images; bounded, usually nothing to do), compare revisions (republish on a mismatch,
    so a missed or failed publish heals itself), then pull the outbox: optagelse applications and
    visit counts, stored here and acknowledged so gahk.dk can delete them.
  * rebuild_gahk_web — 00:05: gahk.dk re-renders from the snapshot it has, so "upcoming events"
    drop yesterday's.
  * nudge_gahk_web_update — every 15 minutes (and the overview's "Opdatér gahk.dk nu"): the webhost
    updates its own code from GitHub's production release. This is its ONLY update trigger; there is
    no cron job on the webhost.

With no GAHK_WEB_KEY every task logs and does nothing, so a dev machine or CI without a webhost
works as before. Errors never escape as task failures for an unreachable gahk.dk: they are stored on
GahkWebSync / Publication for the overview, and the next run tries again.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from celery import shared_task
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .gahk_web import GahkWebClient, GahkWebError, GahkWebRejected, MissingImages
from .images import sha256_hex
from .legacy_images import import_pending_legacy_images
from .models import CmsImage, GahkWebSync, Publication
from .snapshot import Snapshot, build_snapshot

logger = logging.getLogger(__name__)

# pg advisory lock id for "a publish is running" (any constant; "gahkweb" in ASCII, truncated).
PUBLISH_LOCK_ID = 0x6761686B776562
# A publish that finds another one running tries again this much later, a few times. If it gives
# up, sync_gahk_web still notices the stale revision within two minutes.
BUSY_RETRY_SECONDS = 15
BUSY_RETRIES = 8
MAX_ERROR_LENGTH = 2000


@contextmanager
def _publish_lock() -> Iterator[bool]:
    """Hold the publish lock if it is free; yields whether it was taken. Session-level, so it is
    released in `finally` even when the worker process lives on."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [PUBLISH_LOCK_ID])
        taken = bool(cursor.fetchone()[0])
    try:
        yield taken
    finally:
        if taken:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [PUBLISH_LOCK_ID])


def _note(**fields: Any) -> None:  # noqa: ANN401 — model field values
    """Update the GahkWebSync singleton's given fields only (other tasks write the rest)."""
    GahkWebSync.load()
    GahkWebSync.objects.filter(pk=1).update(**fields)


def _short(error: object) -> str:
    return str(error)[:MAX_ERROR_LENGTH]


# -------------------------------------------------------------------------------------- publish
def _upload_missing(client: GahkWebClient, snapshot: Snapshot, missing: list[str]) -> int:
    uploaded = 0
    for sha in missing:
        image = snapshot.images.get(sha)
        if image is None:
            continue  # not one we sent; gahk.dk will say so if it matters
        with image.file.open("rb") as handle:
            data = handle.read()
        if sha256_hex(data) != sha:
            # The stored file is not the one the row describes. Forget the metadata, so the next
            # snapshot reads it again, and fail this publish rather than send a wrong copy.
            CmsImage.objects.filter(pk=image.pk).update(sha256="", content_type="", size=None)
            raise GahkWebError(f"library image {image.pk} changed on disk; its hash is read again next time")
        client.put_image(sha, data, image.content_type)
        uploaded += 1
    return uploaded


def _failed(publication: Publication, error: BaseException) -> Publication:
    publication.status = Publication.Status.FAILED
    publication.error = _short(error) or error.__class__.__name__
    publication.finished_at = timezone.now()
    publication.save(update_fields=["status", "error", "finished_at"])
    _note(last_error=publication.error, last_error_at=publication.finished_at)
    return publication


def publish(
    reason: str, triggered_by_id: int | None = None, client: GahkWebClient | None = None
) -> Publication | None:
    """Publish the current content to gahk.dk. Returns the Publication, or None when disabled.

    Skipped (and recorded as such) when the last good publication had this revision and gahk.dk
    still reports it — unless asked by hand ("manual").
    """
    client = client or GahkWebClient()
    if not client.enabled:
        logger.info("GAHK_WEB_KEY is not set; not publishing to gahk.dk (reason=%s)", reason)
        return None

    try:
        snapshot = build_snapshot()
    except Exception as exc:
        # Recorded, so the overview shows a failed publish rather than nothing; raised, so the
        # traceback reaches the worker-jobs page.
        _failed(Publication.objects.create(reason=reason, triggered_by_id=triggered_by_id), exc)
        raise

    if reason != Publication.Reason.MANUAL:
        last_ok = Publication.objects.filter(status=Publication.Status.OK).first()
        if last_ok is not None and last_ok.revision == snapshot.revision:
            try:
                remote = client.status().get("revision")
            except GahkWebError:
                remote = None
            if remote == snapshot.revision:
                return Publication.objects.create(
                    reason=reason,
                    triggered_by_id=triggered_by_id,
                    revision=snapshot.revision,
                    status=Publication.Status.SKIPPED,
                    finished_at=timezone.now(),
                )

    publication = Publication.objects.create(
        reason=reason, triggered_by_id=triggered_by_id, revision=snapshot.revision
    )
    try:
        uploaded = _upload_missing(client, snapshot, client.images_missing(sorted(snapshot.images)))
        try:
            result = client.publish(snapshot.body())
        except MissingImages as exc:
            # Something removed images between asking and publishing (gahk.dk's garbage collection,
            # a restored backup). Send what it names and try once more.
            uploaded += _upload_missing(client, snapshot, exc.missing)
            result = client.publish(snapshot.body())
    except (GahkWebError, OSError) as exc:
        logger.warning("Publishing to gahk.dk failed (reason=%s): %s", reason, exc)
        return _failed(publication, exc)
    except Exception as exc:
        # Anything else (a storage error reading an image, say) must not leave the row "running".
        _failed(publication, exc)
        raise

    publication.status = Publication.Status.OK
    publication.pages_written = int(result.get("pages_written") or 0)
    publication.images_uploaded = uploaded
    publication.finished_at = timezone.now()
    publication.save(update_fields=["status", "pages_written", "images_uploaded", "finished_at"])
    _note(remote_revision=snapshot.revision)
    logger.info(
        "Published revision %s to gahk.dk (%s pages, %s images uploaded, reason=%s)",
        snapshot.revision[:12],
        publication.pages_written,
        uploaded,
        reason,
    )
    return publication


@shared_task
def publish_gahk_web(reason: str = "save", triggered_by_id: int | None = None, attempt: int = 0) -> str:
    """Publish to gahk.dk (see `publish`). Waits its turn behind a publish already running."""
    with _publish_lock() as taken:
        if not taken:
            if attempt < BUSY_RETRIES:
                publish_gahk_web.apply_async(
                    kwargs={"reason": reason, "triggered_by_id": triggered_by_id, "attempt": attempt + 1},
                    countdown=BUSY_RETRY_SECONDS,
                )
            return "busy"
        publication = publish(reason, triggered_by_id)
    return publication.status if publication is not None else "disabled"


# ----------------------------------------------------------------------------------------- sync
def _store_status(status: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "status_checked_at": timezone.now(),
        "remote_version": str(status.get("version") or "")[:64],
        "remote_revision": str(status.get("revision") or "")[:64],
    }
    update = status.get("update")
    if isinstance(update, dict):
        if update.get("available_version"):
            fields["update_available_version"] = str(update["available_version"])[:64]
        fields.update(_finished_update(update, fields["remote_version"]))
    _note(**fields)


def _finished_update(update: dict[str, Any], version: str) -> dict[str, Any]:
    """How the webhost's last self-update ended, from GET /api/status, when it ended after what the
    overview shows. An install slower than POST /api/update waits answers "still running" and goes
    on detached; this is how its outcome (an error above all) reaches the overview."""
    checked = parse_datetime(str(update.get("checked_at") or ""))
    result = update.get("last_result")
    if checked is None or timezone.is_naive(checked) or result not in ("updated", "unchanged", "failed"):
        return {}
    known = GahkWebSync.load().update_checked_at
    if known is not None and checked <= known:
        return {}
    if result == "failed":
        error = _short(f"failed: {update.get('last_error') or 'no reason given'}")
        return {"update_checked_at": checked, "update_result": error[:255], "update_error": error}
    summary = f"Opdateret til {version[:12]}" if result == "updated" else "Ingen ny version"
    return {"update_checked_at": checked, "update_result": summary, "update_error": ""}


def pull_outbox(client: GahkWebClient) -> dict[str, int]:
    """Store what gahk.dk's outbox holds and acknowledge it. Idempotent: pulling twice is harmless."""
    from admissions.services import ingest_outbox_applications
    from stats.services import ingest_outbox_visits

    outbox = client.outbox()
    applications = outbox.get("applications")
    visits = outbox.get("visits")
    result = ingest_outbox_applications(applications if isinstance(applications, list) else [])
    visits_through = ingest_outbox_visits(visits if isinstance(visits, list) else [])
    if result.ack_ids or visits_through is not None:
        client.ack(result.ack_ids, visits_through.isoformat() if visits_through else None)
    state = GahkWebSync.load()
    _note(
        outbox_pulled_at=timezone.now(),
        rejected_applications=state.rejected_applications + result.rejected,
    )
    return {"created": len(result.created), "acked": len(result.ack_ids), "rejected": result.rejected}


@shared_task
def sync_gahk_web() -> dict[str, Any]:
    """Every 2 minutes: bring in legacy images the content still points at, republish if gahk.dk is
    on another revision, then pull its outbox."""
    client = GahkWebClient()
    if not client.enabled:
        logger.info("GAHK_WEB_KEY is not set; not syncing with gahk.dk")
        return {"enabled": False}
    errors: list[str] = []
    report: dict[str, Any] = {"enabled": True}

    # Before the snapshot, so its rewrites are published in this same run. Bounded in files and
    # time (cms.legacy_images), so the outbox below never waits long; a failure here (S3 down) is
    # logged and the publish and the pull go ahead.
    try:
        legacy = import_pending_legacy_images()
        report["legacy_images"] = legacy.summary()
        if legacy.error:
            errors.append(_short(f"Gamle billeder: {legacy.error}"))
    except Exception as exc:
        logger.exception("Syncing with gahk.dk: importing the old site's images failed")
        errors.append(_short(f"Gamle billeder: {exc}"))

    try:
        status = client.status()
        _store_status(status)
        local = build_snapshot().revision
        report["in_step"] = status.get("revision") == local
        if not report["in_step"]:
            with _publish_lock() as taken:
                publication = publish(Publication.Reason.RECONCILE, client=client) if taken else None
            if publication is not None and publication.status == Publication.Status.FAILED:
                errors.append(publication.error)
    except GahkWebError as exc:
        errors.append(_short(exc))
    except Exception as exc:
        # Never let a publish problem (S3 down, one bad row) keep the applications and visit counts
        # waiting in gahk.dk's outbox, which is not durable storage. The traceback goes to the log.
        logger.exception("Syncing with gahk.dk: the revision check or publish failed")
        errors.append(_short(exc) or exc.__class__.__name__)

    try:
        report["outbox"] = pull_outbox(client)
    except GahkWebError as exc:
        errors.append(_short(exc))

    if errors:
        _note(last_error=_short("\n".join(errors)), last_error_at=timezone.now())
        logger.warning("Syncing with gahk.dk: %s", "; ".join(errors))
    else:
        _note(last_error="")
    report["errors"] = errors
    return report


# -------------------------------------------------------------------------------------- nightly
@shared_task
def rebuild_gahk_web() -> str:
    """00:05: gahk.dk re-renders from its snapshot. Publishes instead when it has none yet."""
    client = GahkWebClient()
    if not client.enabled:
        logger.info("GAHK_WEB_KEY is not set; no nightly rebuild of gahk.dk")
        return "disabled"
    with _publish_lock() as taken:
        if not taken:
            return "busy"
        started = timezone.now()
        try:
            result = client.rebuild()
        except GahkWebRejected as exc:
            if exc.status != 409:
                _note(last_error=_short(exc), last_error_at=timezone.now())
                return "failed"
            publication = publish(Publication.Reason.NIGHTLY, client=client)
            return publication.status if publication is not None else "disabled"
        except GahkWebError as exc:
            _note(last_error=_short(exc), last_error_at=timezone.now())
            return "failed"
        Publication.objects.create(
            started_at=started,
            finished_at=timezone.now(),
            reason=Publication.Reason.NIGHTLY,
            revision=GahkWebSync.load().remote_revision,
            status=Publication.Status.OK,
            pages_written=int(result.get("pages_written") or 0),
        )
    return "ok"


@shared_task
def nudge_gahk_web_update() -> dict[str, Any]:
    """Ask the webhost to update its code now (POST /api/update) and store what it says."""
    client = GahkWebClient()
    if not client.enabled:
        logger.info("GAHK_WEB_KEY is not set; not nudging gahk.dk to update")
        return {"enabled": False}
    now = timezone.now()
    try:
        result = client.update()
    except GahkWebError as exc:
        _note(update_checked_at=now, update_error=_short(exc))
        logger.warning("gahk.dk's self-update failed: %s", exc)
        return {"enabled": True, "error": str(exc)}
    version = str(result.get("version") or "")[:64]
    available = str(result.get("available_version") or "")[:64]
    detail = str(result.get("detail") or "")
    if result.get("updated"):
        summary = f"Opdateret til {version[:12]}" + (f": {detail}" if detail else "")
    else:
        summary = detail or "Ingen ny version"
    fields: dict[str, Any] = {
        "update_checked_at": now,
        "update_result": summary[:255],
        "update_error": "",
    }
    # An updater that could not start or gave no result answers without an available version: keep
    # the one we know rather than forget that an update is waiting.
    if available:
        fields["update_available_version"] = available
    if not result.get("updated") and detail.startswith("failed"):
        fields["update_error"] = _short(detail)
        logger.warning("gahk.dk's self-update failed: %s", detail)
    if version:
        fields["remote_version"] = version
    _note(**fields)
    return {"enabled": True, **result}
