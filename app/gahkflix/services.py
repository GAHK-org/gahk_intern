"""State changes for GAHKFlix. The views, the worker tasks and the demo seed all go through these.

The rules themselves are in access.py (who may) and models.py (the windows and the counts); this
module is how each change is carried out — and, for the ones that need somebody else to agree, how
they are proposed, voted on and applied (ChangeRequest).
"""

import hashlib
import logging
import random
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile, File
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, F, Max, Q, QuerySet, Sum
from django.db.models.functions import Coalesce, Lower, TruncDate
from django.utils import timezone

from photo_album.models import validate_tag_name
from residents.models import Resident

from . import dating, encoding, notify
from .dating import DatePrecision, DateSource, PartialDate
from .models import (
    BIN_RETENTION,
    DRAFT_REMINDER_AFTER,
    DRAFT_TTL_DAYS,
    FRAMES_PER_DRAW,
    REQUEST_TTL,
    TRENDING_WINDOW,
    VIEW_DEDUPE_WINDOW,
    CastMember,
    ChangeRequest,
    ChangeVote,
    FileState,
    Series,
    Tag,
    Video,
    VideoEvent,
    VideoFile,
    VideoFrame,
    VideoSubtitle,
    ViewEvent,
    Watch,
    key_of,
    normalize_name,
    original_key,
    picture_path,
)
from .storage import get_video_storage, loopback_url, s3_storage
from .uploads import UploadTicket, check_file, content_type_for, valid_fingerprint

logger = logging.getLogger(__name__)

# An encode that fails this many times — the worker killed under it, the bucket unreachable — is
# given up on rather than retried every quarter of an hour for ever.
MAX_ATTEMPTS = 3
# A file whose worker has not reported for this long is presumed dead (tasks.recover_stalled).
HEARTBEAT_TIMEOUT = timedelta(minutes=15)
# A position closer than this to the end counts as watched to the end.
FINISHED_MARGIN = 0.05
# Less than this into a film is not worth offering to resume.
RESUME_FROM = 10.0

PICTURE_KINDS = ("thumbnail", "banner")


class NotProcessed(Exception):
    """The file is marked FAILED and its uploader told. Raised, not returned as False, so the job is
    listed as failed on the worker-jobs page, with why."""


def _now() -> datetime:
    return timezone.now()


def _file_keys(video_file: VideoFile) -> list[str | None]:
    """Every stored file of one upload: the original, its renditions and its subtitle tracks."""
    return [
        video_file.original.name,
        video_file.full_hd.name,
        video_file.hd.name,
        *(subtitle.track.name for subtitle in video_file.subtitles.all()),
    ]


def _delete_keys(keys: Iterable[str | None]) -> None:
    """Delete stored files once the transaction that stopped referring to them has committed."""
    present = list(dict.fromkeys(key for key in keys if key))
    if not present:
        return

    def delete() -> None:
        from . import cache

        storage = get_video_storage()
        for key in present:
            try:
                storage.delete(key)
            except Exception:
                logger.warning("Could not delete %s", key, exc_info=True)
            cache.discard(key)

    transaction.on_commit(delete)


# --- History ------------------------------------------------------------------------------------


def record_event(
    video: Video,
    kind: str,
    actor: Resident | None = None,
    *,
    changes: dict[str, Any] | None = None,
    request: ChangeRequest | None = None,
) -> VideoEvent:
    """A line in the video's history (VideoEvent)."""
    return VideoEvent.objects.create(
        video=video, kind=kind, actor=actor, changes=changes or {}, request=request
    )


def _event_pictures(changes: dict[str, Any]) -> list[str]:
    """The keys of the pictures a line of history names: each picture's before and after."""
    keys = []
    for kind in PICTURE_KINDS:
        entry = changes.get(kind)
        for side in ("before", "after"):
            picture = entry.get(side) if isinstance(entry, dict) else None
            if isinstance(picture, dict) and picture.get("key"):
                keys.append(picture["key"])
    return keys


# --- Tags ---------------------------------------------------------------------------------------


def tag_named(name: str) -> Tag:
    name = normalize_name(name.strip())
    validate_tag_name(name)
    if (existing := Tag.objects.filter(name__iexact=name).first()) is not None:
        return existing
    try:
        with transaction.atomic():
            return Tag.objects.create(name=name)
    except IntegrityError:
        return Tag.objects.get(name__iexact=name)


def search_tags(query: str, limit: int = 20) -> list[str]:
    """Tags some video carries, those starting with `query` first."""
    query = normalize_name(query.strip())
    tags = Tag.objects.filter(videos__isnull=False).distinct()
    if query:
        tags = tags.filter(name__icontains=query)
    names = list(tags.order_by(Lower("name")).values_list("name", flat=True)[:200])
    lowered = query.lower()
    return sorted(names, key=lambda name: (not name.lower().startswith(lowered), name.lower()))[:limit]


# --- Uploading ----------------------------------------------------------------------------------


def pending_upload(resident: Resident) -> VideoFile | None:
    """The upload of theirs still being processed — while there is one, they cannot start another."""
    return (
        VideoFile.objects.filter(uploaded_by=resident, state__in=(FileState.QUEUED, FileState.PROCESSING))
        .select_related("video")
        .first()
    )


def queue_ahead(video_file: VideoFile) -> int:
    """How many uploads are processed before this one, which waits its turn: the one being processed
    and those waiting since before it. Encodes run one at a time (tasks.py); the media worker's
    other jobs, the photo album's, are not counted."""
    earlier = Q(created_at__lt=video_file.created_at) | Q(
        created_at=video_file.created_at, pk__lt=video_file.pk
    )
    return (
        VideoFile.objects.filter(Q(state=FileState.PROCESSING) | (Q(state=FileState.QUEUED) & earlier))
        .exclude(pk=video_file.pk)
        .count()
    )


def fingerprint_duplicate(fingerprint: str, size: int) -> Video | None:
    """A video that already has a file with this fingerprint and size (failed uploads aside)."""
    match = (
        VideoFile.objects.filter(fingerprint=fingerprint, size=size)
        .exclude(state=FileState.FAILED)
        .select_related("video")
        .first()
    )
    return match.video if match else None


def _upload_refusal(resident: Resident, fingerprint: str, size: int) -> str | None:
    if (pending := pending_upload(resident)) is not None:
        return f"Du har allerede en video, der behandles ({pending.video.title}). Vent, til den er færdig."
    if (duplicate := fingerprint_duplicate(fingerprint, size)) is not None:
        return f"Den video er allerede uploadet til GAHKFlix ({duplicate.title})."
    return None


def begin_upload(
    resident: Resident,
    *,
    name: str,
    size: int,
    content_type: str,
    fingerprint: str,
    last_modified: str = "",
    video: Video | None = None,
) -> UploadTicket:
    """Check a file before it is sent and decide where it goes. Raises ValidationError."""
    name = normalize_name(Path(name).name.strip())
    if error := check_file(name, size, content_type):
        raise ValidationError(error)
    fingerprint = fingerprint.lower()
    if not valid_fingerprint(fingerprint):
        raise ValidationError("Ugyldigt fingeraftryk af filen.")
    if error := _upload_refusal(resident, fingerprint, size):
        raise ValidationError(error)
    video_uuid = str(video.uuid) if video else str(uuid4())
    file_uuid = str(uuid4())
    return UploadTicket(
        resident=resident.pk,
        video_uuid=video_uuid,
        file_uuid=file_uuid,
        key=original_key(video_uuid, file_uuid, name),
        name=name,
        size=size,
        content_type=content_type_for(name),
        fingerprint=fingerprint,
        last_modified=last_modified if last_modified.isdecimal() else "",
        video=video.pk if video else None,
    )


