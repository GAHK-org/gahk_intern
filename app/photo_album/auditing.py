"""What the audit trail keeps of the photo album.

Albums, the media in them, their tags, and the imports and merges people ask for. What the workers
work out about a file on their own (its copies, hash, size, dates, how far a job has got) is no one's
decision and is left out: an upload is recorded, its thumbnail is not.
"""

from audit import trail

from .models import (
    Album,
    AlbumDownload,
    AlbumImport,
    AlbumMerge,
    FeaturedMedia,
    Media,
    MediaView,
    StorageReport,
    Tag,
)

trail.register(Tag, "photo_album.tag", name=lambda tag: tag.name, label="Tag i fotoalbummet")
trail.register(
    Album,
    "photo_album.album",
    name=lambda album: f"{album.name} ({album.folder})",
    label="Album",
    # Where it sits in the tree, kept from `parent` by code (a sub-album also takes its root's
    # folder), and when the row was made.
    ignore={"path", "depth", "created_at"},
    labels={
        "folder": "Mappe",
        "name": "Navn",
        "parent": "Overalbum",
        "manually_locked_at": "Låst",
        "unlocked_at": "Låst op",
        "tags": "Tags",
    },
)
trail.register(
    Media,
    "photo_album.media",
    name=lambda media: media.display_name,
    label="Medie",
    # What the workers find out and make: the file's fixed id, hash, size, type and metadata, its
    # copies and their conversion, and every date the album orders by (taken, file time, the settled
    # approximation). None of it is anyone's choice.
    ignore={
        "uuid",
        "sha256",
        "hash_failed_at",
        "size",
        "high_definition",
        "thumbnail",
        "sidecar",
        "content_type",
        "is_raw",
        "derivative_state",
        "derivative_attempts",
        "derivative_started_at",
        "derivative_task_id",
        "metadata",
        "added_at",
        "captured_at",
        "capture_date_checked",
        "file_modified_at",
        "file_time_source",
        "sort_at",
        "date_source",
    },
    labels={
        "album": "Album",
        "title": "Titel",
        "filename": "Filnavn",
        "original": "Original",
        "requested_by": "Uploadet af",
        "status": "Status",
        "approved_by": "Godkendt af",
        "approved_at": "Godkendt",
        "deleted_at": "Lagt i papirkurven",
        "deleted_by": "Lagt i papirkurven af",
        "tags": "Tags",
    },
)
trail.register(
    AlbumImport,
    "photo_album.import",
    name=lambda album_import: album_import.archive_name,
    label="ZIP-import",
    # The job's own bookkeeping: its id, its archive while it waits, how far it has got and how it
    # ended. The ending is an event of its own (completed, refused, failed, interrupted).
    ignore={
        "token",
        "task_id",
        "archive",
        "state",
        "album_ids",
        "skipped",
        "errors",
        "merged",
        "error",
        "progress",
        "progressed_at",
        "created_at",
        "completed_at",
    },
    labels={
        "requested_by": "Importeret af",
        "folder": "Mappe",
        "archive_name": "ZIP-fil",
        "lock_when_done": "Låses bagefter",
        "merge_into_existing": "Tilføjes til eksisterende album",
        "album_name": "Albumnavn",
    },
)
trail.register(
    AlbumMerge,
    "photo_album.merge",
    name=lambda job: job.source_path,
    label="Fletning af album",
    # The job's own bookkeeping, as for imports; the merge itself is the event that moves the media.
    ignore={
        "task_id",
        "state",
        "result",
        "error",
        "progress",
        "progressed_at",
        "created_at",
        "completed_at",
        "notice_closed_at",
    },
    labels={
        "target": "Flettes ind i",
        "source": "Album der flettes",
        "source_path": "Album der flettes",
        "requested_by": "Bedt om af",
    },
)
trail.exempt(AlbumDownload, "a resident's ZIP to download: reading, not a change, and gone after seven days")
trail.exempt(MediaView, "how often a resident has looked at a picture: viewing, not a change")
trail.exempt(FeaturedMedia, "the dashboard's pictures of the day, drawn at random by the first visit")
trail.exempt(StorageReport, "the nightly measurement of the bucket, for the dashboard")

trail.actions(
    {
        "photo_album.album.created": "Oprettede albummet",
        "photo_album.album.renamed": "Omdøbte albummet",
        "photo_album.album.deleted": "Slettede albummet",
        "photo_album.album.locked": "Låste albummet",
        "photo_album.album.unlocked": "Låste albummet op",
        "photo_album.album.deleted_with_contents": "Slettede albummer med alt indhold (kommando på serveren)",
        "photo_album.tag.added": "Satte tag på",
        "photo_album.tag.removed": "Fjernede tag",
        "photo_album.media.uploaded": "Uploadede til albummet",
        "photo_album.media.approved": "Godkendte medier",
        "photo_album.media.rejected": "Afviste medier",
        "photo_album.media.binned": "Lagde medier i papirkurven",
        "photo_album.media.withdrawn": "Trak sin upload tilbage",
        "photo_album.media.removed": "Fjernede valgte medier",
        "photo_album.media.restored": "Hentede medier tilbage fra papirkurven",
        "photo_album.media.deleted": "Slettede medier for altid",
        "photo_album.media.purged": "Slettede medier, hvis 30 dage var gået",
        "photo_album.media.missing_removed": "Fjernede medier, hvis original manglede",
        "photo_album.media.retried": "Satte konvertering i gang igen",
        "photo_album.sidecars.applied": "Gav medier datoerne fra deres macOS-metadata",
        "photo_album.import.requested": "Bad om at importere en ZIP-fil",
        "photo_album.import.completed": "Importerede ZIP-filen",
        "photo_album.import.refused": "ZIP-filen blev afvist",
        "photo_album.import.failed": "Importen mislykkedes",
        "photo_album.import.interrupted": "Importer gik i stå og blev opgivet",
        "photo_album.merge.requested": "Bad om at flette album",
        "photo_album.merge.completed": "Flettede albummerne",
        "photo_album.merge.refused": "Fletningen blev afvist",
        "photo_album.merge.failed": "Fletningen mislykkedes",
        "photo_album.merge.interrupted": "Fletninger gik i stå og blev opgivet",
    }
)
