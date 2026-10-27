"""A photo's own metadata — EXIF, IPTC and XMP — carried over to the copies made of it, with exiv2.

The high-definition copy is a new JPEG, and Pillow writes nothing into one but the pixels: a photo
downloaded as "Komprimeret (HD)" came without its capture date, camera, lens, place or keywords, so
a photo library it was imported into dated it by the day of the download. exiv2 copies them from
the original, maker notes included: it knows each camera maker's notes well enough to move them
without breaking the offsets inside them. (A video's copy keeps its metadata through ffmpeg, see
services._encode_video.)

exiv2 reads every format the album takes but BMP and GIF, which have nothing to copy, and RAW
files above all, whose metadata Pillow cannot read at all — so `carrier` hands Pillow a JPEG with
the RAW's metadata in it instead, for the album's own readers of dates, camera and place.
"""

import logging
from io import BytesIO

import exiv2
from PIL import Image

logger = logging.getLogger(__name__)

# exiv2 warns of every tag it does not write back byte for byte — a maker note above all, which it
# writes from what it parsed out of it instead ("Exif.Photo.MakerNote not encoded"), on every Canon
# photo. Its errors still reach the log.
exiv2.LogMsg.setLevel(exiv2.LogMsg.Level.error)

# The tags of a TIFF's or a RAW file's first directory that say something about the photo. The rest
# describe how that file stores its pixels — its width, compression, strips, colour filter, a DNG's
# private data — which is untrue of the JPEG, and would mislead whatever reads it.
_PHOTO_TAGS = frozenset(
    {
        "ImageDescription",
        "Make",
        "Model",
        "Orientation",
        "XResolution",
        "YResolution",
        "ResolutionUnit",
        "Software",
        "DateTime",
        "DateTimeOriginal",
        "Artist",
        "HostComputer",
        "Copyright",
        "Rating",
        "RatingPercent",
        "XPTitle",
        "XPComment",
        "XPAuthor",
        "XPKeywords",
        "XPSubject",
        "CameraSerialNumber",
        "ExifTag",
        "GPSTag",
    }
)


def copy_metadata(source: bytes, jpeg: bytes, size: tuple[int, int]) -> bytes:
    """`jpeg`, a copy made of the file `source`, with `source`'s EXIF, IPTC and XMP in it.

    The copy is upright and `size` pixels, and the metadata is made to say so: its orientation is
    "normal" (the pixels were turned already, and turning them again would put the picture on its
    side) and its pixel dimensions are the copy's. The embedded EXIF thumbnail is left out: it is
    the original's, still turned the original's way.

    `jpeg` comes back as it was when `source` has no metadata, or none exiv2 can read. Metadata is
    worth having, not worth losing the copy over.
    """
    try:
        original = exiv2.ImageFactory.open(source)
        original.readMetadata()
        exif, iptc, xmp = original.exifData(), original.iptcData(), original.xmpData()
        if exif.empty() and iptc.empty() and xmp.empty():
            return jpeg
        _describe_copy(exif, xmp, size)
        copy = exiv2.ImageFactory.open(jpeg)
        copy.readMetadata()  # keeps what Pillow wrote: the colour profile
        copy.setExifData(exif)
        copy.setIptcData(iptc)
        copy.setXmpData(xmp)
        copy.writeMetadata()
        return bytes(copy.io().mmap())
    except exiv2.Exiv2Error as exc:
        logger.warning("Could not copy the metadata into a copy of %d bytes: %s", len(source), exc)
        return jpeg


def _describe_copy(exif: exiv2.ExifData, xmp: exiv2.XmpData, size: tuple[int, int]) -> None:
    width, height = size
    for key in [datum.key() for datum in exif if datum.groupName() == "Image"]:
        if key.removeprefix("Exif.Image.") not in _PHOTO_TAGS:
            exif.erase(exif.findKey(exiv2.ExifKey(key)))
    exiv2.ExifThumb(exif).erase()
    for key, value in (
        ("Exif.Image.Orientation", 1),
        ("Exif.Photo.PixelXDimension", width),
        ("Exif.Photo.PixelYDimension", height),
    ):
        if exif.findKey(exiv2.ExifKey(key)) != exif.end():
            exif[key] = value
    for key, value in (
        ("Xmp.tiff.Orientation", 1),
        ("Xmp.tiff.ImageWidth", width),
        ("Xmp.tiff.ImageLength", height),
        ("Xmp.exif.PixelXDimension", width),
        ("Xmp.exif.PixelYDimension", height),
    ):
        if key in xmp:
            xmp[key] = str(value)


def carrier(source: bytes) -> bytes:
    """A one-pixel JPEG carrying the metadata of `source`, a file Pillow cannot open (a RAW file),
    so Pillow can read it after all."""
    blank = BytesIO()
    Image.new("RGB", (1, 1)).save(blank, format="JPEG")
    return copy_metadata(source, blank.getvalue(), (1, 1))
