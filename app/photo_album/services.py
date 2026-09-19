"""State transitions for albums. Views and scheduled cleanup use these rules together."""

import logging
import subprocess  # nosec B404
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile, File
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from PIL import ExifTags, Image, ImageOps
from pillow_heif import register_heif_opener

from residents.models import Resident

from .models import Album, DerivativeState, Media, MediaStatus
from .uploads import check_media_upload, is_video

register_heif_opener()
logger = logging.getLogger(__name__)


def _is_macos_archive_metadata(path: PurePosixPath) -> bool:
    """Whether a Finder-created ZIP member is metadata rather than user media."""
    return (
        "__MACOSX" in path.parts
        or ".AppleDouble" in path.parts
        or path.name.startswith("._")
        or path.name == ".DS_Store"
    )


def import_zip_album(*, archive: File, folder: str, resident: Resident) -> tuple[list[Album], list[str]]:
    """Import supported archive members into albums named after their archive paths.

    Members are deliberately processed independently: one corrupt file or a storage failure is
    reported, while the rest of a family archive remains useful. Archive paths are never extracted.
    """
    imported_albums: dict[str, Album] = {}
    skipped: list[str] = []
    root_name = Path(archive.name or "album.zip").stem
    maximum_member_bytes = (
        max(settings.PHOTO_ALBUM_IMAGE_MAX_MB, settings.PHOTO_ALBUM_VIDEO_MAX_MB) * 1024 * 1024
    )

    archive.seek(0)
    try:
        with zipfile.ZipFile(archive) as zip_archive:
            file_paths = [
                PurePosixPath(member.filename)
                for member in zip_archive.infolist()
                if not member.is_dir() and not _is_macos_archive_metadata(PurePosixPath(member.filename))
            ]
            root_parts = 0
            while file_paths and all(
                len(path.parts) > root_parts + 1 and path.parts[root_parts] == root_name
                for path in file_paths
            ):
                root_parts += 1
            for member in zip_archive.infolist():
                member_path = PurePosixPath(member.filename)
                if member.is_dir():
                    continue
                if _is_macos_archive_metadata(member_path):
                    skipped.append(f"{member.filename}: macOS-metadata")
                    continue
                if member_path.is_absolute() or ".." in member_path.parts or not member_path.name:
                    skipped.append(f"{member.filename}: ugyldig sti i ZIP-filen")
                    continue
                if root_parts:
                    member_path = PurePosixPath(*member_path.parts[root_parts:])
                if member.file_size > maximum_member_bytes:
                    skipped.append(f"{member.filename}: filen er for stor")
                    continue
                try:
                    with zip_archive.open(member) as source:
                        content = source.read(maximum_member_bytes + 1)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    skipped.append(f"{member.filename}: kunne ikke læses ({exc})")
                    continue
                if len(content) > maximum_member_bytes:
                    skipped.append(f"{member.filename}: filen er for stor")
                    continue

                uploaded_file = ContentFile(content, name=member_path.name)
                error = check_media_upload(uploaded_file)
                if error:
                    skipped.append(f"{member.filename}: {error}")
                    continue
                album_name = (
                    root_name
                    if member_path.parent == PurePosixPath(".")
                    else f"{root_name}/{member_path.parent}"
                )
                album = imported_albums.get(album_name)
                if album is None:
                    try:
                        album = Album(folder=folder, name=album_name)
                        album.full_clean()
                        album.save()
                    except (ValidationError, ValueError) as exc:
                        skipped.append(f"{member.filename}: albummet kunne ikke oprettes ({exc})")
                        continue
                    imported_albums[album_name] = album
                try:
                    upload_media(album=album, uploaded_file=uploaded_file, resident=resident, approved=True)
                except Exception as exc:  # A broken member must not abandon the rest of the archive.
                    logger.warning("Could not import photo album member %s: %s", member.filename, exc)
                    skipped.append(f"{member.filename}: kunne ikke tilføjes ({exc})")
    except zipfile.BadZipFile as exc:
        raise ValidationError("ZIP-filen kunne ikke læses.") from exc
    return list(imported_albums.values()), skipped


def upload_media(
    *, album: Album, uploaded_file: File, resident: Resident, title: str = "", approved: bool = False
) -> Media:
    """Store an original and queue its derivatives after the database transaction commits."""
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
    media.derivative_state = DerivativeState.PENDING
    media.save()
    from .tasks import build_media_derivatives

    transaction.on_commit(lambda: build_media_derivatives.delay(media.pk))
    return media


