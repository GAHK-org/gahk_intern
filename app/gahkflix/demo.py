"""A small GAHKFlix for the local demo: a series with two episodes and two films, really encoded.

The clips are ffmpeg's own test patterns, mostly short and small, so the whole thing takes a few
seconds — but they go through the real pipeline (services.process_file), so the rows, renditions,
frames and pictures are exactly what an upload makes, and the player plays them.

Beside them, the premieres and drafts (_seed_premieres, _seed_drafts): two upcoming premieres for
`Kommer snart`, one that came out a few hours ago, and beboer@gahk.dk's drafts in every state the
upload page and `Mine videoer` show.
"""

import random
import subprocess  # nosec B404
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.base import ContentFile
from django.utils import timezone

from residents.models import Account

from . import encoding, services
from .dating import premiere_value
from .models import FileState, PremiereBlur, Series, Video, VideoEvent, VideoFile, original_key
from .storage import get_video_storage

# (title, description, pattern, episode or None, days ago, tags)
FILMS = [
    ("Pilot", "Hvem har taget ostehapsen fra fælleskøleskabet?", "testsrc2", 1, 20, ["sketch"]),
    ("Opvasken", "Turnusordningen bryder sammen.", "smptehdbars", 2, 12, ["sketch"]),
    (
        "Revy",
        "Årets revy: sange, sketches og en finale, ingen kommer sig over.",
        "testsrc",  # a running clock: switching quality or speed visibly keeps the place
        None,
        45,
        ["revy", "fest"],
    ),
    ("Julefrokost", "Fra sildemadder til sidste dans.", "life", None, 3, ["fest", "jul"]),
]


# What the player needs more of than three seconds at 640x360: (size, seconds). The episodes run long
# enough for `Næste afsnit` (the last 20 seconds), the controls to fade and a 10-second jump; Revy is
# Full HD, so the quality menu has two choices, and long enough to resume in (see seed); Julefrokost was
# filmed upright, so the player shows it whole between black bars.
SHAPES = {
    "Pilot": ("640x360", 25),
    "Opvasken": ("640x360", 25),
    "Revy": ("1920x1080", 20),
    "Julefrokost": ("360x640", 3),
}

# Text subtitle tracks in the uploaded file, which the real processing extracts as WebVTT:
# (language, title, default, forced, lines spread over the clip). The episodes both have Danish and
# English, so a language chosen in one carries over to the next. Revy's menu names a titled track
# (`Engelsk (Kommentarer)`), and its forced track is not in the menu but shows when `Fra` is chosen.
# Julefrokost has none, so the player has no subtitles button.
SUBTITLES = {
    "Pilot": [
        ("dan", "", True, False, ["Hvem tog min ostehaps?", "Den stod lige her.", "Det var ikke mig."]),
        ("eng", "", False, False, ["Who took my cheese?", "It was right here.", "It wasn't me."]),
    ],
    "Opvasken": [
        ("dan", "", True, False, ["Hvis tur er det?", "Det står på tavlen.", "Tavlen er væk."]),
        ("eng", "", False, False, ["Whose turn is it?", "It's on the board.", "The board is gone."]),
    ],
    "Revy": [
        ("dan", "", True, False, ["♪ Vi bor på GAHK ♪", "♪ og vi går aldrig hjem ♪", "Tak for i aften!"]),
        ("eng", "Kommentarer", False, False, ["This number took three weeks.", "Nobody knew the words."]),
        ("dan", "Skilte", False, True, ["SKILT: Ølkælderen", "SKILT: Udgang"]),
    ],
}


def _srt(lines: list[str], seconds: float) -> str:
    """The lines as SubRip cues, one after another over the whole clip."""
    step = seconds / len(lines)

    def stamp(at: float) -> str:
        millis = round(at * 1000)
        return f"00:{millis // 60000:02d}:{millis // 1000 % 60:02d},{millis % 1000:03d}"

    return "\n".join(
        f"{n}\n{stamp(n * step - step)} --> {stamp(n * step - 0.2)}\n{line}\n"
        for n, line in enumerate(lines, 1)
    )


