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
- Thumbnails and high-definition variants are built in the background. A video's conversion may take up to four hours. Its high-definition copy is never larger than the video itself, keeps one audio track (the one with the most channels; a broken one is passed over) and its text subtitles with their languages. Picture-based subtitles (DVD, Blu-ray) stay in the original only. Safari shows the kept subtitles; Chrome and Firefox do not show subtitles embedded in an MP4. A file that cannot be converted, or whose conversion is cut short (the worker running out of memory, say), is tried again the next night, three times in all; then it shows `Videoen kunne ikke behandles` with a link to the original. Every failed try is a failed job on `/admin/worker-jobs`, with the reason.
- The cache is bounded (`PHOTO_ALBUM_CACHE_THUMBNAIL_MB`, default 2048; `PHOTO_ALBUM_CACHE_HD_MB`, default 3072) and evicts least-recently-used files in the background. A failed copy falls back to the storage redirect. In production it lives on the `photo-album-cache` volume so it survives redeploys. Local development has it on too (`task dev`: the same volume on `web`; `task dev:local`/`debug`: `app/cache/`), so it serves pictures as production does; the test suite leaves it off.

## Accepted files

- Images: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.heic`, `.heif`, `.avif`, `.tif`, `.tiff`, `.bmp`. Videos: `.mp4`, `.m4v`, `.mov`, `.webm`, `.avi`, `.mkv`, `.3gp`, `.3g2`, `.mts`, `.m2ts`, `.mpg`, `.mpeg`, `.wmv`, `.ogv`. SVG and RAW formats are not accepted.
- Both the extension and the content are checked; an image that cannot be decoded is refused. Size limits are `PHOTO_ALBUM_IMAGE_MAX_MB` (default 50) and `PHOTO_ALBUM_VIDEO_MAX_MB` (default 1000).

## Metadata and ordering

- The original upload is inspected for available metadata. Supported image metadata includes camera make/model and GPS coordinates, displayed as `Kamera` and `Sted`. `Sted` links to the location on OpenStreetMap.
- `captured_at` is derived from EXIF `DateTimeOriginal`, falling back to `DateTimeDigitized`, then XMP (`DateTimeOriginal`, Photoshop's `DateCreated`, `CreateDate`), then IPTC's date and time created, or from a video's container metadata. A date with no time of day is taken as noon. Both standard and nested EXIF IFD layouts, including HEIC/HEIF uploads, are supported.
- `file_modified_at` is the file's own last-modified time as reported by the browser or the ZIP entry, and where it came from is recorded with it: the uploader's device, the ZIP, or the sidecar. (For items stored before that was recorded, see the dashboard's spec.) In a ZIP, the file's AppleDouble sidecar (`.AppleDouble/<name>`, `._<name>`) wins when it keeps a believable creation or modification date: the earlier of the two. The sidecar itself is kept as it was, beside the original in the bucket (`…/<uuid>/sidecar/<name>`), believed or not; it is never served and goes when the item does.
- A date is believed in a year folder only if it falls within six months either side of that year (for 2004: 1 July 2003 up to 1 July 2005), so a New Year's Eve album keeps December's preparations and January's aftermath. Elsewhere a file time before 1990 is not believed.
- Media is ordered newest first by `sort_at`: `captured_at`, else a believable `file_modified_at`, else:
  - between the nearest items before and after it in its album, in file-name order (`IMG_9` before `IMG_10`), that have a date of their own inside the album's year — or next to the only one;
  - else the middle date of such items in the parent album, then further up;
  - else the upload time, if it is in the folder's year (always in `Andet`);
  - else the start of the folder's year.
- A capture date outside the album's year is still that item's date, but nothing is approximated from it.
- Only a capture date is exact. Every other date is shown as an approximation: its tile carries a small `ca.`, and the viewer shows `Anslået dato` with what it is based on (`ca. september 2004, ud fra filens dato` / `ud fra de andre billeder i albummet` / `ud fra hvornår det blev uploadet`, or only the year: `ca. 2004, ud fra albummets årstal`). A file time counts as one because it is when the file was last written: in the archive that is the moment of capture for some albums, but for others the day they were copied, days or months later.
- Approximations are worked out again when an album gets new media (upload, import, or a video's date from its transcode). Every night at 03:05 every album is, after the originals stored before XMP and IPTC were read are read again (at most 20 minutes a night); `manage.py settle_photo_album_dates` does both in one go. Media stored before this existed is dated by the first night's run.
- The viewer displays the capture date as `Optaget`, the location when available, the album path, the uploader, upload time, and view counts. The album path links to the album: clicking it closes the viewer and opens the album (on that album already, it only closes the viewer).

## Access and upload workflow

- Members of Fotogruppen and administrators can create, rename and delete albums, create sub-albums, import ZIP files, manage media, approve/reject submissions, view album bins, manually lock albums, and add or remove tags.
- Any signed-in resident can upload one or more photos or videos to an unlocked album. The upload form lists the chosen filenames before submission and may apply one optional title to all selected uploads.
- A file of 8 MB or more goes straight into the bucket in 64 MB parts, uploaded by Uppy with each request retried and an unfinished upload aborted, so it is not cut off by the proxy's one-minute limit. The server signs each of Uppy's requests, only for the key it chose and only for the resident it gave the upload to. The server then reads it back and runs exactly the checks a file sent with the form gets (type, size, content, duplicates); one that fails is refused with the same message and removed from the bucket. Smaller files, a server without a bucket, and a browser that cannot reach the bucket send the file with the form. The ZIP import page sends its ZIP the same way.
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
- On the server, `manage.py retry_failed_photo_album_media [--year Y] [--dry-run]` gives every item whose conversion failed three fresh attempts and queues it; `manage.py reencode_enlarged_photo_album_videos [--year Y] [--dry-run]` re-encodes, one job at a time on the media queue, the high-definition copies an earlier conversion enlarged to 1600 px. A video stays viewable throughout, and keeps its old copy if the re-encode fails.
- On the server, `manage.py check_photo_album_originals [--year Y] [--delete]` lists media whose original is missing from storage (one HEAD request each); with `--delete` it removes them, and the albums that leaves empty, so an import can send them again.
- On the server, `manage.py delete_photo_album --folder <folder>` or `--album <id>` deletes a whole folder, or an album with its sub-albums, regardless of locks: every media item (binned ones too), every stored file under the albums' `photo-album/<album id>/` prefixes (leftovers included), every download ZIP that holds any of the media, and the local cache copies. It lists what it will delete and asks for a random 8-character code to be typed back; a wrong code deletes nothing. It refuses outright when any album in scope was created, or any media uploaded, on or after 2026-10-01; `PHOTO_ALBUM_DELETE_PROTECTED_FROM` moves that date, but a date in the future is refused. It stops if anything is added while it waits for the code, and fails loudly if stored files remain afterwards.

## Viewing experience

- The album index displays root albums as folders with a cover (the newest thumbnail in the subtree) and a media count for the whole subtree. Album pages show a breadcrumb, the sub-albums, and the album's own media as a thumbnail grid; `Alle medier, også i underalbummer` (`?alle=1`) includes the media of every sub-album.
- Album cards are a uniform grid (two per row on a phone) with the cover on top; a long name wraps onto two lines and is cut off with an ellipsis, with the full name as a tooltip. Long album titles and breadcrumbs wrap instead of overflowing.
- An album's actions — download, select, bin, rename, lock, delete and `Nyt underalbum` — sit in one row in the album header; download, rename and `Nyt underalbum` open as small dropdowns.
- The timeline (`Tidslinje`) shows everything the reader may see across all albums, newest first, with a jump to any year.
- A ruler with the month and year (`Oktober 2025`) divides the timeline at each new month, by `sort_at` in local time. Items dated only by their folder's year come at the end of that year under `2004 · ukendt måned`.
- A time rail down the right edge marks every year, newest at the top, each with an equal share split into months. A dot shows the month the reader has scrolled to, and while scrolling it is named (`Oktober 2025`). On a phone the rail is a line and a dot at the screen's edge, without the years. The sticky year bar highlights the current year.
- The album index leads with a large card into the timeline: a mosaic of seven random photos (three on mobile) drawn from what every resident may see, redrawn every ten minutes. A folder page shows a plain `Tidslinje` link instead.
- The timeline can be filtered on tags (`?tag=<name>`, repeatable). The filter shows media tagged directly or through an album in its lineage. Several tags combine with AND, so an item must carry all of them. Active filters show as chips that remove the filter when clicked. The tag input searches as you type, and paging and year jumps keep the filter. Any signed-in resident can filter; visibility rules apply as usual.
- Grids load 60 items at a time and scroll infinitely in both directions, paged by `(sort_at, pk)` so pages do not shift as uploads arrive.
- `?media=<pk>` on an album page is a deep link: the grid opens at that item, the viewer opens on it, and the address follows the viewer as the reader browses.
- On desktop, selecting media opens a modal with the high-definition variant, metadata in a right sidebar, an original-download action, and deletion where permitted. Previous/next arrows and keyboard arrow keys navigate without looping past either end.
- On mobile, selecting media opens a full-screen viewer that shows only the media, its name, and capture date (or its approximate date, or the upload date, when it has none). A kebab menu contains metadata, original download, and deletion where permitted.
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
- A build that stops making progress for 15 minutes is marked failed; a slow build that keeps making progress is left to finish, however long it takes.

## ZIP import

- Fotogruppen members and administrators can import a ZIP file into a folder as a new album tree. The import runs in the background; the import page shows its progress and result. When the upload reaches 100%, a spinner shows that the server is still working until the import is done.
- The import is all-or-nothing. A first pass reads the whole archive and collects every problem — unsupported or unreadable files, files that are too large, invalid or case-colliding folder names, folders more than five levels deep, an existing root album with the same name — and shows them all at once. Only an archive with no problems is imported, in one transaction; nothing is left behind if storing fails half-way.
- The root album is named after the ZIP file. While a level holds exactly one folder and no files, the import descends into it, and the album is named after the folder it stops in. Folders below become sub-albums. The import page says so briefly.
- macOS metadata (`__MACOSX/`, `._*`, `.DS_Store`, `.AppleDouble`) of at most 16 KB, `.DS_Store` of any size, `Thumbs.db` and `desktop.ini` are skipped and listed. An AppleDouble sidecar is not: it is stored with its file, and listed as skipped only when its file is not imported (a folder's, a duplicate's, or one with no file). For a file the album already has, the sidecar is stored with that one if it has none. A larger file that looks like macOS metadata fails the import.
- Exact duplicates (same folder, same name, same bytes) within the archive are skipped and listed. Imported media is approved immediately and keeps its file time as `file_modified_at` (see Metadata and ordering).
- An import that stops making progress for 15 minutes, or whose worker is lost, is marked failed (`Importen blev afbrudt undervejs`), and nothing of it is imported.
- The import job logs what it did — the archive and folder, every skipped file, the result, or every reason it was refused. Administrators see the log on the job's page under `/admin/worker-jobs` for 30 days (a failed job's log for longer).

### System import and the Dropbox script

- With `PHOTO_ALBUM_IMPORT_TOKEN` set, `importer-zip/system` accepts imports authenticated by that bearer token, and `importer-zip/system/albummer?folder=<folder>` lists the folder's root albums and returns the import rules (accepted extensions, size limits, maximum sub-album levels, macOS metadata size limit).
- `scripts/import_dropbox_photo_albums.py` imports `<Dropbox root>/<year or Andet>/<album folder>` from Dropbox, one album per ZIP. It checks the server first. Using the server's rules, it checks each album against its Dropbox listing before downloading, and skips albums already on the server. Each ZIP is verified before it is sent, and the script waits for the server's result, showing how many files the server has checked or stored.
- A ZIP goes straight into the bucket, not through the app: `importer-zip/system/upload` starts a multipart upload and returns a signed ticket and a presigned URL per 64 MB part; the script sends the parts, each retried on its own, and then submits the import with the ticket and the parts' ETags. The server completes the upload, checks its size against what was announced, and imports it as any other archive. Without a bucket (dev, CI) the server says so and the ZIP is sent with the import. Through the app, the proxy drops any upload that takes over a minute.
- Files the server would refuse (not an accepted photo or video, over a size limit, too deep) are kept out of the ZIP so the rest of the album is imported. They are saved, mirroring the Dropbox folder, in `<work-dir>/<year>/<album>/invalid/`, and the album is then sent by name with paths relative to it. A file the server refuses on reading it is saved there too, and the rest is sent again (up to three times). Metadata (`.DS_Store`, AppleDouble sidecars, `Thumbs.db`, …) is sent with the album; the server keeps each file's sidecar with it, and a copy of what it reports skipping is saved in `metadata/`. An album with nothing acceptable fails, with its files saved in `invalid/` and `metadata/`; an album that fails otherwise keeps what is not in `invalid/` in `failed.zip` until it is imported. Each album folder holds an `album.json` naming the album its files belong to.
- Nothing the server took is kept on disk, and everything it did not take is, so every Dropbox file missing from the photo album can be gone through by hand. Files beside the album folders in a year folder (and that year folder's `.AppleDouble/`) are saved in `<work-dir>/<year>/_not-in-an-album/`, files and folders beside the year folders in `<work-dir>/_not-in-a-year-folder/`. `<work-dir>/not-on-server.json` lists every such file by its Dropbox path, with why and where its copy is (none for a duplicate the server skipped, as the album has that file), and every album folder that failed or was not sent because an album of its name existed. After every run it is written out as `<work-dir>/NOT-ON-SERVER.md`: counts, then a table per reason linking each copy; `--overview` writes it again without contacting anything. A copy deleted by hand is listed as dealt with and never fetched again. A file the server takes later is struck off and its copy deleted. The first run with the list starts it from what earlier runs saved.
- `--retry-invalid <work-dir>/<year>/<album>` sends only the files waiting in that album's `invalid/` folder (fixed in place) to that album by name, so they are merged into it; the album must be unlocked on the site first. The sidecars saved for them in `metadata/` go with them. What the server took is deleted; files still refused stay. A file already saved in `invalid/` is never overwritten by a later run.
- `--send-dates` (with `--year`/`--year-range`) sends the AppleDouble sidecars saved in each imported album's `metadata/` folder to `importer-zip/system/datoer`, for albums imported before the server read sidecars. The server keeps each sidecar beside its item and gives the item its date as an import would (`file_modified_at`), matching it by name in the album at the sidecar's path, then works out the album's approximations again. It answers which sidecars its items now have, and the script deletes those copies. Locked albums are dated too. It needs no Dropbox access, reports sidecars that match no item or several, and sending again changes nothing. For photos that also have a capture date it reports how far the sidecar is from it (within a minute, within a day, further), per album and in total: the measure of how far a sidecar can be trusted for the photos that have nothing else.
- Every album imported through the system endpoint is locked as soon as it is imported (in the same transaction), so the archive is not edited by accident. Fotogruppen can unlock it within the usual six months; the script reports `Imported and locked`.
- The system endpoint merges instead of refusing an existing root album name: the archive is added to that album, its folders to the sub-albums of the same names (ignoring case; missing ones are created). Only new files are stored. A file with the same name and the same bytes already in that album is skipped and listed; a file that only shares the name is a different photo and is kept beside it. The merge is still all-or-nothing, it is refused while any album it would add to is locked (unlock it first), and the album is locked again afterwards. The import page always refuses an existing name.
- The system endpoint takes an optional `album`: the root album's name, with the archive's paths relative to it. The import then does not descend through single folders, so a few files from one sub-album are merged into the album they belong to rather than named after that sub-album.
- Progress is kept in `<work-dir>/state.json` and everything is logged to `<work-dir>/import.log`. Every failure — an album, a retry, or the run stopping — is also appended, with its time and all its reasons, to `<work-dir>/failures.log`, which is never truncated. A rerun skips albums already imported, resumes waiting for albums sent on an earlier run, and skips earlier failures unless `--retry-failed` is given. Transient Dropbox and server errors are retried. A server that does not answer at all (no connection, or a 502/503/504 from its proxy) is tried again every 30 seconds, 5 times; then the run stops rather than failing every album after it, and a rerun continues. Every failure is reported with all its reasons. A job's skipped metadata files are shown as a single count, while other skipped files are listed one by one; a run ends with the counts of what is not on the server. `--dry-run`, `--confirm`, `--year`/`--year-range` and `--stop-on-error` control a run.

## Dashboard

- The resident dashboard shows `Dagens billeder`: three approved photos chosen at random once a day and shown to everyone, each linking to the photo in its album. A photo is not chosen again for 365 days unless there are too few others.
- Below it, `På denne dag` shows up to six photos captured on this calendar day in earlier years, spread across the years, newest year first.
- The section is hidden when there is nothing to show.

## Development seed data

The photo-album seed generator creates albums in year and `Andet` folders, including three sub-album trees three levels deep, tags on albums at every level of those trees and on single media (some with several), thumbnail-ready media, an album large enough to page, and lifecycle examples for recent/old uploads, binned media, and an automatically locked album.
