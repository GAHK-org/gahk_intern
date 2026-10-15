# Photo album

The kollegium's photos and videos ("media"), organised in albums by year.

## Access
- Every resident can browse, upload to unlocked albums, filter, view and download.
- Fotogruppen and administrators create, rename, lock and delete albums, import ZIP files, approve and reject uploads, manage bins and tags.
- Administrators also have one bin across all albums.

## Albums
- Every media item belongs to exactly one album. Albums form a tree up to five levels deep.
- A top-level album belongs to a folder: a year or `Andet`. The index shows `Andet` first, then years newest first.
- By hand, sub-albums can be created only directly under a top-level album; deeper trees arrive by ZIP import.
- Album names behave like folder names: unique among siblings ignoring case, the same name whether typed on a Mac or Windows, no slashes, control characters, leading/trailing spaces or trailing dot.
- Albums can be renamed unless locked, but not moved.
- An album can be deleted only when it has no sub-albums and no media, including binned media.

## Uploading
- Accepted: common photo formats (JPEG, PNG, GIF, WebP, HEIC, AVIF, TIFF, BMP) up to 50 MB and video formats (MP4, MOV, WebM, AVI, MKV, 3GP, MTS, MPEG, WMV, OGV and similar) up to 1 GB. SVG and RAW are refused, as are images that cannot be decoded.
- Several files can be uploaded at once, with one optional shared title.
- An exact copy (same name and identical bytes) of an item already in the album is skipped. A file that already exists in other albums is stored, with a warning naming them.
- Uploads by Fotogruppen and administrators are approved immediately. Others wait for approval, visible only to Fotogruppen, administrators and the uploader, and marked `Afventer godkendelse`.
- The uploader can withdraw a pending upload, which deletes it outright. Rejected uploads go to the bin. Uploads still pending after 30 days are deleted.
- Each item keeps its original file and gets a 1600 px HD version and a 320 px thumbnail. Animated GIFs stay animated.
- Each file keeps its original name in every download.

## Metadata and ordering
- Capture date, camera and GPS location are read from the file; the location links to OpenStreetMap.
- Media is sorted newest first by capture date, else the file's own modification date, else upload time.
- The viewer shows capture date, location, album path (a link to the album), uploader, upload time and view counts.

## Locking
- A locked album accepts no uploads or sub-albums, cannot be renamed, and its media cannot be deleted. Tags still work.
- A lock covers the album's whole subtree. The album page names the album holding the lock.
- An album locks automatically 90 days after the newest approved media anywhere in its subtree was added. Fotogruppen can also lock any album by hand.
- Fotogruppen can unlock an album's own lock only within six months of it taking effect; after that the page explains it is permanent. An inherited lock is lifted on the ancestor. Unlocking restarts the 90-day timer.

## Tags
- Albums and single items can carry any number of tags: 1–30 letters, digits, `.`, `-` or `_`, unique ignoring case (the first spelling sticks).
- An album's tags apply to everything in its subtree. Inherited tags are shown after the item's own, marked, and can only be removed on the album that holds them.
- Fotogruppen adds and removes tags, picking from existing tags as they type or creating new ones. Unused tags are no longer suggested.
- Every tag links to the timeline filtered on it.

## Deleting and bins
- Fotogruppen can delete media uploaded within the last 30 days, unless the album is locked. Deleted media goes to the bin.
- Each album has a bin covering its subtree. Binned media can be restored until it is purged after 30 days.
- A binned item can be deleted permanently by hand only within an hour of its upload; otherwise it waits for the 30-day purge.
- A server command can delete a whole year folder or album tree, regardless of locks, after the operator types back a confirmation code. It refuses anything created or uploaded from 1 October 2026 onwards.

## Browsing
- The index shows top-level albums as cards with a cover and a media count for the whole subtree, led by a mosaic card of random photos (refreshed every ten minutes) into the timeline.
- An album page shows a breadcrumb, sub-albums and the album's own media; a toggle includes every sub-album's media too.
- The timeline shows everything the reader may see across all albums, divided by month, with a year rail for jumping. It can be filtered on one or more tags (all must match).
- Grids load 60 at a time and scroll endlessly in both directions without shifting as new uploads arrive.
- A link can point at one item in an album; the viewer opens on it and the address follows as the reader browses.
- On desktop the viewer is a modal with metadata beside the picture; arrows and arrow keys browse without wrapping. On a phone it is full screen with swipe to browse, pinch to zoom (1–4×) and details in a menu.
- A view counts once the full picture or the video has loaded. Each item shows unique viewers and total views; repeat views within 10 minutes count once.
- `Vælg` turns on selection: download the selected items, and for Fotogruppen approve, reject or delete them. Items the reader may not act on are skipped and reported.

## Downloads
- Any resident can download a single original, or request a ZIP of an album or a selection, as originals or HD.
- ZIPs are built in the background and listed under `Mine downloads` for 7 days. The same request reuses a pending or ready ZIP. A build that stalls is marked failed after an hour.
- An album with sub-albums can be downloaded with or without them. The ZIP mirrors the album tree; clashing file names are numbered and listed in an included text file.

## ZIP import
- Fotogruppen imports a ZIP into a folder as a new album tree, named after the ZIP (or after the single folder it contains). Progress shows on the import page.
- The import is all or nothing: every problem in the archive (bad or oversized files, invalid or clashing names, too deep, an existing album name) is reported at once, and nothing is imported until there are none.
- Operating-system clutter (`.DS_Store`, `__MACOSX`, `Thumbs.db` and the like) and exact duplicates within the archive are skipped and listed.
- Imported media is approved immediately. Administrators can read each import's log for 30 days.
- A token-authenticated import endpoint lets the Dropbox migration script import one album per ZIP. It merges into an existing album of the same name instead of refusing, adds only new files, and locks the album afterwards. The script sets aside files the server would refuse, can retry them later, resumes after interruptions and logs every failure.

## Scheduled jobs
- Nightly: retry HD and thumbnail builds that never finished, fill in duplicate-detection fingerprints for old uploads, delete expired ZIPs, purge the bin and expired pending uploads.
- Hourly: mark stalled ZIP builds as failed.

## Dashboard
- `Dagens billeder`: three random approved photos a day, the same for everyone, not repeated within a year.
- `På denne dag`: up to six photos captured on this date in earlier years, newest year first.
- Hidden when there is nothing to show.

## Decisions
- Uploads from outside Fotogruppen need approval, so the album stays curated without stopping anyone from contributing.
- Albums lock after 90 days of quiet, and locks become permanent after six months, so the archive cannot be edited by accident years later.
- Deletion goes through a 30-day bin; manual permanent deletion is only for undoing a fresh mistake.
- A duplicate is only skipped when the bytes are identical, never on a matching hash or name alone.
- ZIP imports are all or nothing, so a half-imported album never needs cleaning up.
- Album tags apply to the whole subtree, so tagging a party once tags every photo from it.
