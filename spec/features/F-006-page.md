# Feature: Page — the public site's CMS pages, and how editors change them

**Feature ID:** F-006

## What it is

The public website (`gahk.dk`) is a handful of Danish content pages — Faciliteter, Kollegielivet,
Vision, Legater, Kontakt and their sub-pages — each one a `cms.Page` row holding a heading, an HTML
body and a background image. Visitors read them anonymously. Residents holding a content-editor role
edit them in the Django admin at `/django-admin/cms/`, which is the only write path back into the
content.

**What it replaces.** The PHP site served every page from one controller by numeric id, with SEO
aliases (`faciliteter/kokken` → page 12) in its route map, and let editors with the `editpage` right
change heading, body and background inline with CKEditor/KCFinder. The rewrite keeps the public
addresses but makes the address itself the lookup key, and moves editing into the role-gated admin.
The page rows were imported once; the import tooling has since been removed from the repo.

## Access

| Action | Who |
| --- | --- |
| Read a page | everyone, no login |
| List, add, edit pages, redirects and images; view and restore history | `administrator`, `indstilling`, `inspektion`, `pr` (`residents.permissions.CMS_EDITOR_ROLES`), or a superuser |
| Delete a page | `administrator` only (`PageAdmin.has_delete_permission`) |
| Add, change or delete a page *version* | nobody — `PageVersionAdmin` is read-only |

`cms.admin.ContentEditorAdmin` overrides every `has_*_permission` with
`has_active_role(user, *CMS_EDITOR_ROLES)`. Django's per-model permission bits are not used: role
holders are `is_staff` but have no model perms, so without the overrides they would see nothing.
Other staff roles (ak, ølkælder, køkkengruppe, regnskab) get 403 on the CMS screens. The `pr` role
comes from being on the "PR-gruppen" embedsgruppe for the month.

Pages are deleted by administrator only because there is no soft-delete and no undo for a Page; the
`False` return also removes admin's "delete selected" action. Redirects are deliberately *not* held
to that rule: deleting one only stops an old link working.

## Routes

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `/` | GET | public | `cms.views.home`: front page — body of Page id 1, hard-coded hero image, 3 upcoming front-page events and 4 latest news titles (see F-007) |
| `/<path>` and `/<path>/` | GET | public | `cms.views.page`: catch-all, last in `config/urls.py` (`re_path(r"^(?P<url_path>[\w/-]+?)/?$")`) |
| `/django-admin/cms/page/…` | GET/POST | editors | list, add, change; delete for administrator |
| `/django-admin/cms/page/<pk>/versioner/` | GET | editors | version history with diffs |
| `/django-admin/cms/page/<pk>/versioner/<id>/gendan` | POST | editors | restore a version (405 for GET) |
| `/django-admin/cms/pageredirect/…` | GET/POST | editors | old addresses, editable by hand |
| `/django-admin/cms/pageversion/…` | GET | editors | site-wide read-only change log |
| `/django-admin/cms/cmsimage/…` | GET/POST | editors | image library |
| `/django-admin/cms/cmsimage/toolbar/list` | GET | editors | JSON list for the body toolbar |
| `/django-admin/cms/cmsimage/toolbar/upload` | POST | editors | JSON upload for the body toolbar (201) |

`cms.views.page` resolves in a fixed order, documented in its docstring as load-bearing:

1. **A live page** whose `slug` equals the stripped path is rendered. A new page therefore reclaims
   an address that used to redirect elsewhere, and a redirect can never shadow real content.
2. **A former address** (`PageRedirect.old_path`) 301s to the page's current slug, keeping the query
   string (`_redirect_preserving_query`). Only if that page still has an address.
3. **APPEND_SLASH restoration**: because the catch-all matches slashless paths such as `/optagelse`,
   `/intern`, `/nyintern` and `/begivenheder`, Django's APPEND_SLASH never fires for them. If
   `/<path>/` resolves to some view other than the catch-all, the view 301s there.
4. Otherwise 404.

## Data model

- **`Page`** — `slug` ("Adresse (URL)", `CharField(80)`, unique, nullable), `header`, `body` (HTML),
  `background_image` (a URL *string*, not a file field), `menu_category` (legacy `menuCat`,
  `editable=False`; nothing reads it, kept as the only record of the legacy menu grouping).
  `get_absolute_url()` reverses the catch-all, or returns `""` for a page with no address.
