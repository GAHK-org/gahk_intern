# Feature: Dashboard — the internal-area landing page, and the secrets it used to hard-code

**Feature ID:** F-013

## What it is, and what it replaces

`/intern/` is where a resident lands after logging in (`LOGIN_REDIRECT_URL = "/intern/"` in
`app/config/settings.py`). It greets the resident by first name and shows the active month, their
roles for that month, the shared Google Calendar agenda, a few shortcuts, the WiFi password and the
shared calendar login, and the photo album's daily strip.

The PHP dashboard was a mostly static page that printed the WiFi password and a shared Google
account's credentials straight from the template source, revealed only to a hard-coded list of
campus IPs. The rewrite keeps the content but changes both halves of that: the values come from the
environment, and the gate is "logged in", not "on the campus network". Because the old spec for
this page is where hard-coded secrets were flagged, `F-013` is also the citation the code uses for
**"secrets are read from the environment, never from source"** — see *Configuration* below.

## Access

| Action | Who |
| --- | --- |
| View the dashboard, incl. WiFi password and calendar login | every logged-in resident |
| See the Ølkælder-admin / AK-oversigt / Ansøgninger shortcuts | `oelkaelder` / `ak` / `indstilling` |

`residents.views.dashboard` is `@login_required`; anonymous users go to `LOGIN_URL`
(`/intern/admin/login`). Roles are the *effective* roles (`residents.permissions.effective_roles`),
so an administrator previewing the site as another role sees that role's chips and shortcuts
(`app/tests/test_preview.py`).

## Routes

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `/intern/` | GET | logged in | The dashboard (`residents.views.dashboard`, name `dashboard`) |
| `/nyintern`, `/nyintern/<rest>` | GET | anyone | 301 to `/intern/<rest>` — the legacy bookmarks (`app/config/urls.py`) |

`test_slashless_legacy_urls_redirect_instead_of_404` pins `/nyintern` → `/nyintern/` (301), which
then hits the `nyintern/<rest>` redirect.

## What the page shows

From `app/templates/residents/dashboard.html`:

- **Header:** "Goddag, {first name}" and a *Søg værelse* button (`/intern/soegvaerelse/`).
- **At a glance:** the active period as `YYYY-MM` (`residents.models.active_period`, i.e. the newest
  published residency list that has already started) and the resident's roles as raw role codes,
  sorted; "Ingen særlige denne måned" when there are none. An administrator sees every role.
- **Begivenheder på GAHK:** the shared Google Calendar as an agenda-mode `<iframe>`, or a note that
  it is not configured. The URL is built by `residents.views._calendar_embed_url` from
  `GOOGLE_CALENDAR_USER` plus the comma-separated `GOOGLE_CALENDAR_EXTRA_IDS`, with the
  Europe/Copenhagen time zone, Monday as the first weekday and all chrome hidden. Empty user → no
  iframe.
- **Genveje:** links to Alumneliste, Mine AK-krydser and Min ølkælder-saldo for everyone, plus the
  role-gated shortcuts in the Access table. Below them: the WiFi password (`WIFI_PASSWORD`, or the
  placeholder "(sæt WIFI_PASSWORD)"), the shared calendar's user and, if set, its password, and a
  link to change your own password.
- **Dagens billeder / På denne dag:** the `{% photo_album_strip %}` inclusion tag
  (`app/photo_album/templatetags/photo_album_extras.py`). Rendered only if there is anything to show;
  see `spec/features/Photo-album.md` for how the pictures are picked.

The sidebar (`core.context_processors.navigation`) is shared by every internal page and is not part
of this feature.

## Side effects of loading the page

A GET of `/intern/` is not read-only:

- **AK monthly charge backstop.** The view calls `ak.services.ensure_active_month_applied()` first.
  If the `ak_monthly_assessment` cron job has not booked the active month's AK deduction yet, the
  first dashboard load of the month does it. It is a no-op once the month is marked applied;
  failures are logged, never raised, and retried on the next request.
- **Daily photo pick.** `photo_album.featured.featured_for(day)` stores the day's featured pictures
  (`FeaturedMedia`) the first time anyone loads the dashboard that day; everyone sees the same set
  for the rest of the day.

## Configuration: secrets from the environment

