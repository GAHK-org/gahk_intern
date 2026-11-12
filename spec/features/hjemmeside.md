# Feature: Hjemmeside — editing gahk.dk from the intranet

All of gahk.dk's content is kept and edited on intern.gahk.dk, under Hjemmeside. gahk.dk itself is
static pages on separate webhosting, which intern updates whenever something is saved. gahk.dk keeps
working when intern or the image storage is down; editing needs intern.

## Who

- Administrator, Indstillingen, Inspektionen and PR-gruppen (the roles of the current month), and
  superusers. Nobody else sees Hjemmeside.
- Only an administrator may delete a page. The front page can never be deleted.

## What can be edited

- **Pages:** address (at most two levels, e.g. `faciliteter/kokken`), heading, text, background
  image and a short description for search engines. A page without an address is kept but not shown.
  The front page is shown at `/` whatever its own address is.
- **Optagelse** is an ordinary page at `/optagelse`, so the rent and the criteria can be changed
  there. Its two forms, `/optagelse/ansoeg` and `/optagelse/fremlej`, belong to gahk.dk.
- **News and front-page events** (title, text, date). Not the residents' own events.
- **The menu:** its entries, their order and their links (a page on gahk.dk or an outside address).
- **Images:** a library of pictures for pages, news, events and backgrounds, and the default
  background for pages that have none of their own.
- **Old addresses:** where an old link should send visitors.
- Headings and titles are plain text. Page text is HTML; anything that could run a script is
  removed when it is saved, and again whenever it is published. Text stored before the editor
  existed was cleaned once, recorded as a system change.

## The editor

- Hjemmeside is in the intranet's menu under Administration, at `/hjemmeside/`, with tabs for the
  overview, pages, news, front-page events, images, the menu, old addresses and the change log.
  gahk.dk's "Rediger siden" opens a page's editor directly.
- Pages are listed by section, each with its address, who changed it last, a link to it on gahk.dk,
  and whether the menu leads to it.
- A page's address is picked as a section plus a last part, so it cannot be typed into a third level
  or onto an address gahk.dk uses itself, and taken addresses are refused.
- Text is edited visually: headings, bold, italic, lists, quotes, links, pictures from the library
  (or uploaded on the spot) placed in the text, left or right, with an alt text, undo and redo.
  An "HTML" button shows the text as HTML. Pasted text keeps its structure but not its fonts and
  colours. Pictures on the old pages keep their size and placement.
- Text the visual editor cannot show (a table, for example) opens as HTML with a note, so nothing is
  lost by opening it. Saving a page untouched changes nothing.
- If someone else saved the page after it was opened, saving stops once and says so; saving again
  overwrites.
- An image can only be deleted when nothing uses it; the library shows where each one is used.
- The menu is edited in place: text and link per entry, move up or down, remove, add.
- The change log can be filtered by type and person and shows each change's before and after; text
  as a line-by-line difference.
- An editor's browser carries a harmless cookie on gahk.dk's domain, which is how gahk.dk knows to
  show "Rediger siden" (it opens the page's editor, or the overview). It is given at login, and on
  any page if it is missing, to whoever really holds an editor role (a role preview does not count),
  and taken away at logout and as soon as the role is gone. Following the link checks the role
  again.

## History and audit

- Every change is recorded: who, when, what, and the values before and after — pages, news,
  events, images, the menu, old addresses and settings alike. Automatic changes are recorded as
  "System", and the log's System filter shows all of them.
- Saving a news item without changing it writes nothing, even though its time is edited to the
  minute.
- Every saved version of a page is kept and can be restored. A restore is itself a change, so it can
  be undone too. If the old address is taken by another page by then, the content comes back and the
  address does not, and the editor is told so.
- Renaming a page keeps the old address working: it sends visitors on to the new one.
- A page that nothing in the menu leads to (neither itself nor its section) is pointed out.

## Images

- Uploads must be JPEG, PNG, GIF or WebP (an iPhone's HEIC becomes a JPEG). They are made at most
  2400 px on the long side, turned upright, and stripped of metadata such as the GPS position.
- gahk.dk gets its own copy of every image it shows. A copy's address never changes, so browsers
  may keep it for a year.
- The old site's pictures move into the library by themselves: the sync every two minutes fetches
  the ones the content still uses (a batch at a time) and points the content at the library copies,
  recorded as "System". Their old addresses on gahk.dk send visitors on to the copies.
- A picture that cannot be fetched or is not a usable image is reported once and not tried again;
  the overview lists them, and a manual import can try them again. Until a picture has moved, it is
  missing on gahk.dk.
- Images keep their file name, Danish letters included (Værelse på Øen.jpg is stored as
  Værelse_på_Øen.jpg and shown on gahk.dk as værelse-på-øen.jpg). Images already stored under a
  name without them keep that name.

## Publishing

- Saving publishes automatically, a moment after the save. There is also "Udgiv igen nu".
- Every two minutes intern checks that gahk.dk shows the current content and publishes again if
  not, so a publish that failed while gahk.dk was down catches up by itself.
- Every night after midnight gahk.dk renders its pages again, so passed events drop off.
- Every publish is listed with when, why, by whom, and whether it went well.

## Preview

- A page, news item or event can be previewed as gahk.dk would show it, with the current menu,
  before it is saved. Nothing is published by a preview.
- When gahk.dk cannot be reached (or intern is not connected to it), the preview shows the text alone
  in the intranet's styling and says why.

## Status of gahk.dk

- The overview shows whether gahk.dk is reachable, whether it shows the current content (not while a
  saved change is still waiting to be published), when applications and visit counts were last
  fetched, and the last error.
- A publish that fails never stops applications and visit counts from being fetched.
- It shows the code version gahk.dk runs, the newest one available and how the last update went.
  gahk.dk updates its code only when intern asks it to: every 15 minutes, or at once with
  "Opdatér gahk.dk nu" (administrators only).

## Decisions

- **Edited on intern, shown by gahk.dk.** gahk.dk has no login and no access to intern's database or
  image storage; intern sends it signed snapshots. Losing the webhost loses no content.
- **The Django admin no longer edits website content.** It would be a second way in that records no
  history and publishes nothing.
- **The old pictures are content, not code.** They lived in the code repository; now they are in
  the image library like any other picture, and the repository no longer holds them. The import
  fetches them from the repository's public history (one fixed version), so nobody has to remember
  to supply a copy at the move.
- **File names stay UTF-8.** Æ, ø and å are kept, not spelled out: a name only has to be
  normalized (a Mac sends "å" as two characters) and percent-encoded where it is part of an address.
- **Plain-text headings.** They were shown as HTML without ever being cleaned.
- **The menu is data.** It was written into the code, so changing it needed a developer.
- **A visual editor that keeps the old pages intact.** Editors should not need HTML, but the old
  pages' picture layout must survive being opened and saved, and anything the editor cannot show is
  left as HTML rather than silently dropped.
- **The editor cookie is a hint, not a permission.** gahk.dk's pages are static and cannot ask intern
  who is signed in, so they read a cookie; it grants nothing. It changes only when the editor
  status does, so ordinary pages stay free of cookie headers.
- **The preview never runs scripts on intern.** It is gahk.dk's HTML shown inside the intranet, so it
  is shown without JavaScript.
