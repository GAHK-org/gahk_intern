"""Camera RAW files, decoded with LibRaw (rawpy).

A RAW file is the sensor's data before the camera made a picture of it: every pixel one colour of a
mosaic, at the sensor's own brightness. LibRaw does what the camera would have — fills in the
colours, applies the white balance the camera measured, turns it upright — and gives back an sRGB
picture, from which the viewer and grid JPEGs are made like any other photo's.
"""

import logging
from io import BytesIO

import rawpy
from PIL import Image

logger = logging.getLogger(__name__)


def unpacks(path: str) -> bool:
    """Whether LibRaw recognises the file and can read all of its sensor data — the RAW
    counterpart of decoding an image to check it, which catches a truncated file too."""
    try:
        with rawpy.imread(path) as raw:
            raw.unpack()
    except (rawpy.LibRawError, OSError, ValueError) as exc:
        logger.info("LibRaw could not read %s: %s", path, exc)
        return False
    return True


def decode(data: bytes, minimum_size: int) -> Image.Image:
    """The picture in a RAW file, upright, in sRGB, at least `minimum_size` pixels on its long side
    where the sensor has that many. Raises OSError, as Pillow does, for one it cannot read.

    Decoded at half size whenever that is still big enough, which is nearly always: it is four
    times less work, and a 20-megapixel sensor at half size is still 2700 pixels across.
    """
    try:
        with rawpy.imread(BytesIO(data)) as raw:
            half_size = max(raw.sizes.width, raw.sizes.height) // 2 >= minimum_size
            pixels = raw.postprocess(half_size=half_size, use_camera_wb=True, output_bps=8)
    except rawpy.LibRawError as exc:
        raise OSError(f"LibRaw could not decode it: {exc}") from exc
    return Image.fromarray(pixels)
