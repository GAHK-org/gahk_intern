# Feature: Arkiv — the kollegium's files

**Unnumbered, like opslagstavlen and begivenheder.** `F-001`–`F-015` are legacy-parity documents,
each pointing at a PHP controller it reimplements. Arkiv is greenfield and replaces two *external*
services, so an `F-0NN` would make every existing citation ambiguous.

## What it is, and what it replaces

Two paid services, both outside anyone's control and neither tied to who actually lives here:

- **Dropbox**, ~2 TB of photographs — parties, værelsesrunder, building work, twenty years of the
  place. Shared by a password that has been passed around for longer than anyone can date.
- **Google Drive**, the embedsgruppers' documents — Regnskabsgruppen's accounts, Indstillingen's
  notes, Inspektionen's paperwork. Access granted by hand, per person, and revoked when somebody
  remembers.

The second problem is the interesting one. The app already knows, month by month, who is in which
embedsgruppe: that is `residents.Residency`, the same monthly list that decides everything else.
Arkiv makes that the access rule, so joining Regnskabsgruppen in the månedsliste grants its folders
and leaving it takes them away — with nobody administering anything.

**It is not a wiki.** Prose that people edit together belongs in MediaWiki, which is kept and not
rewritten (scope §3). Arkiv holds *files*: things with bytes, a name, and an owner.

## Access

| Action | Who |
| --- | --- |
| Reach the feature at all | the rollout gate — `administrator` / `inspektion` for the trial |
| See a folder with no embedsgruppe | every resident who is through the gate |
| See a folder owned by an embedsgruppe | its **current** members, per `Residency` for `active_period()` |
| Create or rename a **root** folder | `administrator` / `inspektion` (`can_manage_roots`) |
| Create subfolders, upload | anyone who can see the folder (`can_write`) |

`access.visible_folders` / `visible_files` are the **only** querysets any view may start from, the
same rule `events/access.py` sets. Two refusals, and the split is deliberate:

```
404  you may not know it exists          (a group folder you are not in)
403  you know it exists, but not this    (the feature gate, or a write you may not make)
```

### Membership is current, not historical

Decided, not defaulted. Access resolves through `Residency` for the **active period**, so leaving an
embedsgruppe ends access to its folders that month.

The cost is real and worth stating plainly: a resident loses the folders of a group they were in
last year, *including photographs they took themselves*. The answer to that is not to widen the rule
— "anyone who was ever a member" is exactly wrong for Regnskabsgruppen, where leaving the group is
precisely when access should stop — but to file anything meant to outlive a rotation in a folder
with **no** embedsgruppe, which every resident can read. A shared archive that only its current
caretakers can see is a filing mistake, not an access-control one.

### No role sees everything

Administratorer and Inspektionen shape the root folders but get **no special read access**, for the
same reason `events/access.py` refuses it to moderators: a group folder's whole promise is that
non-members cannot read it, and "except Inspektionen" makes that promise false in exactly the case
anyone would care about. A genuinely misfiled document is fixed in the Django admin, which has
always seen every table.

## Data model

`ArchiveFolder` (parent self-FK, name, workgroup, **effective_workgroup**, created_by, deleted_at)
and `ArchiveFile` (folder, name, sha256, size, content_type, uploaded_by, deleted_at). Both soft
delete: the point of leaving Dropbox is not to lose undo.

**`effective_workgroup` is the access column, and `workgroup` is not.** `workgroup` is what somebody
declared on a folder; `effective_workgroup` is that or the nearest ancestor's, denormalised on write
by `ArchiveFolder.save()` and, for a subtree whose owner changed, by `services.reassign_subtree`.
Resolved on write because the alternative is a recursive CTE on every page load of a tree four
levels deep — and because a read-time walk has to fetch the whole ancestor chain before it can
decide whether to show the row it is already holding. The invariant it buys: a private subfolder
inside a public parent stays private, and one indexed predicate proves it.

`Workgroup` is referenced with **PROTECT, and that is security rather than tidiness**: `SET_NULL` on
a deleted workgroup would turn every folder that group owned into `effective_workgroup = NULL` —
readable by the whole kollegium — silently, as a side effect of cleaning up a lookup table.

## Storage

Keys are **content-addressed**: `arkiv/<sha256[:2]>/<sha256>`, with the display name in the row.
Three things follow, and together they pay for the indirection:

- renaming and moving become DB updates — no S3 copy, and no window where an object is in two places
  or in neither;
- the fourth copy of the same party photograph costs nothing, which across 2 TB of phone uploads
  from one weekend is not a rounding error;
- the import is restartable — re-running re-hashes and skips, so an interrupted 2 TB upload resumes.