def _file_modified_at(value: str) -> datetime | None:
    """A browser's File.lastModified (milliseconds), unless it is "now" or nonsense."""
    if not value.isdecimal():
        return None
    try:
        moment = datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    if moment.year < 1990 or moment > _now() - timedelta(minutes=1):
        return None
    return moment


def _title_from(name: str) -> str:
    return " ".join(Path(name).stem.replace("_", " ").replace("-", " ").split())[:200] or "Uden titel"


def commit_upload(ticket: UploadTicket, resident: Resident, stored_size: int | None) -> Video:
    """Create the rows for a file the storage has, and queue its encode. Raises ValidationError.

    Refusing here deletes what was uploaded: nothing else will ever refer to it.
    """

    def refuse(message: str) -> ValidationError:
        if stored_size is not None:
            get_video_storage().delete(ticket.key)
        return ValidationError(message)

    if ticket.resident != resident.pk:
        raise ValidationError("Uploaden tilhører en anden.")
    if stored_size is None:
        raise ValidationError("Filen nåede ikke frem. Prøv igen.")
    if stored_size != ticket.size:
        raise refuse("Filen kom ikke helt frem. Prøv igen.")
    with transaction.atomic():
        # Locked, so two tabs committing at once cannot both get past the one-at-a-time rule.
        Resident.objects.select_for_update().filter(pk=resident.pk).first()
        if error := _upload_refusal(resident, ticket.fingerprint, ticket.size):
            raise refuse(error)
        if ticket.video is not None:
            video = Video.objects.filter(pk=ticket.video, deleted_at__isnull=True).first()
            if video is None:
                raise refuse("Videoen findes ikke længere.")
            # Earlier failed versions are only clutter once a new one is on its way.
            for failed in video.files.filter(state=FileState.FAILED).exclude(pk=video.file_id or 0):
                _delete_keys(_file_keys(failed))
                failed.delete()
        else:
            video = Video.objects.create(
                uuid=ticket.video_uuid, title=_title_from(ticket.name), uploaded_by=resident
            )
        video_file = VideoFile(
            uuid=ticket.file_uuid,
            video=video,
            uploaded_by=resident,
            original_name=ticket.name,
            size=ticket.size,
            content_type=ticket.content_type,
            fingerprint=ticket.fingerprint,
            file_modified_at=_file_modified_at(ticket.last_modified),
        )
        video_file.original.name = ticket.key
        video_file.save()
        record_event(
            video,
            VideoEvent.Kind.NEW_VERSION if ticket.video is not None else VideoEvent.Kind.UPLOADED,
            resident,
            changes={"file": ticket.name, "size": ticket.size},
        )
        from .tasks import process_video_file

        transaction.on_commit(lambda: process_video_file.delay(video_file.pk))
    logger.info("Upload of %s committed as video %s by %s", ticket.name, video.pk, resident.pk)
    return video


# --- Processing ---------------------------------------------------------------------------------


def _heartbeat(video_file: VideoFile) -> encoding.Throttle:
    def write(fraction: float) -> None:
        VideoFile.objects.filter(pk=video_file.pk).update(progress=round(fraction * 100), heartbeat_at=_now())

    return encoding.Throttle(write)


def _download(name: str, target: Path, size: int, on_progress: Callable[[float], None]) -> str:
    """Copy a stored file to local disk, hashing it on the way. Returns its SHA-256.

    Straight from the bucket rather than through Storage.open(), which downloads the whole object
    into a temporary file of its own first — twice the disk for a 5 GB film.
    """
    digest = hashlib.sha256()
    written = 0
    storage = s3_storage()
    with target.open("wb") as out:
        if storage is not None:
            body = storage.connection.meta.client.get_object(Bucket=storage.bucket_name, Key=name)["Body"]
            chunks: Iterable[bytes] = body.iter_chunks(8 * 1024 * 1024)
        else:
            source = get_video_storage().open(name, "rb")
            chunks = iter(lambda: source.read(8 * 1024 * 1024), b"")
        for chunk in chunks:
            digest.update(chunk)
            out.write(chunk)
            written += len(chunk)
            on_progress(written / max(size, 1))
    return digest.hexdigest()


def _fail(video_file: VideoFile, reason: str, *, delete_original: bool = False) -> bool:
    VideoFile.objects.filter(pk=video_file.pk).update(
        state=FileState.FAILED, error=reason[:255], completed_at=_now()
    )
    if delete_original:
        get_video_storage().delete(key_of(video_file.original))
    logger.warning("Video file %s (%s) failed: %s", video_file.pk, video_file.original_name, reason)
    record_failure(video_file, reason)
    return False


def record_failure(video_file: VideoFile, reason: str) -> None:
    """Tell the uploader an upload could not be processed, and put it in the video's history."""
    record_event(
        video_file.video, VideoEvent.Kind.FAILED, changes={"file": video_file.original_name, "error": reason}
    )
    notify.processing_failed(video_file.video, reason)


def _store_frames(video: Video, frames: list[tuple[float, Path]]) -> list[VideoFrame]:
    """Replace the video's candidate frames with a new batch. Pictures already cut from the old
    ones are files of their own, so they stay."""
    batch = frame_batch(video) + 1
    stored = []
    for at, path in frames:
        frame = VideoFrame(video=video, at_seconds=at, batch=batch)
        with path.open("rb") as image:
            frame.image.save("frame.jpg", File(image), save=False)
        frame.save()
        stored.append(frame)
    old = list(video.frames.filter(batch__lt=batch))
    _delete_keys(frame.image.name for frame in old)
    VideoFrame.objects.filter(pk__in=[frame.pk for frame in old]).delete()
    return stored


def _store_subtitles(video_file: VideoFile, tracks: list[tuple[encoding.SubtitleStream, Path]]) -> None:
    """Keep the upload's text subtitles, replacing any that an earlier attempt at it stored."""
    earlier = list(video_file.subtitles.all())
    _delete_keys(subtitle.track.name for subtitle in earlier)
    VideoSubtitle.objects.filter(pk__in=[subtitle.pk for subtitle in earlier]).delete()
    for position, (stream, path) in enumerate(tracks):
        subtitle = VideoSubtitle(
            file=video_file,
            position=position,
            language=stream.language[:16],
            title=stream.title[:255],
            is_default=stream.default,
            is_forced=stream.forced,
        )
        language = "".join(c for c in stream.language if c.isalnum()) or "und"
        with path.open("rb") as track:
            subtitle.track.save(f"{position + 1}-{language}.vtt", File(track), save=False)
        subtitle.save()
    if tracks:
        logger.info("Kept %d subtitle track(s) of %s", len(tracks), video_file.name_and_size)