- **`PageRedirect`** — `old_path` (unique, same validator), `page` (FK, **CASCADE**), `created_at`,
  `created_by`.
- **`PageVersion`** — a full snapshot: `slug`, `header`, `body`, `background_image`, `note`,
  `created_at`, `created_by`, `page` (FK, **SET_NULL**). Ordered `-created_at, -id`.
- **`CmsImage`** — `file` (`FileField`, `upload_to="cms/%Y/%m/"`), `caption` ("Beskrivelse", used
  as alt text), `uploaded_at`, `uploaded_by`. A `post_delete` receiver deletes the stored file.

A page with `slug = NULL` is legitimate: the `optagelse` bodies have no public address and are
rendered by the admissions app (F-001). The admin stores `None`, never `""`, because `unique` allows
many NULLs but only one empty string.

## Addresses

One rule set, `cms.paths.validate_page_path`, is shared by the model field, the admin form,
`PageRedirect`, the system check and the tests:

- at most **two segments** (`MAX_SEGMENTS = 2`) — the deepest the legacy route map went, and the
  sidebar derives a page's parent from the first segment only;
- each segment matches `^[a-z0-9]+(?:[-_][a-z0-9]+)*$` — no uppercase, spaces, `.`, `..` or `æøå`;
- no leading or trailing `/`, no `//`;
- the first segment must not be in `RESERVED_TOP_SEGMENTS` (`admin`, `django-admin`, `intern`,
  `nyintern`, `optagelse`, `begivenheder`, `kalender`, `media`, `photo-album`, `static`, `sw.js`,
  `favicon.ico`, `robots.txt`). Only the first: `faciliteter/media` is fine.

`cms.checks.check_reserved_top_segments` (error `cms.E001`) fails `manage.py check` if
`config/urls.py` mounts a literal top-level prefix that is missing from that list. It never touches
the database.

**The admin form composes the address.** `PageAdminForm` hides `slug` and offers a *Placering*
picker (`path_parent`: top level, or any top-level page other than the page itself) plus *Sidste del
af adressen* (`path_segment`). `normalize_segment` trims, lowercases and turns whitespace into `-`;
anything still illegal is reported on `path_segment`, as is a missing segment under a section and an
address already used by another page (named in the message). `static/cms/page_path.js` previews the
resulting address live but never writes the field.

## Navigation and reachability

There is no navigation table. The public menus are hard-coded lists (`NAV_PUBLIC`, `NAV_LEGACY` in
`core.context_processors`) of top-level entries. A sub-page is linked only from the **section
sidebar** that `cms.views._section_nav` builds from `cms.nav.section_pages` — every page whose slug
is the section or starts with `section/`, ordered by slug — and the sidebar renders only when the
section holds at least two pages. Its title is the top-level page's header.

