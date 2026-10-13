# Feature: Photo album

The photo album stores and presents photos and videos. Hereafter, "media" means either a photo or a video.

## Albums and the album tree

- Every media item belongs to exactly one album.
- Albums form a tree. A root album belongs to a folder; valid folder names are a four-digit year (`yyyy`) or `Andet`. The album index shows `Andet` first, then the years newest first. A sub-album has a parent album and inherits its folder.
- The tree is at most five levels deep: a root album (level 0) down to level 4. By hand, Fotogruppen members and administrators can create sub-albums directly under a root album only; deeper trees arrive by ZIP import.
- Album names behave like folder names on a computer: sibling names (root albums in the same folder, or sub-albums of the same parent) are unique ignoring case, and names are stored Unicode-normalised (NFC) so that `å` from a Mac and from Windows is one name. A name may not contain `/` or `\`, control characters, leading or trailing spaces, or a trailing dot.
- Albums can be renamed — root albums and sub-albums alike — under the same lock policy as their media: a locked album keeps its name. Albums cannot be moved to another parent or folder.
- An album can only be deleted when it has no sub-albums and no media, including media in the bin.
- Internally each album stores a materialised path of primary keys (`/12/34/56/`), never names, so renaming never touches stored keys or paths; breadcrumbs and ZIP folder names are always looked up fresh.

## Storage

- Each media item has three independently stored variants: the unmodified original upload, a compressed high-definition version (images at most 1600 px, video as viewer-sized MP4), and a thumbnail (320 px JPEG). Image high-definition variants are JPEG, except an animated GIF, whose high-definition variant stays an animated GIF (the file itself when it fits within 1600 px, otherwise every frame scaled down with its timing and loop count kept). Its thumbnail is a JPEG of the first frame.
- Binary files use the photo album's own storage (the S3 media bucket in production). Keys are `photo-album/<album-id>/<media-uuid>/<variant>/<filename>`; the per-item UUID directory lets one album hold any number of files with the same name.
- A file keeps its exact original filename (`Media.filename`), which is the name it gets back in every download. The name in the storage key is cosmetic.
- Derivative keys are immutable: a rebuild writes new keys rather than overwriting existing ones.
- Each original's SHA-256 is stored for duplicate detection. The hash only finds candidates: a file counts as a duplicate only if its name matches and its bytes are identical, compared byte for byte, so a hash collision can never drop a file. Rows uploaded before hashing existed are filled in by a nightly job (02:40) that reads originals for at most 20 minutes per night; `manage.py backfill_photo_album_hashes` does the same in one go.

### Local cache

- When `PHOTO_ALBUM_CACHE_DIR` is set, thumbnails and image high-definition variants are copied to local disk on first request and served from there afterwards (`Cache-Control: private, max-age=86400`), after the same visibility check as any other request. Originals and video high-definition variants are never cached; they redirect to object storage, which supports range requests.
- The cache is bounded (`PHOTO_ALBUM_CACHE_THUMBNAIL_MB`, default 2048; `PHOTO_ALBUM_CACHE_HD_MB`, default 3072) and evicts least-recently-used files in the background. A failed copy falls back to the storage redirect. In production it lives on the `photo-album-cache` volume so it survives redeploys. Local development has it on too (`task dev`: the same volume on `web`; `task dev:local`/`debug`: `app/cache/`), so it serves pictures as production does; the test suite leaves it off.

## Accepted files

- Images: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.heic`, `.heif`, `.avif`, `.tif`, `.tiff`, `.bmp`. Videos: `.mp4`, `.m4v`, `.mov`, `.webm`, `.avi`, `.mkv`, `.3gp`, `.3g2`, `.mts`, `.m2ts`, `.mpg`, `.mpeg`, `.wmv`, `.ogv`. SVG and RAW formats are not accepted.
- Both the extension and the content are checked; an image that cannot be decoded is refused. Size limits are `PHOTO_ALBUM_IMAGE_MAX_MB` (default 50) and `PHOTO_ALBUM_VIDEO_MAX_MB` (default 1000).

