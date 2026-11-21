"""Approving what residents outside Fotogruppen upload, and the albums they make.

A request is everything one resident has waiting in one album tree: their pending uploads anywhere
under a root album, sub-albums included, and the albums in it they made (Album.pending) — the root
album itself, or sub-albums of one that exists. Fotogruppen decides a request as a whole: every item
it was shown is approved, except those picked to be rejected, and each new album is approved under
the name (and, a root album, in the folder) the approver gives it, or merged into an album that
exists. So a party's 80 photos are one decision rather than 80, and two residents' photos of the
same party are two, as each may be judged on its own.

Requests are not stored. They are worked out from the pending rows, so an item approved on its
album page, withdrawn by its uploader or restored from the bin leaves no request to keep in step.

Only what the approver was shown is decided. An item or an album that arrived while they looked
stays pending, in a request of its own.

A rejected item goes to the bin, as one rejected anywhere else, where Fotogruppen can restore it for
30 days. A new album that holds only binned items is no request: it stays pending and hidden until
an item is restored (or its maker uploads again), or the bin empties and the album goes with it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from residents.models import Resident

from . import merge, notify, services
from .models import (
    PURGE_AFTER,
    Album,
    AlbumMerge,
    AlbumMergeState,
    Media,
    MediaStatus,
    normalize_name,
)
from .storage import get_photo_album_storage

# A request is in the daily reminder once it has waited this long: the new-request notification
# has told about it already.
REMIND_AFTER = timedelta(days=1)
# The reminder is marked Høj when a request's first item goes for good within this (PURGE_AFTER).
URGENT_BEFORE = timedelta(days=3)


def root_id(album: Album) -> int:
    return int(album.path.strip("/").split("/")[0])


@dataclass
class Request:
    album: Album  # the root album
    uploader: Resident | None  # None only for an empty new album whose maker was deleted
    media: list[Media] = field(default_factory=list)
    # The albums in it its uploader made, awaiting approval: shallowest first, then by name.
    new_albums: list[Album] = field(default_factory=list)
    failed_merges: list[AlbumMerge] = field(default_factory=list)

    @property
    def new_album(self) -> bool:
        return bool(self.new_albums)

    @property
    def tops(self) -> list[Album]:
        """The new albums decided on their own: those not inside another new album, which follow it."""
        ids = {album.pk for album in self.new_albums}
        return [album for album in self.new_albums if album.parent_id not in ids]

    @property
    def uploader_pk(self) -> int:
        """In the request's address: 0 for an album whose maker was deleted."""
        return self.uploader.pk if self.uploader is not None else 0

    @property
    def first_at(self) -> datetime:
        """Since when it has waited: its first item, or a new album it has nothing in yet."""
        times = [item.added_at for item in self.media]
        times += [album.created_at for album in self.new_albums if not getattr(album, "media_total", 0)]
        return min(times)

    @property
    def last_at(self) -> datetime:
        return max([item.added_at for item in self.media], default=self.first_at)

    @property
    def expires_at(self) -> datetime:
        """When its first item, or an empty new album, goes for good (services.purge_expired)."""
        return self.first_at + PURGE_AFTER

    @property
    def videos(self) -> int:
        return sum(1 for item in self.media if services.is_video_media(item))

    @property
    def photos(self) -> int:
        return len(self.media) - self.videos


def _pending() -> Q:
    return Q(status=MediaStatus.PENDING, deleted_at__isnull=True)


