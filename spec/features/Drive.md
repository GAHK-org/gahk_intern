# Feature: Drive

Drive holds the kollegium's working files: each embedsgruppe's documents, documents for everyone, and folders
shared by invitation. Office documents can be edited together live. It lives at `intern.gahk.dk/drive/` and
replaces Arkiv, which is removed first; nothing is migrated from it.

## Structure

```
Drive
├── Fælles
│   ├── Uden årstal
│   ├── 2027
│   └── 2026
├── Embedsgrupper
│   └── Regnskabsgruppen
│       ├── Uden periode
│       ├── 2027 Sommer
│       └── 2026-27 Vinter
└── Delte mapper
    ├── Temafest
    └── 2026
        └── Sommerfest          (archived)
```

- Drive has three kinds of space: an embedsgruppe's folder, Fælles and a Delt mappe. Every folder inside a space follows that space's access rules.
- Every embedsgruppe in the app gets a group folder automatically.
- Group periods are half-years: `Vinter` from 1 October to 31 March, `Sommer` from 1 April to 30 September. A period folder is named year first, a winter with both years (`2026-27 Vinter`, `2027 Sommer`), so names sort by date anywhere, also in a zip.
- Fælles has calendar-year folders (`2027`).
- Period and year folders are created automatically two weeks before they begin.
- Each group has `Uden periode` and Fælles has `Uden årstal` for what is used across periods or years.
- Folders the system creates (Embedsgrupper, the group folders, periods, years, `Uden periode`, `Uden årstal`, Fælles, Delte mapper and standing folders) cannot be renamed, moved or deleted, and never lock.
- You only see what you can open, except for the overview admins and Inspektionen get (below).
- The current period or year is highlighted in the list.
- Creating a folder or uploading directly at the top level of `Uden periode` or `Uden årstal` asks first, and offers to put it in the current period or year instead.
- Links name folders and files by a random id, never by a number that counts up, so nobody can tell how many there are.

## Access

- A member of a period is someone in the group, per the månedsliste, for at least one month of that period.
- Admins and Inspektionen means residents with the administrator role and members of Inspektionen.
- A resident who has moved out has no access to anything in Drive. Their memberships, roles and past periods are kept, and they get exactly the same access back if they move in again.
- The kitchen account has no access.
- Drive starts behind the rollout gate (administrators and Inspektionen) and opens to everyone after a trial.

### Group folders

| Folder | Current members | Former members of that period | Admins and Inspektionen |
| --- | --- | --- | --- |
| Next period (from two weeks before) | write, with the incoming members | — | name, size, file count |
| Current period | write | read | name, size, file count |
| Most recent previous period | write if also a member of it, else read | read | read |
| Older periods | read | read | read |
| `Uden periode` | write | none | read |

- Incoming members are those on the next month's list once it exists. Outgoing members who do not continue lose access to the new period when it begins.
- When a group changes over completely, nobody can edit its previous period any more.

### Fælles

- Every resident can read all of Fælles.
- Every resident can write in `Uden årstal`, the current year, the next year and the most recent previous year. Older years are read-only.
- Only admins and Inspektionen create, rename and delete the first level of folders in `Uden årstal`. Below that, anyone can create folders.

### Delte mapper

- Any resident can create a Delt mappe and becomes its first moderator; those invited start as editors. Only residents living here can be invited. It has at least 3 members, counting every role. Nobody can be removed, or leave, if that would leave fewer than 3. Moving out does not count.
- Roles: moderator (everything edit can, plus inviting and removing members, changing roles, and renaming or deleting the folder), edit (upload, create folders, edit, rename, delete), read-only (view and download).
- There is always a moderator. The last one cannot leave or step down without naming another.
- Only members can see it. Admins and Inspektionen see it by a short random tag (`Delt mappe 3f2c9a`, never a number that counts up), its member count, file count and size, never its name or contents.
- At creation the moderator sets an expiry date (default 6 months, at most 2 years after creation) and chooses whether the folder is deleted or archived when it expires. Moderators can move the date within those 2 years. The choice can change from archive to delete, never from delete to archive.
- Moderators are notified 14 days before expiry, and again if the date is moved. A Delt mappe expires the day after its expiry date.
- Two Delte mapper may have the same name: refusing a name would tell that a Delt mappe of that name exists.
- Moderators can delete a Delt mappe once it is empty, like any folder.
- Deletion at expiry is permanent, with no bin.
- An archived Delt mappe moves to `Delte mapper/<year it was created>/<name>` (with `(2)` added if the name is taken). It becomes read-only for every resident, its real name is shown to everyone, and it never expires.

