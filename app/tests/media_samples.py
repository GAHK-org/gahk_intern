"""Real, decodable media for the photo album tests.

The album now opens every upload to check it is what its name says (uploads.check_media_content),
so the old `b"bytes"` stand-ins are refused. These are tiny but genuine files.
"""

import functools
import itertools
import subprocess
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

import imageio_ffmpeg
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

_colours = itertools.count(1)


def jpeg_bytes(colour: tuple[int, int, int] | None = None, size: tuple[int, int] = (64, 48)) -> bytes:
    """A JPEG. Each call without a colour gets a different one, so two calls never hash the same."""
    if colour is None:
        n = next(_colours)
        colour = (n * 37 % 256, n * 91 % 256, n * 151 % 256)
    output = BytesIO()
    Image.new("RGB", size, colour).save(output, format="JPEG")
    return output.getvalue()


def png_bytes() -> bytes:
    output = BytesIO()
    n = next(_colours)
    Image.new("RGB", (32, 32), (n % 256, 0, 0)).save(output, format="PNG")
    return output.getvalue()


def heic_bytes(size: tuple[int, int] = (64, 48), colour: str = "red") -> bytes:
    """A HEIC, as an iPhone saves by default. Only Safari can show one, so core.uploads converts it."""
    from pillow_heif import from_pillow

    output = BytesIO()
    from_pillow(Image.new("RGB", size, colour)).save(output)
    return output.getvalue()


@functools.cache
def mp4_bytes() -> bytes:
    """A one-second H.264 clip, generated once per test session (~0.5 s)."""
    with TemporaryDirectory() as directory:
        target = Path(directory) / "clip.mp4"
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=320x240:rate=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-metadata",
                "creation_time=2021-06-05T14:30:00.000000Z",
                str(target),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return target.read_bytes()


def image_upload(name: str = "photo.jpg", content: bytes | None = None) -> SimpleUploadedFile:
    return SimpleUploadedFile(
        name, content if content is not None else jpeg_bytes(), content_type="image/jpeg"
    )


def video_upload(name: str = "clip.mp4") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, mp4_bytes(), content_type="video/mp4")


def _ffmpeg_output(suffix: str, *arguments: str, inputs: dict[str, bytes] | None = None) -> bytes:
    """What ffmpeg writes for `arguments` — inputs named in `inputs` are written beside it first."""
    with TemporaryDirectory() as directory:
        for name, content in (inputs or {}).items():
            (Path(directory) / name).write_bytes(content)
        target = Path(directory) / f"out{suffix}"
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", *arguments, str(target)],
            check=True,
            cwd=directory,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return target.read_bytes()


def sized_mp4_bytes(width: int, height: int) -> bytes:
    """A one-second H.264 clip of the given size."""
    return _ffmpeg_output(
        ".mp4",
        *("-f", "lavfi", "-i", f"testsrc=duration=1:size={width}x{height}:rate=5"),
        *("-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"),
    )


def broken_audio_mpg_bytes() -> bytes:
    """An MPEG like an old camera's that would not convert: beside its real (MP2) sound, an AC3 track
    so damaged that ffmpeg makes it out as "0 channels". Its sync words are wiped to get there."""
    mpeg = _ffmpeg_output(
        ".mpg",
        *("-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25"),
        *("-f", "lavfi", "-i", "sine=duration=1"),
        *("-map", "0", "-map", "1", "-map", "1", "-c:v", "mpeg1video", "-c:a:0", "ac3", "-c:a:1", "mp2"),
        *("-f", "mpeg"),
    )
    return mpeg.replace(b"\x0b\x77", b"\x00\x00")


def subtitled_mkv_bytes() -> bytes:
    """A one-second Matroska clip with two text subtitle tracks, Danish (the default) and English."""
    cue = "1\n00:00:00,000 --> 00:00:01,000\n{}\n"
    return _ffmpeg_output(
        ".mkv",
        *("-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10"),
        *("-i", "da.srt", "-i", "en.srt", "-map", "0", "-map", "1", "-map", "2"),
        *(
            "-c:v",
            "mpeg4",
            "-c:s",
            "srt",
            "-metadata:s:s:0",
            "language=dan",
            "-metadata:s:s:1",
            "language=eng",
        ),
        inputs={"da.srt": cue.format("Hej med dig").encode(), "en.srt": cue.format("Hello there").encode()},
    )
