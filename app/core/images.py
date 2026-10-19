"""Re-encoding images on the server, for the cases the browser cannot handle for us.

frontend/src/imageupload.ts normally does this job: it redraws every upload through a canvas and
sends a JPEG of at most MAX_DIMENSION px. Two cases slip past it, and this module covers both.

  * **HEIC/HEIF.** It is what an iPhone saves by default, and only Safari can decode it. In Chrome,
    Firefox and Edge `createImageBitmap` throws, so the file goes up untouched. Even if it were
    stored, every one of those browsers would then show a broken image. core.uploads converts it to
    a JPEG before anything is saved.
  * **Derived sizes of something already stored.** Profile thumbnails (residents.models) are made
    from the stored picture, so there is no browser in the loop to make them.

MAX_DIMENSION and QUALITY mirror MAX_DIM and QUALITY in imageupload.ts. A HEIC converted here and a
JPEG downscaled there should land at the same size, whichever browser it came from.
"""

from io import BytesIO
from typing import Any

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

MAX_DIMENSION = 1600
QUALITY = 82

# Everything Pillow raises for a file it cannot read. Every caller catches the same set, so a
# corrupt upload reaches them as None rather than as a 500.
DECODE_ERRORS = (ImportError, OSError, ValueError, SyntaxError, Image.DecompressionBombError)


def _flattened(image: Image.Image) -> Image.Image:
    """`image` as RGB, with any transparency composited onto white.

    A plain convert("RGB") drops the alpha channel and keeps whatever colour the transparent pixels
    happen to hold. That is usually black, so a transparent PNG avatar would turn into a black
    square.
    """
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _encode(image: Image.Image, icc_profile: bytes | None) -> bytes:
    output = BytesIO()
    options: dict[str, Any] = {"format": "JPEG", "quality": QUALITY, "optimize": True}
    if icc_profile:
        options["icc_profile"] = icc_profile
    image.save(output, **options)
    return output.getvalue()


def _decode(file: Any, draft: tuple[int, int]) -> tuple[Image.Image, bytes | None]:  # noqa: ANN401 — any file-like
    """The first frame of `file`, upright and RGB, plus its colour profile when it has a usable one."""
    file.seek(0)
    with Image.open(file) as source:
        # Same rule as photo_album.services.image_variants: a CMYK or greyscale profile attached to
        # the RGB JPEG we write would make browsers render it in nonsense colours.
        icc_profile = source.info.get("icc_profile") if source.mode in {"RGB", "RGBA", "RGBX"} else None
        # Lets libjpeg decode a large JPEG at reduced scale. A no-op for every other format.
        source.draft("RGB", draft)
        image = _flattened(ImageOps.exif_transpose(source))
    return image, icc_profile


def jpeg_copy(file: Any, max_dimension: int = MAX_DIMENSION) -> bytes | None:  # noqa: ANN401 — any file-like
    """`file` re-encoded as a JPEG no larger than `max_dimension` on its longest edge, or None if it cannot be read."""
    try:
        image, icc_profile = _decode(file, (max_dimension, max_dimension))
        image.thumbnail((max_dimension, max_dimension))
        return _encode(image, icc_profile)
    except DECODE_ERRORS:
        return None
    finally:
        file.seek(0)


def square_jpeg(file: Any, size: int) -> bytes | None:  # noqa: ANN401 — any file-like
    """`file` cropped to its centre square and scaled to `size` px, as a JPEG, or None if it cannot be read.

    Square because every avatar on the site is drawn as a circle with object-fit: cover, so the
    browser would throw the crop away in any case. Cropping here means none of those bytes are
    sent in the first place.
    """
    try:
        image, icc_profile = _decode(file, (size, size))
        return _encode(ImageOps.fit(image, (size, size), Image.Resampling.LANCZOS), icc_profile)
    except DECODE_ERRORS:
        return None
    finally:
        file.seek(0)
