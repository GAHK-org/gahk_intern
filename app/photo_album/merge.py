"""Merging one album into another beside it — an administrator's tool.

Everything in `source` — its media, its sub-albums, its tags — moves to `target`, and `source` goes;
`target` keeps its name, its place and its address. The two lie side by side, root albums of one
folder or sub-albums of one album, so nothing changes depth: a sub-album of `source` moves under
`target` whole, unless `target` has one of the same name, which the two are then merged into in
the same way, all the way down.

An item `target` already holds is not kept twice. It is a duplicate only if the originals are the
same byte for byte — the hash finds the candidates, the bytes decide — and, when both copies have an
AppleDouble sidecar, the sidecars are too: two sidecars that differ are two records of the file, two
dates, and both copies stay. When only one has a sidecar, the copy that stays gets it. The copy that
stays is the one that shows — live over binned, approved over awaiting approval over rejected, then
the one in `target` — and it takes the other's tags, views and days on the dashboard.

Reading the files is the slow part, so it comes first and outside any transaction (`compare`). The
merge proper (`apply`) is one transaction that only changes rows, and the files of the copies not
kept are deleted once it has committed. A worker runs it (tasks.process_album_merge): two albums of
one event imported twice is every original read twice.

Moved items keep their storage keys (models._media_key), so the album id in a key is the album the
file was first stored in, and nothing in the bucket is copied.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import CharField, Value
from django.db.models.fields.files import FieldFile
from django.db.models.functions import Concat, Substr

from residents.models import Resident

from .models import (
    Album,
    AlbumDownload,
    AlbumImport,
    AlbumMerge,
    DerivativeState,
    FeaturedMedia,
    FileTimeSource,
    Media,
    MediaStatus,
    MediaView,
)
from .services import Progress, hash_original, no_progress, settle_dates
from .storage import get_photo_album_storage

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_STATUS_RANK: dict[str, int] = {MediaStatus.APPROVED: 2, MediaStatus.PENDING: 1, MediaStatus.REJECTED: 0}


@dataclass
class MergeResult:
    moved: int = 0  # items moved over
    duplicates: int = 0  # items `target` had already, kept once
    sidecars_kept: int = 0  # of those, the sidecars carried over to the copy that stayed
    albums_merged: int = 0  # sub-albums merged into one of the same name
    albums_moved: int = 0  # sub-albums moved over whole


@dataclass
class Comparison:
    """What `compare` read: the pairs of identical originals, and the sidecars of those items."""

    identical: set[tuple[int, int]] = field(default_factory=set)  # (source item, target item)
    sidecars: dict[str, bytes | None] = field(
        default_factory=dict
    )  # storage key -> bytes, None if unreadable


def check(target: Album, source: Album) -> list[tuple[Album, Album]]:
    """The albums to merge, pair by pair, `target` and `source` first. Raises ValidationError."""
    if target.pk == source.pk:
        raise ValidationError("Et album kan ikke flettes med sig selv.")
    if target.parent_id != source.parent_id or target.folder != source.folder:
        raise ValidationError(
            "Kun albummer side om side kan flettes: to albummer i samme mappe, "
            "eller to underalbummer af samme album."
        )
    if target.is_locked() or source.is_locked():
        raise ValidationError("Begge albummer skal være låst op, før de kan flettes.")
    pairs = _pairs(target, source)
    for pair in pairs[1:]:
        for album in pair:
            if album.is_locked():
                raise ValidationError(
                    f"Underalbummet {album.name} er låst. Lås det op, før albummerne flettes."
                )
    return pairs


def _pairs(target: Album, source: Album) -> list[tuple[Album, Album]]:
    # Lower-cased as the database's unique constraint on sibling names is (models.Album.Meta).
    by_name = {child.name.lower(): child for child in target.children.all()}
    pairs = [(target, source)]
    for child in source.children.order_by("pk"):
        if (match := by_name.get(child.name.lower())) is not None:
            pairs.extend(_pairs(match, child))
    return pairs


def merge_candidates(album: Album) -> list[tuple[Album, bool]]:
    """The albums `album` could be merged with — those beside it — each with whether it is locked."""
    if album.parent_id is None:
        siblings = Album.objects.filter(folder=album.folder, parent__isnull=True)
    else:
        siblings = Album.objects.filter(parent_id=album.parent_id)
    return [(other, other.is_locked()) for other in siblings.exclude(pk=album.pk).order_by("name")]


def request_merge(target: Album, source: Album, *, by: Resident) -> AlbumMerge:
    """Queue the merge of `source` into `target`, once the transaction commits. Raises
    ValidationError for albums that cannot be merged, or while another merge in the folder runs."""
    check(target, source)
    if AlbumMerge.objects.filter(state__in=AlbumMerge.ACTIVE_STATES, target__folder=target.folder).exists():
        raise ValidationError(
            f"Der er allerede en fletning i gang i mappen {target.folder}. Vent, til den er færdig."
        )
    job = AlbumMerge.objects.create(target=target, source=source, source_name=source.name, requested_by=by)
    from .tasks import process_album_merge

    def enqueue() -> None:
        result = process_album_merge.delay(job.pk)
        AlbumMerge.objects.filter(pk=job.pk).update(task_id=result.id)

    transaction.on_commit(enqueue)
    return job


def merge_albums(target: Album, source: Album, progress: Progress = no_progress) -> MergeResult:
    """Merge `source` into `target`. Raises ValidationError for albums that cannot be merged."""
    return apply(target, source, compare(check(target, source), progress))


# --- Reading the files ------------------------------------------------------------------------


def compare(pairs: list[tuple[Album, Album]], progress: Progress = no_progress) -> Comparison:
    """Find which items of each source album are identical to items of its target album.

    An original that was never hashed is hashed now (and keeps it); one that cannot be read is no
    one's duplicate, so it is simply moved.
    """
    comparison = Comparison()
    work = [(list(target.media.all()), list(source.media.order_by("pk"))) for target, source in pairs]
    total, done = sum(len(source_items) for _, source_items in work), 0
    for target_items, source_items in work:
        for item in [*target_items, *source_items]:
            if not item.sha256 and item.original:
                try:
                    hash_original(item)
                except OSError:
                    pass
        by_hash: dict[str, list[Media]] = defaultdict(list)
        for item in target_items:
            if item.sha256:
                by_hash[item.sha256].append(item)
        for item in source_items:
            for candidate in by_hash.get(item.sha256, []) if item.sha256 else []:
                if _same_stored_bytes(item.original, candidate.original):
                    comparison.identical.add((item.pk, candidate.pk))
                    for each in (item, candidate):
                        if (name := each.sidecar.name) and name not in comparison.sidecars:
                            comparison.sidecars[name] = _read(each.sidecar)
            done += 1
            progress("Sammenligner filer", done, total)
    return comparison


def _same_stored_bytes(first: FieldFile, second: FieldFile) -> bool:
    try:
        with first.open("rb") as one, second.open("rb") as other:
            while True:
                chunk = _read_up_to(one, _CHUNK)
                if chunk != _read_up_to(other, _CHUNK):
                    return False
                if not chunk:
                    return True
    except OSError:
        logger.warning("Could not compare %s with %s", first.name, second.name, exc_info=True)
        return False


def _read_up_to(stored: FieldFile, size: int) -> bytes:
    """`size` bytes, or what is left: a read may return fewer before the end."""
    data = stored.read(size)
    while data and len(data) < size and (more := stored.read(size - len(data))):
        data += more
    return data


def _read(stored: FieldFile) -> bytes | None:
    try:
        with stored.open("rb") as opened:
            return bytes(opened.read())
    except OSError:
        logger.warning("Could not read %s", stored.name, exc_info=True)
        return None


# --- Merging ----------------------------------------------------------------------------------


def apply(target: Album, source: Album, comparison: Comparison) -> MergeResult:
    """Merge `source` into `target` in one transaction, with what `compare` found out.

    The albums are checked again, locked: anything may have changed while the files were read. An
    item or a sub-album that arrived meanwhile was not compared, so it is moved as it is.
    """
    result = MergeResult()
    unused: list[str] = []
    with transaction.atomic():
        albums = {
            album.pk: album
            for album in Album.objects.select_for_update().filter(pk__in=[target.pk, source.pk])
        }
        if len(albums) < 2:
            raise ValidationError("Et af albummerne findes ikke længere.")
        target, source = albums[target.pk], albums[source.pk]
        check(target, source)
        _merge(target, source, comparison, result, unused)
        settle_dates(target)
        transaction.on_commit(lambda: _delete_stored(unused))
    return result


def _merge(
    target: Album, source: Album, comparison: Comparison, result: MergeResult, unused: list[str]
) -> None:
    _merge_media(target, source, comparison, result, unused)
    by_name = {child.name.lower(): child for child in target.children.all()}
    for child in source.children.order_by("pk"):
        if (match := by_name.get(child.name.lower())) is not None:
            _merge(match, child, comparison, result, unused)
            result.albums_merged += 1
        else:
            _move(child, under=target)
            result.albums_moved += 1
    target.tags.add(*source.tags.all())
    AlbumDownload.objects.filter(album=source).update(album=target)
    # An import's albums are what its status page lists, and what tells the import script which
    # albums it made: it made this one too, now.
    for album_import in AlbumImport.objects.filter(album_ids__contains=[source.pk]):
        album_import.album_ids = list(
            dict.fromkeys(target.pk if pk == source.pk else pk for pk in album_import.album_ids)
        )
        album_import.save(update_fields=["album_ids"])
    source.delete()


def _move(album: Album, *, under: Album) -> None:
    """Move `album` and its subtree under `under`, beside it at the same depth: only paths change."""
    old, new = album.path, f"{under.path}{album.pk}/"
    Album.objects.filter(pk=album.pk).update(parent=under)
    Album.objects.filter(path__startswith=old).update(
        path=Concat(Value(new), Substr("path", len(old) + 1), output_field=CharField())
    )


def _merge_media(
    target: Album, source: Album, comparison: Comparison, result: MergeResult, unused: list[str]
) -> None:
    # Each of target's items, by its id, mapped to the copy of it kept so far: itself, or a better
    # copy from source that took its place.
    kept: dict[int, Media] = {}
    by_hash: dict[str, list[int]] = defaultdict(list)
    for item in target.media.all():
        kept[item.pk] = item
        if item.sha256:
            by_hash[item.sha256].append(item.pk)
    moved: list[int] = []
    for item in source.media.order_by("pk"):
        match = next(
            (
                pk
                for pk in (by_hash.get(item.sha256, []) if item.sha256 else [])
                if (item.pk, pk) in comparison.identical and _sidecars_agree(item, kept[pk], comparison)
            ),
            None,
        )
        if match is None:
            moved.append(item.pk)
        else:
            kept[match] = _keep_one(kept[match], item, target, result, unused)
    result.moved += Media.objects.filter(pk__in=moved).update(album=target)


def _sidecars_agree(item: Media, other: Media, comparison: Comparison) -> bool:
    if not item.sidecar or not other.sidecar:
        return True
    mine = comparison.sidecars.get(item.sidecar.name or "")
    theirs = comparison.sidecars.get(other.sidecar.name or "")
    return mine is not None and mine == theirs


def _rank(media: Media) -> tuple[bool, int, bool]:
    return (
        media.deleted_at is None,
        _STATUS_RANK.get(media.status, 0),
        media.derivative_state == DerivativeState.READY,
    )


def _keep_one(current: Media, item: Media, target: Album, result: MergeResult, unused: list[str]) -> Media:
    """Of two identical copies — the one kept so far for an item of `target`, and `item` from the
    source album — keep the better, with what the other had that it did not."""
    keep, drop = (item, current) if _rank(item) > _rank(current) else (current, item)
    if not keep.sidecar and drop.sidecar:
        keep.sidecar.name = drop.sidecar.name
        if (
            drop.file_time_source == FileTimeSource.SIDECAR
            and keep.file_time_source != FileTimeSource.SIDECAR
        ):
            keep.file_modified_at, keep.file_time_source = drop.file_modified_at, drop.file_time_source
        drop.sidecar.name = ""  # it is the kept copy's now, not one of the files to delete
        result.sidecars_kept += 1
    keep.album = target
    keep.save(update_fields=["album", "sidecar", "file_modified_at", "file_time_source"])
    keep.tags.add(*drop.tags.all())
    _merge_views(keep, drop)
    taken = FeaturedMedia.objects.filter(media=keep).values("date")
    FeaturedMedia.objects.filter(media=drop).exclude(date__in=taken).update(media=keep)
    unused.extend(
        name
        for stored in (drop.original, drop.high_definition, drop.thumbnail, drop.sidecar)
        if (name := stored.name)
    )
    drop.delete()
    result.duplicates += 1
    return keep


def _merge_views(keep: Media, drop: Media) -> None:
    """One resident's views of either copy are their views of the one kept."""
    views = {view.resident_id: view for view in MediaView.objects.filter(media=keep)}
    for view in MediaView.objects.filter(media=drop):
        mine = views.get(view.resident_id)
        if mine is None:
            view.media = keep
            view.save(update_fields=["media"])
            continue
        mine.count += view.count
        mine.first_viewed_at = min(mine.first_viewed_at, view.first_viewed_at)
        mine.last_viewed_at = max(mine.last_viewed_at, view.last_viewed_at)
        mine.save(update_fields=["count", "first_viewed_at", "last_viewed_at"])


def _delete_stored(keys: list[str]) -> None:
    storage = get_photo_album_storage()
    for key in keys:
        try:
            storage.delete(key)
        except Exception:
            logger.warning("Could not delete %s, a duplicate's file, after a merge", key, exc_info=True)


# --- Telling about it -------------------------------------------------------------------------


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def describe(result: dict[str, int]) -> str:
    """'12 medier flyttet hertil, 3 dubletter gemt én gang, 1 sidecar-fil bevaret, …'"""
    parts = [f"{_count(result.get('moved', 0), 'medie', 'medier')} flyttet hertil"]
    if duplicates := result.get("duplicates", 0):
        parts.append(f"{_count(duplicates, 'dublet', 'dubletter')} gemt én gang")
    if sidecars := result.get("sidecars_kept", 0):
        parts.append(f"{_count(sidecars, 'sidecar-fil', 'sidecar-filer')} bevaret")
    if merged := result.get("albums_merged", 0):
        parts.append(f"{_count(merged, 'underalbum', 'underalbummer')} flettet med et af samme navn")
    if moved := result.get("albums_moved", 0):
        parts.append(f"{_count(moved, 'underalbum', 'underalbummer')} flyttet hertil")
    return ", ".join(parts)
