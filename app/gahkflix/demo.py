"""A small GAHKFlix for the local demo: a series with two episodes and two films, really encoded.

The clips are a few seconds of ffmpeg's own test patterns at 640x360, so the whole thing takes a few
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


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> None:
    uploader, *others = residents
    series = Series.objects.create(
        title="Gangen", description="Hverdagen på 3. sal, fortalt af dem, der bor der.", created_by=uploader
    )
    with TemporaryDirectory(prefix="gahkflix-demo-") as directory:
        for index, (title, description, pattern, episode, days, tags) in enumerate(FILMS):
            path = _clip(Path(directory), pattern, index)
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
            frames = services.current_frames(video)
            for kind, frame in (("thumbnail", frames[0]), ("banner", frames[-1])):
                key = services.store_picture(video, kind, services.picture_from_frame(frame, kind))
                services.set_picture(video, kind, key, frame, uploader, needs_approval=False)
            cast = [
                {"resident": r.pk, "name": "", "role": ""} for r in rng.sample(others, min(3, len(others)))
            ]
            services.apply_changes(video, {"tags": tags, "cast": cast}, uploader)
            video.refresh_from_db()
            services.publish(video)
            moment = now - timedelta(days=days)
            Video.objects.filter(pk=video.pk).update(created_at=moment, published_at=moment)
            for viewer in rng.sample(residents, min(len(residents), 2 + index * 2)):
                services.record_view(video, viewer, now - timedelta(hours=rng.randint(1, 100)))
