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