`cms.nav.statuses` classifies every page in one pass: `nav` (in a menu, or the front page id 1),
`section` (sub-page of a menu section with a rendered sidebar), `linked` (its `/slug` appears in
another page's body), `unrouted` (no address — deliberate) or `orphan` (the only problem state).
The page changelist shows this as a *Synlighed* column and filter, and saving a page that ends up
orphaned shows a warning ("Siden står ikke i nogen menu…").

## Editing

- **Sanitizing.** `clean_body` runs `cms.sanitize.clean_html` (nh3) on every save: a generous
  allowlist (tables, `style`/`class`/`id` on everything, images, links) with `http`, `https`,
  `mailto` and relative URLs, and `rel="noopener noreferrer"` forced on links. Bodies are then
  rendered with `|safe`.
- **Background image** is a dropdown of library images, with the current value kept as an extra
  option so migrated `/public/…` paths survive a save.
- **Image toolbar.** `static/cms/insert_image.js` adds upload/insert above the `body`/`description`
  fields. Uploads go through `core.uploads.validate_image_upload` with `CMS_IMAGE_MAX_MB` (default
  5): SVG, disguised extensions, non-images and oversized files are refused. The returned
  `/media/cms/…` URL is inserted with the caption as alt text. `/media/cms/` is one of the prefixes
  `core.media` serves anonymously, because the public pages embed these images.
- **Image usage.** The `CmsImage` change form lists where the image is referenced (page, news and
  event bodies, page backgrounds), computed only on that form.
- **Save.** `PageAdmin.save_model` reads the row before saving. If the page has no versions yet, it
  first snapshots the pre-edit state with an unknown author. After saving it snapshots again
  ("Redigeret i CMS") and calls `cms.services.record_slug_change`, which deletes any redirect
  *from* the new address and upserts one from the old address to this page.
- **History.** `versions_view` pairs each version with its predecessor: before/after rows for
  address, heading and background, and a `difflib` line diff of the body (`cms.diff.line_diff`,
  rendered escaped; skipped above 200,000 characters).
- **Restore.** `restore_view` (POST, atomic) snapshots the current state, writes back the version's
  header, re-sanitized body and background, and its address only if that is still valid and free —
  otherwise it keeps the current address and says why. It records the redirect, snapshots again and
  writes an admin `LogEntry`.
- History is capped at `VERSIONS_PER_PAGE_CAP = 100` snapshots per page; the oldest are trimmed.

## Legacy image paths

Migrated content refers to the old site's `/public/…` files, which now live under
`static/legacy/`. Two filters in `cms/templatetags/cms_extras.py` rewrite them at render time:

- `legacy_img` maps one stored path (`/public/image/…`) to `static("legacy/…")`. Used for the page
  background in `cms/page.html` and `public_base.html` (default `gang.jpg`) and the front-page hero.
- `body_media` rewrites `src`/`href` attributes in body HTML that point at `/public/…`, `public/…`
  or `http(s)://[www.]gahk.dk/public/…` to `/static/legacy/…`, which also removes the mixed-content
  warnings of the absolute `http://` form. It leaves `/media/` URLs alone.

## Decisions

- **The lookup key is the whole path, in a `CharField`, not a `SlugField`.** The router needs `/`,
  and `forms.SlugField` re-adds `validate_slug` regardless of the model field (`Page.slug` comment).
- **Redirects point at the page, not at a path.** Renaming `a → b → c` leaves both `a` and `b`
  resolving to `c` in one hop, and no chain-following or loop detection is needed
  (`PageRedirect` docstring).
- **Versions use SET_NULL, redirects CASCADE.** History is the recovery story and must outlive a
  deleted page; a redirect to a deleted page has nowhere to go, and freeing `old_path` matters more.
- **Snapshots and redirects are written explicitly from the admin, not from a `post_save` signal.**
  A signal would also fire for `seed_demo` and shell saves, producing author-less snapshots that
  cannot be told apart from real edits (`cms.services` docstring).
- **The site-wide change log is `PageVersion`, not admin's `LogEntry`.** `LogEntry` covers every app
  and its `object_repr` can be an applicant's name or personnel data, which the `pr` role has no
  business reading (`PageVersionAdmin` docstring).
- **The editor-facing safeguards exist because of one incident:** an editor renamed
  `/faciliteter/kokken` to `faciliteter-kokken`, the page dropped out of its sidebar, and the old
  value could not be typed back because the form rejected `/` (`cms.paths`, `cms.admin` docstrings).
- **Admin screens use plain DOM JavaScript** (`static/cms/`) because Django admin does not load the
  Vite bundle.

## Open questions

- **`legacy_img` breaks library backgrounds.** It prefixes every non-`/public/` path with
  `legacy/`, so a background chosen from the image library (`/media/cms/…`) renders as
  `/static/legacy/media/cms/…`. No test renders a page with a library background.
- **`header` is not sanitized but is rendered `|safe`** (`<title>`, `<h1>`, sidebar links). Only
  `body` goes through `clean_html`, so the promise that editors cannot inject script does not hold
  for the heading.
- Every page answers on both `/x` and `/x/`, with no canonical redirect between them.
- Orphaned versions of a deleted page are visible in the change log but cannot be restored from the
  UI: `restore_view` requires the version to belong to an existing page.
- The history screen says "Der bliver aldrig slettet noget i historikken", but snapshots beyond 100
  per page are trimmed.
- Several comments still describe the removed import or a no-editing CMS: the `Page.menu_category`
  comment ("the ETL writes it on every run"), the `cms.models` docstring ("sanitized on import"),
  `cms/page.html` ("code/fixture-managed") and `residents.models` ("no runtime CMS editing").
