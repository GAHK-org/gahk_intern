# Feature: Statistik — applications and front-page visits on one internal page

**Feature ID:** F-012

## What it is

A read-only page at `/intern/statistik/` with three charts and a table, mainly for the admissions
committee and PR: how many applications came in by type, where this year's tour applicants heard
about the kollegium, which universities they came from, and how many people visited the public
front page each day. It also covers the front-page visit counter that feeds the last chart, which
lives in the same app.

Code: `app/stats/` (view, models, middleware), template `app/templates/statistik/stats.html`, charts
in `frontend/src/charts.ts`. The URL is mounted in `app/residents/urls.py` (namespace `stats:`). The
sidebar lists it under *Ressourcer* for every resident (`app/core/context_processors.py`), and the
site-admin home page links it too.

**What it replaces.** The PHP page drew its charts from about ten JSON/HTML "feeder" endpoints, all
of them reachable without logging in. Here, one view renders all the data into the page. Several
legacy charts were not carried over: alumni per month by field of study, tour applications by month ×
university, and the 18-month tour-vs-sublet time series.

## Access

| Action | Who |
| --- | --- |
| View `/intern/statistik/` | every logged-in resident (`@login_required`, no role) |

There are no other endpoints. The data reaches the browser only inside the rendered page.

## Routes

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `/intern/statistik/` | GET | logged in | The dashboard (`stats.views.stats`) |

## What the page shows

`stats.views.stats`, with "this year" being the local calendar year (`timezone.localtime()`):

| Panel | Source | Scope |
| --- | --- | --- |
| *Ansøgninger fordelt på type* (pie) | `admissions.Application` count per `type` — Rundvisning vs. Fremleje | **all rows in the table** |
| *Hvor hørte de om os? (year)* (pie) | tour applications, grouped by `heard_about_us`, blanks excluded | this year |
| *Rundvisninger pr. universitet (year)* (table) | tour applications, grouped by `university`, blanks excluded, top 15 | this year |
| *Besøg pr. dag (seneste 30)* (line) | `DailyVisitCount`, the newest 30 rows, shown oldest first | last 30 days that have a row |

Each panel shows a "no data" message instead of an empty chart. The view puts the series into a
`json_script` block (`id="stats-data"`). `frontend/src/charts.ts` (Chart.js, tree-shaken, imported
from `frontend/src/main.ts`) reads that block and draws into `chart-applications`, `chart-heard` and
`chart-visits`. On any page without the block it does nothing. The pies use a fixed categorical
palette in a fixed order, and their tooltips show percentages.

Applications are deleted after one year (`admissions` `purge_applications`, F-001), so the
"all rows" pie covers roughly the last twelve months, not all time.

## The front-page visit counter

`stats.middleware.FrontPageVisitCounterMiddleware` is installed globally (`MIDDLEWARE` in
`app/config/settings.py`). After the response has been built, it counts the request if and only if
it is a `GET` of exactly `/` that returned 200:

1. The visitor IP is the **first** `X-Forwarded-For` entry, or `REMOTE_ADDR` if there is no header.
   No IP means nothing is counted.
2. The IP is hashed with **HMAC-SHA256** under `VISIT_COUNTER_HMAC_KEY` (env, default
   `"dev-hmac-key"`). Raw IPs are never stored.
3. In one transaction it locks or creates the `VisitTally` row for that hash. The visit counts if the
   row is new, or if the last *counted* visit (`last_seen`) was 30 minutes or more ago. In that case
   `count` and `last_seen` are bumped and today's `DailyVisitCount` is incremented with an `F()`
   update.
4. Any exception is logged as a warning and swallowed, so the front page never breaks because of
   the counter.

## Data model

`app/stats/models.py`:

- `DailyVisitCount` — `date` (unique) and `count`; newest first. Migrated from the legacy
  `gahk_counterdato`.
- `VisitTally` — `ip_hash` (64 hex chars, unique), `count`, `first_seen`, `last_seen`. The legacy
  `gahk_counter`, minus the raw IP.

Both are kept indefinitely; nothing purges them.

## Scheduled jobs

None.

## Decisions

From the module docstrings in `app/stats/models.py`, `middleware.py` and `views.py`:

- **Members only.** The legacy feeders were unauthenticated. Now everything is behind login, and
  there is no separate JSON endpoint to leave open.
- **The "this year" figures really are year-scoped.** The legacy "heard about us" donut was labelled
  as this year but counted all time.
- **Only the front page is counted**, and it is counted after the response, so the counter never
  delays or breaks it.
- **IPs are stored as a keyed hash, never raw.** The 30-minute dedup window per hash copies the
  legacy `counter()` behaviour.
- **Counts are kept indefinitely**, and `DailyVisitCount` was migrated so the chart has history from
  day one.

## Open questions

- **The counter trusts the first `X-Forwarded-For` hop**, which any client can set. The ølkælder till
  (`oelkaelder.views._client_ip`) deliberately takes the *last* hop, the one Traefik appended,
  because the first one is spoofable. As written, anyone can inflate the visit count by sending a
  fresh fake XFF value with each request. If the proxy appends rather than replaces, every visitor
  behind it may also share one hash.
- **The visits chart is the last 30 *rows*, not the last 30 days.** A day with no visits has no
  `DailyVisitCount` row, so gaps are silently skipped, and the x-axis can span more than 30 days.
- **The type pie is not year-scoped**, while the two panels next to it are. With one-year retention
  it effectively shows "the last 12 months", which the heading does not say.
- **`VISIT_COUNTER_HMAC_KEY` has a hardcoded fallback.** If the env var is missing in production,
  hashes are keyed with a public string, and IPv4 hashes are then trivially reversible by brute
  force. No code anywhere in `app/` checks that it is set.
- **`VisitTally` grows without bound**: one row per distinct IP hash, forever, and nothing on the page
  reads it.