def pending_requests() -> list[Request]:
    """Every request waiting, the longest-waiting first.

    A new album being merged into another as it is approved is not waiting any more, nor is what is
    in it; one whose merge failed is, again, with the reason.
    """
    merging = [
        job.source.path
        for job in AlbumMerge.objects.filter(
            state__in=AlbumMerge.ACTIVE_STATES, source__pending=True
        ).select_related("source")
        if job.source is not None
    ]

    def being_merged(path: str) -> bool:
        return any(path.startswith(prefix) for prefix in merging)

    new_albums = [
        album
        for album in Album.objects.filter(pending=True)
        .annotate(media_total=Count("media"))
        .select_related("created_by")
        .order_by("depth", "name", "pk")
        if not being_merged(album.path)
    ]
    items = [
        item
        for item in Media.objects.filter(_pending())
        .select_related("album", "requested_by")
        .order_by("added_at", "pk")
        if not being_merged(item.album.path)
    ]
    # A request is made by a pending item, or by a new album with nothing in it yet; a new album that
    # holds only binned items (rejected) joins its maker's request when there is one.
    uploaders: dict[tuple[int, int | None], Resident | None] = {}
    for item in items:
        uploaders[(root_id(item.album), item.requested_by_id)] = item.requested_by
    for album in new_albums:
        if not album.media_total:
            uploaders.setdefault((root_id(album), album.created_by_id), album.created_by)
    roots = Album.objects.in_bulk({key[0] for key in uploaders})
    by_key = {key: Request(roots[key[0]], uploader) for key, uploader in uploaders.items()}
    for item in items:
        by_key[(root_id(item.album), item.requested_by_id)].media.append(item)
    for album in new_albums:
        if (request := by_key.get((root_id(album), album.created_by_id))) is not None:
            request.new_albums.append(album)
    tops = {album.pk: request for request in by_key.values() for album in request.tops}
    latest: dict[int, AlbumMerge] = {}
    for job in (
        AlbumMerge.objects.filter(source_id__in=tops).select_related("target").order_by("created_at", "pk")
    ):
        latest[job.source_id] = job  # type: ignore[index]
    for source_id, job in latest.items():
        if job.state == AlbumMergeState.FAILED:
            tops[source_id].failed_merges.append(job)
    return sorted(by_key.values(), key=lambda request: (request.first_at, request.album.pk))


def _paths(albums: list[Album]) -> dict[int, list[str]]:
    """Each album's names from the folder down, from one query."""
    ids = {int(part) for album in albums for part in album.path.strip("/").split("/")}
    names = dict(Album.objects.filter(pk__in=ids).values_list("pk", "name"))
    return {
        album.pk: [names.get(int(part), "?") for part in album.path.strip("/").split("/")] for album in albums
    }


def find(album_pk: int, uploader_pk: int) -> Request | None:
    """One request, by its address, with its items in album order, then by name — or None."""
    for request in pending_requests():
        if request.album.pk == album_pk and request.uploader_pk == uploader_pk:
            paths = _paths([item.album for item in request.media] + request.new_albums)
            for item in request.media:
                item.album_path = paths[item.album_id]  # type: ignore[attr-defined]
            for album in request.new_albums:
                album.display = paths[album.pk]  # type: ignore[attr-defined]
            request.media.sort(
                key=lambda item: (
                    [name.lower() for name in item.album_path],  # type: ignore[attr-defined]
                    services._natural_key(item.display_name),
                    item.pk,
                )
            )
            return request
    return None


def is_new_request(album: Album, uploader: Resident) -> bool:
    """Whether a pending upload to `album`, or a new album in it, would start a request: Fotogruppen
    hears of each once.

    Asked before the upload is stored or the album made. Two at once may both say yes; the
    notification is grouped (notify.approval_requested), so the second only folds into the first.
    """
    root = album if album.parent_id is None else Album.objects.get(pk=root_id(album))
    in_tree = Q(path__startswith=root.path)
    if Media.objects.filter(_pending(), requested_by=uploader, album__path__startswith=root.path).exists():
        return False
    return not Album.objects.filter(in_tree, pending=True, created_by=uploader, media__isnull=True).exists()


# --- Deciding -----------------------------------------------------------------------------------


@dataclass
class AlbumChoice:
    """What becomes of one new album (Request.tops): approved under `name`, and a root album in
    `folder` — or, with `merge_into`, merged into that album, in any folder."""

    name: str = ""
    folder: str = ""
    merge_into: Album | None = None


