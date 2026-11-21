from collections.abc import Collection
from datetime import timedelta

from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from residents.models import Resident, Role, RoleAssignment, active_period
from residents.permissions import current_resident, request_has_role

from .models import MANUAL_MAX_ALBUM_DEPTH, Album, Media, MediaStatus


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
    """Fotogruppen's album work: albums made at once, sub-albums, imports, renaming, deleting."""
    return is_photo_group_member(request)


def can_request_album(request: HttpRequest) -> bool:
    """Anyone signed in may make an album; outside Fotogruppen it awaits approval (Album.pending)."""
    return request.user.is_authenticated


def can_approve(request: HttpRequest) -> bool:
    """The approval page (photo_album.approvals): Fotogruppen, and administrators through it."""
    return is_photo_group_member(request)


def approvers() -> QuerySet[Resident]:
    """Who is told about uploads awaiting approval: Fotogruppen this month — or the administrators
    while nobody is in it, so a request is never told to no one."""
    year, month = active_period()
    holders = RoleAssignment.objects.filter(year=year, month=month)
    group = Resident.objects.filter(
        pk__in=holders.filter(role=Role.FOTO).values("resident_id"), is_active=True
    )
    if group.exists():
        return group
    return Resident.objects.filter(
        Q(pk__in=holders.filter(role=Role.ADMINISTRATOR).values("resident_id")) | Q(is_superuser=True),
        is_active=True,
    )


def is_album_creator(request: HttpRequest, album: Album) -> bool:
    return album.created_by_id is not None and album.created_by_id == current_resident(request).pk


def can_view_album(request: HttpRequest, album: Album) -> bool:
    """A pending album is its maker's and Fotogruppen's until it is approved."""
    return not album.pending or is_photo_group_member(request) or is_album_creator(request, album)


def can_withdraw_album(request: HttpRequest, album: Album) -> bool:
    """Its maker may take back a pending album, with everything they uploaded to it."""
    return album.pending and is_album_creator(request, album)


def visible_albums(request: HttpRequest, albums: QuerySet[Album]) -> QuerySet[Album]:
    """`albums` without the pending ones this request may not see."""
    if is_photo_group_member(request):
        return albums
    return albums.filter(pending=False) | albums.filter(pending=True, created_by=current_resident(request))


def is_administrator(request: HttpRequest) -> bool:
    return request_has_role(request, Role.ADMINISTRATOR)


def can_upload(request: HttpRequest, album: Album) -> bool:
    """Not into a locked album, nor into one being merged, which is about to move or to go. A pending
    album takes its maker's uploads only, all of them to be approved with it."""
    if album.pending and not is_album_creator(request, album):
        return False
    return not album.is_locked() and album.active_merge() is None


def can_merge_album(request: HttpRequest, album: Album) -> bool:
    """Merging this album into another, which is kept (photo_album.merge): administrators only."""
    return is_administrator(request) and not album.pending and not album.is_locked()


def can_rename_album(request: HttpRequest, album: Album) -> bool:
    """Same lock policy as the media in it: a locked album keeps its name, roots and sub-albums alike."""
    return can_create_album(request) and not album.is_locked()


def can_create_sub_album(request: HttpRequest, album: Album) -> bool:
    """By hand, sub-albums go directly under a root album; deeper trees only arrive by ZIP import.

    Anyone who may upload to the album; outside Fotogruppen, the sub-album awaits approval. In an
    album awaiting approval itself, only its maker, whose request it is part of.
    """
    if album.depth >= MANUAL_MAX_ALBUM_DEPTH or album.is_locked():
        return False
    if album.pending:
        return is_album_creator(request, album)
    return can_request_album(request)


def can_view_album_bin(request: HttpRequest) -> bool:
    """One album's bin (its whole subtree): Fotogruppen, and administrators through it."""
    return can_manage_media(request)


def can_view_all_bins(request: HttpRequest) -> bool:
    """Every album's bin in one list — administrators only."""
    return is_administrator(request)


def can_manage_media(request: HttpRequest) -> bool:
    return is_photo_group_member(request)


def can_tag(request: HttpRequest) -> bool:
    """Adding and removing tags, on albums and media alike. Locks do not apply (services.add_tag)."""
    return is_photo_group_member(request)


def can_lock_album(request: HttpRequest, album: Album) -> bool:
    """Not when it is already closed — by its own lock, or by one an ancestor holds."""
    return can_manage_media(request) and not album.pending and not album.is_locked()


def can_unlock_album(request: HttpRequest, album: Album) -> bool:
    return can_manage_media(request) and album.can_be_unlocked()


def visible_media(
    request: HttpRequest, album: Album | None = None, *, subtree: bool = False
) -> QuerySet[Media]:
    """The live media this request may see: in `album`, in its whole subtree, or (no album) anywhere.

    Approved items for everyone, plus the reader's own pending uploads; Fotogruppen sees all pending.
    A pending album holds only its maker's pending uploads (approvals), so this covers it too.
    """
    resident = current_resident(request)
    if album is None:
        scope = Media.objects.all()
    elif subtree:
        scope = album.subtree_media()
    else:
        scope = album.media.all()
    visible = scope.filter(deleted_at__isnull=True)
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
