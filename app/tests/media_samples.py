"""Real, decodable media for the photo album tests.

The album now opens every upload to check it is what its name says (uploads.check_media_content),
so the old `b"bytes"` stand-ins are refused. These are tiny but genuine files.
"""

import functools
import itertools
import struct
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


def dng_bytes(
    size: tuple[int, int] = (64, 48),
    *,
    orientation: int = 1,
    captured: str = "2004:05:06 07:08:09",
    make: str = "Canon",
    model: str = "EOS 5D",
) -> bytes:
    """A camera RAW file: a DNG of a flat colour behind a Bayer filter, 12 bits in 16, with the
    camera, the orientation and the capture time a camera writes into one. LibRaw decodes it; Pillow
    cannot open it, as it cannot open any RAW file."""
    width, height = size
    rows = struct.pack("<2H", 3000, 1500) * (width // 2) + struct.pack("<2H", 1500, 800) * (width // 2)
    pixels = rows * (height // 2)
    srgb_from_xyz = [3240, -1537, -499, -969, 1876, 42, 56, -204, 1057]  # in thousandths
    Value = bytes | list[int | str]
    main: list[tuple[int, int, Value]] = [  # (tag, TIFF type, value); "exif" and "pixels" are offsets
        (254, 4, [0]),  # the main image
        (256, 4, [width]),
        (257, 4, [height]),
        (258, 3, [16]),
        (259, 3, [1]),  # uncompressed
        (262, 3, [32803]),  # a colour filter array
        (271, 2, make.encode() + b"\0"),
        (272, 2, model.encode() + b"\0"),
        (273, 4, ["pixels"]),
        (274, 3, [orientation]),
        (277, 3, [1]),
        (278, 4, [height]),
        (279, 4, [len(pixels)]),
        (284, 3, [1]),
        (33421, 3, [2, 2]),  # the filter's pattern: 2x2,
        (33422, 1, bytes([0, 1, 1, 2])),  # red, green, green, blue
        (34665, 4, ["exif"]),
        (50706, 1, bytes([1, 4, 0, 0])),  # DNG 1.4
        (50708, 2, f"{make} {model}".encode() + b"\0"),
        (50717, 4, [4095]),  # white level
        (50721, 10, [*srgb_from_xyz]),  # colour matrix
        (50778, 3, [21]),  # under D65
    ]
    exif: list[tuple[int, int, Value]] = [(36867, 2, captured.encode() + b"\0")]

    def directory(entries: list[tuple[int, int, Value]], start: int, at: dict[str, int]) -> bytes:
        """An IFD written at `start`, followed by the values too long to fit in it."""
        end = start + 2 + 12 * len(entries) + 4
        table, overflow = struct.pack("<H", len(entries)), b""
        for tag, kind, value in entries:
            if isinstance(value, bytes):
                count, data = len(value), value
            elif kind == 10:  # signed rationals
                count, data = len(value), b"".join(struct.pack("<2i", int(v), 1000) for v in value)
            else:
                numbers = [at[v] if isinstance(v, str) else v for v in value]
                count, data = (
                    len(numbers),
                    b"".join(struct.pack("<H" if kind == 3 else "<I", v) for v in numbers),
                )
            if len(data) <= 4:
                table += struct.pack("<HHI", tag, kind, count) + data.ljust(4, b"\0")
            else:
                overflow += b"\0" * (len(overflow) % 2)
                table += struct.pack("<HHII", tag, kind, count, end + len(overflow))
                overflow += data
        return table + struct.pack("<I", 0) + overflow

    at = {"exif": 0, "pixels": 0}
    for _ in range(2):  # the offsets depend on the lengths before them, which do not depend on them
        first = directory(main, 8, at)
        at["exif"] = 8 + len(first) + len(first) % 2
        second = directory(exif, at["exif"], at)
        at["pixels"] = at["exif"] + len(second) + len(second) % 2
    header = b"II*\0" + struct.pack("<I", 8)
    return (header + first).ljust(at["exif"], b"\0") + second.ljust(at["pixels"] - at["exif"], b"\0") + pixels


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
