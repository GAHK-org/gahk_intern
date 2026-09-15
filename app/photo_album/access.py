from collections.abc import Collection
from datetime import timedelta

from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from residents.models import Residency, Role, active_period
from residents.permissions import current_resident, request_has_role

from .models import Album, Media, MediaStatus


def is_photo_group_member(request: HttpRequest) -> bool:
    if request_has_role(request, Role.ADMINISTRATOR):
        return True
    resident = current_resident(request)
    year, month = active_period()
    return Residency.objects.filter(
        resident=resident, year=year, month=month, workgroup__name__iexact="Fotogruppen"
    ).exists()


def roles_allowed(_roles: Collection[str]) -> bool:
    return True


def can_create_album(request: HttpRequest) -> bool:
    return is_photo_group_member(request)


def can_upload(request: HttpRequest, album: Album) -> bool:
    return not album.is_locked()


def can_manage_media(request: HttpRequest) -> bool:
    return is_photo_group_member(request)


def visible_media(request: HttpRequest, album: Album) -> QuerySet[Media]:
    resident = current_resident(request)
    visible = album.media.filter(deleted_at__isnull=True)
    if is_photo_group_member(request):
        return visible
    return visible.filter(status=MediaStatus.APPROVED) | visible.filter(
        status=MediaStatus.PENDING, requested_by=resident
    )


def can_delete(media: Media, request: HttpRequest) -> bool:
    if media.deleted_at is not None or media.album.is_locked():
        return False
    if media.status == MediaStatus.PENDING and media.requested_by_id == current_resident(request).pk:
        return True
    return can_manage_media(request) and media.added_at >= timezone.now() - timedelta(days=30)


def can_permanently_delete(media: Media, request: HttpRequest) -> bool:
    return (
        can_manage_media(request)
        and media.deleted_at is not None
        and media.added_at >= timezone.now() - timedelta(hours=1)
    )
