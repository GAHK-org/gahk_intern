"""ffmpeg for GAHKFlix: what a file holds, its two renditions, and frames to cut pictures from.

Everything here works on local paths: the worker downloads the original first (tasks.py), because
an encode reads the whole file and ffmpeg seeks in it.

ONE PASS FOR BOTH RENDITIONS. The source is decoded once and split, so a film costs one decode and
two encodes, not two of each. `-preset veryfast` is a deliberate trade: the files come out somewhat
larger than with the default preset, but a long revy encodes in a fraction of the time on a box that
also serves the site, and size is the cheaper of the two at our scale.

NEVER UPSCALED. Full HD is only made when the source is larger than HD, and an HD rendition of a
small source keeps the source's size. Portrait phone video gets portrait boxes (1080x1920, 720x1280).
"""

import logging
import random
import re
import subprocess  # nosec B404
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryFile

from django.utils import timezone
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

from photo_album.uploads import parse_video_creation_time

logger = logging.getLogger(__name__)

FULL_HD = (1920, 1080)
HD = (1280, 720)
THUMBNAIL_SIZE = (640, 360)
BANNER_MAX_WIDTH = 1920
# Pictures whose shape is in this range (4:3 up to cinema widescreen) are cropped to 16:9: they lose
# a little at the edges. Anything narrower or wider — a phone held upright, a square, a panorama —
# would lose most of itself, so it is shown whole on a blurred, enlarged copy of itself instead.
CROP_ASPECTS = (1.3, 2.4)
FRAME_MAX_WIDTH = 1920
# A frame this flat (luminance standard deviation) is a fade, a black screen or a lens cap.
BORING_FRAME_STDDEV = 8.0


class EncodeError(Exception):
    """ffmpeg refused; the message is the end of what it printed."""


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return str(imageio_ffmpeg.get_ffmpeg_exe())


@dataclass
class Probe:
    duration: float | None
    # As displayed: a phone's rotation flag already applied.
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool
    recorded_at: datetime | None


_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_VIDEO_STREAM = re.compile(r"Stream #\d+:\d+.*?: Video: (?P<rest>.*)")
_SIZE = re.compile(r"(?<![0-9a-fA-Fx])(\d{2,5})x(\d{2,5})(?![0-9])")
_ROTATION = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees|rotate\s*:\s*(-?\d+)")
_AUDIO_STREAM = re.compile(r"Stream #\d+:\d+.*?: Audio: ")


def parse_probe(output: str) -> Probe:
    """`ffmpeg -i`'s description of a file, as the few facts GAHKFlix needs."""
    duration = None
    if match := _DURATION.search(output):
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    width = height = None
    video = _VIDEO_STREAM.search(output)
    if video and (size := _SIZE.search(video["rest"])):
        width, height = int(size[1]), int(size[2])
        rotation = _ROTATION.search(output)
        degrees = float(rotation[1] or rotation[2]) if rotation else 0.0
        if round(abs(degrees)) % 180 == 90:
            width, height = height, width
    return Probe(
        duration=duration,
        width=width,
        height=height,
        has_video=video is not None,
        has_audio=_AUDIO_STREAM.search(output) is not None,
        recorded_at=_recorded_at(output),
    )


def _recorded_at(output: str) -> datetime | None:
    """The recording time from the container, or None. Before 1971 is a camera's unset clock."""
    value = parse_video_creation_time(output)
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.year < 1971:
        return None
    return timezone.make_aware(moment) if timezone.is_naive(moment) else moment


