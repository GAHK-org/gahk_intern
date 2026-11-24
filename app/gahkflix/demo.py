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

After the history, the premieres and drafts (seed: _seed_premieres, _seed_drafts): two upcoming
premieres for `Kommer snart`, one that came out a few hours ago, and beboer@gahk.dk's drafts in
every state the upload page and `Mine videoer` show. They are seeded like the rest of the data,
without a history of their own in the trail.
"""

import random
import subprocess  # nosec B404
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from django.core.files.base import ContentFile
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from audit import trail
from audit.demo import HOUSE, PHONES, Moment, ago
from residents.models import Resident

from . import encoding, services
from .dating import premiere_value
from .models import (
    ChangeRequest,
    FileState,
    PremiereBlur,
    Series,
    Video,
    VideoEvent,
    VideoFile,
    original_key,
)
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


def _released_on(title: str, moment: datetime) -> str:
    """The release date as typed on the edit page. Not every film's day is known: an old revy found on
    a disk is known by its year, last year's julefrokost by its month — so the pages show all three
    kinds of date, and the timeline its `Uden måned`."""
    day = timezone.localdate(moment)
    if title == "Revy":
        return str(day.year - 7)
    if title == "Julefrokost":
        return f"{day.year - 1}-12"
    return day.isoformat()


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
        details["released_on"] = _released_on(title, uploaded)
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
            video.published_at = video.announced_at = published  # out at once: no premiere
            video.save(update_fields=["published_at", "announced_at"])
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


def seed(residents: list[Resident], now: datetime) -> None:
    """The premieres and drafts, after the history (whose films and series they join)."""
    if len(residents) < 2:
        return
    uploader, *others = residents
    series = Series.objects.get(title=SERIES)
    # A random source of their own, so the seeders after this one draw the same numbers as before.
    extra = random.Random(423)  # noqa: S311  # nosec B311 - demo data, not security-sensitive
    beboer = next((r for r in residents if r.email == "beboer@gahk.dk"), residents[-1])
    with TemporaryDirectory(prefix="gahkflix-demo-") as directory:
        _seed_premieres(Path(directory), series, uploader, beboer, others, now, extra)
        _seed_drafts(Path(directory), beboer, now, extra)


def _sound_only(directory: Path, name: str) -> Path:
    """An MP4 with sound and no picture: a voice memo picked by mistake."""
    target = directory / name
    subprocess.run(  # noqa: S603  # nosec B603
        [
            encoding.ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=262",
            "-t", "3", "-c:a", "aac", "-f", "mp4", str(target),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    return target


def _draft_upload(path: Path, title: str, uploader: Resident, rng: random.Random, state: str) -> VideoFile:
    """A new draft with its file in storage, as a committed upload leaves it — but not sent to the
    worker: the demo decides which state each draft stays in."""
    video = Video.objects.create(title=title, uploaded_by=uploader)
    video_file = VideoFile(
        video=video,
        uploaded_by=uploader,
        original_name=path.name,
        size=path.stat().st_size,
        fingerprint=f"{rng.getrandbits(256):064x}",
        state=state,
    )
    video_file.original.name = get_video_storage().save(
        original_key(video.uuid, video_file.uuid, path.name), ContentFile(path.read_bytes())
    )
    video_file.save()
    services.record_event(video, VideoEvent.Kind.UPLOADED, uploader, changes={"file": path.name})
    return video_file


def _processed(path: Path, title: str, uploader: Resident, rng: random.Random) -> Video:
    """A draft that has been through the real encode, with its thumbnail and banner picked."""
    video_file = _draft_upload(path, title, uploader, rng, FileState.PROCESSING)
    services.process_file(video_file, rng)
    video = Video.objects.get(pk=video_file.video_id)
    frames = services.current_frames(video)
    for kind, frame in (("thumbnail", frames[0]), ("banner", frames[-1])):
        key = services.store_picture(video, kind, services.picture_from_frame(frame, kind))
        services.set_picture(video, kind, key, frame, uploader, needs_approval=False)
    video.refresh_from_db()
    return video


def _backdate(video: Video, uploaded: datetime, **fields: datetime) -> None:
    """Move a video's upload (and the history's lines about it) into the past."""
    Video.objects.filter(pk=video.pk).update(created_at=uploaded, **fields)
    upload_kinds = (VideoEvent.Kind.UPLOADED, VideoEvent.Kind.PROCESSED)
    video.events.filter(kind__in=upload_kinds).update(created_at=uploaded)


def _at(day: date, hour: int) -> datetime:
    """A premiere on the hour, local time, as the edit page's fields make it."""
    return timezone.make_aware(datetime.combine(day, time(hour)))


def _premiere(moment: datetime, blur: str) -> dict[str, str]:
    """The edit form's premiere fields: the moment, its day as the release date, and the blur."""
    return {
        "released_on": timezone.localdate(moment).isoformat(),
        "premiere_at": premiere_value(moment) or "",
        "premiere_blur": blur,
    }


