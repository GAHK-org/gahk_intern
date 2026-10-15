# Feature: Pylon — retired public calendar, kept only as archived data

**Feature ID:** F-008

## Status: retired, not ported

The PHP site had a public "pylon" page (`/pylon/`): a CMS text block (legacy page id 5) above a
table of upcoming dated notices, with an editor-only screen to add and delete entries. The rewrite
has **no pylon page, route or editor**. `/pylon` falls through the CMS catch-all, finds no page and
no redirect, and returns 404 (`cms.views.page`). DEPLOY.md's URL list marks it `/pylon` → n/a.

**What took its place.** Public dated announcements are `cms.Event` on `/begivenheder/` (F-007);
residents' own events with tilmelding are `events.Event` (`begivenheder.md`).

## What remains in code

- **`cms.PylonEvent`** (`title`, `description`, `starts_on`) — the legacy `gahk_pylon_calendar`
  rows, kept as an archive. Deliberately *not* registered in the admin: "a retired feature kept as
  an archive, so it is data to migrate, not a screen anybody needs" (`cms.models.PylonEvent.Meta`
  comment). Nothing renders it. `seed_demo` creates one row so the table is not empty in dev.
- **The pylon text block**, legacy page id 5: the since-removed import copied every `gahk_page` row
  and gave a slug only to the pages in the legacy route map, so this one (if present in production)
  is a `cms.Page` with no address. It shows in the page admin as *Ingen adresse*.
- **Images** under `static/legacy/image/upload/images/`: `pylon-bg/pylon_3_scaleret2.JPG` and
  `Pylonjulefrokost_2019_1.jpg`. They are only reachable if some content still references them.

## Open questions

- Should `PylonEvent` (and page id 5) be dropped, or exported somewhere else, now that the
  migration is done?
- Should `/pylon` 301 somewhere, such as `/begivenheder/`, instead of 404ing?
