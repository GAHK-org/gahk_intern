"""State transitions for albums. Views and scheduled cleanup use these rules together."""

import subprocess  # nosec B404
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.base import ContentFile, File
from django.utils import timezone
from PIL import ExifTags, Image, ImageOps
from pillow_heif import register_heif_opener

from residents.models import Resident

from .models import Album, Media, MediaStatus

register_heif_opener()


def upload_media(
    *, album: Album, uploaded_file: File, resident: Resident, title: str = "", approved: bool = False
) -> Media:
    """Store three independently-addressable variants and extract safe, available image metadata."""
    if album.is_locked():
        raise ValueError("Albummet er låst.")
    filename = uploaded_file.name or "upload"
    status = MediaStatus.APPROVED if approved else MediaStatus.PENDING
    media = Media(
        album=album,
        title=title or Path(filename).stem,
        requested_by=resident,
        status=status,
        approved_by=resident if status == MediaStatus.APPROVED else None,
        approved_at=timezone.now() if status == MediaStatus.APPROVED else None,
        content_type=getattr(uploaded_file, "content_type", ""),
        metadata=extract_metadata(uploaded_file),
        captured_at=extract_captured_at(uploaded_file),
    )
    media.original.save(filename, uploaded_file, save=False)
    variants = image_variants(uploaded_file, filename) or video_variants(uploaded_file, filename)
    if variants is None:
        # Preserve an unsupported source rather than rejecting the resident's original upload.
        for field in (media.high_definition, media.thumbnail):
            uploaded_file.seek(0)
            field.save(filename, File(uploaded_file), save=False)
    else:
        high_definition, thumbnail = variants
        media.high_definition.save(high_definition.name or "high-definition.jpg", high_definition, save=False)
        media.thumbnail.save(thumbnail.name or "thumbnail.jpg", thumbnail, save=False)
    media.save()
    return media


def image_variants(uploaded_file: File, filename: str) -> tuple[ContentFile, ContentFile] | None:
    """Build the viewer and grid JPEGs for one image, or defer unsupported media unchanged."""
    try:
        uploaded_file.seek(0)
        image = ImageOps.exif_transpose(Image.open(uploaded_file)).convert("RGB")
        return _jpeg_variant(image, filename, 1600), _jpeg_variant(image, filename, 320)
    except (ImportError, OSError, ValueError):
        return None
    finally:
        uploaded_file.seek(0)


def _jpeg_variant(image: Image.Image, filename: str, maximum_dimension: int) -> ContentFile:
    variant = image.copy()
    variant.thumbnail((maximum_dimension, maximum_dimension))
    output = BytesIO()
    variant.save(output, format="JPEG", quality=82, optimize=True)
    return ContentFile(output.getvalue(), name=f"{Path(filename).stem}.jpg")


def video_variants(uploaded_file: File, filename: str) -> tuple[ContentFile, ContentFile] | None:
    """Transcode a video to a viewer-sized MP4 and extract a grid-sized JPEG frame."""
    try:
        import imageio_ffmpeg
    except ImportError:
        return None

    try:
        with TemporaryDirectory(prefix="photo-album-") as directory:
            source = Path(directory) / f"source{Path(filename).suffix or '.upload'}"
            high_definition = Path(directory) / "high-definition.mp4"
            thumbnail = Path(directory) / "thumbnail.jpg"
            uploaded_file.seek(0)
            with source.open("wb") as target:
                for chunk in uploaded_file.chunks():
                    target.write(chunk)
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            _run_ffmpeg(
                ffmpeg,
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                "scale=1600:1600:force_original_aspect_ratio=decrease",
                "-c:v",
                "libx264",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(high_definition),
            )
            _run_ffmpeg(
                ffmpeg,
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:320:force_original_aspect_ratio=decrease",
                str(thumbnail),
            )
            return (
                ContentFile(high_definition.read_bytes(), name=f"{Path(filename).stem}.mp4"),
                ContentFile(thumbnail.read_bytes(), name=f"{Path(filename).stem}.jpg"),
            )
    except (OSError, subprocess.CalledProcessError):
        return None
    finally:
        uploaded_file.seek(0)