def process_file(video_file: VideoFile, rng: random.Random | None = None) -> bool:
    """Download, check, encode and cut frames from one claimed upload. Returns whether it is READY.

    A refusal (a duplicate, a file with no video in it) marks it FAILED, tells the uploader and
    returns False: the job did what it should. An encode ffmpeg rejects is marked FAILED the same way
    but raises NotProcessed, so the job is listed as failed. Anything else is raised for the task to
    retry, ffmpeg stopped by a signal included: running out of memory says nothing about the file.
    """
    video = video_file.video
    beat = _heartbeat(video_file)
    with TemporaryDirectory(prefix="gahkflix-") as directory:
        work = Path(directory)
        source = work / f"source{Path(video_file.original_name).suffix.lower() or '.video'}"
        sha256 = _download(key_of(video_file.original), source, video_file.size, lambda f: beat(f * 0.1))
        VideoFile.objects.filter(pk=video_file.pk).update(sha256=sha256)
        twin = (
            VideoFile.objects.filter(sha256=sha256)
            .exclude(pk=video_file.pk)
            .exclude(state=FileState.FAILED)
            .select_related("video")
            .first()
        )
        if twin is not None:
            if twin.video_id == video.pk:
                return _fail(
                    video_file, "Det er den samme fil som videoens nuværende version.", delete_original=True
                )
            return _fail(
                video_file, f"Videoen findes allerede på GAHKFlix ({twin.video.title}).", delete_original=True
            )
        info = encoding.probe(source)
        if not info.has_video or not info.width or not info.height:
            return _fail(video_file, "Filen indeholder ingen video, der kan afspilles.")
        plan = encoding.plan_renditions(info.width, info.height)
        outputs = {rendition.name: work / f"{rendition.name}.mp4" for rendition in plan}
        try:
            encoding.encode(
                source, outputs, plan, info.duration, lambda f: beat(0.1 + f * 0.8), audio=info.audio_stream
            )
        except encoding.EncodeError as exc:
            if exc.killed:
                logger.warning(
                    "Encoding %s stopped, attempt %d of %d: %s",
                    video_file.name_and_size,
                    video_file.attempts,
                    MAX_ATTEMPTS,
                    exc,
                )
                raise
            logger.warning("ffmpeg refused video file %s: %s", video_file.pk, exc)
            _fail(video_file, "Videoen kunne ikke omkodes. Filen er måske ødelagt.")
            raise NotProcessed(f"{video_file.name_and_size} could not be encoded: {exc}") from exc
        beat(0.9, force=True)
        subtitles = encoding.extract_subtitles(source, info.subtitles, work, lambda f: beat(0.9 + f * 0.03))
        stem = "".join(c if c.isalnum() or c in "-_" else "-" for c in Path(video_file.original_name).stem)[
            :60
        ]
        with outputs["hd"].open("rb") as hd:
            video_file.hd.save(f"{stem or 'video'}-hd.mp4", File(hd), save=False)
        video_file.hd_size = outputs["hd"].stat().st_size
        if "fullhd" in outputs:
            with outputs["fullhd"].open("rb") as full_hd:
                video_file.full_hd.save(f"{stem or 'video'}-fullhd.mp4", File(full_hd), save=False)
            video_file.full_hd_size = outputs["fullhd"].stat().st_size
        _store_subtitles(video_file, subtitles)
        beat(0.95, force=True)
        frames = encoding.draw_frames(source, info.duration, work, FRAMES_PER_DRAW, rng or random.Random())  # noqa: S311  # nosec B311
        _store_frames(video, frames)
    with transaction.atomic():
        video_file.state = FileState.READY
        video_file.progress = 100
        video_file.error = ""
        video_file.completed_at = _now()
        video_file.duration = info.duration
        video_file.width, video_file.height = info.width, info.height
        video_file.recorded_at = info.recorded_at
        video_file.save(
            update_fields=[
                "state", "progress", "error", "completed_at", "duration", "width", "height",
                "recorded_at", "hd", "full_hd", "hd_size", "full_hd_size",
            ]
        )  # fmt: skip
        video = Video.objects.select_for_update().get(pk=video.pk)
        previous = video.file
        dated = video.release
        video.file = video_file
        video.save(update_fields=["file", *_date_from_file(video, video_file, info)])
        changes: dict[str, Any] = {"file": video_file.original_name, "replaced": previous is not None}
        if video.release != dated:
            changes["released_on"] = {
                "before": dated.iso() if dated else None,
                "after": video.release.iso() if video.release else None,
            }
        record_event(video, VideoEvent.Kind.PROCESSED, changes=changes)
        if previous is not None and previous.pk != video_file.pk:
            _delete_keys(_file_keys(previous))
            previous.delete()
    logger.info("Video file %s ready: %s, %.0f s", video_file.pk, plan, info.duration or 0)
    notify.processed(video, revision=previous is not None)
    return True


def _date_from_file(video: Video, video_file: VideoFile, info: encoding.Probe) -> list[str]:
    """Guess the film's release date from its new file (dating.guess), unless it was set by hand.
    Returns the fields it changed."""
    if video.release_source == DateSource.MANUAL:
        return []
    found = dating.guess(
        name=video_file.original_name,
        recorded_at=info.recorded_at,
        tags=info.date_tags,
        modified_at=video_file.file_modified_at,
    )
    if found is None:
        return []
    video.released_on, video.release_precision = found.date.start, found.date.precision
    video.release_source = found.source
    return ["released_on", "release_precision", "release_source"]


def redraw_frames(video: Video, rng: random.Random | None = None) -> int:
    """Ten new candidate frames, cut from the HD rendition (smaller to fetch than the original)."""
    video_file = video.file
    if video_file is None or not video_file.hd:
        return 0
    with TemporaryDirectory(prefix="gahkflix-frames-") as directory:
        work = Path(directory)
        source = work / "hd.mp4"
        _download(key_of(video_file.hd), source, 1, lambda f: None)
        frames = encoding.draw_frames(
            source,
            video_file.duration,
            work,
            FRAMES_PER_DRAW,
            rng or random.Random(),  # noqa: S311  # nosec B311
        )
        return len(_store_frames(video, frames))


def frame_batch(video: Video) -> int:
    """The number of the newest batch of frames; 0 before the first."""
    return video.frames.aggregate(latest=Max("batch"))["latest"] or 0


def current_frames(video: Video) -> list[VideoFrame]:
    latest = frame_batch(video)
    return list(video.frames.filter(batch=latest)) if latest else []


# --- Pictures -----------------------------------------------------------------------------------


def picture_from_frame(frame: VideoFrame, kind: str) -> bytes:
    with frame.image.open("rb") as image:
        return encoding.thumbnail_jpeg(image) if kind == "thumbnail" else encoding.banner_jpeg(image)


def picture_at(video: Video, at: float, kind: str) -> bytes:
    """A thumbnail or banner from the exact moment `at` (seconds) the uploader picked themselves.

    Cut from the best rendition there is, reading only the parts of it ffmpeg needs (loopback_url).
    Raises ValidationError.
    """
    video_file = video.file
    if video_file is None:
        raise ValidationError("Videoen er ikke færdigbehandlet endnu.")
    duration = video_file.duration or 0
    if not 0 <= at <= duration:
        raise ValidationError("Tidspunktet ligger uden for videoen.")
    key = key_of(video_file.full_hd) or key_of(video_file.hd)
    with TemporaryDirectory(prefix="gahkflix-frame-") as directory, loopback_url(key) as source:
        target = Path(directory) / "frame.jpg"
        # The very last instant has no frame to cut; the one just before it does.
        if not encoding.extract_frame(source, min(at, max(duration - 0.1, 0)), target):
            raise ValidationError("Billedet kunne ikke hentes fra videoen. Prøv et andet tidspunkt.")
        with target.open("rb") as image:
            return encoding.thumbnail_jpeg(image) if kind == "thumbnail" else encoding.banner_jpeg(image)


def picture_from_upload(upload: Any, kind: str) -> bytes:  # noqa: ANN401 — an UploadedFile
    """A resident's own thumbnail or banner: any picture, HEIC included, cut to 16:9. Raises ValidationError."""
    from django.conf import settings

    from core.uploads import validate_image_upload

    prepared = validate_image_upload(upload, settings.GAHKFLIX_PICTURE_MAX_MB)
    try:
        prepared.seek(0)
        return encoding.banner_jpeg(prepared) if kind == "banner" else encoding.thumbnail_jpeg(prepared)
    except OSError as exc:
        raise ValidationError("Billedet kunne ikke læses.") from exc


def store_picture(video: Video, kind: str, data: bytes) -> str:
    """Save a picture under a key of its own and return the key. Nothing refers to it yet."""
    return get_video_storage().save(picture_path(video, f"{kind}.jpg"), ContentFile(data))


