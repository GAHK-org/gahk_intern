"""What the photo album is willing to store, checked on the server.

core/uploads.py owns the policy for the shape every other feature has — one OPTIONAL image beside
a text field — and deliberately allows raster images only. The album cannot reuse it: it also takes
video and the HEIC/HEIF a modern iPhone produces, and its reaction to a bad file has to differ. In
the other features the picture is a garnish, so `attached_image` warns and drops it rather than
losing what somebody typed. Here the file *is* the submission, so a refused one has to fail the
upload with a message; silently dropping it would look exactly like a successful upload.

**No SVG**, for precisely the reason core/uploads.py's docstring sets out: an SVG is a document,
not a picture, and one served from our own origin under /media/ executes its script as us when
navigated to directly. The album is the worst place to allow it — every resident may upload, and
the original is served from our origin as it was uploaded.

Both the content type and the extension are checked, for the reason given there too: the content
type is a hint the client controls, and the extension is what the file is ultimately served as.
"""

import re
import subprocess  # nosec B404
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()

# Extension -> the content type the file is stored and served as. The browser's own claim is only
# checked against it (see `check_media_upload`); what gets stored is always this.
IMAGE_TYPES_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}
VIDEO_TYPES_BY_EXTENSION = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".3gp": "video/3gpp",
    ".3g2": "video/3gpp2",
    ".mts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".wmv": "video/x-ms-wmv",
    ".ogv": "video/ogg",
}
IMAGE_EXTENSIONS = frozenset(IMAGE_TYPES_BY_EXTENSION)
VIDEO_EXTENSIONS = frozenset(VIDEO_TYPES_BY_EXTENSION)
# Every content type a browser has been seen to send for those extensions — the canonical ones plus
# the aliases. Browsers disagree about HEIC, AVI and MTS, and some send nothing at all, so the
# extension is what actually decides.
IMAGE_CONTENT_TYPES = frozenset({*IMAGE_TYPES_BY_EXTENSION.values(), "image/x-ms-bmp", "image/heic-sequence"})
VIDEO_CONTENT_TYPES = frozenset(
    {
        *VIDEO_TYPES_BY_EXTENSION.values(),
        "video/avi",
        "video/msvideo",
        "video/vnd.avi",
        "video/x-ms-asf",
        "video/3gpp2",
        "video/x-mpeg",
        "video/x-mts",
        "video/avchd",
    }
)
# What a browser sends when it has no idea. Accepted, like an empty type, when the extension is known.
UNKNOWN_CONTENT_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

EXTENSION_HELP = "JPEG, PNG, GIF, WebP, HEIC, AVIF, TIFF, BMP, MP4, MOV, M4V, WebM, AVI, MKV, 3GP, MTS, MPEG, WMV eller OGV"


def _extension(upload: Any) -> str:  # noqa: ANN401 — an UploadedFile
    return Path((getattr(upload, "name", "") or "").lower()).suffix


def is_video(upload: Any) -> bool:  # noqa: ANN401 — an UploadedFile
    """Whether this upload should take the video path through `upload_media`."""
    content_type = (getattr(upload, "content_type", "") or "").lower()
    return content_type in VIDEO_CONTENT_TYPES or _extension(upload) in VIDEO_EXTENSIONS


def content_type_for(name: str) -> str:
    """The content type a file with this name is stored as, or "" for one the album does not take."""
    extension = Path(name.lower()).suffix
    return IMAGE_TYPES_BY_EXTENSION.get(extension) or VIDEO_TYPES_BY_EXTENSION.get(extension, "")


