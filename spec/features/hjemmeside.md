# Hjemmeside

The public website: the front page, the content pages, and news and front-page events, all edited by residents in the admin.

## Access
- Everyone can read the public site without logging in.
- Administrator, Indstilling, Inspektion and PR (the month's PR-gruppen) may add and edit pages, news, front-page events, old addresses and images. Other roles cannot open these screens.
- Only an administrator may delete a page. News, events, images and old addresses may be deleted by any editor.
- Editors reach the editor through "Rediger indhold" in the internal sidebar.

## Front page and menu
- The front page shows the welcome text (itself an editable page), a fixed hero image, the next 3 upcoming front-page events and the 4 newest news headlines, with a link to /begivenheder/. The teaser block is hidden when there is neither.
- The public top menu is fixed: Forside, Faciliteter, Kollegielivet, Vision, Legater, Begivenheder, Kontakt, Optagelse.
- A sub-page (e.g. /faciliteter/kokken) is reachable only from its section's sidebar, which lists every page in the section and appears once the section holds at least two pages.
- Visits to the front page are counted anonymously for the statistics page.

## Pages and their addresses
- A page has a heading, a body, a background image and an address of at most two levels (`faciliteter/kokken`).
- Addresses are lowercase letters, digits and `-`/`_`; no æøå, spaces or dots. Addresses that belong to the rest of the site (intern, optagelse, begivenheder, media, …) cannot be used.
- The editor picks a section (top level, or under an existing top-level page) and types the last part; the result is previewed as you type. Mistakes, a missing last part, or an address another page already uses (named in the message) are reported on the field.
- A page may have no address at all; it is then not public (used for text that another feature shows, such as the admissions pages).
- Changing an address keeps the old one working: it permanently redirects to the page's current address, keeping any query string. Renaming twice still redirects in one hop, and renaming back removes the now-pointless redirect.
- A live page always wins over an old address, so a new page can reclaim it.
- Editors can view, add, edit and delete old addresses by hand.
- Saving a page that is in no menu, no section sidebar and not linked from another page warns that it can only be reached by direct link. The page list shows each page's visibility and can be filtered by it and by section.
- Every page answers with and without a trailing slash; a slashless address of another part of the site (e.g. /intern) redirects there.

## Version history
- Every save stores a complete copy of the page; the first edit of an older page also stores what it said before.
- A page's history shows who changed what and when, with before/after for address, heading and background and a line diff of the body.
- Any version can be restored. The restore is itself recorded, so it can be undone. If the old address is invalid or taken by another page, the content is restored and the current address kept, with a message saying why.
- History is kept even when a page is deleted, and is capped at the 100 newest versions per page.
- A site-wide change log lists every page version; it is read-only.

## News and front-page events
- /begivenheder/ shows all upcoming front-page events (soonest first, with description), the 40 newest news items in full, and the 20 most recent past events (date and title).
- A news item has a title, a body and a date; an event has a title, a description and a date (no time, no sign-up). Both are public as soon as they are saved.
- The admin can filter events into Kommende/Afholdt using the same split visitors see.
- Front-page events are separate from the residents' own begivenheder in the internal area.

## Editing and images
- Page bodies, news bodies and event descriptions are HTML; unsafe markup is stripped on every save and again on restore.
- Editors can upload an image from the body editor and insert it, with its description as alt text. Images must be real raster images up to 5 MB; SVG and disguised files are refused.
- The image library shows where each image is used. Deleting an image deletes its file.
- A page's background is chosen from the image library; an existing old background is kept as an option.
- Image links in content migrated from the old site keep working.

## Decisions
- The address is the lookup key, multi-segment, because the public addresses had to survive the move from the old site unchanged.
- Old addresses point at the page rather than at a path, so a chain of renames never needs chain-following and cannot loop.
- History outlives a deleted page and pages are administrator-only to delete, because there is no undo for a deleted page.
- Snapshots and redirects are written only by the editor screens, not on every save, so a script or seed cannot fill history with author-less versions.
- The change log is CMS-only rather than the admin's general log, which would expose applicants' names and personnel data to the PR role.
- The address picker, history and orphan warning exist because an editor once renamed a page out of its section and it vanished with no way back.