## Sharing

- A file or folder in a group folder can be shared, read-only or edit, with named residents or with an embedsgruppe. Anyone who can write there can share it or stop a share. The item shows who it is shared with. Shares do not expire.
- A resident finds what is shared with them under `Delt med mig`.
- A share with a group appears in `Delt med <group>` inside the recipient group's matching folder: the item's own period, or `Uden periode` for an item from `Uden periode`. That folder's rules decide who in the recipient group can see it.
- A share is never more open than the item: editing needs an edit share and write access to the recipient's folder, and a share becomes read-only once the item's own period is read-only or the item is locked.
- Fælles and Delte mapper cannot be shared.
- An edit share on a file lets the recipient add versions. Renaming, moving and deleting it stay with those who can write in its folder. An edit share on a folder lets the recipient work inside it, but not rename, move or delete the folder itself.
- Sharing is for those who can write there by the access rules, so a recipient cannot share further.
- Where a share lets somebody in partway down, the folders above are shown as names, not links.

## Standing folders

- A space can have standing folders, such as `Fællesmøder` or `Erfaringsrapporter`, created automatically in every new period or year with the same name.
- Each standing folder gets a combined view across periods or years (`Fællesmøder (alle år)`), newest first, showing only what the reader can open anyway.
- A standing folder can have a naming rule: a button such as `Opret fællesmøde` asks for a date and creates `2026-11-03 Fællesmøde`.
- In Fælles, a standing folder can limit writing to named groups. `Fællesmøder` is read by every resident and written only by Inspektionen and admins.
- Admins and Inspektionen define standing folders in Fælles, and a group's current members define their group's own. A new standing folder applies from the next period or year.
- A standing folder can be stopped: no new copies are made, and those made stay. A resident's folder already holding the name keeps it, and the standing folder is numbered.

## Files and folders

- Any file type can be uploaded, up to 5 GB per file. A dragged-in folder keeps its structure. Types outside the preview list are always downloaded, never opened in the browser.
- Uploading a name that already exists in the folder asks `Erstat (ny version)` or `Behold begge` (which saves `Budget (2).xlsx`). A file also has `Upload ny version`.
- If a file has changed since you last downloaded it, uploading a replacement says who changed it and when, and offers to upload anyway, keep both, or cancel.
- A file open for live editing cannot be replaced by an upload.
- Names follow the photo album's rules: unique within a folder ignoring case, Unicode-normalised, no `/`, `\` or control characters, no leading or trailing space, no trailing dot.
- Anyone who can write in a folder can rename, move or delete what is in it.
- Moving needs write access in both places and an unlocked item. A move to another space first shows how access changes. Shares follow the item; they end if it leaves the group's space, and the confirmation says so.
- Copying needs read access to the source and write access to the destination. A copy is a new file with the copier as creator and no history.
- When a name is taken at the destination, a move is refused and a copy is numbered (`Budget (2).xlsx`).
- A folder can be deleted only when it holds no files or subfolders. Files in the bin do not count; restoring one brings its folder back.
- Deleted files go to their space's bin for 30 days, then are removed with all their versions. Anyone who can write in a folder sees its binned files and can restore them. A restored file whose name has been taken meanwhile is numbered.
- A file's creator (who uploaded the first version) is notified when someone else deletes it (with a link to restore it), renames it or moves it. One notification per action, not per file, and only while the creator can still see the folder.
- Several files or a whole folder download as one zip of at most 5 GB. Subfolders the reader cannot see are left out without a trace.
- A zip is built in the background, and its page waits for it. Only the resident who asked can fetch it, for 24 hours, and only while they can still read everything in it.
- A folder shows how many files its whole subtree holds, counting what the reader can see.

## Versions

- A new version is created by an upload that replaces a file, or when a live editing session ends. A version lists everyone who edited in it, and when it was saved.
- Every version is kept for as long as the file exists.
- Anyone who can read a file can view and download its old versions. Restoring one adds it as a new version on top.
- A single version, or a binned file, is permanently deleted before its time only by request: a member who can write asks, and an admin or Inspektionen approves (moderators in a Delt mappe). The approver sees the name and version details, never the contents. The newest version cannot be purged; the file goes in the bin first.

## Locking

