"""ffmpeg for GAHKFlix: what a file holds, its two renditions, and frames to cut pictures from.

Everything here works on local paths: the worker downloads the original first (tasks.py), because
an encode reads the whole file and ffmpeg seeks in it.

ONE PASS FOR BOTH RENDITIONS. The source is decoded once and split, so a film costs one decode and
two encodes, not two of each. `-preset veryfast` is a deliberate trade: the files come out somewhat
larger than with the default preset, but a long revy encodes in a fraction of the time on a box that
also serves the site, and size is the cheaper of the two at our scale.

NEVER UPSCALED. Full HD is only made when the source is larger than HD, and an HD rendition of a
small source keeps the source's size. Portrait phone video gets portrait boxes (1080x1920, 720x1280).

ONE SOUND TRACK, the one with the most channels (Probe.audio_stream), and the text subtitles as WebVTT
files beside the renditions (extract_subtitles), for the player to offer.
"""

import logging
import random
import re
import subprocess  # nosec B404
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
    """ffmpeg failed; the message says why: the end of what it printed, or the signal that stopped it.

    `killed` means a signal stopped it, most often the out-of-memory killer. That says nothing about
    the file, so the file is tried again (tasks.py) instead of refused as broken.
    """

    def __init__(self, message: str, *, killed: bool = False) -> None:
        super().__init__(message)
        self.killed = killed

    @classmethod
    def from_exit(cls, returncode: int, output: str) -> "EncodeError":
        if returncode < 0:
            signal_number = -returncode
            hint = " — most likely the out-of-memory killer" if signal_number == 9 else ""
            return cls(f"ffmpeg was stopped by signal {signal_number}{hint}", killed=True)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return cls(" / ".join(lines[-3:])[-250:] or f"ffmpeg exited with {returncode}")


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return str(imageio_ffmpeg.get_ffmpeg_exe())


@dataclass(frozen=True)
class AudioStream:
    index: int  # the stream's number in the file, as `-map 0:<index>` takes it
    channels: int  # 0 for a track ffmpeg cannot make out, like an old camera's broken AC3
    default: bool = False


# Subtitle formats that are text, so can become WebVTT. Picture-based ones (DVD, Blu-ray, DVB)
# cannot; only the original keeps those.
TEXT_SUBTITLE_CODECS = frozenset(
    {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "microdvd", "sami", "subviewer", "mpl2"}
)


@dataclass(frozen=True)
class SubtitleStream:
    index: int
    codec: str
    language: str = ""  # as the file names it ("dan", "eng"); "" when it does not say
    title: str = ""
    default: bool = False
    forced: bool = False

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_SUBTITLE_CODECS


@dataclass
class Probe:
    duration: float | None
    # As displayed: a phone's rotation flag already applied.
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool
    recorded_at: datetime | None
    audio: list[AudioStream] = field(default_factory=list)
    subtitles: list[SubtitleStream] = field(default_factory=list)
    # The tags that say when the film is from (dating.DATE_TAGS), by lower-cased name, as written.
    date_tags: dict[str, str] = field(default_factory=dict)

    @property
    def audio_stream(self) -> int | None:
        """The sound to keep, as ffmpeg itself would pick it: the track with the most channels.

        Not simply the first. An old camera's MPEG can have a broken AC3 track ("0 channels") before
        its real sound, and encoding that one fails the whole video; a film can have a mono
        commentary track before its own sound. None when no track has any channels: the video is
        then kept without sound rather than refused.
        """
        usable = [stream for stream in self.audio if stream.channels > 0]
        if not usable:
            return None
        return max(usable, key=lambda stream: (stream.channels, stream.default, -stream.index)).index


