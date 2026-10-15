# Feature: Alumneliste — the resident directory and the monthly list that drives roles

**Feature ID:** F-010

## What it is

The alumneliste is the kollegium's monthly list: who lives in which room this month, in which
embedsgruppe (workgroup) and rengøring (cleaning group). It is two things at once:

1. **A directory** every resident can search, sort, export and browse back through month by month.
2. **The source of site permissions.** A resident's roles are `RoleAssignment` rows for a specific
   month, and a privileged embedsgruppe on the list grants its role for that month. Editing the
   list is how Indstillingen hands over access when the groups rotate.

Code: `app/residents/models.py`, `app/residents/views.py` (directory and the list editor),
`app/residents/permissions.py`, `app/residents/signals.py`, `app/residents/forms.py`; templates in
`app/templates/alumneliste/`.

**What it replaces.** The PHP `alumneliste` controller, which rendered old flat-file scripts with
their own DB connection, plus a JSON dump of the list gated only by a campus-IP allowlist. Here the
JSON endpoint is gone and the directory is behind login; the docstring of `directory` notes that
"the members-only login is the control". The legacy network-closed (MAC) fields were dropped with
the feature they served.

## Access

| Action | Who |
| --- | --- |
| Directory, history picker, search/sort, CSV/Excel export | every logged-in resident |
| Room-clash warning on the directory | `indstilling` only |
| Edit a resident's core data (`✎` in the list) | `indstilling` |
| Edit next month's (or the current) list | `indstilling` |
| Role editor (`/admin/roles`) | `administrator` |

`administrator` and superusers hold every role (`residents.permissions._load_real_roles`).

## Routes

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `/intern/alumneliste/` | GET | resident | Directory; `?period=YYYY-M`, `?q=`, `?sort=`, `?dir=` |
| `/intern/alumneliste/rows` | GET | resident | htmx fragment: the sortable table |
| `/intern/alumneliste/eksport` | GET | resident | `?format=csv` (UTF-8 with BOM) or `?format=xlsx` |
| `/intern/alumneliste/naeste-maaned` | GET, POST | indstilling | List editor; `?period=current` for the live month |
| `/intern/beboer/<pk>/rediger` | GET, POST | indstilling | Edit a resident's non-monthly data |

URL names are un-namespaced: `directory`, `directory_rows`, `directory_export`, `next_month_list`,
`edit_resident`.

## Data model

- `Resident` — the user model (login by email, case-insensitive lookup in
  `ResidentManager.get_by_natural_key`). Holds the facts that do not change monthly: name, phone,
  birthday, `move_in_date`, `move_out_date`, study, `sponsor` (fylgje, F-011) with `fylgje_raw` as
  free-text fallback, and profile fields.
- `Residency` — one row per resident per month: room, workgroup, cleaning, `year`, `month` (1–12).
  Unique on `(resident, year, month)`. Uniqueness of a *room* per month is not a DB constraint; it
  is enforced by the editor and by F-004's `end_round`.
- `RoleAssignment` — resident, `role`, `year`, `month`. Unique on all four.
- `Role` — the role codes. `WORKGROUP_ROLE` maps the privileged embedsgrupper (by `Workgroup.name`)
  to roles: Indstillingen, Inspektionen, Køkkengruppen, AK-gruppen, Ølkælderen, Regnskabsgruppen,
  PR-gruppen, Repperne, Vicevært, Fotogruppen. `administrator` is deliberately not a workgroup.
- `core.Workgroup` / `core.Cleaning` — name plus `size`: the exact member count a list must give the
  group (0 = no limit).

## The active period

`active_period()` is the **newest month with a list that has already started**, falling back to
the calendar month when there is none. Two consequences:

- A list Indstillingen prepares for next month changes nothing — not even access — until that month
  arrives (`test_future_list_does_not_change_active_period`).
- If nobody prepares a new list, the last one stays in force, roles included. Nothing runs at
  month rollover; there is no scheduled job.

`next_period` / `prev_period` derive from it. The roster lottery (F-004), birthdays on the calendar
(`residents/birthdays.py`), bylines on opslagstavlen and the dashboard all read the active period.
The date comes from `core.clock.current_date`, which a DEBUG-only dev clock can override.

## Roles from the list

`real_roles` reads the user's `RoleAssignment` rows for the active period, memoised per request.
`effective_roles` applies the administrator-only preview override; `role_required` and
`request_has_role` use effective roles. Signals keep two things in sync on every role write:

- `Resident.is_staff` is True iff the resident holds a role in **any** period (superusers untouched).
- Gaining `administrator` opens an `AdminAccessGrant` that a second administrator must approve
  before Django-admin permissions apply (`residents/admin_access.py`); approvers are mailed by the
  Celery task `notify_admin_access_request`. It is opened in the signal because the list editor,
  the F-004 roll-forward and the role editor all write role rows.