**No extension in the key**, deliberately, even though it makes the bucket unbrowsable by eye: two
files with identical bytes and different names must be one object or the deduplication is a fiction.
The download view puts the name back with `ResponseContentDisposition`.

## Upload

**The file never touches the app server in production.** `begin` checks access and returns a
presigned POST policy; the browser sends the bytes straight to Hetzner; `commit` `HEAD`s the object
and only then creates the row. An abandoned upload leaves an object nobody references — swept by the
lifecycle rule and a future audit — whereas the reverse, a row pointing at bytes that are not there,
would be a broken file in a listing with nothing to explain it.

This is the one place direct-to-bucket earns its complexity, and it is the opposite of what media
does: opslag images are capped at 5 MB and already downscaled, so posting them through Django costs
nothing. Arkiv holds the 2 GB video from sommerfest, uploaded from a phone, against three
synchronous gunicorn workers with a 60-second timeout. That cannot go through the app at all.

**The `HEAD` is the real check.** The size, the content type and the hash were all the client's word
until then; the row records what the bucket actually has. The policy's `content-length-range` is
what stops a 40 GB upload *before* the bytes are paid for, which a row-level check could not.

**The hash is computed in the browser** and is what the object is keyed by. It costs a full read of
the file client-side and buys deduplication — the second copy of a photograph uploads nothing at all,
which across one weekend's phone uploads is not a micro-optimisation — and an idempotent, restartable
upload.

**The bucket needs a CORS rule** (DEPLOY.md §4c) and only production can notice its absence, since
the dev path never leaves the app.

Arkiv does **not** use `STORAGES["default"]`. That is `MediaS3Storage`, pinned to `location="media"`,
and the prefix is a security boundary (DEPLOY.md §4c/§4d) — a storage that could reach `arkiv/`
could reach `backups/`. `arkiv/storage.py` talks to the bucket directly, with a local-filesystem
backend for dev and CI so the whole feature works offline.

## Decisions and rejected alternatives

**Rendering the archive by listing S3.** Rejected. `Prefix`/`Delimiter` is the obvious way to draw a
file browser and gives none of what this needs: no per-folder access control, no search, no
ordering, no "who uploaded this", no soft delete, and it pages slowly with a cost per request. The
DB index costs keeping two things in step, which `import_arkiv` does and an audit command will
check. Live listing stays fine for a debugging command.

**Presigned URLs in the page.** Rejected *here*, though it is what media does. A presigned URL to a
Regnskabsgruppen document is a bearer token for that document: valid for its lifetime, forwardable
to anyone, and unaffected by the reader leaving the group ten minutes later. Downloads route through
a Django view that re-checks access on every request. The cost is a redirect per file and no shared
caching — acceptable for documents, and worth revisiting *only* for the thumbnail grid, where the
objects are public-ish and the volume is a hundred per page.

**Path-shaped URLs (`/arkiv/billeder/2026/fest/`).** Deferred, not refused. Prettier, and would need
every segment resolved and access-checked on the way down. Archive URLs are followed from the page
rather than typed or pasted into a chat, so `mappe/<pk>/` is enough until that stops being true.

**A separate read-only permission tier.** Rejected. The kollegium is a hundred people who already
share one Dropbox password; a folder you can read is a folder you can add to. Every write is
attributed and soft-deleted rather than lost, which is a better answer to the real risk (somebody
tidying up over-enthusiastically) than a permission matrix nobody maintains.

**Letting anyone create root folders.** Rejected. The root is the kollegium's filing system, and an
unowned root is the junk drawer that made the Dropbox unusable. Roots are Inspektionen's;
*everything below them* is free, because needing a ticket to make a folder for this year's fest is
how an archive turns back into a chat thread full of attachments.

**Deleting objects when a row is deleted.** Impossible by construction, and worth saying out loud:
two rows can share one object. `services.unreferenced_keys` is the only thing that may decide bytes
can go, and soft-deleted rows still count as references — otherwise undo restores a row pointing at
nothing.

## Not built yet

Browse, download and **upload** are built. Still to come, in rough order:

1. **Thumbnails.** Client-side via the existing `downscaleImage()` (`frontend/src/imageupload.ts`),
   uploaded as a second object under `arkiv-thumb/`. Same no-Pillow, no-worker posture as the rest of
   the app. For the imported backlog, a **one-off local script** where Pillow as a dev-only
   dependency is fine — it must not become a prod dependency.
2. **Folder and file management in the app** — create, rename, move, soft delete, restore. Today the
   admin does it.
3. **An audit command**, the sibling of `audit_media`: rows whose object is missing, and objects no
   row references. Report-only, for the reasons that command's docstring gives.
4. **Search.** A file archive without it is a filing cabinet in the dark, and 2 TB makes that acute.
