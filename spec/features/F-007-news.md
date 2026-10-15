# Feature: News — the public "Nyheder & begivenheder" page and its news archive

**Feature ID:** F-007

## What it is

Short news posts (`cms.NewsItem`) and dated public events (`cms.Event`, labelled
"Forsidebegivenhed" in the admin), shown to anonymous visitors on `/begivenheder/` and as a teaser
on the front page. Both are edited by the CMS editors in the Django admin, next to the pages of
F-006. There is no news detail page: every item is shown in full on the list.

**What it replaces.** The PHP site had a news controller (iframe list box, article pages, a
CKEditor create/edit form) and a separate "Begivenheder" box whose events were hard-coded in a PHP
view. By the time of the rewrite the live front page embedded a Facebook feed instead of the news
box. The news rows were imported as an archive; `cms.Event` is new and replaces the hard-coded
array (`cms.models.Event` comment).

**Not to be confused with** `events.Event` (`/intern/begivenheder/`, residents' events with
tilmelding — see `begivenheder.md`) or opslagstavlen's *Begivenhed* category. `cms.Event` is public
marketing copy: a date, a title and a description, no time and no attendance.

## Access

| Action | Who |
| --- | --- |
| Read `/` and `/begivenheder/` | everyone, no login |
| Add, edit, delete news and front-page events | `administrator`, `indstilling`, `inspektion`, `pr`, or a superuser |

Both admins extend `cms.admin.ContentEditorAdmin`, so the gate is the same as for pages (F-006),
including delete — there is no administrator-only delete rule for news or events.

## Routes

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `/` | GET | public | `cms.views.home`: next 3 upcoming events (title + date), 4 newest news titles, link to `/begivenheder/` |
| `/begivenheder/` | GET | public | `cms.views.events_news`: the full page (name `events_news`) |
| `/begivenheder` | GET | public | 301 to `/begivenheder/` via the catch-all's slash restoration (F-006) |
| `/django-admin/cms/newsitem/…` | GET/POST | editors | news list and editor |
| `/django-admin/cms/event/…` | GET/POST | editors | front-page event list and editor |

`begivenheder` is in `cms.paths.RESERVED_TOP_SEGMENTS`, so no CMS page or redirect can take the
address.

## Data model

- **`NewsItem`** — `title`, `body` (HTML), `published_at` (datetime). Ordered `-published_at`.
- **`Event`** — `title`, `description` (HTML), `starts_on` (date). Ordered `starts_on`.

There is no draft or visibility flag on either: a saved item is public.

## The public page

`cms/events_news.html` renders three sections, splitting on `timezone.localdate()`:

1. **Kommende begivenheder** — every event with `starts_on >= today`, soonest first, with day,
   month and the description (`|safe`). Empty state: "Ingen kommende begivenheder er annonceret
   lige nu."
2. **Nyheder** — the 40 newest news items, each with title, date (`j. F Y`) and full body, the body
   passed through `body_media` so legacy `/public/…` image links resolve (F-006). Empty state:
   "Ingen nyheder endnu."
3. **Tidligere begivenheder** — the 20 most recent past events, date and title only; the section is
   omitted when there are none.

The front page (`cms/home.html`) shows its teaser block only if there is at least one upcoming event
or news item. News titles there are plain text with a date, not links.

## Editing

- `NewsItemAdminForm` and `EventAdminForm` sanitize `body` / `description` with
  `cms.sanitize.clean_html` on save, the same allowlist as page bodies.
- Both forms carry the image upload/insert toolbar (`BodyEditorMixin`, `static/cms/insert_image.js`),
  so library images can be placed in news bodies and event descriptions. The `CmsImage` usage
  column lists references from both.
- News admin: list by title and date, date hierarchy and filter on `published_at`, search in title
  and body, newest first.
- Event admin: date hierarchy on `starts_on`, a *Tidspunkt* filter (`UpcomingFilter`: *Kommende* /
  *Afholdt*) using the same split as the public page, search in title and description.
- Neither has version history or redirects; those are page-only (F-006).

## Decisions

- **`cms.Event` is labelled "Forsidebegivenhed", not "Begivenhed"**, so the admin index does not
  show two identically named sections next to `events.Event` (`cms.models.Event.Meta` comment).
- **The admin's upcoming/past split mirrors the public view**, so an editor filtering *Kommende*
  sees exactly what visitors see (`UpcomingFilter` docstring).

## Open questions

- **News titles are rendered `|safe` but never sanitized** (`events_news.html`, `home.html`); only
  `body` goes through `clean_html`. An editor can put script in a title.
- Only the 40 newest news items are ever shown; older ones are unreachable from the site, with no
  pagination or archive view.
- A news item with a future `published_at` is shown immediately — the field is a sort key, not a
  publish date.
- The `NewsItem` model comment calls it "archive-only (live site shows the Facebook feed)", but news
  is editable in the admin, featured on the front page, and no Facebook feed exists in the
  templates. Is news meant to be written going forward?
- `begivenheder.md` calls `cms.Event` "developer-edited"; it is editable by the CMS roles in the
  admin.
- Event descriptions are not passed through `body_media`, unlike news bodies, so a legacy
  `/public/…` path in a description would not be rewritten.
