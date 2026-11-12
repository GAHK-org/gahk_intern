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
  removed when it is saved.

## History and audit

- Every change is recorded: who, when, what, and the values before and after — pages, news,
  events, images, the menu, old addresses and settings alike. Automatic changes are recorded as
  "System".
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
- The old site's pictures were moved into the library the first time intern published. Their old
  addresses on gahk.dk send visitors on to the copies.

## Publishing

- Saving publishes automatically, a moment after the save. There is also "Udgiv nu".
- Every two minutes intern checks that gahk.dk shows the current content and publishes again if
  not, so a publish that failed while gahk.dk was down catches up by itself.
- Every night after midnight gahk.dk renders its pages again, so passed events drop off.
- Every publish is listed with when, why, by whom, and whether it went well.

## Preview

- A page, news item or event can be previewed as gahk.dk would show it, with the current menu,
  before it is saved. Nothing is published by a preview.

## Status of gahk.dk

- The overview shows whether gahk.dk is reachable, whether it shows the current content, when
  applications and visit counts were last fetched, and the last error.
- It shows the code version gahk.dk runs, the newest one available and how the last update went.
  gahk.dk updates its code only when intern asks it to: every 15 minutes, or at once with
  "Opdatér gahk.dk nu" (administrators only).

## Decisions

- **Edited on intern, shown by gahk.dk.** gahk.dk has no login and no access to intern's database or
  image storage; intern sends it signed snapshots. Losing the webhost loses no content.
- **The Django admin no longer edits website content.** It would be a second way in that records no
  history and publishes nothing.
- **The old pictures are content, not code.** They lived in the code repository; now they are in
  the image library like any other picture, and a later change removes them from the repository.
- **Plain-text headings.** They were shown as HTML without ever being cleaned.
- **The menu is data.** It was written into the code, so changing it needed a developer.