@dataclass(frozen=True)
class PastPicture:
    """A thumbnail or banner the video has had, as a line of its history names it."""

    kind: str
    key: str
    frame: int | None
    # The picture this one is a copy of, when it was brought back; else its own key.
    origin: str
    event: VideoEvent
    side: str  # "before" or "after"

    @property
    def ref(self) -> str:
        """What a form sends to bring it back: the line of history and the side of it."""
        return f"{self.event.pk}-{self.side}"


def pictures_named(event: VideoEvent, kind: str) -> list[PastPicture]:
    """The thumbnails (or banners) a line of history names: the one after the change, then before."""
    entry = event.changes.get(kind)
    found = []
    for side in ("after", "before"):
        picture = entry.get(side) if isinstance(entry, dict) else None
        if isinstance(picture, dict) and picture.get("key"):
            key = picture["key"]
            found.append(
                PastPicture(kind, key, picture.get("frame"), picture.get("from") or key, event, side)
            )
    return found


def _all_named(video: Video, kind: str) -> list[PastPicture]:
    return [
        picture
        for event in video.events.filter(kind=VideoEvent.Kind.EDITED)
        for picture in pictures_named(event, kind)
    ]


def _in_use(video: Video, kind: str, named: list[PastPicture]) -> set[str]:
    current = key_of(getattr(video, kind))
    return {current} | {picture.origin for picture in named if picture.key == current}


def pictures_in_use(video: Video, kind: str) -> set[str]:
    """The thumbnail (or banner) the video has now, as the origins a PastPicture may have: its own
    key and, when it was brought back, that of the picture it is a copy of."""
    return _in_use(video, kind, _all_named(video, kind))


def past_pictures(video: Video, kind: str) -> list[PastPicture]:
    """The thumbnails (or banners) the video has had other than the one it has now, the latest
    first, each once: a picture brought back is the same picture as the one it is a copy of."""
    named = _all_named(video, kind)
    seen = _in_use(video, kind, named)
    found = []
    for picture in named:
        if picture.origin not in seen:
            seen.add(picture.origin)
            found.append(picture)
    return found


def past_picture(video: Video, kind: str, ref: str) -> PastPicture:
    """The earlier picture a form names (PastPicture.ref). Raises ValidationError."""
    event_id, _, side = ref.partition("-")
    event = video.events.filter(
        kind=VideoEvent.Kind.EDITED, pk=int(event_id) if event_id.isdecimal() else 0
    ).first()
    for picture in pictures_named(event, kind) if event else []:
        if picture.side == side and picture.key != key_of(getattr(video, kind)):
            return picture
    raise ValidationError("Det billede findes ikke længere.")


def copy_of(picture: PastPicture) -> bytes:
    """An earlier picture's bytes, to store again under a key of its own (store_picture): a
    picture proposed for approval is deleted if the proposal is turned down."""
    with get_video_storage().open(picture.key, "rb") as stored:
        data: bytes = stored.read()
    return data


@dataclass
class Outcome:
    """What an edit did: applied straight away, became (part of) a proposal, or changed nothing."""

    applied: bool = False
    proposal: ChangeRequest | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.applied or self.proposal is not None


def set_picture(
    video: Video,
    kind: str,
    key: str,
    frame: VideoFrame | None,
    resident: Resident,
    *,
    needs_approval: bool,
    origin: str = "",
) -> Outcome:
    """Make a stored picture the thumbnail or the banner. `origin` is the earlier picture it is a
    copy of, when one is brought back (PastPicture.origin)."""
    if kind not in PICTURE_KINDS:
        raise ValueError(kind)
    picture: dict[str, Any] = {"key": key, "frame": frame.pk if frame else None}
    if origin:
        picture["from"] = origin
    change = {kind: picture}
    try:
        if needs_approval:
            return Outcome(proposal=propose(video, change, resident))
        with transaction.atomic():
            apply_changes(video, change, resident)
    except Exception:
        # Stored before anything referred to it, so nothing else will ever delete it.
        get_video_storage().delete(key)
        raise
    return Outcome(applied=True)


# --- Details and editing ------------------------------------------------------------------------
#
# A video's editable details as one plain, JSON-ready dict. The edit form produces one, a proposal
# stores the part of one that differs, and applying either goes through `apply_changes`.

DETAIL_FIELDS = ("title", "description", "released_on", "series", "season", "episode", "cast", "tags")


def cast_list(video: Video) -> list[dict[str, Any]]:
    return [
        {"resident": member.resident_id, "name": member.display_name, "role": member.role}
        for member in video.cast.select_related("resident")
    ]


def snapshot(video: Video) -> dict[str, Any]:
    return {
        "title": video.title,
        "description": video.description,
        "released_on": release.iso() if (release := video.release) else None,
        "series": video.series_id,
        "season": video.season,
        "episode": video.episode,
        "cast": cast_list(video),
        "tags": sorted(video.tags.values_list("name", flat=True), key=str.lower),
    }


def _comparable(key: str, value: Any) -> Any:  # noqa: ANN401 — any detail value
    if key == "tags":
        return sorted(str(name).lower() for name in value)
    if key == "cast":
        return [
            (member.get("resident"), "" if member.get("resident") else member["name"], member["role"])
            for member in value
        ]
    return value