@dataclass
class Decision:
    approved: int = 0
    rejected: int = 0
    left: int = 0  # items of the request that arrived after it was shown, still waiting
    root: Album | None = None  # the request's root album as it is now; None once deleted
    kept: list[tuple[str, str, Album]] = field(default_factory=list)  # (old name, old folder, album)
    merges: list[tuple[str, Album, AlbumMerge]] = field(default_factory=list)  # (old name, target, job)


def _start(request: Request, shown_albums: set[int]) -> tuple[Album, dict[int, Album]]:
    """Lock the request's root album and its new albums shown, checking those are still undecided."""
    root = Album.objects.select_for_update().filter(pk=request.album.pk).first()
    if root is None:
        raise ValidationError("Albummet findes ikke længere.")
    wanted = shown_albums & {album.pk for album in request.new_albums}
    albums = {
        album.pk: album for album in Album.objects.select_for_update().filter(pk__in=wanted, pending=True)
    }
    merging = AlbumMerge.objects.filter(state__in=AlbumMerge.ACTIVE_STATES, source_id__in=albums)
    if set(albums) != wanted or merging.exists():
        raise ValidationError("Det er allerede afgjort.")
    return root, albums


def _items(request: Request, root: Album, shown: set[int]) -> tuple[list[Media], int]:
    """The request's items shown, locked, and how many more it has now that were not."""
    waiting = Media.objects.filter(
        _pending(), requested_by=request.uploader, album__path__startswith=root.path
    )
    items = list(waiting.filter(pk__in=shown).select_related("album").select_for_update(of=("self",)))
    return items, waiting.exclude(pk__in=shown).count()


def approve(
    request: Request,
    *,
    by: Resident,
    shown: set[int],
    rejected: set[int],
    shown_albums: set[int] | None = None,
    choices: dict[int, AlbumChoice] | None = None,
    note: str = "",
) -> Decision:
    """Approve every item of `request` in `shown` but those in `rejected`, which go to the bin.

    Each new album shown (`shown_albums`, Request.tops) is approved as `choices` says, with the new
    albums inside it: under a name its siblings do not have, or merged into an album that exists, by
    a worker (photo_album.merge) that approves what it moves in the same transaction. Raises
    ValidationError, having changed nothing, for a name taken, a folder that is no folder or a merge
    that cannot be.
    """
    shown_albums = shown_albums if shown_albums is not None else {album.pk for album in request.new_albums}
    choices = choices or {}
    decision = Decision()
    with transaction.atomic():
        root, albums = _start(request, shown_albums)
        items, decision.left = _items(request, root, shown)
        approving = [item for item in items if item.pk not in rejected]
        for item in items:
            if item.pk in rejected:
                services.reject(item, by)
        decision.rejected, decision.approved = len(items) - len(approving), len(approving)
        merged: set[int] = set()
        for top in [album for album in request.tops if album.pk in albums]:
            top = albums[top.pk]
            inside = [
                album.pk for album in albums.values() if album.path.startswith(top.path) and album != top
            ]
            choice = choices.get(top.pk, AlbumChoice())
            if choice.merge_into is not None:
                moving = [item.pk for item in approving if item.album.path.startswith(top.path)]
                job = merge.request_merge(
                    choice.merge_into, top, by=by, approve_media_ids=moving, approve_album_ids=inside
                )
                merged.update(moving)
                decision.merges.append((top.name, choice.merge_into, job))
            else:
                old_name, old_folder = top.name, top.folder
                _approve_album(top, name=choice.name, folder=choice.folder, inside=inside)
                decision.kept.append((old_name, old_folder, top))
        for item in approving:
            if item.pk not in merged:
                services.approve(item, by)
        root.refresh_from_db()
        decision.root = root
        _tell(request, by, decision, note=note)
    return decision


