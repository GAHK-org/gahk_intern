# Feature: Optagelse — public tour and sublet applications, reviewed by Indstillingen

**Feature ID:** F-001

## What it is

The public face of admissions, at `/optagelse/`. A prospective resident either requests a guided
tour (**rundvisning**, the first step towards living here permanently) or applies to sublet a room
(**fremleje**, living here temporarily via a current resident). Each submission is stored as an
`admissions.Application` and the applicant gets an automatic "we got it" mail. Indstillingen
(role `indstilling`) reviews the submissions in a searchable, sortable list, marks each one received,
can discard spam, and can export contact details as CSV or Excel. Applications are deleted after one
year.

**What it replaces.** The PHP `optagelse` controller had the same two forms and an admin list, but
inserted the whole `$_POST` into the table, mailed the committee on every submission with a GET link
that marked the application received, and had inconsistent role checks between its admin actions.
The Django version keeps the legacy URLs (`ansoeg`, `send_rundvisning`, `fremlej`, `send_fremleje`,
`listansoegninger`, `showAnsoegning/<id>`, `setasreceived/<id>`) and fixes those structurally: explicit
form fields, POST-only state changes, one decorator for every admin view (`app/admissions/views.py`
module docstring). The English sublet form (`fremlej/eng`) was not carried over.

## Access

| Action | Who |
| --- | --- |
| Landing page, both forms, success page | anyone (no login) |
| List, detail, export, mark received, discard / un-discard | effective role `indstilling` |
| `ApplicationAdmin` in `/django-admin/` | Django-admin users with model permissions (see F-002) |

Admin views use `residents.permissions.role_required("indstilling")`, which reads *effective* roles:
`administrator` implies every role, and an administrator previewing another role loses access
(`app/tests/test_preview.py`). Anonymous users are redirected to the login page; logged-in residents
without the role get 403. The sidebar shows "Ansøgninger" only to `indstilling`
(`core/context_processors.py`).

## Routes

Namespaced `admissions:`, mounted at `/optagelse/` (`app/admissions/urls.py`).

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `/optagelse/` | GET | public | Landing page: process, criteria, links to both forms |
| `/optagelse/ansoeg` | GET, POST | public | Tour request form (`views.ansoeg`) |
| `/optagelse/send_rundvisning` | POST | public | Same view; the form's POST target (legacy URL) |
| `/optagelse/fremlej` | GET, POST | public | Sublet form (`views.fremlej`) |
| `/optagelse/send_fremleje` | POST | public | Same view; the form's POST target (legacy URL) |
| `/optagelse/ansoeg/success` | GET | public | Thank-you page, shared by both forms |
| `/optagelse/listansoegninger` | GET | indstilling | List with search, sort, filters, pagination (50) |
| `/optagelse/eksport` | GET | indstilling | CSV/XLSX download of the filtered list |
| `/optagelse/showAnsoegning/<pk>` | GET | indstilling | Detail page |
| `/optagelse/setasreceived/<pk>` | POST | indstilling | Mark received (405 on GET) |
| `/optagelse/kasser/<pk>` | POST | indstilling | Toggle discarded (405 on GET) |

`/optagelse` without a slash 301s to `/optagelse/` despite the CMS catch-all route
(`app/tests/test_features.py::test_slashless_legacy_urls_redirect_instead_of_404`).

## Data model

`Application` (`app/admissions/models.py`): `type` (`rundvisning` / `fremleje`), `full_name`,
`email`, `gender` (`male` / `female` / `other`), `age` (free text, kept as in the legacy data),
tour-only `study_year`, `year_left`, `university`, `field_of_study`, sublet-only `occupation`, shared
`heard_about_us` and `motivation`, `submitted_at`, `received_by` / `received_at`, and
`discarded_by` / `discarded_at`. Both resident FKs are `SET_NULL`. Default ordering is newest first;
indexes on `(type, submitted_at)` and `received_by`.

Compared with the legacy table: one `submitted_at` replaces day/month/year/timestamp columns, gender
replaces a `female` boolean, and `received_by` is a real foreign key.

## The public forms

`RundvisningForm` and `FremlejeForm` (`app/admissions/forms.py`) are `ModelForm`s with explicit field
lists, sharing a base class:

- **Tour:** full name, email, gender, age, years studied, study years left, university, field of
  study, "heard about us", motivation. Motivation is optional and capped at **500 characters**
  (rendered as `maxlength="500"`, enforced server-side; tests cover 500 vs 501).
- **Sublet:** full name, email, gender, age, occupation, "heard about us", motivation (optional, no
  length cap).
- **Gender** and **"heard about us"** are required choice fields; "heard about us" is a fixed list
  (`HEARD_CHOICES`: social media, website, residents, newspaper ad, recommendation, poster, found it
  myself).