_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_VIDEO_STREAM = re.compile(r"Stream #\d+:\d+.*?: Video: (?P<rest>.*)")
_SIZE = re.compile(r"(?<![0-9a-fA-Fx])(\d{2,5})x(\d{2,5})(?![0-9])")
_ROTATION = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees|rotate\s*:\s*(-?\d+)")
# "  Stream #0:2[0x3](dan): Subtitle: mov_text (tx3g / 0x67337874), 0 kb/s (default)"
_STREAM = re.compile(
    r"^\s*Stream #\d+:(?P<index>\d+)(?:\[\w+\])?(?:\((?P<language>[^)]*)\))?: "
    r"(?P<kind>\w+): (?P<codec>\w+)(?P<rest>.*)$",
    re.MULTILINE,
)
_TITLE = re.compile(r"^\s+title\s*:\s*(?P<title>.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_DATE_TAG = re.compile(
    r"^\s+(?P<key>date_released|date_recorded|date|year)\s*:\s*(?P<value>.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_LAYOUT_CHANNELS = {"mono": 1, "stereo": 2, "downmix": 2, "quad": 4, "hexagonal": 6, "octagonal": 8}


def _channels(details: str) -> int:
    """The channel count in an audio stream's line: "stereo", "5.1(side)", "0 channels". A layout
    named some other way still counts as sound, with one channel."""
    for part in details.split(","):
        layout = part.split("(")[0].strip()
        if match := re.fullmatch(r"(\d+) channels", layout):
            return int(match[1])
        if layout in _LAYOUT_CHANNELS:
            return _LAYOUT_CHANNELS[layout]
        if match := re.fullmatch(r"(\d+)\.(\d+)", layout):
            return int(match[1]) + int(match[2])
    return 1


def _streams(output: str) -> tuple[list[AudioStream], list[SubtitleStream]]:
    """The audio and subtitle streams `ffmpeg -i` lists, each with what its own block says."""
    matches = list(_STREAM.finditer(output))
    audio, subtitles = [], []
    for position, match in enumerate(matches):
        default = "(default)" in match["rest"]
        if match["kind"] == "Audio":
            audio.append(AudioStream(int(match["index"]), _channels(match["rest"]), default))
        elif match["kind"] == "Subtitle":
            end = matches[position + 1].start() if position + 1 < len(matches) else len(output)
            title = _TITLE.search(output, match.end(), end)
            language = match["language"] or ""
            subtitles.append(
                SubtitleStream(
                    index=int(match["index"]),
                    codec=match["codec"],
                    language="" if language == "und" else language,
                    title=title["title"] if title else "",
                    default=default,
                    forced="(forced)" in match["rest"],
                )
            )
    return audio, subtitles


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
    audio, subtitles = _streams(output)
    return Probe(
        duration=duration,
        width=width,
        height=height,
        has_video=video is not None,
        has_audio=bool(audio),
        recorded_at=_recorded_at(output),
        audio=audio,
        subtitles=subtitles,
        date_tags=_date_tags(output),
    )


def _date_tags(output: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for match in _DATE_TAG.finditer(output):
        tags.setdefault(match["key"].lower(), match["value"])
    return tags


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
    *,
    audio: int | None,
) -> None:
    """Encode every rendition in `plan` to `outputs[rendition.name]`, reporting progress as 0–1.

    `audio` is the one sound stream to keep (Probe.audio_stream), None for none. Subtitles are left
    out here; extract_subtitles keeps the text ones.

    Raises EncodeError with why ffmpeg failed. The ffmpeg process is killed
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
    # Errors only, so what is left in the log when it fails is the reason, not the stream listing.
    arguments = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(source)]
    arguments += ["-filter_complex", graph]
    for label, rendition in zip(labels, plan, strict=True):
        arguments += [
            "-map",
            f"[{label}]",
            *(["-map", f"0:{audio}"] if audio is not None else []),
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
            raise EncodeError.from_exit(returncode, log.read().decode("utf-8", "replace"))


# Reading a film through for one subtitle track takes seconds to a minute from local disk. Kept well
# under the heartbeat's 15 minutes, since a track's conversion reports no progress of its own.
SUBTITLE_TIMEOUT = 5 * 60


def extract_subtitles(
    source: Path,
    streams: list[SubtitleStream],
    directory: Path,
    on_progress: Callable[[float], None] = lambda fraction: None,
) -> list[tuple[SubtitleStream, Path]]:
    """Each text subtitle track of `source` as a WebVTT file, the format a browser's player reads.

    A conversion of text, not an encode, so it is quick whatever the film's length. A track ffmpeg
    cannot convert is left out and logged; the original keeps every track either way, picture
    subtitles included.
    """
    text = [stream for stream in streams if stream.is_text]
    if pictures := sorted({stream.codec for stream in streams if not stream.is_text}):
        logger.info(
            "%s has picture subtitles (%s), which only the original keeps.", source.name, ", ".join(pictures)
        )
    extracted = []
    for done, stream in enumerate(text, start=1):
        target = directory / f"subtitles-{stream.index}.vtt"
        command = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(source)]
        command += ["-map", f"0:{stream.index}", "-c:s", "webvtt", str(target)]
        try:
            result = subprocess.run(  # noqa: S603  # nosec B603
                command,
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=SUBTITLE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Gave up converting subtitle stream %d (%s) after %d s",
                stream.index,
                stream.codec,
                SUBTITLE_TIMEOUT,
            )
            continue
        if result.returncode == 0 and target.is_file() and target.stat().st_size:
            extracted.append((stream, target))
        else:
            reason = EncodeError.from_exit(result.returncode, result.stderr)
            logger.warning(
                "Could not convert subtitle stream %d (%s): %s", stream.index, stream.codec, reason
            )
        on_progress(done / len(text))
    return extracted


def frame_times(duration: float | None, count: int, rng: random.Random) -> list[float]:
    """`count` random moments, one in each equal stretch of the film, away from its first and last 5%.

    Stratified rather than uniformly random, so ten picks never bunch up in one scene.
    """
    if not duration or duration <= 0:
        return [0.0]
    start, end = duration * 0.05, duration * 0.95
    step = (end - start) / count
    return [round(start + step * index + rng.random() * step, 3) for index in range(count)]


def extract_frame(source: Path | str, at: float, target: Path) -> bool:
    """One frame at `at` seconds as a JPEG at most FRAME_MAX_WIDTH wide. False if there is none.

    `source` may be a URL (storage.loopback_url) as well as a path: ffmpeg then seeks in it with range
    requests and reads only what it needs, so a frame from a long film costs no download of it.
    """
    network = str(source).startswith(("http://", "https://"))
    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            ffmpeg_exe(),
            "-hide_banner",
            "-nostdin",
            "-y",
            *(["-rw_timeout", "30000000"] if network else []),
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
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        # What ffmpeg said, so a failure can be told apart from a moment with no frame in it.
        logger.warning("ffmpeg could not cut a frame at %.3f s: %s", at, result.stderr.strip()[-300:])
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