def _run_ffmpeg(executable: str, *arguments: str) -> None:
    subprocess.run(  # noqa: S603  # nosec B603
        [executable, "-y", *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def extract_metadata(uploaded_file: File) -> dict[str, str]:
    """Read EXIF when Pillow is available; unsupported files remain valid uploads."""
    try:
        uploaded_file.seek(0)
        exif = Image.open(uploaded_file).getexif()
        values = {
            ExifTags.TAGS.get(key, str(key)): value
            for key, value in {**dict(exif.items()), **_exif_ifd(exif)}.items()
        }
        metadata: dict[str, str] = {}
        if values.get("DateTimeOriginal"):
            metadata["Oprindelig dato"] = str(values["DateTimeOriginal"])
        camera = " ".join(str(values[key]) for key in ("Make", "Model") if values.get(key)).strip()
        if camera:
            metadata["Kamera"] = camera
        location = _gps_location(exif)
        if location:
            metadata["Sted"] = location
        return metadata
    except (ImportError, OSError, ValueError):
        return {}
    finally:
        uploaded_file.seek(0)


def extract_captured_at(uploaded_file: File) -> datetime | None:
    """Return the original image capture time when EXIF supplies one."""
    try:
        uploaded_file.seek(0)
        exif = Image.open(uploaded_file).getexif()
        exif_values = _exif_ifd(exif)
        value = (
            exif_values.get(36867)
            or exif_values.get(36868)
            or exif.get(36867)
            or exif.get(36868)
            or exif.get(306)
        )
        if not value:
            return None
        return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S").replace(
            tzinfo=timezone.get_current_timezone()
        )
    except (ImportError, OSError, TypeError, ValueError):
        return None
    finally:
        uploaded_file.seek(0)


def _exif_ifd(exif: Image.Exif) -> dict[int, object]:
    try:
        return dict(exif.get_ifd(ExifTags.IFD.Exif))
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}


def _gps_location(exif: Image.Exif) -> str | None:
    """Format GPS EXIF coordinates as a useful location when an image provides them."""
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        latitude = _decimal_coordinate(gps[2], gps[1])
        longitude = _decimal_coordinate(gps[4], gps[3])
        return f"{latitude:.6f}, {longitude:.6f}"
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _decimal_coordinate(parts: tuple[float, float, float], hemisphere: str) -> float:
    degrees, minutes, seconds = parts
    coordinate = degrees + minutes / 60 + seconds / 3600
    return -coordinate if hemisphere in {"S", "W"} else coordinate


def approve(media: Media, resident: Resident) -> None:
    media.status = MediaStatus.APPROVED
    media.approved_by = resident
    media.approved_at = timezone.now()
    media.save(update_fields=["status", "approved_by", "approved_at"])


def reject(media: Media, resident: Resident) -> None:
    media.status = MediaStatus.REJECTED
    media.deleted_at = timezone.now()
    media.deleted_by = resident
    media.save(update_fields=["status", "deleted_at", "deleted_by"])


def delete(media: Media, resident: Resident, now: datetime | None = None) -> bool:
    """Move media to the recoverable bin."""
    moment = now or timezone.now()
    media.deleted_at = moment
    media.deleted_by = resident
    media.save(update_fields=["deleted_at", "deleted_by"])
    return False


def permanently_delete(media: Media) -> None:
    """Remove all stored media variants and their database record."""
    _delete_files(media)
    media.delete()


def purge_expired(now: datetime | None = None) -> int:
    moment = now or timezone.now()
    expired = Media.objects.filter(
        status=MediaStatus.PENDING, added_at__lt=moment - timedelta(days=30)
    ) | Media.objects.filter(deleted_at__lt=moment - timedelta(days=30))
    count = 0
    for media in expired.distinct():
        _delete_files(media)
        media.delete()
        count += 1
    return count


def _delete_files(media: Media) -> None:
    for field in (media.original, media.high_definition, media.thumbnail):
        if field:
            field.delete(save=False)
