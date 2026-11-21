"""A small GAHKFlix for the local demo: a series with two episodes and two films, really encoded.

The clips are a few seconds of ffmpeg's own test patterns at 640x360, so the whole thing takes a few
seconds — but they go through the real pipeline (services.process_file), so the rows, renditions,
frames and pictures are exactly what an upload makes, and the player plays them.

They are made as part of the demo's history (audit.demo), not with the rest of the seed data: an
upload, its encode, the pictures and details chosen and the publishing are what the video's own
history page (VideoEvent) and the audit trail show, so they happen at the film's own time, weeks
ago, rather than all at the moment of seeding. Around them:

  * the series made, and its description edited later;
  * a proposal by the uploader to change a film past its 30 days, left open for a second
    administrator (the seed has one, admin@);
  * the newest film put in the bin and brought back;
  * a resident's upload that is no video at all: the worker refuses it, and its uploader deletes
    the draft.

Where a service stamps the clock (publishing, the bin) the demo does what it does with the
history's time instead, and the rows the services date themselves (the video's own history, the
upload and request times) are dated to the event afterwards.
"""

import random
import subprocess  # nosec B404
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from django.core.files.base import ContentFile
from django.db.models import Max
from django.urls import reverse

from audit import trail
from audit.demo import HOUSE, PHONES, Moment, ago
from residents.models import Resident

from . import encoding, services
from .models import ChangeRequest, FileState, Series, Video, VideoEvent, VideoFile, original_key
from .storage import get_video_storage

SERIES = "Gangen"

# (title, description, pattern, episode or None, days ago, tags)
FILMS = [
    ("Pilot", "Hvem har taget ostehapsen fra fælleskøleskabet?", "testsrc2", 1, 20, ["sketch"]),
    ("Opvasken", "Turnusordningen bryder sammen.", "smptehdbars", 2, 12, ["sketch"]),
    (
        "Revy",
        "Årets revy: sange, sketches og en finale, ingen kommer sig over.",
        "mandelbrot",
        None,
        45,
        ["revy", "fest"],
    ),
    ("Julefrokost", "Fra sildemadder til sidste dans.", "life", None, 3, ["fest", "jul"]),
]


