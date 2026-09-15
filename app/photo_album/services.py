"""State transitions for albums. Views and scheduled cleanup use these rules together."""

from datetime import datetime, timedelta
from pathlib import Path

from django.core.files.base import File
from django.utils import timezone

from residents.models import Resident

from .models import Album, Media, MediaStatus


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
    )
    media.original.save(filename, uploaded_file, save=False)
    for field in (media.high_definition, media.thumbnail):
        uploaded_file.seek(0)
        field.save(filename, File(uploaded_file), save=False)
    media.save()
    return media


def extract_metadata(uploaded_file: File) -> dict[str, str]:
    """Read EXIF when Pillow is available; unsupported files remain valid uploads."""
    try:
        from PIL import ExifTags, Image

        uploaded_file.seek(0)
        exif = Image.open(uploaded_file).getexif()
        values = {ExifTags.TAGS.get(key, str(key)): value for key, value in exif.items()}
        return {
            key: str(values[key])
            for key in ("DateTimeOriginal", "Model", "Make")
            if values.get(key) is not None
        }
    except (ImportError, OSError, ValueError):
        return {}
    finally:
        uploaded_file.seek(0)


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
    """Delete recent mistakes outright; otherwise move the media to the recoverable bin."""
    moment = now or timezone.now()
    if media.added_at >= moment - timedelta(hours=1):
        _delete_files(media)
        media.delete()
        return True
    media.deleted_at = moment
    media.deleted_by = resident
    media.save(update_fields=["deleted_at", "deleted_by"])
    return False


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