def check_media_upload(upload: Any) -> str | None:  # noqa: ANN401 — an UploadedFile
    """Return a Danish error message, or None when the album is willing to store `upload`.

    This is the cheap check — names, types and size. `check_media_content` is the one that opens it.
    """
    content_type = (getattr(upload, "content_type", "") or "").lower()
    extension = _extension(upload)

    video = extension in VIDEO_EXTENSIONS or content_type in VIDEO_CONTENT_TYPES
    allowed_extensions = VIDEO_EXTENSIONS if video else IMAGE_EXTENSIONS
    allowed_types = VIDEO_CONTENT_TYPES if video else IMAGE_CONTENT_TYPES

    if extension not in allowed_extensions:
        return f"Filtypen er ikke understøttet (tilladt: {EXTENSION_HELP})."
    # An empty or "don't know" content type is accepted only when the extension already vouched for
    # the file: some browsers send none for HEIC and for .mov. A *wrong* one still fails, so an
    # image/png header on a .svg name — or on a .mp4 name — cannot get through.
    if content_type not in UNKNOWN_CONTENT_TYPES and content_type not in allowed_types:
        return f"Filens indhold passer ikke til filtypen (tilladt: {EXTENSION_HELP})."

    maximum = settings.PHOTO_ALBUM_VIDEO_MAX_MB if video else settings.PHOTO_ALBUM_IMAGE_MAX_MB
    size = getattr(upload, "size", 0) or 0
    if size > maximum * 1024 * 1024:
        kind = "Videoen" if video else "Billedet"
        return f"{kind} er for stor{'' if video else 't'} (over {maximum} MB)."
    return None


def check_media_content(upload: Any) -> str | None:  # noqa: ANN401 — an UploadedFile
    """Open the file and make sure it really is the picture or video its name says.

    Without this, a truncated JPEG or a renamed PDF was stored happily and only failed later, in the
    worker — leaving an item in the album that could never be shown. Images are decoded (JPEGs at a
    reduced scale, which is enough to hit a truncated or corrupt stream and costs a fraction of a
    full decode); a video must give ffmpeg one decodable frame.
    """
    if is_video(upload):
        return (
            None
            if _video_decodes(upload)
            else "Videoen kunne ikke læses — filen er ødelagt eller ikke en video."
        )
    try:
        upload.seek(0)
        with Image.open(upload) as image:
            image.draft("RGB", (512, 512))
            image.load()
    except Image.DecompressionBombError:
        return "Billedet har for mange pixels."
    except (OSError, ValueError, SyntaxError, TypeError):
        return "Billedet kunne ikke læses — filen er ødelagt eller ikke et billede."
    finally:
        upload.seek(0)
    return None


def _video_decodes(upload: Any) -> bool:  # noqa: ANN401 — an UploadedFile
    try:
        import imageio_ffmpeg
    except ImportError:  # pragma: no cover — a dependency; without it nothing can be transcoded anyway
        return True
    with local_copy(upload) as path:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-hide_banner",
                "-v",
                "error",
                "-i",
                path,
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    return result.returncode == 0


@contextmanager
def local_copy(upload: Any) -> Iterator[str]:  # noqa: ANN401 — an UploadedFile or File
    """A filesystem path for an upload, for tools that want one (ffmpeg).

    A large browser upload is already on disk as a TemporaryUploadedFile, so its own path is used;
    anything else — an in-memory upload, a ZIP member — is written out to a temporary file first.
    """
    temporary_path = getattr(upload, "temporary_file_path", None)
    if callable(temporary_path):
        yield str(temporary_path())
        return
    with NamedTemporaryFile(prefix="photo-album-", suffix=_extension(upload) or ".upload") as target:
        upload.seek(0)
        for chunk in upload.chunks():
            target.write(chunk)
        target.flush()
        upload.seek(0)
        yield target.name


_CREATION_TIME = re.compile(
    r"^\s*(?P<key>com\.apple\.quicktime\.creationdate|creation_time)\s*:\s*(?P<value>\S+)", re.MULTILINE
)


def parse_video_creation_time(ffmpeg_output: str) -> str | None:
    """The recording time from `ffmpeg -i`'s metadata dump, as ISO 8601, or None.

    Apple's own tag is preferred: it carries the phone's local offset, where `creation_time` is UTC
    and on some cameras simply the local time mislabelled as UTC.
    """
    values: dict[str, str] = {}
    for match in _CREATION_TIME.finditer(ffmpeg_output):
        values.setdefault(match["key"], match["value"])
    return values.get("com.apple.quicktime.creationdate") or values.get("creation_time")


def validate_media_upload(upload: Any) -> None:  # noqa: ANN401 — an UploadedFile
    """check_media_upload and check_media_content as a ValidationError — for the upload form."""
    message = check_media_upload(upload) or check_media_content(upload)
    if message is not None:
        raise ValidationError(message)
