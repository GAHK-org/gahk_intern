"""The kollegium's file archive: a folder tree in the database, bytes in object storage.

    REPLACES DROPBOX (~2 TB of photographs) AND GOOGLE DRIVE (the embedsgruppers' documents).

Two decisions shape everything here.

**The tree lives in the database; the bucket holds only bytes.** Listing a bucket with
Prefix/Delimiter is the obvious way to render a file browser, and it was rejected: it gives no
per-folder access control, no search, no ordering, no "who uploaded this", no soft delete, and it
pages slowly with a cost per request. A DB index gives all of those for the price of keeping two
things in step, which `import_arkiv` exists to do and `audit_media`'s sibling will exist to check.

**Keys are content-addressed, not path-shaped**: `arkiv/<sha256[:2]>/<sha256>`, with the *display*
name in the row. Three things follow, and together they are why the indirection earns its keep:

  * renaming and moving become DB updates - no S3 copy, and no window where an object is in two
    places or in neither;
  * the fourth copy of the same party photograph costs nothing, which across 2 TB of phone uploads
    from one weekend is not a rounding error;
  * the legacy import is restartable - re-running re-hashes and skips, so an interrupted 2 TB upload
    resumes instead of starting again.

**No extension in the key**, deliberately, even though it makes the bucket unbrowsable by eye: two
files with identical bytes and different names have to be one object or the deduplication is a
fiction. The download view puts the name back via `ResponseContentDisposition` on the presigned URL,
so the resident still gets `Sommerfest 2026.jpg`.

There is no FileField anywhere in this app. The key is derived from `sha256`, so the storage backend
never enters the schema and nothing here needs migrating if it ever changes again.
"""

from collections.abc import Iterable

from django.conf import settings
from django.db import models
from django.db.models.base import ModelBase
from django.utils import timezone

# Where an archived object lives, and where its thumbnail will live once thumbnails land.
ARCHIVE_PREFIX = "arkiv"
THUMBNAIL_PREFIX = "arkiv-thumb"


def thumbnail_key(sha256: str) -> str:
    """Where the preview for an object lives.

    KEYED BY THE ORIGINAL'S HASH, not the thumbnail's, which is why `ArchiveFile` needs no second
    digest column. Two rows sharing bytes share one thumbnail for free, the key is derivable from
    what the row already holds, and a client never gets to name it. The thumbnail is produced the
    same way every time, so "the preview of these bytes" is a well-defined thing to address.
    """
    return f"{THUMBNAIL_PREFIX}/{sha256[:2]}/{sha256}"


def object_key(sha256: str) -> str:
    """The bucket key for a hash.

    Fanned out one byte wide so no single prefix holds the whole archive. Ceph does not need this the
    way S3 once did, but a flat prefix with a million siblings is miserable for anything that ever
    has to list it by hand.
    """
    return f"{ARCHIVE_PREFIX}/{sha256[:2]}/{sha256}"


class ArchiveQuerySet(models.QuerySet):
    def alive(self) -> "ArchiveQuerySet":
        """Everything not soft-deleted. Every user-facing query starts here."""
        return self.filter(deleted_at__isnull=True)