`app/config/settings.py`, `app/.env.example` and `DEPLOY.md` §2 all cite F-013 for the same rule:
secrets live in the environment (`app/.env` in dev, `app/.env.prod` in production, the team vault
as the source of truth), never in the repository, and are rotated at cutover because the legacy
values were committed. The values this covers:

| Variable | Read by | Used for |
| --- | --- | --- |
| `WIFI_PASSWORD` | `residents.views.dashboard` (`os.environ`, per request) | Shown on the dashboard |
| `GOOGLE_CALENDAR_USER` | `dashboard`, `_calendar_embed_url` | Shown; first calendar in the embed |
| `GOOGLE_CALENDAR_PASSWORD` | `dashboard` | Shown on the dashboard |
| `GOOGLE_CALENDAR_EXTRA_IDS` | `_calendar_embed_url` | Further calendars in the embed |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_PORT` | settings → `EMAIL_*` | Outgoing mail |
| `VISIT_COUNTER_HMAC_KEY` | settings | Key for hashing visitor IPs (below) |

The dashboard reads its four values straight from `os.environ` rather than through `settings`, so
a changed value takes effect without a settings reload; the tests rely on this and set them with
`monkeypatch.setenv` (`test_dashboard_shows_shared_calendar_credentials`).

### The front-page visit counter

`app/stats/middleware.py` (`FrontPageVisitCounterMiddleware`) and `app/stats/models.py` also cite
F-013, as part of the legacy "who counts visits" question. The counter counts **only** successful
GETs of `/` — the dashboard is not counted. The visitor IP is stored only as an HMAC-SHA256 hash
keyed by `VISIT_COUNTER_HMAC_KEY`; a visit from the same hash within 30 minutes is not counted
again; counts (`VisitTally`, `DailyVisitCount`) are kept indefinitely; a counter failure is logged
and never breaks the page (`test_frontpage_counter_hashes_and_dedups`). The charts that read these
counts are F-012.

## Decisions

- **Authentication replaces the campus-IP gate.** The view docstring: the WiFi and calendar info
  is "now gated by authentication (not campus IP) with secrets read from env". Any logged-in
  resident sees it from anywhere.
- **The calendar IDs come from env even though they are not secret** (`_calendar_embed_url`
  docstring): it keeps the source deployment-agnostic. The embed was restored from the legacy
  dashboard.
- **The dashboard doubles as the AK cron backstop** (`ensure_active_month_applied` docstring): it
  is a hot page, the check is two indexed single-row lookups, and it turns a missed cron run into a
  self-healing one instead of every balance being silently wrong.
- **Effective roles, not real roles** (view docstring): so the preview override is reflected here
  like everywhere else.
- **Visit counter on the front page only, IPs hashed, kept indefinitely** (`app/stats/models.py`
  module docstring).

## Open questions

- **Retiring the shared Google Calendar.** `spec/features/begivenheder.md` names the dashboard's
  shared calendar — whose password is printed for every resident — as what begivenheder is meant to
  replace. Nothing has been removed yet: someone first has to export what is live in the Google
  calendar. Then the embed, the credentials and the assertions in `test_features.py` can go, and the
  account can be retired or rotated.
- **`GOOGLE_CALENDAR_EXTRA_IDS` is undocumented.** It is read by `_calendar_embed_url` but is
  missing from `app/.env.example` and `DEPLOY.md` §2, so a fresh deploy shows only the primary
  calendar.
- **The photo strip is not behind the photo album's rollout gate.** The sidebar entry checks
  `photo_album.access.roles_allowed`; `photo_album_strip` does not. Harmless while that gate is open
  to everyone, but re-gating the album would leave the dashboard linking to pages that answer 403.
- **The visit counter trusts the first `X-Forwarded-For` hop.** `_count` uses
  `fwd.split(",")[0]`, which a client can set freely; the ølkælder kiosk gate
  (`oelkaelder.views._client_ip`) deliberately uses the *last* hop, the one Traefik appended. As it
  stands, anyone can inflate the count by varying the header.
- **No fail-fast for missing production secrets.** `DJANGO_SECRET_KEY` and
  `VISIT_COUNTER_HMAC_KEY` fall back to dev defaults and `DJANGO_DEBUG` defaults to on, so a
  production `.env` missing these starts up rather than refusing to, unlike a missing
  `DATABASE_URL` outside DEBUG.
- **Role chips show raw codes** (`oelkaelder`, `indstilling`) rather than display labels.
