"""Preparing an image for the website's library: downscaled, upright, without metadata.

Every library image ends up on gahk.dk, where it is served to anyone, so what is stored is what is
published:

  * at most MAX_DIMENSION px on the long side. Editors upload camera originals; a hero image does
    not need more, and every byte is copied to the webhost.
  * EXIF orientation applied to the pixels, then every piece of metadata dropped. A phone photo
    carries the GPS position it was taken at; nobody means to publish that.
  * the format kept (JPEG stays JPEG, PNG stays PNG, ...), because gahk.dk accepts exactly JPEG,
    PNG, GIF and WebP, and a transparent PNG logo must not turn into a JPEG with a black box.

An animated GIF is stored untouched: re-encoding would keep only its first frame. HEIC never gets
here — core.uploads has already turned it into a JPEG.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath
from typing import Any

from PIL import Image, ImageOps

from core.images import DECODE_ERRORS

MAX_DIMENSION = 2400
JPEG_QUALITY = 85
WEBP_QUALITY = 85

# Pillow format name -> (content type, file extension). Exactly the types gahk.dk accepts.
FORMATS: dict[str, tuple[str, str]] = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "GIF": ("image/gif", ".gif"),
    "WEBP": ("image/webp", ".webp"),
}

EXTENSION_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class ImageRejected(ValueError):
    """The file is not an image the website can use. The message is Danish, for the editor."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    content_type: str
    extension: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upright(image: Image.Image) -> Image.Image:
    return ImageOps.exif_transpose(image) or image


def _encode(image: Image.Image, image_format: str, source: Image.Image) -> bytes:
    output = BytesIO()
    options: dict[str, Any] = {"format": image_format}
    # A colour profile is not "metadata" in the privacy sense, and dropping it shifts the colours of
    # a photo taken in Display P3. Kept for RGB only, like core.images.
    icc_profile = source.info.get("icc_profile") if source.mode in {"RGB", "RGBA", "RGBX"} else None
    if icc_profile:
        options["icc_profile"] = icc_profile
    if image_format == "JPEG":
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        options.update(quality=JPEG_QUALITY, optimize=True, progressive=True)
    elif image_format == "PNG":
        options["optimize"] = True
    elif image_format == "WEBP":
        options["quality"] = WEBP_QUALITY
    elif image_format == "GIF" and "transparency" in source.info:
        options["transparency"] = source.info["transparency"]
    image.save(output, **options)
    return output.getvalue()


def prepare_image(file: Any) -> PreparedImage:  # noqa: ANN401 — any binary file-like
    """`file` made ready for the library, or ImageRejected when it is not a usable image."""
    file.seek(0)
    original = file.read()
    try:
        with Image.open(BytesIO(original)) as source:
            image_format = source.format or ""
            if image_format not in FORMATS:
                raise ImageRejected("Billedet skal være JPEG, PNG, GIF eller WebP.")
            content_type, extension = FORMATS[image_format]
            if image_format == "GIF" and getattr(source, "n_frames", 1) > 1:
                return PreparedImage(original, content_type, extension)
            source.load()
            image = _upright(source)
            if max(image.size) > MAX_DIMENSION:
                image = image.copy()
                image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)
            data = _encode(image, image_format, source)
    except ImageRejected:
        raise
    except DECODE_ERRORS as exc:
        raise ImageRejected("Filen kunne ikke læses som et billede.") from exc
    return PreparedImage(data, content_type, extension)


def content_type_of(data: bytes) -> str | None:
    """The content type of stored image bytes, or None when gahk.dk could not take them."""
    try:
        with Image.open(BytesIO(data)) as image:
            found = FORMATS.get(image.format or "")
    except DECODE_ERRORS:
        return None
    return found[0] if found else None


# ------------------------------------------------------------------------- gahk.dk's image URLs
BILLEDER_NAME_RE = re.compile(r"[^a-z0-9]+")
DANISH_LETTERS = str.maketrans({"æ": "ae", "Æ": "Ae", "ø": "oe", "Ø": "Oe", "å": "aa", "Å": "Aa"})
STORED_STEM_RE = re.compile(r"[^A-Za-z0-9_-]+")


def stored_stem(file_name: str) -> str:
    """An ASCII stem for a library image's storage name ("Tovtrækning 2019" → "Tovtraekning_2019").

    Storage names appear in /media/cms/… URLs, which gahk.dk's preview only takes as plain ASCII; an
    "æ" there (Django keeps Unicode letters in file names) would make the preview fall back.
    """
    stem = PurePath(file_name).stem.translate(DANISH_LETTERS)
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    return STORED_STEM_RE.sub("_", ascii_stem).strip("_-")[:100] or "billede"


def billeder_name(file_name: str, content_type: str) -> str:
    """The file name part of an image's gahk.dk URL: the basename slugified, lower-case, with an
    extension that matches its content type (gahk.dk checks that it does)."""
    path = PurePath(file_name)
    ascii_stem = unicodedata.normalize("NFKD", path.stem).encode("ascii", "ignore").decode().lower()
    stem = BILLEDER_NAME_RE.sub("-", ascii_stem).strip("-")[:80].strip("-") or "billede"
    extension = path.suffix.lower()
    if EXTENSION_TYPES.get(extension) != content_type:
        extension = next(ext for ext, kind in EXTENSION_TYPES.items() if kind == content_type)
    return f"{stem}{extension}"


def billeder_url(sha256: str, file_name: str, content_type: str) -> str:
    """/billeder/<sha256[:16]>/<name>: where gahk.dk serves its copy. Content-hashed, so immutable."""
    return f"/billeder/{sha256[:16]}/{billeder_name(file_name, content_type)}"