## Metadata and ordering

- The original upload is inspected for available metadata. Supported image metadata includes camera make/model and GPS coordinates, displayed as `Kamera` and `Sted`. `Sted` links to the location on OpenStreetMap.
- `captured_at` is derived from EXIF `DateTimeOriginal`, falling back to `DateTimeDigitized`, or from a video's container metadata. Both standard and nested EXIF IFD layouts, including HEIC/HEIF uploads, are supported.
- `file_modified_at` is the file's own last-modified time as reported by the browser or the ZIP entry.
- Media is ordered newest first by `sort_at`: `captured_at`, else `file_modified_at`, else upload time.
- The viewer displays the capture date as `Optaget`, the location when available, the album path, the uploader, upload time, and view counts.

## Access and upload workflow

- Members of Fotogruppen and administrators can create, rename and delete albums, create sub-albums, import ZIP files, manage media, approve/reject submissions, view album bins, manually lock albums, and add or remove tags.
- Any signed-in resident can upload one or more photos or videos to an unlocked album. The upload form lists the chosen filenames before submission and may apply one optional title to all selected uploads.
- An upload that is an exact copy (same name, same hash, same bytes) of a live item in the same album is skipped with a message. An upload whose content already exists in other albums is stored, with a warning naming those albums.
- Uploads made by Fotogruppen members or administrators are approved immediately. Other uploads are pending until approved.
- Pending media is visible only to Fotogruppen members, administrators, and the requesting resident, and its thumbnail is marked `Afventer godkendelse`.
- Rejected media moves to the bin. Pending media that remains unapproved for 30 days is permanently deleted.
- A requesting resident may withdraw their own pending upload; other deletion and moderation actions require Fotogruppen or administrator access.

## Album locking

- A lock covers the album that holds it and its whole subtree. Any album may be locked on its own: locking a sub-album leaves its parent and siblings open; locking a root album closes the whole tree. An album is locked when it or any ancestor holds a lock, and the album page names the album holding it.
- An album automatically locks when the newest approved, non-deleted media anywhere in its subtree was added at least 90 days ago.
- A locked album accepts no new uploads or sub-albums, cannot be renamed, and its media cannot be deleted. Tags are not subject to locking: they can be added to and removed from a locked album and its media.
- Fotogruppen members and administrators can manually lock an otherwise unlocked album.
- An album's own lock can be removed only by Fotogruppen members or administrators, and only before six calendar months have elapsed since that lock took effect. A lock inherited from an ancestor can only be lifted on that ancestor. Unlocking resets the automatic-lock timer; the album locks again after 90 days unless newer approved media is added first.
- When a Fotogruppen member or administrator attempts to unlock an album after that six-month window, the album page explains that it has been locked for more than six months and cannot be unlocked.

## Tags

- An album or a single media item has any number of tags, including none.
- A tag name is 1–30 characters of letters (including `æøå`), digits, `.`, `-` and `_`; no spaces or other characters. Tags are one per name ignoring case: adding `Jul` where `jul` exists reuses `jul`, and the first spelling sticks. Names are stored Unicode-normalised (NFC).
- An album's tags apply to its whole subtree: every media item in the album and its nested albums carries them. The album page and the viewer show an item's own tags first, then the inherited ones (dashed, with the album they come from as a tooltip). An inherited tag can only be removed on the album that holds it.
- Only Fotogruppen members and administrators add and remove tags, on the album page and in the viewer (in the kebab menu on mobile). The input is a dropdown of existing tags, searched as you type (prefix matches first). Enter or a click picks a suggestion, or adds the typed name as a new tag. Arrow keys move through the suggestions instead of browsing media.
- A tag that nothing carries any more stays in the database but is never suggested. Administrators manage tags in the Django admin.
- Every tag chip links to the timeline filtered on that tag.