def diff(current: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
    """The entries of `proposed` that differ from `current`."""
    return {
        key: value
        for key, value in proposed.items()
        if key not in current or _comparable(key, value) != _comparable(key, current[key])
    }


def _replace_cast(video: Video, cast: list[dict[str, Any]]) -> set[int]:
    """Set the credits; return the residents who were not credited before."""
    before = set(video.cast.exclude(resident=None).values_list("resident_id", flat=True))
    residents = {
        r.pk: r for r in Resident.objects.filter(pk__in=[m["resident"] for m in cast if m.get("resident")])
    }
    video.cast.all().delete()
    seen: set[int] = set()
    rows = []
    for position, member in enumerate(cast):
        resident = residents.get(member.get("resident") or 0)
        if resident is not None and resident.pk in seen:
            continue
        name = resident.full_name if resident else normalize_name(str(member.get("name", "")).strip())
        if not name:
            continue
        if resident is not None:
            seen.add(resident.pk)
        rows.append(
            CastMember(
                video=video,
                resident=resident,
                name=name[:120],
                role=str(member.get("role", "")).strip()[:120],
                position=position,
            )
        )
    CastMember.objects.bulk_create(rows)
    return seen - before


def apply_changes(
    video: Video, changes: dict[str, Any], resident: Resident, *, request: ChangeRequest | None = None
) -> list[str]:
    """Write `changes` to the video, as an edit or as the proposal `request` once it is approved,
    and add what changed to the video's history. Returns notes on anything that could not be
    applied as asked. Call inside a transaction.

    A picture replaced is kept, not deleted: the history names it, so it can be brought back.
    """
    details_before = snapshot(video) if any(key in DETAIL_FIELDS for key in changes) else {}
    pictures_before = {
        kind: {"key": key_of(getattr(video, kind)), "frame": getattr(video, f"{kind}_frame_id")}
        for kind in PICTURE_KINDS
        if kind in changes
    }
    notes: list[str] = []
    fields: list[str] = []
    for name in ("title", "description", "season", "episode"):
        if name in changes:
            setattr(video, name, changes[name])
            fields.append(name)
    if "released_on" in changes:
        # Set by hand now: a new version of the file is no longer dated over it. Cleared, it is.
        release = PartialDate.parse(changes["released_on"])
        video.released_on = release.start if release else None
        video.release_precision = release.precision if release else DatePrecision.DAY
        video.release_source = DateSource.MANUAL if release else ""
        fields += ["released_on", "release_precision", "release_source"]
    if "series" in changes:
        series = Series.objects.filter(pk=changes["series"]).first() if changes["series"] else None
        if changes["series"] and series is None:
            notes.append("Serien findes ikke længere, så videoen er ikke lagt i den.")
        video.series = series
        fields.append("series")
    for kind in pictures_before:
        getattr(video, kind).name = changes[kind]["key"]
        frame_id = changes[kind].get("frame")
        setattr(
            video,
            f"{kind}_frame",
            VideoFrame.objects.filter(pk=frame_id, video=video).first() if frame_id else None,
        )
        fields += [kind, f"{kind}_frame"]
    if fields:
        video.save(update_fields=fields)
    if "tags" in changes:
        video.tags.set([tag_named(name) for name in changes["tags"]])
    if "cast" in changes:
        added = _replace_cast(video, changes["cast"])
        transaction.on_commit(lambda: notify.credited(video, added, resident))
    # As it came out, which is not always as asked: a series deleted meanwhile, a name resolved.
    details_after = snapshot(video) if details_before else {}
    history = {
        key: {"before": details_before[key], "after": details_after[key]}
        for key in DETAIL_FIELDS
        if key in changes and _comparable(key, details_before[key]) != _comparable(key, details_after[key])
    }
    history.update(
        {kind: {"before": before, "after": changes[kind]} for kind, before in pictures_before.items()}
    )
    if history:
        record_event(video, VideoEvent.Kind.EDITED, resident, changes=history, request=request)
    return notes


def edit_video(
    video: Video, proposed: dict[str, Any], resident: Resident, *, needs_approval: bool
) -> Outcome:
    changes = diff(snapshot(video), proposed)
    if not changes:
        return Outcome()
    if needs_approval:
        return Outcome(proposal=propose(video, changes, resident))
    with transaction.atomic():
        notes = apply_changes(video, changes, resident)
    return Outcome(applied=True, notes=notes)


def edit_series(
    series: Series, proposed: dict[str, Any], resident: Resident, *, needs_approval: bool
) -> Outcome:
    current = {"title": series.title, "description": series.description}
    changes = diff(current, proposed)
    if not changes:
        return Outcome()
    if needs_approval:
        return Outcome(proposal=propose(series, changes, resident))
    _apply_series(series, changes)
    return Outcome(applied=True)


def _apply_series(series: Series, changes: dict[str, Any]) -> list[str]:
    for name in ("title", "description"):
        if name in changes:
            setattr(series, name, changes[name])
    try:
        with transaction.atomic():
            series.save(update_fields=[name for name in ("title", "description") if name in changes])
    except IntegrityError:
        return ["Der findes allerede en serie med den titel, så titlen er ikke ændret."]
    return []


# --- Publishing ---------------------------------------------------------------------------------


def missing_for_publishing(video: Video) -> list[str]:
    """Why the draft cannot be published yet, in the order the uploader should fix it."""
    missing = []
    if video.file is None:
        missing.append("Videoen er ikke færdigbehandlet.")
    if not video.title.strip():
        missing.append("Videoen skal have en titel.")
    if not video.thumbnail:
        missing.append("Vælg en miniature.")
    if not video.banner:
        missing.append("Vælg et banner.")
    return missing


def publish(video: Video, resident: Resident) -> None:
    if missing := missing_for_publishing(video):
        raise ValidationError(missing)
    video.published_at = _now()
    video.save(update_fields=["published_at"])
    record_event(video, VideoEvent.Kind.PUBLISHED, resident)
    credited = list(video.cast.exclude(resident=None).values_list("resident_id", flat=True))

    def announce() -> None:
        notify.published(video)
        notify.credited(video, credited, video.uploaded_by)

    transaction.on_commit(announce)


# --- Requests -----------------------------------------------------------------------------------


def open_request(target: Video | Series, kind: str) -> ChangeRequest | None:
    lookup = {"video": target} if isinstance(target, Video) else {"series": target}
    return ChangeRequest.objects.filter(kind=kind, state=ChangeRequest.State.OPEN, **lookup).first()


def _pending_pictures(changes: dict[str, Any]) -> list[str]:
    return [changes[kind]["key"] for kind in PICTURE_KINDS if isinstance(changes.get(kind), dict)]


def propose(target: Video | Series, changes: dict[str, Any], resident: Resident) -> ChangeRequest:
    """An edit for another administrator to accept. The resident's own open proposal is extended
    rather than a second one opened; somebody else's has to be decided first."""
    with transaction.atomic():
        existing = open_request(target, ChangeRequest.Kind.EDIT)
        if existing is not None and existing.requested_by_id != resident.pk:
            raise ValidationError(
                f"{existing.requested_by.full_name} har allerede foreslået en ændring, som venter på godkendelse."
            )
        if existing is not None:
            superseded = [
                existing.changes[kind]["key"]
                for kind in PICTURE_KINDS
                if kind in changes and kind in existing.changes
            ]
            existing.changes = {**existing.changes, **changes}
            existing.save(update_fields=["changes"])
            _delete_keys(superseded)
            return existing
        lookup = {"video": target} if isinstance(target, Video) else {"series": target}
        change = ChangeRequest.objects.create(
            kind=ChangeRequest.Kind.EDIT, requested_by=resident, changes=changes, **lookup
        )
        transaction.on_commit(lambda: notify.request_opened(change))
    return change


def request_deletion(video: Video, resident: Resident, reason: str) -> ChangeRequest:
    if open_request(video, ChangeRequest.Kind.DELETE) is not None:
        raise ValidationError("Der er allerede en anmodning om at slette videoen.")
    if not reason.strip():
        raise ValidationError("Skriv, hvorfor videoen skal slettes.")
    with transaction.atomic():
        change = ChangeRequest.objects.create(
            kind=ChangeRequest.Kind.DELETE, video=video, requested_by=resident, reason=reason.strip()
        )
        transaction.on_commit(lambda: notify.request_opened(change))
    return change


def _close(change: ChangeRequest, state: str, note: str = "") -> None:
    change.state = state
    change.decided_at = _now()
    change.note = note[:255]
    change.save(update_fields=["state", "decided_at", "note"])
    if state != ChangeRequest.State.APPROVED:
        _delete_keys(_pending_pictures(change.changes))


def vote(change: ChangeRequest, voter: Resident, *, approve: bool, comment: str = "") -> ChangeRequest:
    """One administrator's vote. A rejection closes the request; enough approvals carry it out."""
    with transaction.atomic():
        # Only the request row is locked: video and series are nullable joins, which FOR UPDATE refuses.
        change = (
            ChangeRequest.objects.select_for_update(of=("self",))
            .select_related("video", "series", "requested_by")
            .get(pk=change.pk)
        )
        if not change.is_open:
            raise ValidationError("Anmodningen er allerede afgjort.")
        if change.requested_by_id == voter.pk:
            raise ValidationError("Du kan ikke godkende din egen anmodning.")
        try:
            with transaction.atomic():
                ChangeVote.objects.create(
                    request=change, voter=voter, approve=approve, comment=comment.strip()[:255]
                )
        except IntegrityError as exc:
            raise ValidationError("Du har allerede stemt.") from exc
        if not approve:
            _close(change, ChangeRequest.State.REJECTED, comment)
        elif change.votes.filter(approve=True).count() >= change.approvals_needed:
            notes = _carry_out(change)
            _close(change, ChangeRequest.State.APPROVED, " ".join(notes))
        if not change.is_open:
            _tell_requester(change)
    return change


def _tell_requester(change: ChangeRequest) -> None:
    transaction.on_commit(lambda: notify.request_decided(change))


def _carry_out(change: ChangeRequest) -> list[str]:
    if change.kind == ChangeRequest.Kind.DELETE:
        assert change.video is not None  # noqa: S101  # nosec B101 — the check constraint
        if change.video.is_binned:
            return ["Videoen lå allerede i papirkurven."]
        bin_video(change.video, change.requested_by, closing=change)
        return []
    if change.video is not None:
        if change.video.is_binned:
            return ["Videoen er slettet, så ændringen er ikke gennemført."]
        return apply_changes(change.video, change.changes, change.requested_by, request=change)
    assert change.series is not None  # noqa: S101  # nosec B101 — the check constraint
    return _apply_series(change.series, change.changes)


def withdraw(change: ChangeRequest) -> None:
    with transaction.atomic():
        change = ChangeRequest.objects.select_for_update().get(pk=change.pk)
        if change.is_open:
            _close(change, ChangeRequest.State.WITHDRAWN)


def expire_requests(now: datetime | None = None) -> int:
    expired = ChangeRequest.objects.filter(
        state=ChangeRequest.State.OPEN, created_at__lt=(now or _now()) - REQUEST_TTL
    )
    count = 0
    for change in expired.select_related("video", "series", "requested_by"):
        with transaction.atomic():
            _close(change, ChangeRequest.State.EXPIRED, "Ingen nåede at tage stilling inden for 30 dage.")
            _tell_requester(change)
        count += 1
    return count


# --- Bin and deletion ---------------------------------------------------------------------------


def bin_video(video: Video, resident: Resident, *, closing: ChangeRequest | None = None) -> None:
    """Move a published video to the bin. Proposals about it are moot now, and are closed."""
    video.deleted_at = _now()
    video.deleted_by = resident
    video.save(update_fields=["deleted_at", "deleted_by"])
    record_event(video, VideoEvent.Kind.BINNED, resident, request=closing)
    for change in video.change_requests.filter(state=ChangeRequest.State.OPEN).exclude(
        pk=closing.pk if closing else 0
    ):
        _close(change, ChangeRequest.State.WITHDRAWN, "Videoen blev slettet.")


def restore(video: Video, resident: Resident) -> None:
    video.deleted_at = None
    video.deleted_by = None
    video.save(update_fields=["deleted_at", "deleted_by"])
    record_event(video, VideoEvent.Kind.RESTORED, resident)


def delete_permanently(video: Video) -> None:
    """Every stored file of the video, then its rows. For a draft, and for the bin's sweep."""
    keys: list[str | None] = [video.thumbnail.name, video.banner.name]
    for video_file in video.files.all():
        keys += _file_keys(video_file)
    keys += [frame.image.name for frame in video.frames.all()]
    for change in video.change_requests.filter(state=ChangeRequest.State.OPEN):
        keys += _pending_pictures(change.changes)
    for changes in video.events.filter(kind=VideoEvent.Kind.EDITED).values_list("changes", flat=True):
        keys += _event_pictures(changes)
    with transaction.atomic():
        Video.objects.filter(pk=video.pk).update(file=None, thumbnail_frame=None, banner_frame=None)
        video.delete()
        _delete_keys(keys)


def _start_of(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))


