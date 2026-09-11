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

## Three sizes, and the viewer

An image is stored three times: the original under `arkiv/`, a 320px thumbnail under `arkiv-thumb/`,
and a ~1600px preview under `arkiv-preview/`. All three are keyed by the **original's** hash, so two
rows sharing bytes share all three objects and none of them can go stale — different bytes are a
different key.

The middle size exists because neither neighbour can do its job. The thumbnail is drawn at 40px in a
row and is porridge full-screen; the original is a phone photograph of ten megabytes or a scan of
forty, and paging through a folder of those is minutes of waiting on the one variable line of the
Hetzner bill. Egress is the cost that scales with use here — storage is fixed and predictable — so
the preview is a billing control as much as a UX one.

**Both derived sizes are made in the browser, at upload.** `begin` offers a slot for each size the
store has not already got; the browser renders them off the same canvas that downscales room
photos, and `commit` asks the *store* which ones arrived before setting the flags. Two rows sharing
bytes share all three objects, so the second copy of a photograph uploads nothing at all — not even
a thumbnail.

The alternative was Pillow in the production image and a scheduled sweep, and it was rejected
twice over: it breaks the no-worker, no-Celery posture the whole project is built on, and it leaves
a window in which the newest photograph is the one with no preview — while the person who just
uploaded it is precisely the one about to open the folder and look. `make_arkiv_thumbnails` stays
the one-off for the imported backlog and a hand-run net for whatever a browser could not decode.

The cost is two implementations of the same two sizes, one in Pillow and one on a canvas. The
constants name each other in both files. Drift is cosmetic — a folder showing previews at two
sizes depending on how its files arrived — but it is invisible until somebody notices.

**A missing size degrades rather than breaks**, which is what makes the best-effort upload legs
safe. No thumbnail is a file icon; no preview means the viewer serves the original — slower and
more egress, but not a broken image. That fallback is load-bearing for any file that predates this,
for anything the browser could not decode, and for every one of the 57,752 imported photographs
until the backlog command reaches it.

**The viewer is a progressive enhancement.** Each image row is an ordinary link to the download
view; the script intercepts an unmodified left click and opens the overlay instead. Ctrl-, cmd-,
shift- and middle-click are deliberately left alone — hijacking them is what makes a gallery
infuriating — and with the bundle dead, clicking an image downloads it.

## Downloading several at once

Selected files come back as one zip that is **built into the bucket and then redirected to**, like
every other download here. `ZIP_STORED`, because the contents are JPEGs and video and deflate would
spend real CPU on the machine serving the site to save a percent.

The first version streamed the zip straight to the browser, and that was a mistake worth recording.
It made this the only route in Arkiv that holds a gunicorn worker — and held it not for as long as
the zip took to *build* but for as long as the recipient took to *receive* it, because TCP
backpressure means the server can only write as fast as the browser reads. One resident on hotel
wifi occupied one of three synchronous workers for the whole download, and was killed at
`--timeout 60` regardless, left holding a truncated archive.

Building it as an object decouples that completely: the worker waits only for the build, which is
server-to-Hetzner traffic inside `fsn1`, and then hands back a redirect. **This needed no job
runner** — the build is still synchronous, all that changed is what the worker is waiting for. The
Celery-shaped version, where the build happens out of band entirely, would only remove the wait
itself, and is not worth a queue.

**Reused, not rebuilt.** `selection_key` names the object after the selection — each member's
display name and hash — so the morning after sommerfest one build serves everybody who asks for the
same folder. It is also what makes reuse safe with no invalidation logic: adding, removing,
renaming or replacing a file changes the member list and therefore the key, so a cached zip is
always exactly the selection that was asked for. The key is derived from the *access-filtered* list,
so a selection can only ever be named by files its asker may see.

The objects are disposable and expire on a lifecycle rule (DEPLOY.md). **The caps
(`MAX_SELECTED_FILES`, `MAX_SELECTED_BYTES`) now bound the build and the temporary file**, not a
resident's connection — which is the difference between a limit set against something measurable and
one set against hotel wifi.

**POST, not GET**, though nothing is modified: a couple of hundred ids do not belong in a URL, and a
GET would be a link somebody could paste into a chat thread to start a half-gigabyte download for
whoever clicked it. The ids arrive from the client, so they are re-checked through `visible_files`
scoped to the folder — the same rule the listing used, applied again rather than trusted.

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

Browse, download, upload, subfolders, soft delete and restore, the three image sizes, the viewer and
batch download are built. Still to come, in rough order:

1. **Rename and move.** Both are DB-only by construction — the key is the hash, not the path — so
   this is a form and an access check, not a data migration. The admin does it today.
2. **An audit command**, the sibling of `audit_media`: rows whose object is missing, and objects no
   row references. Report-only, for the reasons that command's docstring gives. `_zip_chunks`
   skipping a vanished object rather than truncating the archive is a placeholder for it.
3. **Search.** A folder tree of 57,000 photographs is navigable only if you know where you put
   something. The DB index is what makes this possible at all, and is a large part of why the
   archive is not rendered by listing the bucket.
4. **Search.** A file archive without it is a filing cabinet in the dark, and 2 TB makes that acute.