## The directory

- Defaults to the active period; the picker lists every month that has rows, newest first.
- Columns (`DIRECTORY_COLUMNS`, shared by table and exports): name, room, embedsgruppe, rengøring,
  fylgje (`sponsor` or else `fylgje_raw`), birthday, moved in, study, phone, email.
- Search (`q`) matches first name, last name, study or email; live via htmx.
- Sort is whitelisted (`SORT_FIELDS`), default by name, with `resident_id` as tiebreak.
- Badges: oldest and youngest resident by birthday, longest-living by `move_in_date`
  (`_period_badges`), also shown on the profile page.
- For `indstilling`, `_clash_rooms` flags any room held by two residents in the viewed month.

## The list editor (`next_month_list`)

Works on **next month by default**, or on the month in effect with `?period=current` so a mistake
in the live list can be fixed. Any other value falls back to next month: editing arbitrary past
months would rewrite history that the stamtræ, kvotient and roles read from (`_target_period`).
Editing the current month takes effect immediately, including access, and the page says so.

Actions (POST `action`):

- `copy` — offered when the target month has no list: copies every current row forward and derives
  roles for it.
- `save` — per row: room, embedsgruppe, rengøring, or remove. Rejected as a whole, with nothing
  saved, if two residents share a room or a sized group/cleaning does not have **exactly** its
  `size` members. A rejected save re-renders the editor's draft rather than the database state.
  You cannot remove yourself from the *current* month (it would revoke your own access mid-edit);
  removing yourself from next month is allowed.
- `add_existing` — add a resident to the month; refused if the room is taken.
- `add_new` — create a `Resident` with an unusable password, add them, optionally set their fylgje
  on the spot, and send a welcome mail with a set-password link (F-014). A mail failure does not
  undo the creation and is reported to the editor.

Rejected add-forms keep what was typed (`_add_form_draft`).

**Role derivation** (`_sync_month_roles`): for the target month, every workgroup-derived role is
deleted and the role of the chosen embedsgruppe re-added; `administrator` is re-added if the
resident holds it in the current month. Removing a resident deletes their roles for that month.

## Editing a resident

`ResidentEditForm` covers the non-monthly fields: names, email, phone, birthday, study, move-in and
move-out dates, fylgje (any other resident) and free-text fylgje. Date fields use `LocalDateInput`
so the native date picker pre-fills under the Danish locale instead of silently clearing the date.
After saving, a same-host `?next=` URL is honoured.

## Related screens

- `/admin/roles` (`residents.views_admin.roles`): add or remove any role for the active month.
- `/intern/regnskab/` (`residents.views_regnskab`, role `regnskab`): residents who were on the
  previous month's list but not the selected one, with AK and ølkælder balances for settling the
  deposit, plus a free lookup.

## Decisions

- **Time-bound roles** (decided 2026-06): privileges come from the current month's assignments, not
  a static table, because embedsgrupper rotate monthly.
- **Future lists are held back** by `active_period`, so next month can be prepared in advance.
- **Only next and current month are editable**, to protect history.
- **No self-removal from the live list**, to avoid locking out the editor.
- **Two-person rule on `administrator`**, opened from the signal so every write path is covered.

## Open questions

- **The `✎` edit link breaks after an htmx search or sort.** `_rows.html` builds
  `?next={{ request.get_full_path }}`; when rendered by `directory_rows`, that path is
  `/intern/alumneliste/rows?…`, so after saving Indstillingen lands on the bare table fragment.
- **Export ignores search and sort.** The export links pass only `format` and `period`, although
  `directory_export` honours `q`/`sort`/`dir` and its docstring says "in the shown order".
- **Future lists are visible to every resident.** The history picker includes a month Indstillingen
  is still preparing, so a draft is readable before it takes effect. Possibly intended.
- **Manually granted roles behave differently per path.** The list editor's `save`/`copy` delete
  every workgroup-derived role and re-add only the one matching the embedsgruppe, so a role such as
  `foto` granted by hand to someone outside Fotogruppen is wiped on the next save. Non-workgroup
  roles (other than `administrator`) are not carried forward by `copy` at all, whereas F-004's
  `_carry_roster_forward` copies every role.
- **Changing your own embedsgruppe on the live list** can still remove your own `indstilling` role;
  only self-removal is guarded.
- **`add_new` checks for an existing email case-sensitively** (`email=` after lower-casing the
  input), so an existing mixed-case address does not count as a duplicate.
- **The clash banner links to the editor's default (next month)**, even when the clash is in the
  month being viewed.