## Deletion and bins

- Fotogruppen members and administrators can delete media uploaded within the last 30 days, provided its album is not locked.
- Deletion moves media to the bin. Each album has its own bin, covering its whole subtree, visible to Fotogruppen members and administrators. Administrators also have one bin listing every album's binned media.
- Managers can restore binned media to its original album until automatic cleanup removes it after 30 days in the bin. Restoring returns to the album's bin.
- A binned item may be permanently deleted manually only while it is less than one hour old; otherwise the scheduled 30-day cleanup removes it. All stored variants are removed with the database record.
- On the server, `manage.py delete_photo_album --folder <folder>` or `--album <id>` deletes a whole folder, or an album with its sub-albums, regardless of locks: every media item (binned ones too), every stored file under the albums' `photo-album/<album id>/` prefixes (leftovers included), every download ZIP that holds any of the media, and the local cache copies. It lists what it will delete and asks for a random 8-character code to be typed back; a wrong code deletes nothing. It refuses outright when any album in scope was created, or any media uploaded, on or after 2026-10-01; `PHOTO_ALBUM_DELETE_PROTECTED_FROM` moves that date, but a date in the future is refused. It stops if anything is added while it waits for the code, and fails loudly if stored files remain afterwards.

## Viewing experience

- The album index displays root albums as folders with a cover (the newest thumbnail in the subtree) and a media count for the whole subtree. Album pages show a breadcrumb, the sub-albums, and the album's own media as a thumbnail grid; `Alle medier, også i underalbummer` (`?alle=1`) includes the media of every sub-album.
- Album cards are a uniform grid (two per row on a phone) with the cover on top; a long name wraps onto two lines and is cut off with an ellipsis, with the full name as a tooltip. Long album titles and breadcrumbs wrap instead of overflowing.
- An album's actions — download, select, bin, rename, lock, delete and `Nyt underalbum` — sit in one row in the album header; download, rename and `Nyt underalbum` open as small dropdowns.
- The timeline (`Tidslinje`) shows everything the reader may see across all albums, newest first, with a jump to any year.
- The timeline can be filtered on tags (`?tag=<name>`, repeatable). The filter shows media tagged directly or through an album in its lineage. Several tags combine with AND, so an item must carry all of them. Active filters show as chips that remove the filter when clicked. The tag input searches as you type, and paging and year jumps keep the filter. Any signed-in resident can filter; visibility rules apply as usual.
- Grids load 60 items at a time and scroll infinitely in both directions, paged by `(sort_at, pk)` so pages do not shift as uploads arrive.
- `?media=<pk>` on an album page is a deep link: the grid opens at that item, the viewer opens on it, and the address follows the viewer as the reader browses.
- On desktop, selecting media opens a modal with the high-definition variant, metadata in a right sidebar, an original-download action, and deletion where permitted. Previous/next arrows and keyboard arrow keys navigate without looping past either end.
- On mobile, selecting media opens a full-screen viewer that shows only the media, its name, and capture date (or upload date when no capture date exists). A kebab menu contains metadata, original download, and deletion where permitted.
- Mobile users swipe left/right to browse without looping and pinch to zoom images or videos between $1\times$ and $4\times$. Swiping is disabled while zoomed.
- Viewer details are fetched when an item is opened, not rendered into every grid tile.

### View counts

- The viewer reports a view explicitly once the full-size image or the video has loaded; thumbnails and prefetches are not views.
- Each item shows the number of unique viewers and the total number of views. Repeated views by the same resident within 10 minutes count once.

### Selection

- `Vælg` on an album page or the timeline turns on selection mode. Selected items can be downloaded together (original or HD); Fotogruppen members and administrators can also approve, reject or delete them.
- Each selected item is checked as the single-item action would check it; items the reader may not act on are left alone and reported, never a reason to refuse the rest.

## Downloads

