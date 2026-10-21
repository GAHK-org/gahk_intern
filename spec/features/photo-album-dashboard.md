# Feature: Fotoalbum — the photo album's dashboard

Administrators' overview of the photo album: what it holds, how well its photos and videos are
dated, what it takes up in the bucket, and what needs looking after. Under Administration →
Fotoalbum, with a page of its own for the dating and a list behind every count that needs a look.

## Who

- Administrators only (the effective role, so a preview as another role loses access).

## What it counts

- Everything not in the bin: pending uploads included, rejected ones not (they are in the bin).
- How an item is dated, best first: a capture date from the file's own metadata; the file's date,
  from its sidecar, from the uploader's device or from the ZIP; estimated from the album; estimated
  from the album's year; none (shown at the upload time).

## The overview

- **Tiles:** photos and videos, albums and sub-albums (and how many are empty), the share with a
  capture date, the storage, those without a date of their own, those dated outside ±6 months,
  those awaiting approval (and in the bin), and those still without their HD version and thumbnail
  (and how many failed). Each opens what it counts.
- **Dating:** a stacked bar of how the items are dated, each way with its count and share, and a
  link to the dating page.
- **By folder** (years newest first, then the rest): albums, items, how they are dated, the share
  with a capture date, and the storage. A folder opens its dating.
- **Storage** and **upkeep**, below.

## The dating page

- Everything for the whole album or one folder, chosen at the top: the ways of dating with what
  each means; a chart of every year from the first to the last date, stacked by how they are dated,
  with the numbers as a table; the folders' dating; and the albums — all of a chosen folder's, or
  the 20 with the most items without a date of their own or outside ±6 months.
- **Uden for ±6 måneder:** items in a year folder shown at a date more than six months outside its
  year — usually a camera with a wrong clock, or a photo in the wrong album. Or, switched over,
  those whose file time is outside it and was therefore not used.
- **Uden egen dato:** items estimated from their album or its year, or shown at their upload time,
  filterable by which.
- Both 25 to a page, with a thumbnail, the date shown and where it came from, the capture date and
  the file's date with its source. Each opens the item in its album.

## Upkeep

- Each with its count and what it means, opening a list of its items: awaiting approval; in the bin;
  waiting for their HD version and thumbnail; failed to get them; without a hash for duplicate
  detection; without a known size; the same file stored more than once; originals waiting to be read
  again for a capture date; file times whose source is not known yet. Failures are marked red. Also
  the latest imports.
- A list shows its items 50 to a page, each with its thumbnail, name, type, size, album, uploader,
  status, and what concerns that list: when it goes for good (awaiting approval, in the bin), its
  attempts and the job that tells why (failed conversions), which items share their content
  (duplicates). Its details open in place: every date and its source, the hash, the storage keys,
  the camera metadata. The original can be opened; the list never sends one to the album.
- Two more lists: the largest files, by the original's size; and the files no item points at.

## Storage

- Measured every night at 05:00, after the night's purges, or at once with "Mål igen": the bucket's
  photo-album prefixes are listed, so the numbers are the files themselves.
- By kind: originals, HD versions, thumbnails, sidecars, import archives, built downloads, and the
  older versions the bucket still keeps. With the growth since the measurement about a month
  before, the largest albums (with their sub-albums), and each folder's share.
- Files no item points at, among those over a day old, are counted and the first 50 listed with
  their size, time and album: what a failed upload, import or deletion left behind. Nothing is
  deleted.
- A year of measurements is kept.

## Decisions

- **Where a file time came from is recorded with it.** The sidecar, the ZIP entry and the uploader's
  device are trusted very differently, and nothing else tells them apart afterwards. For items stored
  before that, an import's files are told from uploads by when and where the import stored them,
  and a file with a sidecar by reading the sidecar, which the nightly date job does.
- **The storage is measured, not estimated.** The database knows the originals' sizes but not the
  versions', and listing the bucket also finds the older versions and the files no item points at.
- **Without its own date is not an alarm.** Most of the Dropbox archive is estimated, and will stay
  so; only dates that contradict the album, and failed conversions, are marked red.
- **The dating has its own page,** so the overview stays one screen of numbers; the lists to work
  through live with what they are about.