- A locked file cannot be edited, replaced, renamed, moved or deleted. It can still be viewed, downloaded and copied.
- A locked folder cannot be renamed, moved or deleted, and nothing can be added to it. A lock covers the folder's whole subtree, and is lifted only on the folder that holds it.
- A file locks automatically one year after its last new version. A folder locks automatically after a year with no new file, version or subfolder anywhere inside it. Renaming does not count.
- Anyone who can write can lock a file or folder by hand.
- Anyone who could otherwise write to a locked item can request an unlock for up to 30 days, with a reason. Admins and Inspektionen decide (moderators in a Delt mappe), and a rejection comes with a reason. The requester is told the outcome.
- Nobody decides their own unlock or purge request.
- When the unlock period ends, the item locks again immediately.
- Unlocking never grants write access: a file in a read-only period cannot be unlocked for editing.
- Unlocking a folder does not unlock the files inside it.

## History

- Every file and folder has a history: created, each version, renamed, moved, deleted, restored, locked, unlock requested and decided, shared and unshared, purge requested and decided. Anyone who can read the item can read it.
- Each space has an activity page for its members. A Delt mappe's page also shows member and role changes.
- Admins and Inspektionen have a page of pending unlock and purge requests, and of past decisions, showing names and paths only.
- History outlives what it describes: a purged version leaves its entry ("Version 3 slettet permanent af Mette, godkendt af Jonas").

## Preview and live editing

| Type | Preview |
| --- | --- |
| Images (jpg, png, gif, webp, heic) | viewer with next and previous through the folder |
| PDF | the browser's viewer |
| Video and audio (mp4, webm, mov, mp3, m4a) | the browser's player, no conversion |
| Text (txt, csv, json, log …) | the first 1 MB; csv as a table |
| Markdown | rendered |
| Office (docx, xlsx, pptx, odt, ods, odp) | OnlyOffice, view mode |

- Images get thumbnails; other files get an icon for their type.
- An image is shown as a copy of at most 1600 px, made in the background with its thumbnail, so a HEIC is seen in every browser. Images over 50 MB are only downloaded. Next and previous follow the folder's images by name, also with the arrow keys and a swipe, and are not offered to someone the file alone is shared with.
- A file's type is read from its name, never from what the uploader's browser said. Text is read as UTF-8, or as Windows' Western encoding for what a Danish Excel saves; a CSV shows its first 2,000 rows; markdown is rendered without any HTML of its own.
- Until OnlyOffice is switched on, office files are downloaded.
- docx, xlsx and pptx can be edited live together in OnlyOffice, showing who else is in the document. Any folder you can write to has `Nyt dokument`, `Nyt regneark` and `Ny præsentation`.
- Where you cannot write, or the file is locked, OnlyOffice opens in view mode.
- Older formats (doc, xls, ppt, odt …) open for viewing. `Rediger` makes a docx, xlsx or pptx copy next to the original.
- OnlyOffice is switched on and off by a server setting. When off, or not responding, office files download, the new-document buttons are hidden, and nothing opens for live editing; everything else works.
- The OnlyOffice server never receives a resident's login. It only reaches the documents Drive opens in it.

## Search and the list

- Search finds file and folder names across everything you can open, showing where each lives, with filters for space, type and period or year. File contents are not searched.
- The list shows name, size, and last changed and by whom, folders first, sortable by column, with breadcrumbs above and lock and share markers on the rows. On a phone it is one column.
- Search needs two characters and shows at most 200 names, folders first. Sorting orders the files; the folders above them keep their own order, periods newest first.

## Decisions

- **Periods are folders**, because access differs per period: admins and Inspektionen read a group's work only after the period ends, so the group works freely and is overseen afterwards. They do read `Uden periode`, so it cannot become a place to keep things from oversight.
- **Only the most recent previous period stays editable**, by those who were in it: late corrections are possible, and older records are frozen without anyone locking them.
- **Moving out suspends access, it does not delete it**, so a resident who moves back picks up where they left off.
- **Shares land in the item's own period**, so last period's accounts sit with last period, and a template from `Uden periode` stays where future members will find it.
- **A Delt mappe can go from archive to delete, never back**: people uploaded to a folder that promised to disappear.
- **Purging needs a second person**, so one member cannot quietly rewrite a group's history; binning the file hides it meanwhile.
- **Unlocking never widens access, and a folder unlock leaves its files locked**: adding one document to an old folder should not reopen everything in it.
- **Anyone who can write can delete**, rather than only the creator: the bin, the creator's notification and the history protect against mistakes, and a creator-only rule leaves files nobody can tidy once the creator leaves.
- **No quotas and no full-text search**: admins see every space's size, and indexing file contents costs the server capacity that is short.