def _seed_premieres(
    directory: Path,
    series: Series,
    uploader: Resident,
    beboer: Resident,
    others: list[Resident],
    now: datetime,
    rng: random.Random,
) -> None:
    today = timezone.localdate(now)

    def cast() -> list[dict[str, object]]:
        return [{"resident": r.pk, "name": "", "role": ""} for r in rng.sample(others, min(2, len(others)))]

    # Two upcoming premieres, so `Kommer snart` has a row with an order (soonest first). The first is
    # the series' next episode: the series page and the timeline show its card too, which cannot be
    # opened, with its thumbnail lightly blurred.
    episode = _processed(_clip(directory, "rgbtestsrc", 10), "Fællesspisning", uploader, rng)
    premiere = _at(today + timedelta(days=2), 20)
    changes = {"series": series.pk, "season": 1, "episode": 3, "tags": ["sketch"], "cast": cast()}
    services.apply_changes(episode, {**changes, **_premiere(premiere, PremiereBlur.LIGHT)}, uploader)
    # The second is beboer@'s own, heavily blurred: beboer@ and the administrators can open its page
    # before the premiere, ak@ and oel@ only see the card.
    film = _processed(_clip(directory, "pal100bars", 11), "Vinterbadning", beboer, rng)
    premiere = _at(today + timedelta(days=12), 21)
    changes = {"description": "Første dukkert i havnen.", "tags": ["sketch"], "cast": cast()}
    services.apply_changes(film, {**changes, **_premiere(premiere, PremiereBlur.HEAVY)}, beboer)
    for video in (episode, film):
        video.refresh_from_db()
        services.publish(video, video.uploaded_by)  # upcoming: announced at the premiere
        _backdate(video, now - timedelta(days=3), published_at=now - timedelta(days=1))
        video.events.filter(kind=VideoEvent.Kind.PUBLISHED).update(created_at=now - timedelta(days=1))

    # One that came out three hours ago: published two days before its premiere and announced at it.
    # Its page shows the date without the time, its history `Havde premiere`, and for a day yet its
    # uploader (beboer@) may still make it a draft again.
    concert = _processed(_clip(directory, "cellauto", 12), "Kælderkoncert", beboer, rng)
    premiere = (now - timedelta(hours=3)).replace(second=0, microsecond=0)
    changes = {"description": "Husbandet spiller op i ølkælderen.", "tags": ["fest", "musik"], "cast": cast()}
    services.apply_changes(concert, {**changes, **_premiere(premiere, PremiereBlur.MEDIUM)}, beboer)
    concert.refresh_from_db()
    services.publish(concert, beboer)  # out at once, as its premiere has passed: announced now
    published = premiere - timedelta(days=2)
    _backdate(concert, published - timedelta(hours=1), published_at=published, announced_at=premiere)
    concert.events.filter(kind=VideoEvent.Kind.PUBLISHED).update(created_at=published)
    services.record_event(concert, VideoEvent.Kind.PREMIERED)
    concert.events.filter(kind=VideoEvent.Kind.PREMIERED).update(created_at=premiere)
    for viewer in rng.sample(others, min(len(others), 4)):
        services.record_view(concert, viewer, premiere + timedelta(minutes=rng.randint(5, 150)))


def _seed_drafts(directory: Path, beboer: Resident, now: datetime, rng: random.Random) -> None:
    """beboer@'s drafts, one of each kind `Mine videoer` and the upload page list: six of the ten
    allowed, so there is still room to try uploading several files at once."""
    today = timezone.localdate(now)

    # Ready but without a release date: publishing asks for it, and `Brug videoens dato` offers the
    # one in its file name. Ten days old, so the Monday reminder names it.
    name = f"Sommerfest {today.year - 1}-06-21.mp4"
    summer = _processed(_clip(directory, "smptebars", 13).rename(directory / name), "Sommerfest", beboer, rng)
    services.apply_changes(summer, {"description": "Grill, vandkamp og lange borde i gården."}, beboer)
    _backdate(summer, now - timedelta(days=10))

    # Finished, with a premiere ahead, but not published: the daily warning that there will be no
    # premiere unless it is, and the edit page's preview of the blurred thumbnail.
    party = _processed(_clip(directory, "yuvtestsrc", 14), "Gangfest", beboer, rng)
    premiere = _at(today + timedelta(days=5), 22)
    services.apply_changes(party, {"tags": ["fest"], **_premiere(premiere, PremiereBlur.MEDIUM)}, beboer)

    # Published by mistake and taken back at once: a draft again, whose 90 days run from now rather
    # than from its upload 80 days ago, and whose history says so.
    trip = _processed(_clip(directory, "testsrc", 15), "Rusturen", beboer, rng)
    services.apply_changes(trip, {"released_on": f"{today.year}-08"}, beboer)
    trip.refresh_from_db()
    services.publish(trip, beboer)
    services.unpublish(trip, beboer)
    _backdate(trip, now - timedelta(days=80))

    # One being encoded and one waiting behind it (`I kø`, one ahead). Their originals are real, so a
    # local worker that picks them up once their heartbeat is old (tasks.recover_stalled) finishes them.
    rehearsal = _draft_upload(
        _clip(directory, "testsrc2", 16), "Revyprøve", beboer, rng, FileState.PROCESSING
    )
    VideoFile.objects.filter(pk=rehearsal.pk).update(progress=40, attempts=1, heartbeat_at=now)
    _draft_upload(_clip(directory, "smptehdbars", 17), "Gangmøde", beboer, rng, FileState.QUEUED)

    # A file with no picture in it, refused by the real check: `Mislykkedes`, with why.
    memo = _draft_upload(
        _sound_only(directory, "Lydoptagelse.mp4"), "Lydoptagelse", beboer, rng, FileState.PROCESSING
    )
    services.process_file(memo, rng)