- Any signed-in resident can request a ZIP of an album or of a selection, as originals or as compressed HD. ZIPs are built in the background, privately for the requester, and listed under `Mine downloads`, where they remain for 7 days. Requesting the same files and variant again while a build is pending or ready reuses it.
- An album with sub-albums offers a choice in the download menu: `Med underalbummer` (the default) or `Kun dette album`.
- The ZIP mirrors the album tree: sub-albums become folders below the downloaded album; a timeline selection is placed under its folder (year) and full album path. Files keep their original names. Files that would collide in one folder (ignoring case) are numbered `name_01.ext`, `name_02.ext`, … and listed in `_OMDØBTE_FILER.txt` (the underscore sorts it first in most file browsers).
- A build that a worker abandons is marked failed after one hour.

## ZIP import

- Fotogruppen members and administrators can import a ZIP file into a folder as a new album tree. The import runs in the background; the import page shows its progress and result. When the upload reaches 100%, a spinner shows that the server is still working until the import is done.
- The import is all-or-nothing. A first pass reads the whole archive and collects every problem — unsupported or unreadable files, files that are too large, invalid or case-colliding folder names, folders more than five levels deep, an existing root album with the same name — and shows them all at once. Only an archive with no problems is imported, in one transaction; nothing is left behind if storing fails half-way.
- The root album is named after the ZIP file. While a level holds exactly one folder and no files, the import descends into it, and the album is named after the folder it stops in. Folders below become sub-albums.
- macOS metadata (`__MACOSX/`, `._*`, `.DS_Store`, `.AppleDouble`) of at most 4 KB, `Thumbs.db` and `desktop.ini` are skipped and listed. A larger file that looks like macOS metadata fails the import.
- Exact duplicates (same folder, same name, same bytes) within the archive are skipped and listed. Imported media is approved immediately and keeps the ZIP entry's modification time as `file_modified_at`.
- The import job logs what it did — the archive and folder, every skipped file, the result, or every reason it was refused. Administrators see the log on the job's page under `/admin/worker-jobs` for 7 days.

### System import and the Dropbox script

- With `PHOTO_ALBUM_IMPORT_TOKEN` set, `importer-zip/system` accepts imports authenticated by that bearer token, and `importer-zip/system/albummer?folder=<folder>` lists the folder's root albums and returns the import rules (accepted extensions, size limits, maximum sub-album levels, macOS metadata size limit).
- `scripts/import_dropbox_photo_albums.py` imports `<Dropbox root>/<year or Andet>/<album folder>` from Dropbox, one album per ZIP. It checks the server first. Using the server's rules, it checks each album against its Dropbox listing before downloading, and skips albums already on the server. Each ZIP is verified before it is sent, and the script waits for the server's result.
- Every album imported through the system endpoint is locked as soon as it is imported (in the same transaction), so the archive is not edited by accident. Fotogruppen can unlock it within the usual six months; the script reports `Imported and locked`.
- Progress is kept in `<work-dir>/state.json` and everything is logged to `<work-dir>/import.log`. A rerun skips albums already imported, resumes waiting for albums sent on an earlier run, and skips earlier failures unless `--retry-failed` is given. Transient Dropbox and server errors are retried; every failure is reported with all its reasons. `--dry-run`, `--confirm`, `--year`/`--year-range` and `--stop-on-error` control a run.

## Dashboard

- The resident dashboard shows `Dagens billeder`: three approved photos chosen at random once a day and shown to everyone, each linking to the photo in its album. A photo is not chosen again for 365 days unless there are too few others.
- Below it, `På denne dag` shows up to six photos captured on this calendar day in earlier years, spread across the years, newest year first.
- The section is hidden when there is nothing to show.

## Development seed data

The photo-album seed generator creates albums in year and `Andet` folders, including a sub-album tree three levels deep, thumbnail-ready media, an album large enough to page, and lifecycle examples for recent/old uploads, binned media, and an automatically locked album.