- **Spam defences:** a hidden honeypot field `website` (a filled value fails validation), and
  Cloudflare **Turnstile**, verified server-side in `views._verify_turnstile`. When
  `TURNSTILE_SECRET_KEY` is unset (dev) the check is skipped; a failed or unreachable verification
  shows "Captcha-verifikation fejlede" and nothing is saved.

On success the view sets `type` and `submitted_at`, saves, sends the auto-reply and redirects to the
success page. The tour form shows the admission criteria (`optagelse/_criteria.html`) beside it.

## Review (Indstillingen)

**List** (`views.list_applications`), all driven by the query string so the export reuses it:

- `q` — case-insensitive search, OR-ed across name, email, university, field of study, study year,
  occupation, heard-about-us and motivation. Search filters before pagination, so a match on a later
  page is found.
- `sort` = `dato` / `type` / `navn` / `uddannelse` / `modtaget`, `dir` = `asc` / `desc`. Unknown
  values fall back to newest first. "Uddannelse" sorts by university, field of study, occupation.
- `pending=1` — only applications not yet marked received ("kun afventende").
- `show_discarded=1` — include discarded applications (hidden by default, also from search); the
  page shows how many are discarded.
- `from` / `to` — submitted-date range (`YYYY-MM-DD`; malformed dates are ignored).

The receiver is shown as first name plus last initial; unreceived rows are flagged "Afventer".

**Detail** shows every field, who received it and when, and the discard state.

**Mark received** (`views.mark_received`) records the current resident and time. It is
first-wins: an already received application is left unchanged, and there is no undo.

**Discard** (`views.toggle_discarded`) toggles `discarded_by` / `discarded_at`. It is reversible
on purpose; a discarded application is kept (and still purged on schedule), just hidden.

**Export** (`views.export_applications`): `?format=xlsx` gives an Excel workbook, anything else a
UTF-8 CSV with BOM (so Excel opens Danish characters). It honours every list filter. The columns are
contact details only: Dato, Type, Navn, E-mail, Uddannelse (university, else occupation). Filename
`ansoegninger-<from|start>_<to|nu>`.

## Notifications

The **applicant** gets a plain-text auto-reply ("GAHK – vi har modtaget din henvendelse") from
`DEFAULT_FROM_EMAIL` (`views._send_auto_reply`). **Indstillingen gets no mail** for either form; they
work from the list. A mail failure is logged and does not lose the saved application.

## Scheduled jobs

`manage.py purge_applications` deletes every application with `submitted_at` older than 365 days,
received, discarded or not. `--dry-run` prints the count. Celery beat runs it daily at 03:20 via
`admissions.tasks.purge_expired_applications` (`CELERY_BEAT_SCHEDULE` in `app/config/settings.py`).

## Decisions

- **No committee mail per application.** The committee asked to stop the per-request rundvisning
  mail; they review the list instead (`views.py` docstring, citing F-011). Sublets followed (test
  comment: "decision 2026-06").
- **The auto-reply is sent without `fail_silently`.** SMTP errors, such as one.com refusing a
  `DEFAULT_FROM_EMAIL` the account is not an alias of, must reach the log instead of vanishing.
- **Mark-received is POST-only** because the legacy GET link was CSRF-able.
- **Discard is a reversible toggle, not a delete.** Spam and obvious non-fits drop out of the
  working list, but a mistaken discard can be undone; retention still applies.
- **One-year retention** of applicant personal data, enforced by the purge rather than by hand.
  `DEPLOY.md` asks for a `--dry-run` before the first scheduled run, since the delete has no undo.

## Open questions

- **Spreadsheet formula injection.** `full_name`, `email` and `university` / `occupation` are
  applicant-controlled and written unescaped to the CSV and to `openpyxl` cells. openpyxl treats a
  string starting with `=` as a formula, so an applicant named `=HYPERLINK(...)` becomes a live
  formula in Indstillingen's Excel.
- **Turnstile misconfiguration fails closed silently.** If `TURNSTILE_SECRET_KEY` is set but
  `TURNSTILE_SITE_KEY` is not, the widget is not rendered, no token is posted, and every submission
  fails the captcha.
- **Statistics shrink with retention.** The F-012 statistics page counts `Application` rows, so once
  the purge runs its tour/sublet totals cover only the last year. Not stated on the page.
- **Stale ETL reference.** The `purge_applications` docstring says it is "NOT run by the ETL", but
  the legacy-migration ETL has been removed on this branch.
- `ApplicationAdmin` in Django admin is reachable only with model permissions, which in practice
  means an approved administrator or superuser, not Indstillingen. Whether it is still needed next
  to the review screens is undecided.
