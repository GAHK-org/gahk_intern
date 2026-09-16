from collections.abc import Collection
from datetime import timedelta

from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from residents.models import Role
from residents.permissions import current_resident, request_has_role

from .models import Album, Media, MediaStatus


def is_photo_group_member(request: HttpRequest) -> bool:
    """Fotogruppen, as an ordinary role — like repper or vicevaert, not a workgroup name lookup.

    `Role.FOTO` is granted by WORKGROUP_ROLE the same way every other embedsgruppe role is, so
    indstilling assigning somebody to Fotogruppen for the month is what makes this true, and the
    monthly sync in residents.views._sync_month_roles handles it with no special case here.

    `administrator` needs no mention: real_roles() returns every role for an admin or superuser.

    This replaces a Residency query against a workgroup literally named "Fotogruppen" — a row that
    existed in no database, so the check could only ever pass for administrators. It also means the
    answer now comes from the role set already memoised on the request, rather than a database
    query per media item.
    """
    return request_has_role(request, Role.FOTO)


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


def can_delete(media: Media, request: HttpRequest) -> bool:
    """Either way a live item can leave the album: its uploader withdraws it, or a curator bins it.

    Two different actions behind one button, which is why views.delete branches afterwards — see
    can_withdraw for which is which.
    """
    return can_withdraw(media, request) or can_curate_delete(media, request)


def _is_deletable(media: Media) -> bool:
    """Neither route applies to an item that is already gone or whose album has closed.

    The lock is a property of the ALBUM, not of the reader, and no role gets past it: once an album
    locks, its media cannot be deleted by anyone (spec/features/Photo-album.md). That is why it sits
    here rather than in either of the role checks below.
    """
    return media.deleted_at is None and not media.album.is_locked()


def can_withdraw(media: Media, request: HttpRequest) -> bool:
    """Take back your OWN upload before anyone has reviewed it.

    Not a curator's power and not role-gated at all: any resident may retract what they submitted,
    while it is still pending. It is a hard delete rather than a trip to the bin, because nothing
    has been accepted into the album yet — views.delete reads this to choose between the two.
    """
    return (
        _is_deletable(media)
        and media.status == MediaStatus.PENDING
        and media.requested_by_id == current_resident(request).pk
    )


def can_curate_delete(media: Media, request: HttpRequest) -> bool:
    """Fotogruppen removing somebody else's media, within 30 days of it being uploaded."""
    return (
        _is_deletable(media)
        and can_manage_media(request)
        and media.added_at >= timezone.now() - timedelta(days=30)
    )


def can_permanently_delete(media: Media, request: HttpRequest) -> bool:
    """The hour runs from the UPLOAD, not from the binning. Deliberate — do not "fix" it.

    spec/features/Photo-album.md reads "a binned item may be permanently deleted manually only while
    it is less than one hour old", where "it" is the item: manual purge exists to undo a mistake
    somebody just made, not to give managers a way to erase the album's history on demand.

    The consequence is intended: anything binned more than an hour after it was uploaded leaves only
    via the 30-day sweep, so for most real deletions this button never appears at all. That is the
    point — the bin is meant to be recoverable, and the 30 days are the recovery window.
    """
    return (
        can_manage_media(request)
        and media.deleted_at is not None
        and media.added_at >= timezone.now() - timedelta(hours=1)
    )
