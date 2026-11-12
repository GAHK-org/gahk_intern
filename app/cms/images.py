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
# A name keeps its letters, Danish or not: names are UTF-8 everywhere, and wherever one reaches a URL
# it is percent-encoded (Storage.url() for /media/…, quote() for a preview's background). Only
# NFC-normalized first, because a Mac hands over "å" as "a" plus a combining ring (NFD), which would
# otherwise give two different names, and URLs, for what looks like the same file.
STORED_STEM_MAX = 100
PUBLIC_IMAGE_STEM_MAX_BYTES = 120  # gahk.dk's file system allows 255 bytes per name, not characters
PUBLIC_IMAGE_STEM_MAX_CHARS = 80  # an ASCII name was always cut here; its URL must not change
SEPARATORS = "-_."


def _is_letter_or_digit(char: str) -> bool:
    """A Unicode letter or decimal digit: "æ", "é", "ß" and "7" are; "²", "(" and a space are not."""
    category = unicodedata.category(char)
    return category[0] == "L" or category == "Nd"


def _letters_runs(text: str, keep: str, replacement: str) -> str:
    """`text` with every run of characters that are neither letters, digits nor in `keep` turned into
    `replacement`, a run of separators cut down to its first, and none at either end. Combining
    marks are dropped, as Django's get_valid_filename drops them."""
    out: list[str] = []
    for char in text:
        if unicodedata.category(char)[0] == "M":
            continue  # a combining mark NFC had no letter for ("İ" lower-cased is "i" + a dot): dropped
        if not (_is_letter_or_digit(char) or char in keep):
            char = replacement
        if char in SEPARATORS and (not out or out[-1] in SEPARATORS):
            continue  # never two in a row, and never one first
        out.append(char)
    return "".join(out).rstrip(SEPARATORS)


def _cut_utf8(text: str, max_bytes: int) -> str:
    return text.encode()[:max_bytes].decode(errors="ignore")


def stored_stem(file_name: str) -> str:
    """The stem of a library image's storage name ("Tovtrækning 2019 (2).jpg" → "Tovtrækning_2019_2").

    Letters stay as they are (NFC); spaces and everything that is not a letter, a digit, "-", "_" or
    "." become "_". That is Django's own get_valid_filename, minus the punctuation it would still let
    through, so a storage name never needs more in a URL than percent-encoding of its letters.
    """
    stem = unicodedata.normalize("NFC", PurePath(file_name).stem)
    cleaned = _letters_runs(stem, SEPARATORS, "_")[:STORED_STEM_MAX].rstrip(SEPARATORS)
    return unicodedata.normalize("NFC", cleaned) or "billede"


def public_image_name(file_name: str, content_type: str) -> str:
    """The file name part of an image's gahk.dk URL: the basename in lower case, its letters kept
    (NFC), every other run of characters a "-", with an extension that matches its content type
    (gahk.dk checks that it does).

    Never starts with "_" or ".": gahk.dk keeps its own WebP variants ("_960w.webp") beside the copy.
    Only "-" separates words, and the stem is cut at 80 characters as it always was (and at 120 bytes),
    so an ASCII name is spelled as before and its URL is unchanged.
    """
    path = PurePath(file_name)
    lowered = unicodedata.normalize("NFC", unicodedata.normalize("NFC", path.stem).lower())
    # Keep only lower-case or caseless letters: an upper-case letter with no lower-case form is rare,
    # and gahk.dk takes lower-case names only.
    words = "".join(c if unicodedata.category(c) not in ("Lu", "Lt") else "-" for c in lowered)
    stem = _letters_runs(words, "-", "-")
    stem = _cut_utf8(stem[:PUBLIC_IMAGE_STEM_MAX_CHARS], PUBLIC_IMAGE_STEM_MAX_BYTES)
    stem = unicodedata.normalize("NFC", stem).rstrip(SEPARATORS)
    extension = path.suffix.lower()
    if EXTENSION_TYPES.get(extension) != content_type:
        extension = next(ext for ext, kind in EXTENSION_TYPES.items() if kind == content_type)
    return f"{stem or 'billede'}{extension}"


def public_image_url(sha256: str, file_name: str, content_type: str) -> str:
    """/images/<sha256[:16]>/<name>: where gahk.dk serves its copy. Content-hashed, so immutable."""
    return f"/images/{sha256[:16]}/{public_image_name(file_name, content_type)}"