def _approve_album(album: Album, *, name: str, folder: str, inside: list[int]) -> None:
    """Approve a new album under `name` (and a root album in `folder`), with the new albums inside it."""
    old_folder = album.folder
    album.name = normalize_name(name.strip()) or album.name
    if album.parent_id is None:
        album.folder = normalize_name(folder.strip()) or album.folder
    album.pending = False
    album.full_clean(exclude=["path"], validate_constraints=False)
    try:
        album.validate_constraints()
        with transaction.atomic():
            album.save(update_fields=["name", "folder", "pending"])
            Album.objects.filter(pk__in=inside).update(pending=False)
    except (ValidationError, IntegrityError) as exc:
        where = f"i {album.folder}" if album.parent_id is None else f"i {album.parent.name}"  # type: ignore[union-attr]
        raise ValidationError(
            f"Der findes allerede et album, der hedder {album.name}, {where}. "
            "Giv det nye album et andet navn, eller flet det ind i det, der findes."
        ) from exc
    if album.folder != old_folder:
        services.settle_dates(album)  # what is believable depends on the folder's year


def reject(
    request: Request, *, by: Resident, shown: set[int], shown_albums: set[int] | None = None, note: str = ""
) -> Decision:
    """Reject the request: every item shown goes to the bin, new albums' too, where Fotogruppen can
    restore them for 30 days. A new album shown with nothing in it at all is deleted; one with binned
    items stays, hidden, until they are restored or gone (purge_expired_albums)."""
    shown_albums = shown_albums if shown_albums is not None else {album.pk for album in request.new_albums}
    decision = Decision()
    with transaction.atomic():
        root, albums = _start(request, shown_albums)
        items, decision.left = _items(request, root, shown)
        for item in items:
            services.reject(item, by)
        decision.rejected = len(items)
        for album in sorted(albums.values(), key=lambda album: -album.depth):
            if not album.media.exists() and not album.children.exists():
                album.delete()
        decision.root = Album.objects.filter(pk=root.pk).first()
        _tell(request, by, decision, note=note, rejected=True)
    return decision


def _tell(request: Request, by: Resident, decision: Decision, *, note: str, rejected: bool = False) -> None:
    uploader = request.uploader
    if uploader is not None and uploader.pk != by.pk:
        transaction.on_commit(
            lambda: notify.approval_decided(uploader, request, decision, note=note, rejected=rejected)
        )


def delete_album(album: Album) -> int:
    """Delete an album awaiting approval, with its sub-albums and everything in them, binned items
    too: withdrawn by its maker. The files go once the deletion has committed. Returns how many
    items went."""
    albums = list(album.subtree().order_by("-depth"))
    if not all(each.pending for each in albums):
        raise ValidationError("Kun et album, der venter på godkendelse, kan slettes sådan.")
    media = list(Media.objects.filter(album__in=albums))
    keys = [
        stored.name
        for item in media
        for stored in (item.original, item.high_definition, item.thumbnail, item.sidecar)
        if stored and stored.name
    ]
    Media.objects.filter(pk__in=[item.pk for item in media]).delete()
    for each in albums:
        each.delete()
    transaction.on_commit(lambda: _delete_stored(keys), robust=True)
    return len(media)


def _delete_stored(keys: list[str]) -> None:
    storage = get_photo_album_storage()
    for key in keys:
        storage.delete(key)


def purge_expired_albums(now: datetime | None = None) -> int:
    """Delete albums awaiting approval left empty for PURGE_AFTER: never uploaded to, or whose
    uploads were purged unapproved or from the bin. Run after services.purge_expired. Returns how
    many went."""
    moment = now or timezone.now()
    count = 0
    for album in Album.objects.filter(pending=True, created_at__lt=moment - PURGE_AFTER).order_by("-depth"):
        if not album.media.exists() and not album.children.exists():
            album.delete()
            count += 1
    return count


def remind(now: datetime | None = None) -> int:
    """The daily reminder to Fotogruppen of the requests still waiting. Returns how many there are."""
    moment = now or timezone.now()
    waiting = [request for request in pending_requests() if request.first_at <= moment - REMIND_AFTER]
    if waiting:
        notify.approvals_waiting(waiting, urgent=waiting[0].expires_at - moment <= URGENT_BEFORE)
    return len(waiting)