def _clip(directory: Path, pattern: str, index: int) -> Path:
    target = directory / f"demo-{index}.mp4"
    subprocess.run(  # noqa: S603  # nosec B603
        [
            encoding.ffmpeg_exe(), "-v", "error", "-y",
            "-f", "lavfi", "-i", f"{pattern}=size=640x360:rate=25",
            "-f", "lavfi", "-i", f"sine=frequency={330 + 110 * index}",
            "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(target),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    return target


def history(residents: list[Resident], now: datetime, rng: random.Random) -> list[Moment]:
    if len(residents) < 2:
        return []
    uploader, *others = residents
    first_episode = max(days for *_rest, episode, days, _tags in FILMS if episode)
    series_at = ago(now, first_episode, 19, 40)
    moments = [
        Moment(series_at, lambda: _create_series(uploader), uploader, HOUSE, reverse("gahkflix:upload")),
        Moment(
            ago(now, 15, 21, 10),
            lambda: services.edit_series(
                Series.objects.get(title=SERIES),
                {"description": "Hverdagen på 3. sal, fortalt af dem, der bor der. Ny sæson til foråret."},
                uploader,
                needs_approval=False,
            ),
            uploader,
            HOUSE,
        ),
    ]
    for index, (title, description, pattern, episode, days, tags) in enumerate(FILMS):
        details = {
            "title": title,
            "description": description,
            "season": 1 if episode else None,
            "episode": episode,
            "tags": tags,
            "cast": [
                {"resident": r.pk, "name": "", "role": ""} for r in rng.sample(others, min(3, len(others)))
            ],
        }
        viewers = rng.sample(residents, min(len(residents), 2 + index * 2))
        uploaded = ago(now, days, 20 - index, 15)
        moments += _film(now, uploader, index, pattern, uploaded, details, viewers, rng)

    binned, restored, proposed = ago(now, 2, 10, 0), ago(now, 1, 9, 0), ago(now, 9, 20, 30)
    moments += [
        # Past its 30 days a change is a proposal for another administrator to accept.
        Moment(
            proposed,
            _dated(
                proposed,
                lambda: services.edit_video(
                    Video.objects.get(title="Revy"),
                    {"description": "Årets revy: sange, sketches og en finale, som ingen kommer sig over."},
                    uploader,
                    needs_approval=True,
                ),
            ),
            uploader,
            HOUSE,
        ),
        Moment(binned, _dated(binned, lambda: _bin("Julefrokost", uploader, binned)), uploader, PHONES[1]),
        Moment(
            restored,
            _dated(restored, lambda: services.restore(Video.objects.get(title="Julefrokost"), uploader)),
            uploader,
            HOUSE,
        ),
    ]
    moments += _refused_upload(now, others[len(others) // 2], rng)  # not the last: the seed's leaver
    return moments


def _film(
    now: datetime,
    uploader: Resident,
    index: int,
    pattern: str,
    uploaded: datetime,
    details: dict[str, Any],
    viewers: list[Resident],
    rng: random.Random,
) -> list[Moment]:
    """One film's life: uploaded, encoded by the worker, pictures and details chosen, published."""
    name = f"{details['title'].lower()}.mp4"
    published = uploaded + timedelta(minutes=12)
    made: dict[str, Video] = {}

    def upload() -> None:
        with TemporaryDirectory(prefix="gahkflix-demo-") as directory:
            data = _clip(Path(directory), pattern, index).read_bytes()
        made["video"] = _upload(uploader, name, data, rng, uploaded)

    def pictures() -> None:
        video = Video.objects.get(pk=made["video"].pk)
        frames = services.current_frames(video)
        if not frames:
            return  # the encode failed, so there is nothing to choose from
        for kind, frame in (("thumbnail", frames[0]), ("banner", frames[-1])):
            key = services.store_picture(video, kind, services.picture_from_frame(frame, kind))
            services.set_picture(video, kind, key, frame, uploader, needs_approval=False)

    def describe() -> None:
        video = Video.objects.get(pk=made["video"].pk)
        series = Series.objects.get(title=SERIES) if details["episode"] else None
        changes = {**details, "series": series.pk if series else None}
        services.edit_video(video, changes, uploader, needs_approval=False)

    def publish() -> None:
        video = Video.objects.get(pk=made["video"].pk)
        if services.missing_for_publishing(video):
            return  # it stays a draft
        with trail.event("gahkflix.video.published", actor=uploader, subject=video):  # as services.publish
            video.published_at = published
            video.save(update_fields=["published_at"])
            services.record_event(video, VideoEvent.Kind.PUBLISHED, uploader)
        for viewer in viewers:
            seen = min(published + timedelta(hours=rng.randint(1, 100)), now - timedelta(minutes=5))
            services.record_view(video, viewer, seen)

    edit = reverse("gahkflix:upload")
    return [
        Moment(uploaded, _dated(uploaded, upload), uploader, HOUSE, edit),
        _worker(uploaded + timedelta(minutes=3), lambda: _process(made["video"], rng)),
        Moment(uploaded + timedelta(minutes=7), _dated(uploaded + timedelta(minutes=7), pictures), uploader),
        Moment(uploaded + timedelta(minutes=9), _dated(uploaded + timedelta(minutes=9), describe), uploader),
        Moment(published, _dated(published, publish), uploader),
    ]


def _upload(resident: Resident, name: str, data: bytes, rng: random.Random, at: datetime) -> Video:
    """What services.commit_upload does with a file the storage has (the browser's own upload to the
    storage is not the server's to replay)."""
    with trail.event(
        "gahkflix.video.uploaded", actor=resident, details={"file": name, "size": len(data)}
    ) as event:
        video = Video.objects.create(title=services._title_from(name), uploaded_by=resident)
        video_file = VideoFile(
            video=video,
            uploaded_by=resident,
            original_name=name,
            size=len(data),
            content_type="video/mp4",
            fingerprint=f"{rng.getrandbits(256):064x}",
        )
        video_file.original.name = get_video_storage().save(
            original_key(video.uuid, video_file.uuid, name), ContentFile(data)
        )
        video_file.save()
        event.involve(video, "subject")
        services.record_event(
            video, VideoEvent.Kind.UPLOADED, resident, changes={"file": name, "size": len(data)}
        )
    # The upload's own times, as the history has them (bookkeeping the trail leaves out).
    Video.objects.filter(pk=video.pk).update(created_at=at)
    VideoFile.objects.filter(pk=video_file.pk).update(created_at=at)
    return video


def _process(video: Video, rng: random.Random) -> None:
    """The worker's job: claim the upload, then encode it (or refuse it)."""
    video_file = VideoFile.objects.get(video=video)
    VideoFile.objects.filter(pk=video_file.pk).update(state=FileState.PROCESSING)
    video_file.refresh_from_db()
    services.process_file(video_file, rng)


def _worker(at: datetime, play: Callable[[], object]) -> Moment:
    return Moment(at, _dated(at, play), None, None, source="worker")


def _create_series(by: Resident) -> Series:
    """What the series form does (gahkflix.views.series_create)."""
    series = Series(
        title=SERIES, description="Hverdagen på 3. sal, fortalt af dem, der bor der.", created_by=by
    )
    with trail.event("gahkflix.series.created", actor=by, subject=series):
        series.save()
    return series


def _bin(title: str, by: Resident, at: datetime) -> None:
    """services.bin_video, with the history's time."""
    video = Video.objects.get(title=title)
    with trail.event("gahkflix.video.binned", actor=by, subject=video):
        video.deleted_at = at
        video.deleted_by = by
        video.save(update_fields=["deleted_at", "deleted_by"])
        services.record_event(video, VideoEvent.Kind.BINNED, by)


def _refused_upload(now: datetime, resident: Resident, rng: random.Random) -> list[Moment]:
    """A phone's photo uploaded as a video: the worker refuses it, and its uploader deletes the draft."""
    at = ago(now, 6, 22, 40)
    made: dict[str, Video] = {}

    def upload() -> None:
        made["video"] = _upload(resident, "oelsmagning.mov", b"\xff\xd8\xff\xe0 no video in here", rng, at)

    return [
        Moment(at, _dated(at, upload), resident, PHONES[0], reverse("gahkflix:upload")),
        _worker(at + timedelta(minutes=1), lambda: _process(made["video"], rng)),
        Moment(
            at + timedelta(minutes=30),
            lambda: services.delete_permanently(Video.objects.get(pk=made["video"].pk)),
            resident,
            PHONES[0],
        ),
    ]


def _dated(at: datetime, play: Callable[[], object]) -> Callable[[], None]:
    """Run `play`, then date the history lines and requests it wrote to the event's time."""

    def run() -> None:
        newest_event = VideoEvent.objects.aggregate(newest=Max("pk"))["newest"] or 0
        newest_request = ChangeRequest.objects.aggregate(newest=Max("pk"))["newest"] or 0
        play()
        VideoEvent.objects.filter(pk__gt=newest_event).update(created_at=at)
        ChangeRequest.objects.filter(pk__gt=newest_request).update(created_at=at)

    return run