# A video whose transcode keeps failing must not be retried forever: the encode is the most
# expensive thing this app does, and a corrupt upload would otherwise burn the whole nightly run on
# every pass. Three attempts, then FAILED and a line in the command's output for someone to look at.
MAX_DERIVATIVE_ATTEMPTS = 3


def is_video_media(media: Media) -> bool:
    """Whether a STORED row is a video, asking both things that know.

    `is_video` decides from `.content_type` and `.name`. A Media has the first and not the second,
    so calling it on the row alone quietly reduced the test to the content type — and
    `uploads.check_media_upload` deliberately accepts an EMPTY content type when the extension
    vouches for the file, which is exactly what several browsers send for `.mov`. Such a row then
    took the IMAGE path: `image_variants` returned None, the "unsupported source" branch below
    copied the raw video bytes into `high_definition` and `thumbnail`, and the item was marked
    READY — leaving a multi-megabyte .mov being served inside an <img> in the grid.

    The upload path never had this problem, because there `is_video` is handed the UploadedFile and
    can see its filename. This puts the filename back by asking the stored original for it.
    """
    return is_video(media) or is_video(media.original)


def build_derivatives(media: Media) -> bool:
    """Build stored image or video derivatives. Returns whether they are now available.

    Safe to call on the same row twice — it re-reads the original from storage and overwrites — so
    an overlapping run of the management command is harmless, as DEPLOY.md §4b requires.
    """
    media.derivative_attempts += 1
    filename = Path(media.original.name or "upload").name
    video = is_video_media(media)
    with media.original.open("rb") as original:
        source = File(original, name=filename)
        variants = video_variants(source, filename) if video else image_variants(source, filename)
    if variants is None:
        if not video:
            for field in (media.high_definition, media.thumbnail):
                with media.original.open("rb") as original:
                    field.save(filename, File(original, name=filename), save=False)
            media.derivative_state = DerivativeState.READY
            media.save(
                update_fields=["high_definition", "thumbnail", "derivative_state", "derivative_attempts"]
            )
            return True
        media.derivative_state = (
            DerivativeState.FAILED
            if media.derivative_attempts >= MAX_DERIVATIVE_ATTEMPTS
            else DerivativeState.PENDING
        )
        media.save(update_fields=["derivative_state", "derivative_attempts"])
        return False
    high_definition, thumbnail = variants
    media.high_definition.save(high_definition.name or "high-definition.mp4", high_definition, save=False)
    media.thumbnail.save(thumbnail.name or "thumbnail.jpg", thumbnail, save=False)
    media.derivative_state = DerivativeState.READY
    media.save(update_fields=["high_definition", "thumbnail", "derivative_state", "derivative_attempts"])
    return True


def pending_derivatives() -> "QuerySet[Media]":
    """Stored media still waiting for its derivatives, oldest first."""
    return Media.objects.filter(derivative_state=DerivativeState.PENDING).order_by("added_at", "pk")


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
        # DateTimeOriginal (36867) then DateTimeDigitized (36868), and nothing else. Tag 306 is
        # DateTime — the file's last-modified stamp, which a re-save in any editor rewrites to
        # today. Falling back to it gave a re-edited photo a confident, wrong "Optaget" date that
        # also drove the grid's sort order; spec/features/Photo-album.md asks for NULL instead.
        value = exif_values.get(36867) or exif_values.get(36868) or exif.get(36867) or exif.get(36868)
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


def restore(media: Media) -> None:
    """Bring media back out of the bin, into a state something can still act on.

    Resetting `status` is the load-bearing part. The only way a PENDING item reaches the bin is
    "Afvis", which sets REJECTED — and clearing `deleted_at` alone left it REJECTED and un-binned,
    a combination nothing in the app can see or reach: invisible to residents and to its own
    uploader, no Godkend/Afvis controls (those are drawn for PENDING), gone from the bin, and
    matched by neither arm of `purge_expired`. The row and all three files were stranded for good.

    A restored item goes back to PENDING rather than APPROVED so it still passes through review.
    Note that its 30-day pending clock runs from `added_at` as it always has, so restoring
    something long-abandoned puts it back in front of a manager with little time left on it.
    """
    media.deleted_at = None
    media.deleted_by = None
    if media.status == MediaStatus.REJECTED:
        media.status = MediaStatus.PENDING
    media.save(update_fields=["deleted_at", "deleted_by", "status"])


def delete(media: Media, resident: Resident, now: datetime | None = None) -> None:
    """Move media to the recoverable bin."""
    moment = now or timezone.now()
    media.deleted_at = moment
    media.deleted_by = resident
    media.save(update_fields=["deleted_at", "deleted_by"])


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