class ArchiveFolder(models.Model):
    """A folder. The root ones are the embedsgrupper and the shared areas, managed by Inspektionen;
    everything below them is made by residents.

    ACCESS IS OWNED BY `effective_workgroup`, NOT BY `workgroup`. `workgroup` is what somebody
    declared on this folder; `effective_workgroup` is that, or the nearest ancestor's, denormalised
    on write. NULL means "every resident who can reach the feature" - which is what the photo
    archive wants, and what a Regnskabsgruppen folder must never silently become.

    Resolved on write rather than on read because the alternative is a recursive CTE on every page
    load of a tree three or four levels deep, and because a read-time walk has to fetch the whole
    ancestor chain before it can decide whether to show the row it is already holding.
    """

    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    name = models.CharField(max_length=200, verbose_name="Navn")
    # PROTECT, and here that is security rather than tidiness: SET_NULL on a deleted Workgroup would
    # turn every folder that group owned into effective_workgroup=NULL - readable by the whole
    # kollegium - silently, as a side effect of cleaning up a lookup table.
    workgroup = models.ForeignKey(
        "core.Workgroup",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="archive_folders",
        verbose_name="Embedsgruppe",
        help_text="Tom = synlig for alle beboere. Sat = kun for gruppens nuvaerende medlemmer.",
    )
    effective_workgroup = models.ForeignKey(
        "core.Workgroup",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ArchiveQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name = "Mappe"
        verbose_name_plural = "Mapper"
        constraints = [
            # Scoped to live rows, so a deleted folder does not reserve its name forever. Postgres
            # and SQLite both accept the partial index.
            models.UniqueConstraint(
                fields=["parent", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_folder_name_per_parent",
            )
        ]
        indexes = [models.Index(fields=["parent", "name"], name="folder_parent_name_idx")]

    def __str__(self) -> str:
        return self.name

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Keep the denormalised owner in step on every write.

        This covers the folder being saved. It deliberately does NOT walk the subtree - changing a
        parent's ownership has to re-resolve its descendants, and that is `services.reassign_subtree`,
        which is one query per level and has to run in a transaction. Doing it here would make every
        ordinary rename pay for a tree walk it does not need.
        """
        self.effective_workgroup_id = self.resolved_workgroup_id()
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    def resolved_workgroup_id(self) -> int | None:
        """What `effective_workgroup` should be: this folder's own, else its parent's resolved one."""
        if self.workgroup_id is not None:
            return self.workgroup_id
        parent = self.parent
        return parent.effective_workgroup_id if parent is not None else None

    def ancestors(self) -> list["ArchiveFolder"]:
        """Root-first, excluding self. Powers the breadcrumb.

        One query per level, on a tree that is a handful deep. A recursive CTE would be fewer round
        trips and considerably more machinery for a page nobody loads in a loop.
        """
        chain: list[ArchiveFolder] = []
        node = self.parent
        while node is not None:
            chain.append(node)
            node = node.parent
        chain.reverse()
        return chain


class ArchiveFile(models.Model):
    """One archived file. `sha256` is the identity; `name` is what the resident sees.

    The same bytes filed into two folders are two rows and one object, which is the entire point of
    content addressing. Deleting one row therefore must NOT delete the object - see
    `services.unreferenced_keys`, which only lets an object go once no live row references its hash.
    """

    folder = models.ForeignKey(ArchiveFolder, on_delete=models.CASCADE, related_name="files")
    name = models.CharField(max_length=255, verbose_name="Filnavn")
    # 64 hex characters. Indexed because "does any live row still reference this object" is what the
    # orphan sweep asks, and "have I already got these bytes" is what the importer asks.
    sha256 = models.CharField(max_length=64, db_index=True)
    size = models.PositiveBigIntegerField()
    content_type = models.CharField(max_length=100, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    # Who removed it. A soft delete without attribution is an argument waiting to happen in a shared
    # archive - "where did the sommerfest photos go" needs an answer, and the row is the only place
    # that answer can live once the file is out of the listing.
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Whether `thumbnail_key(sha256)` has an object behind it. A column rather than a HEAD per row,
    # because the alternative is one network round trip per file to draw a listing.
    has_thumbnail = models.BooleanField(default=False)

    objects = ArchiveQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name = "Fil"
        verbose_name_plural = "Filer"
        constraints = [
            models.UniqueConstraint(
                fields=["folder", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_file_name_per_folder",
            )
        ]
        indexes = [models.Index(fields=["folder", "name"], name="file_folder_name_idx")]

    def __str__(self) -> str:
        return self.name

    @property
    def key(self) -> str:
        return object_key(self.sha256)

    @property
    def thumb_key(self) -> str:
        return thumbnail_key(self.sha256)

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    def soft_delete(self, by: object = None) -> None:
        """Mark deleted without touching the object.

        The point of leaving Dropbox is not to lose undo. The bytes go later, and only once nothing
        else references them - see services.unreferenced_keys, which counts a soft-deleted row as a
        reference precisely so that undo has something to restore.
        """
        self.deleted_at = timezone.now()
        self.deleted_by = by  # type: ignore[assignment]
        self.save(update_fields=["deleted_at", "deleted_by"])