def expired_drafts(today: date | None = None) -> QuerySet[Video]:
    """Drafts whose deletion date (Video.draft_deleted_on) has come: uploaded DRAFT_TTL_DAYS or more
    calendar days ago."""
    today = today or timezone.localdate()
    return Video.objects.drafts().filter(created_at__lt=_start_of(today - timedelta(days=DRAFT_TTL_DAYS - 1)))


def purge(now: datetime | None = None) -> tuple[int, int]:
    """The nightly sweep: binned videos after 30 days, and drafts never published by their deletion date.

    A draft whose file is still being processed is left for the next night.
    """
    moment = now or _now()
    binned = Video.objects.filter(deleted_at__lt=moment - BIN_RETENTION)
    drafts = expired_drafts(timezone.localdate(moment)).exclude(
        files__state__in=(FileState.QUEUED, FileState.PROCESSING)
    )
    counts = []
    for videos in (binned, drafts):
        count = 0
        for video in videos.distinct():
            delete_permanently(video)
            count += 1
        counts.append(count)
    return counts[0], counts[1]


# --- Reminders about drafts ---------------------------------------------------------------------


def drafts_to_remind(now: datetime | None = None) -> list[Video]:
    """Unpublished drafts more than a week old, the one to be deleted first first."""
    moment = now or _now()
    return list(
        Video.objects.drafts()
        .filter(created_at__lt=moment - DRAFT_REMINDER_AFTER)
        .select_related("uploaded_by")
        .order_by("created_at", "pk")
    )


def send_weekly_draft_reminders(now: datetime | None = None) -> int:
    """Each uploader hears about their own unpublished drafts, the administrators about all of them."""
    drafts = drafts_to_remind(now)
    by_uploader: dict[int, list[Video]] = defaultdict(list)
    for video in drafts:
        by_uploader[video.uploaded_by_id].append(video)
    for own in by_uploader.values():
        notify.drafts_reminder(own)
    if drafts:
        notify.drafts_overview(drafts)
    return len(drafts)


def warn_expiring_drafts(now: datetime | None = None) -> int:
    """The day before a draft is deleted, its uploader and the administrators are told — once."""
    moment = now or _now()
    tomorrow = timezone.localdate(moment) + timedelta(days=1)
    expiring = expired_drafts(tomorrow).filter(draft_warned_at__isnull=True).select_related("uploaded_by")
    count = 0
    for video in expiring:
        if Video.objects.filter(pk=video.pk, draft_warned_at__isnull=True).update(draft_warned_at=moment):
            notify.draft_expiring(video)
            count += 1
    return count


# --- Files nothing refers to --------------------------------------------------------------------

# How old a file must be before the sweep may take it. An upload's token is good for a day, and a
# file is stored before the row that refers to it is committed — an upload being saved, an encode
# being finished — so anything younger may still be about to be claimed.
ORPHAN_GRACE = timedelta(days=2)
# More than this share of what is stored, unreferenced at once, is not abandoned uploads: it is a
# database that does not belong to this bucket (a restore, a developer's laptop pointed at
# production). The sweep then deletes nothing and says so.
ORPHAN_SANITY_SHARE = 0.5
ORPHAN_SANITY_MINIMUM = 20


def referenced_keys() -> set[str]:
    """Every stored file a GAHKFlix row refers to, including pictures proposed in open requests
    and the earlier pictures a video's history names."""
    keys: set[str] = set()
    for fields in (
        VideoFile.objects.values_list("original", "full_hd", "hd"),
        VideoSubtitle.objects.values_list("track"),
        Video.objects.values_list("thumbnail", "banner"),
        VideoFrame.objects.values_list("image"),
    ):
        for row in fields:
            keys.update(key for key in row if key)
    for change in ChangeRequest.objects.filter(state=ChangeRequest.State.OPEN):
        keys.update(_pending_pictures(change.changes))
    for changes in VideoEvent.objects.filter(kind=VideoEvent.Kind.EDITED).values_list("changes", flat=True):
        keys.update(_event_pictures(changes))
    return keys


def _stored_files() -> list[tuple[str, datetime]]:
    """(key, last modified) for everything under gahkflix/ in the storage."""
    storage = s3_storage()
    if storage is not None:
        return [(obj.key, obj.last_modified) for obj in storage.bucket.objects.filter(Prefix="gahkflix/")]
    root = Path(get_video_storage().path(""))
    return [
        (path.relative_to(root).as_posix(), datetime.fromtimestamp(path.stat().st_mtime, tz=UTC))
        for path in (root / "gahkflix").rglob("*")
        if path.is_file()
    ]


