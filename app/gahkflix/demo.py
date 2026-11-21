"""A small GAHKFlix for the local demo: a series with two episodes and two films, really encoded.

The clips are ffmpeg's own test patterns, mostly short and small, so the whole thing takes a few
seconds — but they go through the real pipeline (services.process_file), so the rows, renditions,
frames and pictures are exactly what an upload makes, and the player plays them.
"""

import random
import subprocess  # nosec B404
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.base import ContentFile

from residents.models import Resident

from . import encoding, services
from .models import FileState, Series, Video, VideoFile, original_key
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


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> None:
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
            services.apply_changes(video, {"tags": tags, "cast": cast}, uploader)
            video.refresh_from_db()
            services.publish(video, uploader)
            moment = now - timedelta(days=days)
            Video.objects.filter(pk=video.pk).update(created_at=moment, published_at=moment)
            for viewer in rng.sample(residents, min(len(residents), 2 + index * 2)):
                services.record_view(video, viewer, now - timedelta(hours=rng.randint(1, 100)))
