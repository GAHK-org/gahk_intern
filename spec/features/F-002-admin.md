# Feature: Site-admin — administrator screens, monthly roles, Django-admin approval and role preview

**Feature ID:** F-002

## What it is

The administration area for Netværksgruppen, spelled role `administrator` in code. It is mounted at
the legacy URL `/admin/` (URL namespace `siteadmin`); Django's own admin is moved to `/django-admin/`
so the legacy path stays free (`app/config/urls.py` docstring). It has five jobs:

1. **Roles:** grant and revoke the active month's roles (`/admin/roles`).
2. **Django-admin approval:** a two-person rule before a new administrator gets Django-admin
   permissions (`/admin/adminadgang`).
3. **Role preview:** view the internal site as a given role (`/admin/preview`).
4. **Worker jobs:** the Celery queue, job history and schedules (`/admin/worker-jobs`).
5. **Links** to the application list (F-001), statistics (F-012) and `/django-admin/`.

It also owns the **front-page visit counter**, the data source for the visits chart that the legacy
admin dashboard showed. The chart itself now lives on the statistics page (F-012).

**What it replaces.** The PHP `admin` controller had its own login, a dashboard with visit and
application graphs, an "Administrer brugere" screen that wrote role flags to `gahk_admin_user`, a
"rebuild all admin users from the roster" action, and an unauthenticated mail-all-groups endpoint.
Login is now the members-area login (F-014), roles are monthly `RoleAssignment` rows (F-010), the
graphs moved to `/intern/statistik/`, and the mass-mailer was not carried over
(`app/residents/views_admin.py` docstring).

## Access

| Screen | Who |
| --- | --- |
| `/admin/`, roles, Django-admin approval page, worker jobs | effective role `administrator` |
| Approve or refuse a grant | an *approver* (see below) who is neither the grantee nor the granter |
| Preview switcher | **real** role `administrator` (or superuser), ignoring any active preview |
| Dev clock | any logged-in user, and only when `DEBUG` (404 otherwise) |
| `/django-admin/` | `is_staff`; what is visible inside depends on per-model permissions |

`administrator` implies every role (`residents.permissions._load_real_roles`). The sidebar shows
"Site-admin" and "Roller" only to administrators (`core/context_processors.py`).

## Routes

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `/admin/` | GET | administrator | Home: links, plus a banner listing pending admin grants |
| `/admin/roles` | GET, POST | administrator | Add or remove a role for a resident, active period only |
| `/admin/adminadgang` | GET, POST | administrator | Pending grants with approve/refuse; last 20 decisions |
| `/admin/preview` | GET | real administrator | Preview switcher |
| `/admin/preview/set` | POST | real administrator | Set or clear the preview override |
| `/admin/worker-jobs` | GET | administrator | Queued jobs, past jobs, active schedules |
| `/admin/worker-jobs/<task_id>` | GET | administrator | One job: metadata, result, traceback, children, log |
| `/admin/dev-clock/set` | POST | logged in, `DEBUG` only | Simulate the current date (F-004 testing) |

Views are in `app/residents/views_admin.py`, routes in `app/residents/urls_admin.py`.

## Data model

- `RoleAssignment` (resident, role, year, month), unique per combination (F-010). The role editor
  only touches the **active period** (`residents.models.active_period`: the newest residency list
  whose month has started).
- `AdminAccessGrant` (`app/residents/models.py`): one-to-one with `Resident`; `status`
  pending / approved / denied, `requested_by`, `decided_by`, `decided_at`, `created_at`.
- `stats.VisitTally` (HMAC hash of the visitor IP, count, first/last seen) and
  `stats.DailyVisitCount` (date, count).

## Roles screen

Lists every resident with a residency in the active period and that period's roles. **Add** does
`get_or_create` on the `RoleAssignment` and sets `is_staff`. **Remove** deletes it. When the added
role is `administrator`, the view stamps the current user as `requested_by` on the pending grant.

The `residents.signals` receivers keep the rest in sync, whatever wrote the row:

- `is_staff` is True exactly when the resident holds any `RoleAssignment` in **any** period.
  Superusers are never touched.
- Gaining `administrator` opens an `AdminAccessGrant` if the resident has none (`_open_grant`), and
  mails the approvers on commit.
- Losing `administrator` in every period deletes the grant, so a later return is a fresh request.

Roles derived from an embedsgruppe are re-written whenever the alumneliste editor saves that
resident's month (`residents.views._sync_month_roles`). It clears all workgroup-derived roles for that
month and re-adds the one matching the workgroup. `administrator` is carried forward separately.

## Django-admin access and the two-person rule

`/django-admin/` requires `is_staff`. Inside it, `Resident.has_perm` / `has_module_perms` grant
**every** permission while the resident (a) is active, (b) holds `administrator` in the active
period, and (c) has an **approved** `AdminAccessGrant`. The role is never copied into
`is_superuser`. CMS models use their own role check instead (`CMS_EDITOR_ROLES`, see F-006). Other
staff see only what their model permissions allow, usually nothing.

