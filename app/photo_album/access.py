from collections.abc import Collection
from datetime import timedelta

from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from residents.models import Residency, Role, active_period
from residents.permissions import current_resident, request_has_role

from .models import Album, Media, MediaStatus

# Attribute the Fotogruppen answer is memoised under, on the request. Same pattern, and the same
# reason, as _REAL_ROLES_MEMO in residents/permissions.py: an album page asks this once per media
# item (via can_delete), and without the memo a 300-photo album ran 300 identical Residency
# existence queries. The membership cannot change inside one request.
_PHOTO_GROUP_MEMO = "_gahk_photo_group_member"


def is_photo_group_member(request: HttpRequest) -> bool:
    cached = getattr(request, _PHOTO_GROUP_MEMO, None)
    if cached is not None:
        return cached
    if request_has_role(request, Role.ADMINISTRATOR):
        member = True
    else:
        resident = current_resident(request)
        year, month = active_period()
        member = Residency.objects.filter(
            resident=resident, year=year, month=month, workgroup__name__iexact="Fotogruppen"
        ).exists()
    setattr(request, _PHOTO_GROUP_MEMO, member)
    return member


def roles_allowed(_roles: Collection[str]) -> bool:
    return True


def can_create_album(request: HttpRequest) -> bool:
    return is_photo_group_member(request)


def can_upload(request: HttpRequest, album: Album) -> bool:
    return not album.is_locked()


def can_manage_media(request: HttpRequest) -> bool:
    return is_photo_group_member(request)


def can_lock_album(request: HttpRequest, album: Album) -> bool:
    return can_manage_media(request) and not album.is_locked()


def can_unlock_album(request: HttpRequest, album: Album) -> bool:
    return can_manage_media(request) and album.can_be_unlocked()


def visible_media(request: HttpRequest, album: Album) -> QuerySet[Media]:
    resident = current_resident(request)
    visible = album.media.filter(deleted_at__isnull=True)
    if is_photo_group_member(request):
        return visible
    return visible.filter(status=MediaStatus.APPROVED) | visible.filter(
        status=MediaStatus.PENDING, requested_by=resident
    )


def can_delete(media: Media, request: HttpRequest, *, album_locked: bool | None = None) -> bool:
    """`album_locked` lets a listing pass the answer it already has.

    Album.is_locked() runs a query of its own (the latest approved item decides), so asking it once
    per media item made an album page cost one query per photo on top of everything else. Every item
    in a listing shares one album, so the caller computes it once. None means "work it out".
    """
    if media.deleted_at is not None:
        return False
    if album_locked if album_locked is not None else media.album.is_locked():
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