@dataclass
class OrphanSweep:
    stored: int
    deleted: list[str]
    refused: bool = False


def sweep_orphans(now: datetime | None = None) -> OrphanSweep:
    """Delete stored files no row refers to, once they are ORPHAN_GRACE old.

    What leaves them: an upload that reached the bucket but whose tab was closed before it was
    saved, and anything a crashed worker or a rolled-back transaction stored on its way.
    """
    cutoff = (now or _now()) - ORPHAN_GRACE
    stored = _stored_files()
    referenced = referenced_keys()
    orphans = [key for key, modified in stored if key not in referenced and modified < cutoff]
    if len(orphans) > ORPHAN_SANITY_MINIMUM and len(orphans) > len(stored) * ORPHAN_SANITY_SHARE:
        logger.error(
            "Refusing to delete %d of %d stored GAHKFlix files: that many unreferenced files means the "
            "database does not match this bucket. Nothing was deleted.",
            len(orphans),
            len(stored),
        )
        return OrphanSweep(stored=len(stored), deleted=[], refused=True)
    from . import cache

    storage = get_video_storage()
    for key in orphans:
        storage.delete(key)
        cache.discard(key)
        logger.info("Deleted %s, which nothing refers to.", key)
    return OrphanSweep(stored=len(stored), deleted=orphans)


# --- Watching -----------------------------------------------------------------------------------


def _watch(video: Video, resident: Resident) -> Watch:
    try:
        with transaction.atomic():
            return Watch.objects.get_or_create(video=video, resident=resident)[0]
    except IntegrityError:  # the other tab created it first
        return Watch.objects.get(video=video, resident=resident)


def open_viewing(video: Video, resident: Resident, now: datetime | None = None) -> ViewEvent:
    """The player has opened the video for the resident: go on with their viewing of it if they
    left it short of the end less than VIEW_DEDUPE_WINDOW ago, else begin a new one."""
    moment = now or _now()
    viewing = (
        ViewEvent.objects.filter(
            video=video, resident=resident, finished=False, active_at__gte=moment - VIEW_DEDUPE_WINDOW
        )
        .order_by("-active_at", "-pk")
        .first()
    )
    if viewing is None:
        return ViewEvent.objects.create(video=video, resident=resident, opened_at=moment, active_at=moment)
    ViewEvent.objects.filter(pk=viewing.pk).update(active_at=moment)
    return viewing


def record_view(
    video: Video, resident: Resident, now: datetime | None = None, *, viewing: ViewEvent | None = None
) -> bool:
    """Count a view (the player asks once a few seconds have actually played). One statement, so
    two tabs reporting at once cannot both count. `viewing` is the player's (open_viewing): one
    that has counted already is not counted again."""
    moment = now or _now()
    if viewing is not None and viewing.viewed_at is not None:
        return False
    watch = _watch(video, resident)
    counted = (
        Watch.objects.filter(pk=watch.pk)
        .filter(Q(last_viewed_at__isnull=True) | Q(last_viewed_at__lt=moment - VIEW_DEDUPE_WINDOW))
        .update(views=F("views") + 1, last_viewed_at=moment)
    )
    if counted and viewing is not None:
        ViewEvent.objects.filter(pk=viewing.pk).update(viewed_at=moment, active_at=moment)
    elif counted:
        ViewEvent.objects.create(
            video=video, resident=resident, opened_at=moment, viewed_at=moment, active_at=moment
        )
    return bool(counted)


# Seconds of play one report may add to a viewing whose film has no known length.
MAX_REPORTED = 4 * 60 * 60.0


def save_position(
    video: Video,
    resident: Resident,
    position: float,
    now: datetime | None = None,
    *,
    viewing: ViewEvent | None = None,
    watched: float = 0.0,
) -> Watch:
    """Where the resident is in the video; and, for the player's `viewing`, the seconds of it that
    have played since its last report (`watched`) and whether it has now reached the end."""
    watch = _watch(video, resident)
    duration = video.duration or 0
    watch.position = max(0.0, min(position, duration or position))
    watch.finished = bool(duration) and (duration - watch.position <= max(5.0, duration * FINISHED_MARGIN))
    watch.position_at = now or _now()
    watch.save(update_fields=["position", "finished", "position_at"])
    if viewing is not None:
        # One report adds no more than the whole film, whatever a player says.
        played = max(0.0, min(watched, duration or MAX_REPORTED))
        changes: dict[str, Any] = {"watched": F("watched") + played, "active_at": watch.position_at}
        if watch.finished:
            changes["finished"] = True
        ViewEvent.objects.filter(pk=viewing.pk).update(**changes)
    return watch


def resume_position(video: Video, resident: Resident) -> float:
    watch = Watch.objects.filter(video=video, resident=resident).first()
    if watch is None or watch.finished or watch.position < RESUME_FROM:
        return 0.0
    return watch.position


def progress_by_video(resident: Resident, videos: Iterable[Video]) -> dict[int, float]:
    """How far the resident is into each video, 0–1, for the red bar under a card."""
    ids = [video.pk for video in videos]
    out = {}
    for watch in Watch.objects.filter(resident=resident, video_id__in=ids).select_related("video__file"):
        duration = watch.video.duration
        if duration and watch.position >= RESUME_FROM:
            out[watch.video_id] = 1.0 if watch.finished else min(1.0, watch.position / duration)
    return out


def continue_watching(resident: Resident, limit: int = 12) -> list[Video]:
    watches = (
        Watch.objects.filter(resident=resident, finished=False, position__gte=RESUME_FROM)
        .filter(video__in=Video.objects.published())
        .select_related("video__file", "video__series")
        .order_by("-position_at")[:limit]
    )
    return [watch.video for watch in watches]


def trending(limit: int = 10, now: datetime | None = None) -> list[Video]:
    """The most unique viewers over the last week."""
    since = (now or _now()) - TRENDING_WINDOW
    rows = list(
        ViewEvent.objects.filter(viewed_at__gte=since, video__in=Video.objects.published())
        .values("video")
        .annotate(viewers=Count("resident", distinct=True), latest=Max("viewed_at"))
        .order_by("-viewers", "-latest")[:limit]
    )
    videos = published().in_bulk([row["video"] for row in rows])
    out = []
    for row in rows:
        video = videos[row["video"]]
        video.trending_viewers = row["viewers"]  # type: ignore[attr-defined]
        out.append(video)
    return out


@dataclass
class ViewStats:
    views: int
    viewers: int
    views_last_week: int
    days: list[tuple[date, int]]
    # How often the player was opened on it, a view or not.
    opened: int = 0
    # The views whose playing time was measured — every one since the player has reported it — of
    # them those that reached the end, and the seconds an average one played.
    measured: int = 0
    finished: int = 0
    average_watched: float | None = None
    duration: float | None = None

    @property
    def peak(self) -> int:
        return max((n for _day, n in self.days), default=0)

    @property
    def finished_share(self) -> int | None:
        """The share of the measured views that reached the end, in per cent."""
        return round(100 * self.finished / self.measured) if self.measured else None

    @property
    def average_share(self) -> int | None:
        """How much of the film an average view played, in per cent."""
        if self.average_watched is None or not self.duration:
            return None
        return round(100 * min(self.average_watched / self.duration, 1))