def probe(path: Path) -> Probe:
    # `ffmpeg -i` with no output describes the input on stderr and exits non-zero, as expected.
    result = subprocess.run(  # noqa: S603  # nosec B603
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    return parse_probe(result.stderr)


@dataclass(frozen=True)
class Rendition:
    name: str  # "fullhd" or "hd", the quality the player names
    box: tuple[int, int]  # the most it may be: width, height


def plan_renditions(width: int, height: int) -> list[Rendition]:
    """Which renditions a source of this displayed size gets, best first."""
    landscape = width >= height

    def box(long_side: int, short_side: int) -> tuple[int, int]:
        return (long_side, short_side) if landscape else (short_side, long_side)

    plan = []
    if max(width, height) > HD[0] or min(width, height) > HD[1]:
        plan.append(Rendition("fullhd", box(*FULL_HD)))
    plan.append(Rendition("hd", box(*HD)))
    return plan


# Square pixels first (old DV camcorders are not), then into the box without ever growing, with the
# even dimensions yuv420p needs. Quoted: the commas in min() would otherwise split the filter chain.
def _scale(box: tuple[int, int]) -> str:
    width, height = box
    return (
        "scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1,"
        f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "format=yuv420p"
    )


def _crf(rendition: Rendition) -> str:
    return "21" if rendition.name == "fullhd" else "23"


def encode(
    source: Path,
    outputs: dict[str, Path],
    plan: list[Rendition],
    duration: float | None,
    on_progress: Callable[[float], None] = lambda fraction: None,
) -> None:
    """Encode every rendition in `plan` to `outputs[rendition.name]`, reporting progress as 0–1.

    Raises EncodeError with the tail of ffmpeg's output when it fails. The ffmpeg process is killed
    on the way out whatever happens — including a Celery soft time limit raised inside the loop
    below — so a timed-out task never leaves an encode running behind it.
    """
    labels = [f"v{index}" for index in range(len(plan))]
    if len(plan) == 1:
        graph = f"[0:v:0]{_scale(plan[0].box)}[{labels[0]}]"
    else:
        splits = "".join(f"[s{index}]" for index in range(len(plan)))
        chains = ";".join(f"[s{index}]{_scale(r.box)}[{labels[index]}]" for index, r in enumerate(plan))
        graph = f"[0:v:0]split={len(plan)}{splits};{chains}"
    arguments = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-i", str(source), "-filter_complex", graph]
    for label, rendition in zip(labels, plan, strict=True):
        arguments += [
            "-map",
            f"[{label}]",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            _crf(rendition),
            "-profile:v",
            "high",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(outputs[rendition.name]),
        ]
    arguments += ["-progress", "pipe:1", "-nostats"]
    with TemporaryFile() as log:
        process = subprocess.Popen(  # noqa: S603  # nosec B603
            arguments, stdout=subprocess.PIPE, stderr=log, text=True, errors="replace"
        )
        try:
            assert process.stdout is not None  # noqa: S101  # nosec B101 — stdout=PIPE above
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and duration and value.isdecimal():
                    on_progress(min(1.0, int(value) / 1_000_000 / duration))
            returncode = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if returncode != 0:
            log.seek(0)
            tail = log.read().decode("utf-8", "replace").strip().splitlines()[-3:]
            raise EncodeError(" / ".join(tail)[-250:] or f"ffmpeg exited with {returncode}")


def frame_times(duration: float | None, count: int, rng: random.Random) -> list[float]:
    """`count` random moments, one in each equal stretch of the film, away from its first and last 5%.

    Stratified rather than uniformly random, so ten picks never bunch up in one scene.
    """
    if not duration or duration <= 0:
        return [0.0]
    start, end = duration * 0.05, duration * 0.95
    step = (end - start) / count
    return [round(start + step * index + rng.random() * step, 3) for index in range(count)]


def extract_frame(source: Path, at: float, target: Path) -> bool:
    """One frame at `at` seconds as a JPEG at most FRAME_MAX_WIDTH wide. False if there is none."""
    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            ffmpeg_exe(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            f"{at:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            f"scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1,scale=w='min({FRAME_MAX_WIDTH},iw)':h=-2",
            "-q:v",
            "3",
            str(target),
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    return result.returncode == 0 and target.exists() and target.stat().st_size > 0


def is_boring(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            return ImageStat.Stat(image.convert("L")).stddev[0] < BORING_FRAME_STDDEV
    except OSError:
        return True


def draw_frames(
    source: Path, duration: float | None, directory: Path, count: int, rng: random.Random
) -> list[tuple[float, Path]]:
    """Up to `count` frames worth picking from: a black or flat one is redrawn in its stretch, twice."""
    frames: list[tuple[float, Path]] = []
    times = frame_times(duration, count, rng)
    step = (duration or 0) * 0.9 / max(count, 1)
    for index, at in enumerate(times):
        target = directory / f"frame-{index}.jpg"
        chosen = None
        for attempt in range(3):
            moment = at if attempt == 0 else max(0.0, at - step / 2 + rng.random() * step)
            if not extract_frame(source, moment, target):
                continue
            chosen = moment
            if not is_boring(target):
                break
        if chosen is not None:
            frames.append((chosen, target))
    return frames


def _jpeg(image: Image.Image) -> bytes:
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=84, optimize=True)
    return output.getvalue()


def _crop(image: Image.Image, max_width: int) -> Image.Image:
    """Centre-cropped to 16:9, then no wider than `max_width` — but never enlarged."""
    width = min(image.width, round(image.height * 16 / 9), max_width)
    height = round(width * 9 / 16)
    return ImageOps.fit(image, (max(width, 1), max(height, 1)), Image.Resampling.LANCZOS)


def _blur_fill(image: Image.Image, max_width: int) -> Image.Image:
    """The whole picture on a 16:9 canvas filled with a blurred, darkened copy of itself.

    The picture itself is never enlarged, so the canvas is only as tall as it is (up to the size
    asked for). The background is blurred small and then scaled up: cheaper than blurring the full
    size, and the scaling smooths it further.
    """
    height = min(round(max_width * 9 / 16), image.height)
    width = round(height * 16 / 9)
    small = ImageOps.fit(image, (max(width // 8, 1), max(height // 8, 1)), Image.Resampling.BILINEAR)
    background = small.filter(ImageFilter.GaussianBlur(4)).resize((width, height), Image.Resampling.BICUBIC)
    background = ImageEnhance.Brightness(background).enhance(0.55)
    picture = image.copy()
    picture.thumbnail((width, height), Image.Resampling.LANCZOS)
    background.paste(picture, ((width - picture.width) // 2, (height - picture.height) // 2))
    return background


def sixteen_nine(image: Image.Image, max_width: int) -> Image.Image:
    """`image` as a 16:9 picture at most `max_width` wide: cropped if it is close to that shape, shown
    whole on a blurred copy of itself if it is not (CROP_ASPECTS)."""
    image = image.convert("RGB")
    aspect = image.width / max(image.height, 1)
    if CROP_ASPECTS[0] <= aspect <= CROP_ASPECTS[1]:
        return _crop(image, max_width)
    return _blur_fill(image, max_width)


def thumbnail_jpeg(image_file: object) -> bytes:
    """A 16:9 card picture from any image (a frame, or a resident's own upload)."""
    with Image.open(image_file) as source:  # type: ignore[arg-type]
        return _jpeg(sixteen_nine(ImageOps.exif_transpose(source), THUMBNAIL_SIZE[0]))


def banner_jpeg(image_file: object) -> bytes:
    """A 16:9 hero picture, up to Full HD wide."""
    with Image.open(image_file) as source:  # type: ignore[arg-type]
        return _jpeg(sixteen_nine(ImageOps.exif_transpose(source), BANNER_MAX_WIDTH))


class Throttle:
    """Calls `callback` at most every `seconds`, plus whenever `force` is passed."""

    def __init__(self, callback: Callable[[float], None], seconds: float = 5.0) -> None:
        self._callback = callback
        self._seconds = seconds
        self._last = 0.0

    def __call__(self, fraction: float, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last >= self._seconds:
            self._last = now
            self._callback(fraction)
