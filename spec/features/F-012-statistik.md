# Feature: Statistik — internal statistics/charts dashboard + JSON data feeders

## Today

- `/statistik/` on intern, for every logged-in resident: applications by type, "hvor hørte du om
  os?" and tours by university for this year.
- **Besøgende pr. dag (gahk.dk, seneste 30):** unique visitors to gahk.dk per day. gahk.dk counts
  them itself without storing IP addresses; intern fetches the day totals every couple of minutes.
  Today's figure grows during the day. The history before the split counted the front page only.
- **Mest besøgte sider (30 dage):** gahk.dk's pages by views over the last 30 days, with their
  visitors (each day's unique visitors, added up).
- Nothing here counts visits to intern itself.

Everything below is the analysis of the legacy PHP site, kept for reference.

- **Feature ID:** F-012
- **Source file(s):** `application/controllers/intern/statistik.php`,
  `application/models/ansoegninger_model.php` (stats query methods),
  `application/models/counter_model.php`,
  `application/models/internuser_model.php` (`getNumberOfAlumnePerMonthByStudy`),
  view `application/views/intern/statistik.php`
- **URL / route:** (route prefix `nyintern` → controller `intern/statistik`; default action `index`)
  - `GET /nyintern/statistik/` — dashboard page (index)
  - `GET /nyintern/statistik/getAllStudyData` — JSON: alumni per month per university
  - `GET /nyintern/statistik/getAnsoegningerByStudyAndThisYearJSON` — JSON: tour applications this year by university (donut)
  - `GET /nyintern/statistik/getAnsoegningerByStudyAndMonthTable` — HTML `<tr>` rows: tour applications by month × university
  - `GET /nyintern/statistik/getAngsoegningStatisticJSON` — JSON: tour vs sublet applications per month (18 months) [note the misspelled "Angsoegning"]
  - `GET /nyintern/statistik/getCounterStatistic` — JSON: site visitors per day (31 days)
  - `GET /nyintern/statistik/getAnsoegningerByHeardAboutUsAndThisYearJSON` — JSON: "how heard about us" donut
  - `GET /nyintern/statistik/getStudyData/<study>` — JSON fragment per study (⚠ self-flagged "old and should be removed"; also a `public` method and thus URL-reachable)
  - `GET /nyintern/statistik/getAnsoegningerByStudyAndMonth` — `public` method returning a PHP array (no echo → emits nothing); URL-reachable but useless as an endpoint
  - `GET /nyintern/statistik/addStudyDataToData/<study>/<data>` — `public` helper, URL-reachable but takes an array arg → effectively dead as an endpoint
  - `GET /nyintern/statistik/mn2mstr/<monthNumber>` — `public` helper, URL-reachable, no output
- **HTTP method(s):** GET only. No POST handlers exist; all feeders read GET/route segments or no input at all.
- **Access control:**
  - `index` — **logged-in only.** Enforced inline: reads `session->userdata('username')`; if falsy, sets `redirectToUrlAfterLogin` flashdata and `redirect("nyintern/admin")` (standard auth pattern, `01-infrastructure.md` A4/A5).
  - **ALL JSON/data-feeder methods are UNAUTHENTICATED.** `getAllStudyData`, `getAnsoegningerByStudyAndThisYearJSON`, `getAnsoegningerByStudyAndMonthTable`, `getAngsoegningStatisticJSON`, `getCounterStatistic`, `getAnsoegningerByHeardAboutUsAndThisYearJSON`, and the public helpers (`getStudyData`, `addStudyDataToData`, `getAnsoegningerByStudyAndMonth`, `mn2mstr`) contain **no session/username check whatsoever** → any anonymous visitor can hit them directly and read aggregate data. See Security findings (AUTH BYPASS) — emphasized.
  - `getAnsoegningerByStudyAndThisYear` is `private` (helper for the JSON wrapper); not directly routable.

**Routes / actions:**
| Action | URL | Method | Access | Purpose |
|---|---|---|---|---|
| `index` | `/nyintern/statistik/` | GET | **logged-in** (`username`) | render dashboard page |
| `getAllStudyData` | `/nyintern/statistik/getAllStudyData` | GET | ⚠ **PUBLIC (unauth)** | JSON: alumni/month/university (line chart) |
| `getAnsoegningerByStudyAndThisYearJSON` | `/nyintern/statistik/getAnsoegningerByStudyAndThisYearJSON` | GET | ⚠ **PUBLIC (unauth)** | JSON: tour apps this year by university (donut) |
| `getAnsoegningerByStudyAndMonthTable` | `/nyintern/statistik/getAnsoegningerByStudyAndMonthTable` | GET | ⚠ **PUBLIC (unauth)** | HTML rows: tour apps month × university |
| `getAngsoegningStatisticJSON` | `/nyintern/statistik/getAngsoegningStatisticJSON` | GET | ⚠ **PUBLIC (unauth)** | JSON: rundvisning vs fremleje per month (18 mo) |
| `getCounterStatistic` | `/nyintern/statistik/getCounterStatistic` | GET | ⚠ **PUBLIC (unauth)** | JSON: daily site visitors (31 days) |
| `getAnsoegningerByHeardAboutUsAndThisYearJSON` | `/nyintern/statistik/getAnsoegningerByHeardAboutUsAndThisYearJSON` | GET | ⚠ **PUBLIC (unauth)** | JSON: "how heard about us" donut |
| `getStudyData` | `/nyintern/statistik/getStudyData/<study>` | GET | ⚠ **PUBLIC (unauth)** | ⚠ self-flagged "old and should be removed"; returns JSON fragment |
| `getAnsoegningerByStudyAndMonth` | `/nyintern/statistik/getAnsoegningerByStudyAndMonth` | GET | ⚠ **PUBLIC (unauth)** | returns PHP array (no echo) — dead as endpoint |
| `addStudyDataToData` | `/nyintern/statistik/addStudyDataToData/<study>` | GET | ⚠ **PUBLIC (unauth)** | helper; dead as endpoint (array arg) |
| `mn2mstr` | `/nyintern/statistik/mn2mstr/<n>` | GET | ⚠ **PUBLIC (unauth)** | month-number→string helper; no output |
| `getAnsoegningerByStudyAndThisYear` | (not routable) | — | `private` | helper for the JSON wrapper |

## Purpose
A logged-in committee/board member opens the internal *Statistik* page and sees a dashboard of Morris.js
charts: alumni distribution by university over time, tour (*rundvisning*) and sublet (*fremleje*)
applications per month, daily visitor counts on gahk.dk, two donut charts (which universities tour
applicants study at / how they heard about the collegium), and a table of tour applications by month ×
university. The charts are populated client-side by AJAX calls to JSON/HTML data feeders on the same
controller.

## Inputs
| Name | Source | Type | Required? | Validated/sanitized? | Used for what |
|---|---|---|---|---|---|
| `username` | session (`session->userdata`) | string | for `index` only | n/a | gate `index`; the feeders ignore it entirely |
| `redirectToUrlAfterLogin` | session flashdata (set) | string | n/a | n/a | post-login redirect target (`current_url()`) |
| `study` | route segment (CI passes URI segments as method args) | string | optional | **none** | passed to `getStudyData`/`addStudyDataToData` → interpolated into `getNumberOfAlumnePerMonthByStudy` LIKE clause |
| `monthNumber` | method arg / model row field | int | n/a | **none** | `mn2mstr` math; `<study>` grouping key |
| *(none for the chart feeders)* | — | — | — | — | `getAllStudyData`, `getAngsoegningStatisticJSON`, `getCounterStatistic`, `getAnsoegningerByStudyAndMonthTable`, the two donut feeders take **no request input** — all date ranges are server-computed |

Note: the dashboard's own `index` hardcodes the six universities ("DTU","KU","CBS","RUC","ITU","Kunst")
when building `$data['*Statistic']`, but those `$data` values are **unused by the view** (the view fetches
everything via AJAX) — see Quirks.

## Database interactions
- **Tables touched:** `gahk_ansoegninger` (R), `gahk_counterdato` (R, via `getCounterStatistic`),
  `intern_alumne` (R), `intern_alumne_liste` (R). *(Correction: `statistik` does **not** call
  `$this->counter()`, so it writes **no** `gahk_counter`/`gahk_counterdato` — the earlier "via
  middleware" note was wrong; see `99-index.md` §2.)*
- **Reads:**
  - **Alumni per month per university** — `Internuser_model->getNumberOfAlumnePerMonthByStudy($study)`:
    `SELECT COUNT(*) AS numberOfAlumne, monthNumber FROM intern_alumne IA LEFT JOIN intern_alumne_liste IAL ON IA.ID=IAL.alumne_ID WHERE IA.study LIKE '$study%' GROUP BY IAL.monthNumber`
    (`internuser_model.php:19-26`). Counts alumni grouped by `intern_alumne_liste.monthNumber`, filtered by
    `intern_alumne.study` prefix. Called once per university by `index`, `getAllStudyData`, `getStudyData`.
  - **Tour apps by month × university** — `Ansoegninger_model->getAnsoegningerByStudyAndMonth()`:
    `SELECT CONCAT(year,'-',month) AS date, month, year, university, COUNT(*) AS antal FROM gahk_ansoegninger WHERE typeOfAnsoegning='rundvisning' GROUP BY university, month, year ORDER BY year DESC, month DESC LIMIT 0,80` (`ansoegninger_model.php:63-75`).
  - **Tour apps this year by university** — `getAnsoegningerByStudyAndThisYear()`:
    `SELECT year AS date, university, COUNT(*) AS antal FROM gahk_ansoegninger WHERE year = YEAR(CURDATE()) AND typeOfAnsoegning='rundvisning' GROUP BY university` (`ansoegninger_model.php:77-85`).
  - **Apps per month by type (18 mo)** — `getAnsoegningerByMonth($epoch, $type)` called for
    `"rundvisning"` and `"fremleje"`, 18 iterations:
    `SELECT * FROM gahk_ansoegninger WHERE timestamp > $beginning_of_week AND timestamp < $end_of_week AND typeOfAnsoegning = '$typeOfAnsoegning'`, returns `num_rows()` (`ansoegninger_model.php:55-60`). Variable name `*_of_week` is misleading; it computes the first/last day of the month.
  - **"How heard about us"** — `getAnsoegningerByHowYourHeard()` (no year filter):
    `SELECT heardAboutUs as label, COUNT(*) AS value FROM gahk_ansoegninger WHERE typeOfAnsoegning='rundvisning' AND heardAboutUs != '' GROUP BY heardAboutUs` (`ansoegninger_model.php:99-107`).
    ⚠ The controller calls `getAnsoegningerByHowYourHeard()` (all-time), **not** the year-scoped
    `getAnsoegningerByHowYourHeardAndThisYear()` (defined at `:88-96` but unused), despite the endpoint
    name `...AndThisYearJSON`. See Quirks.
  - **Daily visitors (31 days)** — `Counter_model->get_count_by_date($dato)`:
    `SELECT * FROM gahk_counterdato WHERE dato = '<d/m-Y>'`, 31 iterations, one per day going back from today
    (`counter_model.php:16-20`, called from `statistik.php:269`).
- **Writes:** **None.** All stats methods are read-only SELECTs/COUNTs, and this controller does **not**
  call `counter()`, so there is no visit-counter write either (correction per `99-index.md` §2).
- **Transactions / ordering:** none. All tables are MyISAM (no transactions). The 31× counter query loop
  and 36× (18×2) application-month query loop are issued sequentially per request (N+1 query pattern).

## Business logic
- **`index`**: auth-gate, set `pagename`/`pageheader`, call `getStudyData()` six times into `$data` (results
  unused by view), then `showInternPage('intern/statistik', $data)` which renders the chart shells and the
  inline `<script>` that fires the AJAX feeders.
- **`getAllStudyData`**: builds the multi-series line-chart data. First assigns then **discards**
  `$data['*Statistic']` (re-initialised to `[]` at `:48`), then accumulates via `addStudyDataToData()` for the
  six universities into a `month → {study → {study,value}}` map. Hand-serializes to a JSON array string of
  `{ "date": "<Y-m-1>", "DTU": n, "KU": n, ... }` objects (one per month with data), trims trailing `", "`,
  `echo`s it. ⚠ Hand-rolled string concatenation, not `json_encode`.
- **`addStudyDataToData($study,$data)`**: per month-row from the alumni query, converts `monthNumber` →
  date string via `mn2mstr`, and stores `{study,value}`. Skips rows with empty/null `monthNumber`.
- **`getStudyData($study)`** ⚠ (self-commented "old and should be removed", `:98-100`): builds a per-study
  JSON fragment string (no enclosing `[]` — bracket lines commented out). Still **public/routable**, and
  still invoked by `index` (whose output is discarded). Effectively dead.
- **`mn2mstr($monthNumber)`**: maps a running month index to `"<Y>-<m>-1"` lowercased. `Y = (int)((n-1)/12)`,
  `m = n % 12` (0→12). ⚠ `Y` is a 0-based offset, not a real calendar year (e.g. monthNumber 1 → `"0-1-1"`),
  so chart x-axis dates are relative/synthetic, not absolute calendar dates. See Quirks.
- **`getAnsoegningerByStudyAndThisYearJSON`**: flattens the year/university aggregate into
  `[{"label":"<university>","value":"<antal>"}, ...]` (donut). Hand-serialized.
- **`getAnsoegningerByStudyAndMonthTable`**: builds HTML `<tr>` rows, one per month (up to 9 — breaks when
  `$i > 8`), each with a fixed column order AU, AAU, CBS, DTU, ITU, KU, RUC, SDU, Andet; missing study → `0`.
  Echoes raw HTML injected into the table body client-side.
- **`getAngsoegningStatisticJSON`**: loops 18 months back from now; per month counts rundvisning + fremleje
  applications; emits `[{"date":"Y-m","rundvisning":n,"fremleje":n}, ...]`.
- **`getCounterStatistic`**: loops 31 days back; per day looks up `gahk_counterdato` by `d/m-Y` string,
  emits `[{"date":"Y-m-d","count":"n"}, ...]` (0 if no row).
- **`getAnsoegningerByHeardAboutUsAndThisYearJSON`**: the only feeder using `json_encode`
  (`JSON_THROW_ON_ERROR`); emits `[{"label":...,"value":...}, ...]` for the donut.

## Outputs & side effects
- **JSON / HTML emitted via `echo`** (no `Content-Type` header set; CI default text/html) to **any caller,
  authenticated or not**. This is the core exposure: aggregate alumni counts, application volumes by type
  and university, "how heard about us" breakdown, and daily site-visitor counts are all readable by anonymous
  users.
- **Rendered charts:** `index` renders six Morris.js widgets (`chartStudy` line, `ansoegStudyPie` donut,
  `ansoegHeardAboutPie` donut, `ansoegChart` line, `counterChart` line) plus the `ansoegStudyTable` table,
  populated by jQuery `$.get`/`$.ajax` to the feeders.
- **Redirects:** `index` only, to `nyintern/admin` when unauthenticated (with `redirectToUrlAfterLogin`).
- **Session/headers:** `session_start()` in constructor; flashdata write on the redirect path. No-cache
  headers set by `MY_Controller` constructor (`01-infrastructure.md` A9).
- **No visit-counter write:** `statistik` does **not** call `counter()`, so hitting these endpoints does
  not write `gahk_counter`/`gahk_counterdato` (correction per `99-index.md` §2). `getCounterStatistic`
  only **reads** `gahk_counterdato`.

## Dependencies
- **Cross-cutting infra used:** `MY_Controller` base (session bootstrap, no-cache headers; **does not**
  call `counter()` — no visit-counter write) — `01-infrastructure.md` A9; standard session-based auth — `01-infrastructure.md` A4/A5;
  `showInternPage()` intern layout wrapper. Referenced by name, not re-described.
- **Models:** `Internuser_model` (loaded in constructor), `Ansoegninger_model` and `Counter_model`
  (lazy-loaded per feeder method).
- **Libraries/helpers:** `form` helper + `session` library loaded in constructor (both unused by the
  feeders).
- **Front-end:** Morris.js (Line + Donut), jQuery (`$.get`/`$.ajax`), Raphael.js (Morris dependency),
  Bootstrap panel/table markup, Font Awesome — loaded by the intern layout/`showInternPage`.

## Security findings
| Issue | Location (file:line) | Severity | Note |
|---|---|---|---|
| **AUTH BYPASS — unauthenticated data exposure** | `statistik.php` all feeders (`:38, :177, :192, :226, :262, :289`, + public helpers `:75, :101, :126, :138`) | **High** | Every JSON/HTML feeder lacks any auth check; anonymous users read alumni counts, application stats, "how heard", and visitor counts. Only `index` is gated. |
| SQL injection via `study` | `internuser_model.php:23` (`LIKE '$study%'`) reached through `getStudyData`/`addStudyDataToData` route segments | **High** | Route segment interpolated raw into query; both methods are `public`/routable. |
| SQL injection in stats queries (interpolated vars) | `ansoegninger_model.php:50,58` (`$beginning_of_week`,`$end_of_week`,`$typeOfAnsoegning`), `:121` (`$week`) | **Medium** | Values are server-computed here (epoch ints / literal type strings), so not attacker-controlled via this feature — but the pattern is raw interpolation and would be injectable if any caller passed user input. Record. |
| Hand-rolled JSON via string concat | `statistik.php:56-70, 180-189, 226-258, 262-283` | **Medium** | No escaping; values like `university`/`heardAboutUs`/study labels embedded unescaped into JSON → broken JSON or content injection if those DB values contain `"`/`\`. Only one feeder uses `json_encode`. |
| Stored-data → HTML injection (no output escaping) | `statistik.php:199-219` (`getAnsoegningerByStudyAndMonthTable` echoes `university`/`value` into `<td>`, injected via `.html()`) | **Medium** | `university` comes from public application form (F-001); unescaped into DOM → potential stored XSS in the dashboard. |
| No CSRF protection | site-wide `csrf_protection=false` (`01-infrastructure.md` A4) | **Low** | All actions are GET/read-only here, so low impact, but noted. |
| No `Content-Type` / cache headers on JSON | feeder `echo`s | **Low** | Served as text/html; combined with global no-cache. |
| Hardcoded DB schema name | `internuser_model.php:43,53` (`` `gahk_dk`. ``) — not in this feature's path but in same model | **Low** | Inherited; record. |

## Quirks, edge cases & suspected bugs
- ⚠ **"old and should be removed"** — `getStudyData()` (`:98-100`) is explicitly flagged dead, yet it is
  still `public` (URL-reachable) and still called six times by `index` with its output thrown away. `index`'s
  `$data['*Statistic']` values are entirely unused by the view (which AJAX-loads everything).
- ⚠ **`getAllStudyData` double-init**: assigns `$data['*Statistic']` at `:40-45` then immediately overwrites
  `$data = []` at `:48` — the first block is dead code.
- ⚠ **Endpoint name vs behavior mismatch**: `getAnsoegningerByHeardAboutUsAndThisYearJSON` calls the
  all-time `getAnsoegningerByHowYourHeard()`, **not** the year-scoped variant — so the "this year" donut is
  actually all-time. The view's caption "Tallene er fra 1. januar til dd." is even commented out
  (`statistik.php` view `:406`), suggesting someone noticed. The year-scoped model method exists but is unused.
- ⚠ **`mn2mstr` produces synthetic years**: `Y=(int)((n-1)/12)` yields 0,1,2… (a year *offset*), not a real
  year, so `chartStudy` x-axis dates (`"0-1-1"`, `"1-2-1"`…) are not calendar dates. Whether this is intended
  ("months since some epoch") or a bug is unclear — chart still renders, ordering by monthNumber.
- Dead/odd endpoints: `getAnsoegningerByStudyAndMonth` returns a PHP array but never echoes (HTTP body empty);
  `addStudyDataToData`/`mn2mstr` are helpers that happen to be `public` and thus routable but produce no
  useful HTTP output.
- `getAnsoegningerByStudyAndMonthTable` caps at 9 rows (`if($i > 8) break;`).
- `getAnsoegningerByStudyAndMonth`/`...ByStudyAndThisYear` group by `university` free-text from the public
  form, so spelling variants ("DTU" vs "Dtu") become separate buckets; the table's fixed columns
  (AU/AAU/CBS/DTU/ITU/KU/RUC/SDU/Andet) silently drop any other value.
- `getAnsoegningerByMonth` uses strict `>`/`<` on `timestamp` against first/last-day-of-month epochs — apps
  submitted exactly at midnight boundaries could fall outside both adjacent months (edge case).
- ⚠ `gahk_counterdato.dato` is stored as a `d/m-Y` **string** (`char(50)`); the feeder must format the lookup
  key identically (`date('d/m-Y', ...)`) — brittle string-keyed date matching, locale/format dependent.
- N+1 query pattern: 31 separate counter queries + 36 application-count queries per page load.

## Reimplementation notes (Django)
- **View type:** one server-rendered dashboard page (`TemplateView`, login-required) + a set of small
  read-only JSON endpoints (DRF/`JsonResponse` views) feeding the charts — mirror the current AJAX split, or
  inline the data into the page context to drop the N+1 loops.
- **Models:** read-only over `gahk_ansoegninger`, `gahk_counterdato`, `intern_alumne` (+ `intern_alumne_liste`
  join). Replace raw SQL with ORM aggregates (`annotate(Count(...))`, `TruncMonth`); use `JsonResponse`/
  serializers instead of hand-built strings (fixes JSON/XSS injection).
- **FIX (record + confirm first):** **add authentication to every data feeder** (currently all public) —
  the single most important change; escape table HTML; decide the "this year" vs all-time donut intent; fix or
  document the `mn2mstr` synthetic-year behavior; drop `getStudyData` and the dead endpoints.
- **PRESERVE:** the chart set and visual layout; the `/nyintern/statistik/` page URL; the data semantics
  (counts by month/university/type, daily visitors) for diff-against-old-site parity during migration.
- **URL patterns:** keep `/nyintern/statistik/` for the page; the feeder URLs can be re-pathed since they are
  internal AJAX (no SEO concern).

## Open questions
- Is the all-time vs "this year" donut (`getAnsoegningerByHowYourHeard` vs `...AndThisYear`) intentional or a
  bug? The endpoint name and the commented-out "1. januar til dd." caption disagree with the code.
- Is `mn2mstr`'s 0-based synthetic "year" deliberate (relative month index for the alumni line chart) or a
  defect? Need the intended x-axis semantics.
- Should the unauthenticated feeders be considered an intentional "public stats API" or an oversight? (Almost
  certainly oversight given `index` is gated, but confirm before exposing/locking in Django.)
- `intern_alumne.study` is free-text matched by `LIKE '<study>%'`; what canonical study values exist, and how
  should "Kunst" (queried by `index`/`getAllStudyData`) map vs the application-form universities
  (AU/AAU/CBS/DTU/ITU/KU/RUC/SDU/Andet) used in the tour-application charts? The two axes use different label
  vocabularies.
- Retention/PII: visitor IP counts (`gahk_counter`) and aggregate alumni data — any GDPR retention concern for
  the migrated stats?
