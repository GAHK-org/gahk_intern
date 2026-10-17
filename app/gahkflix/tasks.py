"""GAHKFlix's background jobs. The encodes run on the `media` queue, one at a time (settings.py).

THE CLAIM IS THE LOCK, as in photo_album.tasks: a file moves QUEUED -> PROCESSING in one UPDATE, so
of two workers handed the same id exactly one gets it. A worker that dies mid-encode stops its
heartbeat, and `recover_stalled` puts the file back in the queue — at most MAX_ATTEMPTS times, so a
file that kills the worker every time is given up on instead of killing it every quarter hour.
"""

import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.db.models import F, Q
from django.utils import timezone

from . import notify, services
from .models import FileState, Video, VideoFile

logger = logging.getLogger(__name__)

# A two-hour film at Full HD and HD on a small box takes a while. What tells a slow encode from a
# dead one is its heartbeat (services.HEARTBEAT_TIMEOUT), so this is only the ceiling.
PROCESS_TIME_LIMIT = 8 * 60 * 60
PROCESS_SOFT_TIME_LIMIT = PROCESS_TIME_LIMIT - 300


def _claim(file_id: int) -> bool:
    return bool(
        VideoFile.objects.filter(pk=file_id, state=FileState.QUEUED).update(
            state=FileState.PROCESSING,
            heartbeat_at=timezone.now(),
            attempts=F("attempts") + 1,
            progress=0,
            error="",
        )
    )


def _give_up(video_file: VideoFile, reason: str) -> None:
    VideoFile.objects.filter(pk=video_file.pk).update(
        state=FileState.FAILED, error=reason, completed_at=timezone.now()
    )
    notify.processing_failed(video_file.video, reason)


@shared_task(time_limit=PROCESS_TIME_LIMIT, soft_time_limit=PROCESS_SOFT_TIME_LIMIT)
def process_video_file(file_id: int) -> bool:
    """Encode one committed upload, if it is still waiting and nobody else has it."""
    if not _claim(file_id):
        return False
    video_file = VideoFile.objects.select_related("video", "video__uploaded_by").get(pk=file_id)
    logger.info(
        "Processing %s (%d bytes) for video %s",
        video_file.original_name,
        video_file.size,
        video_file.video_id,
    )
    try:
        return services.process_file(video_file)
    except SoftTimeLimitExceeded:
        _give_up(video_file, "Behandlingen tog for lang tid.")
        return False
    except Exception:
        if video_file.attempts >= services.MAX_ATTEMPTS:
            _give_up(video_file, "Behandlingen mislykkedes gentagne gange.")
        else:
            # Back in the queue; recover_stalled sends it again once the heartbeat is old enough.
            VideoFile.objects.filter(pk=file_id, state=FileState.PROCESSING).update(state=FileState.QUEUED)
        raise


@shared_task(time_limit=60 * 60, soft_time_limit=55 * 60)
def redraw_frames(video_id: int) -> int:
    video = Video.objects.select_related("file").filter(pk=video_id).first()
    return services.redraw_frames(video) if video else 0


@shared_task
def recover_stalled() -> int:
    """Every quarter hour: send again what a lost worker or a lost message left waiting."""
    cutoff = timezone.now() - services.HEARTBEAT_TIMEOUT
    dead = VideoFile.objects.filter(state=FileState.PROCESSING, heartbeat_at__lt=cutoff)
    for video_file in dead.filter(attempts__gte=services.MAX_ATTEMPTS).select_related("video"):
        _give_up(video_file, "Behandlingen blev afbrudt for mange gange.")
    dead.update(state=FileState.QUEUED)
    waiting = VideoFile.objects.filter(state=FileState.QUEUED).filter(
        Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True, created_at__lt=cutoff)
    )
    count = 0
    for file_id in waiting.values_list("pk", flat=True):
        VideoFile.objects.filter(pk=file_id).update(heartbeat_at=timezone.now())
        process_video_file.delay(file_id)
        count += 1
    return count


@shared_task
def housekeeping() -> str:
    """Nightly: empty the bin after 30 days and unpublished drafts after 90, let old requests expire,
    and delete stored files nothing refers to."""
    binned, drafts = services.purge()
    expired = services.expire_requests()
    sweep = services.sweep_orphans()
    logger.info(
        "Purged %d binned video(s) and %d abandoned draft(s); %d request(s) expired; "
        "%d unreferenced file(s) deleted of %d stored%s.",
        binned,
        drafts,
        expired,
        len(sweep.deleted),
        sweep.stored,
        " (sweep refused)" if sweep.refused else "",
    )
    return f"{binned} binned, {drafts} drafts, {expired} requests, {len(sweep.deleted)} orphans"


@shared_task
def weekly_draft_reminders() -> int:
    """Mondays: remind uploaders of their unpublished drafts, and give the administrators an overview."""
    return services.send_weekly_draft_reminders()


@shared_task
def warn_expiring_drafts() -> int:
    """Daily: the day before a draft is deleted, warn its uploader and the administrators."""
    return services.warn_expiring_drafts()