def view_stats(video: Video, days: int = 30, now: datetime | None = None) -> ViewStats:
    totals = video.watches.filter(views__gt=0).aggregate(views=Coalesce(Sum("views"), 0), viewers=Count("pk"))
    measured = Q(viewed_at__isnull=False, watched__gt=0)
    viewings = video.view_events.aggregate(
        opened=Count("pk"),
        measured=Count("pk", filter=measured),
        finished=Count("pk", filter=measured & Q(finished=True)),
        average_watched=Avg("watched", filter=measured),
    )
    today = timezone.localdate(now or _now())
    first = today - timedelta(days=days - 1)
    per_day = dict(
        video.view_events.filter(viewed_at__date__gte=first)
        .annotate(day=TruncDate("viewed_at"))
        .values("day")
        .annotate(n=Count("pk"))
        .values_list("day", "n")
    )
    series = [
        (first + timedelta(days=offset), per_day.get(first + timedelta(days=offset), 0))
        for offset in range(days)
    ]
    return ViewStats(
        views=totals["views"],
        viewers=totals["viewers"],
        views_last_week=sum(n for day, n in series if day > today - timedelta(days=7)),
        days=series,
        duration=video.duration,
        **viewings,
    )


def fill_rendition_sizes(video_file: VideoFile) -> None:
    """Ask the storage for a rendition size the encode did not record — files encoded before the
    sizes were — and keep it, so the download menu asks once."""
    from .storage import stored_size

    changed = []
    for rendition, size_field in (("hd", "hd_size"), ("full_hd", "full_hd_size")):
        key = key_of(getattr(video_file, rendition))
        if key and getattr(video_file, size_field) is None:
            try:
                size = stored_size(key)
            except Exception:  # the menu without a size beats a page that fails
                logger.warning("Could not read the size of %s", key, exc_info=True)
                continue
            if size is not None:
                setattr(video_file, size_field, size)
                changed.append(size_field)
    if changed:
        video_file.save(update_fields=changed)


def view_counts(videos: Iterable[Video]) -> dict[int, tuple[int, int]]:
    """(views, unique viewers) per video, in one query."""
    rows = (
        Watch.objects.filter(video_id__in=[video.pk for video in videos], views__gt=0)
        .values("video")
        .annotate(views=Sum("views"), viewers=Count("pk"))
    )
    return {row["video"]: (row["views"], row["viewers"]) for row in rows}


# --- Browsing -----------------------------------------------------------------------------------


def published() -> QuerySet[Video]:
    return Video.objects.published().select_related("file", "series")


def search(query: str) -> QuerySet[Video]:
    """Every word must turn up somewhere: title, description, a tag, a credit or the series."""
    videos = published()
    for word in query.split()[:8]:
        videos = videos.filter(
            Q(title__icontains=word)
            | Q(description__icontains=word)
            | Q(tags__name__icontains=word)
            | Q(cast__name__icontains=word)
            | Q(cast__role__icontains=word)
            | Q(cast__resident__first_name__icontains=word)
            | Q(cast__resident__last_name__icontains=word)
            | Q(series__title__icontains=word)
        )
    return videos.distinct()


def series_with_episodes() -> QuerySet[Series]:
    return Series.objects.filter(videos__in=Video.objects.published()).distinct()


def episodes(series: Series) -> list[tuple[int | None, list[Video]]]:
    """The published episodes, season by season, in order."""
    ordered = sorted(
        published().filter(series=series),
        key=lambda v: (v.season or 0, v.episode or 10**6, v.display_date, v.pk),
    )
    seasons: dict[int | None, list[Video]] = defaultdict(list)
    for video in ordered:
        seasons[video.season].append(video)
    return list(seasons.items())


def series_cover(series: Series) -> Video | None:
    """The episode whose pictures stand for the whole series: the first one."""
    for _season, videos in episodes(series):
        if videos:
            return videos[0]
    return None


def covers(series_list: Iterable[Series]) -> dict[int, Video]:
    out = {}
    for series in series_list:
        if (cover := series_cover(series)) is not None:
            out[series.pk] = cover
    return out


def next_episode(video: Video) -> Video | None:
    if video.series is None:
        return None
    flat = [episode for _season, videos in episodes(video.series) for episode in videos]
    ids = [episode.pk for episode in flat]
    if video.pk in ids and ids.index(video.pk) + 1 < len(flat):
        return flat[ids.index(video.pk) + 1]
    return None


def up_next(video: Video, resident: Resident) -> Video | None:
    """What autoplay starts after this video: the next episode, else a film the resident hasn't finished."""
    if (episode := next_episode(video)) is not None:
        return episode
    finished = set(Watch.objects.filter(resident=resident, finished=True).values_list("video_id", flat=True))
    newest = published().filter(series=None).order_by("-published_at")[:50]
    for candidate in [*similar(video), *trending(20), *newest]:
        if (
            candidate.series_id is None
            and candidate.pk != video.pk
            and candidate.file_id
            and candidate.pk not in finished
        ):
            return candidate
    # Everything seen: start over with the film watched longest ago.
    rewatch = (
        Watch.objects.filter(resident=resident, finished=True, video__in=published().filter(series=None))
        .exclude(video=video)
        .exclude(video__file=None)
        .select_related("video__file")
        .order_by(F("position_at").asc(nulls_first=True), "pk")
        .first()
    )
    return rewatch.video if rewatch else None


def similar(video: Video, limit: int = 12) -> list[Video]:
    """More like this: other videos sharing its tags or its cast, most in common first."""
    tags = list(video.tags.values_list("pk", flat=True))
    residents = list(video.cast.exclude(resident=None).values_list("resident_id", flat=True))
    names = list(video.cast.filter(resident=None).values_list("name", flat=True))
    if not (tags or residents or names):
        return []
    scores: dict[int, int] = defaultdict(int)
    others = published().exclude(pk=video.pk)
    if video.series_id:
        others = others.exclude(series_id=video.series_id)
    for pk in others.filter(tags__in=tags).values_list("pk", flat=True):
        scores[pk] += 1
    for pk in others.filter(
        Q(cast__resident__in=residents) | Q(cast__name__in=names, cast__resident=None)
    ).values_list("pk", flat=True):
        scores[pk] += 1
    ranked = sorted(scores, key=lambda pk: -scores[pk])[:limit]
    found = published().in_bulk(ranked)
    return [found[pk] for pk in ranked if pk in found]


def sort_date_ordering() -> list[Any]:
    return [Coalesce("released_on", TruncDate("created_at")).desc(), "-pk"]


def timeline() -> list[tuple[int, list[tuple[int | None, list[Video]]]]]:
    """Every published video by year and month of its date, newest first. A video dated only to
    its year comes after that year's months (month None)."""
    years: dict[int, dict[int | None, list[Video]]] = defaultdict(lambda: defaultdict(list))
    for video in published().order_by(*sort_date_ordering()):
        years[video.display_date.year][video.display_month].append(video)
    return [
        (year, sorted(months.items(), key=lambda item: 0 if item[0] is None else -item[0]))
        for year, months in years.items()
    ]


def credits_of(resident: Resident) -> QuerySet[Video]:
    return published().filter(cast__resident=resident).distinct()


def credits_named(name: str) -> QuerySet[Video]:
    return published().filter(cast__resident=None, cast__name__iexact=normalize_name(name.strip())).distinct()


def tag_rows(limit: int = 4, minimum: int = 2) -> list[tuple[Tag, list[Video]]]:
    """The most used tags, each with its videos — the home page's genre rows."""
    tags = (
        Tag.objects.filter(videos__in=Video.objects.published())
        .annotate(n=Count("videos", distinct=True))
        .filter(n__gte=minimum)
        .order_by("-n", Lower("name"))[:limit]
    )
    return [(tag, list(published().filter(tags=tag).order_by("-published_at")[:20])) for tag in tags]


def hero(candidates: list[Video], today: date | None = None) -> Video | None:
    """The banner on the home page: one of the first few candidates, a new one each day."""
    pool = [video for video in candidates[:5] if video.banner]
    if not pool:
        return None
    return pool[(today or timezone.localdate()).toordinal() % len(pool)]