def _with_subtitles(clip: Path, tracks: list[tuple[str, str, bool, bool, list[str]]], seconds: float) -> Path:
    """The clip with the subtitle tracks muxed in, as an MKV: where films with subtitles usually come from."""
    target = clip.with_suffix(".mkv")
    inputs, options = ["-i", str(clip)], ["-map", "0"]
    for n, (language, title, default, forced, lines) in enumerate(tracks):
        srt = clip.with_name(f"{clip.stem}-{n}.srt")
        srt.write_text(_srt(lines, seconds), encoding="utf-8")
        inputs += ["-i", str(srt)]
        options += ["-map", f"{n + 1}", f"-metadata:s:s:{n}", f"language={language}"]
        options += [f"-metadata:s:s:{n}", f"title={title}"] if title else []
        flags = "default" if default else "forced" if forced else "0"
        options += [f"-disposition:s:{n}", flags]
    command = [encoding.ffmpeg_exe(), "-v", "error", "-y", *inputs, *options, "-c", "copy", "-c:s", "srt"]
    subprocess.run(  # noqa: S603  # nosec B603
        [*command, str(target)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return target


def _clip(directory: Path, pattern: str, index: int, size: str = "640x360", seconds: float = 3) -> Path:
    target = directory / f"demo-{index}.mp4"
    subprocess.run(  # noqa: S603  # nosec B603
        [
            encoding.ffmpeg_exe(), "-v", "error", "-y",
            "-f", "lavfi", "-i", f"{pattern}=size={size}:rate=25",
            "-f", "lavfi", "-i", f"sine=frequency={330 + 110 * index}",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(target),
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


def seed(residents: list[Account], now: datetime, rng: random.Random) -> None:
    uploader, *others = residents
    series = Series.objects.create(
        title="Gangen", description="Hverdagen på 3. sal, fortalt af dem, der bor der.", created_by=uploader
    )
    with TemporaryDirectory(prefix="gahkflix-demo-") as directory:
        for index, (title, description, pattern, episode, days, tags) in enumerate(FILMS):
            size, seconds = SHAPES.get(title, ("640x360", 3))
            path = _clip(Path(directory), pattern, index, size, seconds)
            if title in SUBTITLES:
                path = _with_subtitles(path, SUBTITLES[title], seconds)
            video = Video.objects.create(
                title=title,
                description=description,
                uploaded_by=uploader,
                series=series if episode else None,
                season=1 if episode else None,
                episode=episode,
            )
            video_file = VideoFile(
                video=video,
                uploaded_by=uploader,
                original_name=path.name,
                size=path.stat().st_size,
                fingerprint=f"{rng.getrandbits(256):064x}",
                state=FileState.PROCESSING,
            )
            video_file.original.name = get_video_storage().save(
                original_key(video.uuid, video_file.uuid, path.name), ContentFile(path.read_bytes())
            )
            video_file.save()
            if not services.process_file(video_file, rng):
                continue
            video.refresh_from_db()
            if title == "Revy":
                # The logins (seed_demo's first residents) stopped 12 seconds in: `Fortsæt med at se`
                # has a card with a red bar, and the player starts there.
                for viewer in residents[:6]:
                    services.save_position(video, viewer, 12.0, now - timedelta(hours=2))
            frames = services.current_frames(video)
            for kind, frame in (("thumbnail", frames[0]), ("banner", frames[-1])):
                key = services.store_picture(video, kind, services.picture_from_frame(frame, kind))
                services.set_picture(video, kind, key, frame, uploader, needs_approval=False)
            cast = [
                {"resident": r.pk, "name": "", "role": ""} for r in rng.sample(others, min(3, len(others)))
            ]
            moment = now - timedelta(days=days)
            released_on = _released_on(title, moment)
            services.apply_changes(video, {"tags": tags, "cast": cast, "released_on": released_on}, uploader)
            video.refresh_from_db()
            services.publish(video, uploader)
            Video.objects.filter(pk=video.pk).update(created_at=moment, published_at=moment)
            for viewer in rng.sample(residents, min(len(residents), 2 + index * 2)):
                services.record_view(video, viewer, now - timedelta(hours=rng.randint(1, 100)))

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


def _upload(path: Path, title: str, uploader: Account, rng: random.Random, state: str) -> VideoFile:
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


def _processed(path: Path, title: str, uploader: Account, rng: random.Random) -> Video:
    """A draft that has been through the real encode, with its thumbnail and banner picked."""
    video_file = _upload(path, title, uploader, rng, FileState.PROCESSING)
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
    uploader: Account,
    beboer: Account,
    others: list[Account],
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


def _seed_drafts(directory: Path, beboer: Account, now: datetime, rng: random.Random) -> None:
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
    rehearsal = _upload(_clip(directory, "testsrc2", 16), "Revyprøve", beboer, rng, FileState.PROCESSING)
    VideoFile.objects.filter(pk=rehearsal.pk).update(progress=40, attempts=1, heartbeat_at=now)
    _upload(_clip(directory, "smptehdbars", 17), "Gangmøde", beboer, rng, FileState.QUEUED)

    # A file with no picture in it, refused by the real check: `Mislykkedes`, with why.
    memo = _upload(
        _sound_only(directory, "Lydoptagelse.mp4"), "Lydoptagelse", beboer, rng, FileState.PROCESSING
    )
    services.process_file(memo, rng)
