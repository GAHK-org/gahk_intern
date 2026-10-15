# Arkiv

The kollegium's file archive, replacing the shared Dropbox (photographs) and the embedsgruppers' Google Drive folders.

## Access
- Every resident can use Arkiv.
- A folder with no embedsgruppe is visible to every resident. A folder owned by an embedsgruppe, and everything below it, is visible only to the group's members in the current månedsliste.
- Membership is current, not historical: leaving a group in the månedsliste ends access to its folders that month.
- Subfolders inherit their parent's group. A group can be set on a deeper folder in the Django admin; a private subfolder inside a shared folder stays private.
- A folder you may not see does not exist as far as you can tell: its page is "not found", not "forbidden".
- No role sees everything. Administrators and Inspektionen manage the top level but cannot read other groups' folders; misfiled documents are fixed in the Django admin.

## Folders
- The top level holds one folder per embedsgruppe plus shared folders such as Fælles dokumenter. Administrators and Inspektionen create and arrange these in the Django admin.
- Anyone who can see a folder can create subfolders in it. Names are unique within a folder, at most 120 characters, without slashes.
- A folder can be removed only when it is empty (counting files the reader cannot see and removed files), within 30 days of its creation, and when it is not locked. Top-level folders can only be removed by administrators and Inspektionen.
- If a folder is not empty, the reader is told how many files and subfolders it holds, or just "not empty" if some of them are hidden from them.
- Renaming and moving are done in the Django admin.

## Files
- Anyone who can see a folder can upload files to it, up to 5 GB each.
- In production the file goes straight from the browser to object storage, never through the web server.
- Identical files are stored once, however many times and wherever they are filed; uploading a copy of something already stored transfers nothing.
- File names are unique within a folder; uploading a second file with the same name is refused.
- Each listing shows who uploaded what. A folder that is mostly pictures is shown as a grid of thumbnails, otherwise as a list.

## Removing files
- Anyone who can write to a folder can remove a file from it, within 30 days of its upload.
- Removal is recorded with who did it, and the file appears in a "removed" list in the folder, where it can be restored or deleted permanently.
- Restoring fails with an explanation if a new file has taken the name.
- Deleting permanently removes the file; its contents stay only if the same file is filed elsewhere. Storage keeps the deleted bytes for a further 30 days as a last-resort backup.

## Locking
- Administrators can lock a folder, which locks everything below it.
- A locked folder accepts no uploads or subfolders, and nothing in it can be removed, restored or deleted.
- Administrators can unlock a locked folder temporarily for one hour, and lock it again early.

## Pictures and viewing
- Each image gets a small thumbnail for listings and a large preview for viewing, made in the uploader's browser.
- A missing thumbnail shows a file icon; a missing preview means the viewer shows the original.
- Clicking an image opens a viewer for paging through the folder. Ctrl-, cmd-, shift- and middle-click behave as ordinary links.
- Every download re-checks access; links cannot be shared with someone who may not see the file.

## Downloading many files
- Selected files, or a whole folder with its subfolders, can be downloaded as one zip, keeping the folder structure.
- A zip is limited to 500 MB, and a selection to 200 files.
- Hidden subfolders are left out of a folder zip without a trace.
- Zips are built on the server and reused when the same files are requested again; they expire automatically.

## Import
- The Dropbox archive is imported by a re-runnable command that skips anything already stored and never deletes.
- Images imported without thumbnails or previews are filled in by a separate backlog command.

## Decisions
- Access follows the månedsliste, so joining or leaving an embedsgruppe grants or removes its folders with nobody administering anything.
- Anything meant to outlive a group's rotation belongs in a shared folder; widening access to former members would be wrong for groups like Regnskabsgruppen.
- No read-only tier: a folder you can read is a folder you can add to. Every removal is attributed and undoable instead.
- Only the top level is controlled; anyone can make a subfolder, because needing permission to file this year's fest is how an archive turns back into a chat thread.
- Folders can only be removed when empty, so no single tap buries hundreds of photographs.
- Files are stored by content, so renaming, moving and duplicates cost nothing.
- Not a wiki: shared prose belongs in MediaWiki; Arkiv holds files.