**Approvers** (`residents.admin_access.approvers`) are active superusers, plus active residents who
hold `administrator` in the active period and already have an approved grant. `can_decide` also bars
the grantee and the granter (`requested_by`). The approval view re-checks this on POST rather than
trusting the rendered buttons. A superuser is what makes the very first approval possible.

## Role preview

An administrator can make the whole internal site behave as if they held one chosen role, no role
("beboer"), or administrator. The override is stored in the session key `preview_roles`. Only
`effective_roles` / `request_has_role` / `role_required` honour it. `real_roles`, `can_preview` and
`has_active_role` (used by Django admin and the CMS) never read the session. A forged override on a
non-admin session is ignored. A banner on every page shows the active preview with an "Afslut
visning" button, and logout clears it (`app/tests/test_preview.py`).

## Worker jobs

- **Queued:** up to 100 visible rows from the PostgreSQL broker tables (`kombu_message`).
- **Past:** jobs from `celery_taskmeta`, 25 per page. Filter by status, six sort orders (started,
  finished, status; each asc/desc), and optionally include sub-jobs. Filtering, sorting and paging run
  in SQL.
- **Schedules:** enabled `django_celery_beat.PeriodicTask` rows.
- **Detail page:** broker headers (arguments, ETA, parent/root id), result, traceback, worker,
  retries, runtime, child jobs and the stored `core.WorkerJobLog`. It falls back to the log alone if
  the result is gone.
- Photo-album jobs are labelled with the media or ZIP import they concern.

## Front-page visit counter

`stats.middleware.FrontPageVisitCounterMiddleware` counts successful `GET /` responses only. The
client IP (first `X-Forwarded-For` entry, else `REMOTE_ADDR`) is hashed with HMAC-SHA256 under
`VISIT_COUNTER_HMAC_KEY`. A hash counts again only after 30 minutes. Each counted visit increments
that day's `DailyVisitCount`. Errors are logged and never break the page.

## Notifications

`residents.tasks.notify_admin_access_request` mails every approver except the grantee when a grant
opens, linking to `/admin/adminadgang`. It is best-effort: the `/admin/` home also lists pending
grants. `core.tasks.send_admin_dummy_notification` (08:00 and 16:00) mails administrators and
superusers as a temporary liveness check; `DEPLOY.md` says to delete it once confirmed.

## Decisions

- **Roles are derived per request, not stored in `is_superuser`.** Holding a role expires with the
  monthly period, and nothing fires on that rollover (`Resident.has_perm` docstring).
- **The grant is opened in a signal, not the role editor.** The monthly list, the søg-værelse
  roll-forward and the role editor all create role rows. "A gate that only one of four doors respects
  is not a gate" (`residents/signals.py`).
- **No re-approval each month.** `get_or_create` means a continuing administrator's monthly row does
  not reopen an approved grant; twelve approvals a year would train people to click accept without
  reading.
- **Granter and grantee may not approve.** Otherwise one administrator could grant and accept in two
  clicks. The module notes the limit: a majority of the group can still admit a newcomer.
- **The preview switcher is gated on real roles,** so previewing "beboer" cannot lock an admin out
  of ending the preview.
- **Worker-job sort keys are fixed SQL literals.** The request value only selects a key.
- **Old pickled task results are shown through an unpickler that rejects every global**
  (`_ResultUnpickler`), so displaying them cannot execute code.
- **Visitor IPs are never stored raw**, and counts are kept indefinitely (`stats/models.py`).
- **The dev clock is hard-gated on `DEBUG`** and returns 404 in production.

## Open questions

- **An approved administrator can make themselves a permanent superuser.** `ResidentAdmin`
  (`app/residents/admin.py`) has no field list, so the change form exposes `is_superuser`, `groups`,
  `user_permissions` and the raw `password` hash. With all permissions, an administrator can set
  `is_superuser`, which outlives the monthly role and bypasses the two-person rule. Its own comment
  says a proper form "comes with the auth feature build (F-014)".
- **The two-person rule guards only Django admin.** A pending or *denied* administrator still has
  every role on the site, including `/admin/roles`, so they can add or remove anyone's roles there.
- **A denied grant is sticky.** While the resident holds `administrator` in any period, re-adding the
  role finds the existing denied row and opens no new request.
- **Manual roles are short-lived.** Workgroup-derived roles added on `/admin/roles` are removed the
  next time the alumneliste editor saves that resident's month, and do not roll forward. The screen
  says roles apply "only for this month" but not that a list edit undoes them.
- **Unvalidated resident id.** The roles POST trusts the `resident` id and does not check it against
  the listed residents.
- **The visit counter trusts the first `X-Forwarded-For` value.** That value is client-supplied
  unless the proxy overwrites it, so the counter can be inflated or deduplication evaded.
- **`is_staff` comments disagree.** The field comment says "holds any role in the active period";
  the signal uses any period. Stale "ETL" mentions remain in `residents/signals.py` and the
  `AdminAccessGrant.requested_by` comment.
