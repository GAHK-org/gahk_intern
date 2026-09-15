# Feature: Photo album

The photo album stores and presents photos and videos. Hereafter, "media" means either a photo or a video.

## Albums and storage

- Every media item belongs to exactly one album.
- An album belongs to a folder. Valid folder names are a four-digit year (`yyyy`) or `Andet`; `Andet` is shown first in the album index.
- Album names must be unique within their folder. Albums cannot be renamed and can only be deleted when empty.
- Each media item has three independently stored variants: the unmodified original upload, a compressed high-definition version, and a thumbnail. Image viewer and thumbnail variants are JPEG; video viewer variants are MP4 with a JPEG thumbnail.
- Binary files use Django's configured media storage. Production uses the S3 media bucket; media keys are grouped by album and variant as `photo-album/<album-id>/<variant>/<filename>`.

## Metadata and ordering

- The original upload is inspected for available metadata. Supported image metadata includes camera make/model and GPS coordinates, displayed as `Kamera` and `Sted`.
- `captured_at` is derived from EXIF `DateTimeOriginal`, falling back to `DateTimeDigitized`. It is `NULL` when neither is available or readable. Both standard and nested EXIF IFD layouts, including HEIC/HEIF uploads, are supported.
- The viewer displays the capture date as `Optaget`, GPS coordinates when available, the uploader, and upload time. The original date is also retained in metadata when available.
- Album grids are newest first by `captured_at`; media without a capture date use their upload time as the ordering fallback.

## Access and upload workflow

- Members of Fotogruppen and administrators can create albums, manage media, approve/reject submissions, manage the bin, and manually lock albums.
- Any signed-in resident can upload one or more photos or videos to an unlocked album. The upload form lists the chosen filenames before submission and may apply one optional title to all selected uploads.
- Uploads made by Fotogruppen members or administrators are approved immediately. Other uploads are pending until approved.
- Pending media is visible only to Fotogruppen members, administrators, and the requesting resident, and its thumbnail is marked `Afventer godkendelse`.
- Rejected media moves to the bin. Pending media that remains unapproved for 30 days is permanently deleted.
- A requesting resident may withdraw their own pending upload; other deletion and moderation actions require Fotogruppen or administrator access.

## Album locking

- An album automatically locks when its newest approved, non-deleted media was added at least 90 days ago.
- A locked album accepts no new uploads and its media cannot be deleted.
- Fotogruppen members and administrators can manually lock an otherwise unlocked album.
- Any lock can be removed only by Fotogruppen members or administrators, and only before six calendar months have elapsed since that lock took effect. Unlocking resets the automatic-lock timer; the album locks again after 90 days unless newer approved media is added first.
- When a Fotogruppen member or administrator attempts to unlock an album after that six-month window, the album page explains that it has been locked for more than six months and cannot be unlocked.

## Deletion and bin

- Fotogruppen members and administrators can delete media uploaded within the last 30 days, provided its album is not locked.
- Deletion moves media to the bin. Managers can restore binned media to its original album until automatic cleanup removes it after 30 days in the bin.
- A binned item may be permanently deleted manually only while it is less than one hour old; otherwise the scheduled 30-day cleanup removes it. All three stored variants are removed with the database record.

## Viewing experience

- The album index displays albums as folders. Album pages display media as a thumbnail grid.
- On desktop, selecting media opens a modal with the high-definition variant, metadata in a right sidebar, an original-download action, and deletion where permitted. Previous/next arrows and keyboard arrow keys navigate without looping past either end.
- On mobile, selecting media opens a full-screen viewer that shows only the media, its name, and capture date (or upload date when no capture date exists). A kebab menu contains metadata, original download, and deletion where permitted.
- Mobile users swipe left/right to browse without looping and pinch to zoom images or videos between $1\times$ and $4\times$. Swiping is disabled while zoomed.

## Development seed data

The photo-album seed generator creates albums in year and `Andet` folders, thumbnail-ready media, and lifecycle examples for recent/old uploads, binned media, and an automatically locked album.
