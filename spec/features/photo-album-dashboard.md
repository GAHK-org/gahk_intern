# Feature: Fotoalbum — the photo album's dashboard

Administrators' overview of the photo album: what it holds, how well its photos and videos are
dated, what it takes up in the bucket, and what needs looking after. Under Administration →
Fotoalbum.

## Who

- Administrators only (the effective role, so a preview as another role loses access).

## What it counts

- Everything not in the bin: pending uploads included, rejected ones not (they are in the bin).
- **Tiles:** photos and videos, albums and sub-albums (and how many are empty), the share with a
  capture date, the storage, those without a date of their own, those dated outside ±6 months,
  those awaiting approval (and in the bin), and those still without their HD version and thumbnail
  (and how many failed). Each links to its part of the page.
- **How they are dated**, best first: a capture date from the file's own metadata; the file's
  date, from its sidecar, from the uploader's device or from the ZIP; estimated from the album;
  estimated from the album's year; none (shown at the upload time). A stacked bar, then each with
  its count, share and what it means.
- **By year:** a chart of every year from the first to the last date, stacked by how they are
  dated, with the numbers as a table; and a table per folder (years newest first, then the rest)
  with its albums, items, how they are dated, the share with a capture date, how many have no date
  of their own, how many fall outside ±6 months, and its storage.
- **Albums:** for a chosen folder, all its albums; otherwise the 20 with the most items without a
  date of their own or outside ±6 months. Sub-albums are rows of their own, named with their path.

## What it lists

- **Uden for ±6 måneder:** items in a year folder shown at a date more than six months outside its
  year — usually a camera with a wrong clock, or a photo in the wrong album. Or, switched over,
  those whose file time is outside it and was therefore not used.
- **Uden egen dato:** items estimated from their album or its year, or shown at their upload time,
  filterable by which.
- Both follow the chosen folder, 25 to a page, with a thumbnail, the album, the date shown and
  where it came from, the capture date and the file's date with its source. Each opens the item in
  its album.

## Storage

- Measured every night at 05:00, after the night's purges, or at once with "Mål igen": the bucket's
  photo-album prefixes are listed, so the numbers are the files themselves.
- By kind: originals, HD versions, thumbnails, sidecars, import archives, built downloads, and the
  older versions the bucket still keeps. With the growth since the measurement about a month
  before, the largest albums (with their sub-albums), and each folder's and album's share.
- Files no item points at, among those over a day old, are counted and the first 50 named: what a
  failed upload, import or deletion left behind. Nothing is deleted.
- A year of measurements is kept.

## Upkeep

- Awaiting approval, in the bin, waiting for or failed to get their HD version and thumbnail (the
  latest failures listed), without a hash for duplicate detection, the same file stored more than
  once, originals waiting to be read again for a capture date, file times whose source is not known
  yet, and the latest imports.

## Decisions

- **Where a file time came from is recorded with it.** The sidecar, the ZIP entry and the uploader's
  device are trusted very differently, and nothing else tells them apart afterwards. For items stored
  before that, an import's files are told from uploads by when and where the import stored them,
  and a file with a sidecar by reading the sidecar, which the nightly date job does.
- **The storage is measured, not estimated.** The database keeps no file sizes; listing the bucket
  also finds the older versions and the files no item points at.
- **Without its own date is not an alarm.** Most of the Dropbox archive is estimated, and will stay
  so; only dates that contradict the album, and failed versions, are marked red.
