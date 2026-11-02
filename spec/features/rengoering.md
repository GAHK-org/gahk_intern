# Feature: Søndagsrengøring — cleaning rotas, sign-off and inspection

**Unnumbered on purpose**, like `opslagstavle.md`: `F-001`–`F-015` are legacy-parity documents with a
PHP "Source file(s)" header, and this feature has no legacy counterpart. The sections follow the
F-xxx layout so it reads the same way.

- **App:** `rengoering` (new), mounted at `/intern/rengoering/` from `residents/urls.py`, namespace `rengoering:`
- **Replaces:** the printed rota on varme trappe (sign + time when done, Inspektionen signs at 22.00)
  and the standalone prototype in `hackathon/rengoering-server/` (one real round on 1/11/2026).
- **Access control:** logged-in residents for their *own* assignments; `Role.INSPEKTION` (via the
  monthly `RoleAssignment`s) and superusers/`administrator` for everything else, plus the outgoing
  Inspektion's inspection in the handover months (April, October). Enforced per view with
  `role_required` / `request_has_role` / `inspect_required` and per object (ownership), see *Access*.
- **Out of scope (first pass):** køkkenrengøring (the data model leaves room for it, see `CleaningArea.kind`),
  hovedrengøring, notifications (phase 5), any link to AK-krydser.

## Purpose
Every Sunday each alumne on the rota cleans one area (hall, a floor, læsesal, a staircase) by 22.00.
They record when they were done; a member of Inspektionen walks the house at 22.00 with a checklist
and approves each area or records *mangler*, which the alumne must fix by 22.00 the next day, after
which Inspektionen re-checks. Inspektionen also plans the rota for each semester (e.g. "E26":
26 Sundays, every 5th a (*)-uge with extra tasks).

## Routes
| Name | URL (under `/intern/rengoering/`) | Method | Access | Purpose |
|---|---|---|---|---|
| `mine` | `` | GET | resident | "Min rengøring": next assignment with task list, upcoming ones, own history |
| `assignment` | `vagt/<pk>` | GET | owner or inspector | one assignment: tasks, sign-off, inspections, mangler + deadline |
| `sign_off` | `vagt/<pk>/taget` | POST | owner (inspector on behalf) | "Jeg har taget min rengøring" → `signed_off_at = now` |
| `mark_fixed` | `vagt/<pk>/udbedret` | POST | owner | "Mangler er udbedret" on the open Mangler inspection |
| `swap_request` | `vagt/<pk>/byt` | POST | owner | ask another alumne to take this Sunday (`recipient`, optional `message`) |
| `swap_answer` | `bytte/<pk>` | POST | recipient / requester | `action=accepter` (+ `give_back`: one of the recipient's Sundays, or empty), `afvis` (recipient), `traek` (requester) |
| `edit_assignment` | `vagt/<pk>/rediger` | GET/POST | manager | swap resident, toggle (*)-uge, clear sign-off |
| `inspect` | `tjek/` → `tjek/<yyyy-mm-dd>` | GET | inspector | mobile walk view for one Sunday (the prototype's UX) |
| `inspect_save` | `tjek/vagt/<pk>` | POST | inspector | the area's form: `action=gem` (autosave of ticks + note, JSON for `fetch`), `godkend`, `mangler` (+ deadline), `fortryd` (undo the last decision) |
| `overview` | `oversigt/[<yyyy-mm-dd>]` | GET | inspector | one Sunday: every area, who, when taken, status, mangler |
| `history` | `historik/` | GET | inspector | every resident's record, worst first (repeat offenders) |
| `area_history` | `historik/omraade/<slug>` | GET | inspector | all assignments + outcomes for one area |
| `resident_history` | `historik/beboer/<pk>` | GET | inspector | one resident's assignments + outcomes, with counts |
| `rotas` | `lister/` | GET | manager | list of rotas |
| `rota_create` | `lister/opret` | GET/POST | manager | generate a rota (form → preview → save) |
| `rota_import` | `lister/importer` | GET/POST | manager | paste / CSV (/ PDF) → name matching → save |
| `rota_detail` | `lister/<pk>` | GET | manager | the rota as a Sundays × areas grid with statuses; cells link to `edit_assignment` |
| `rota_replace` | `lister/<pk>/erstat` | POST | manager | "replace X by Y from date D" (moving out) |
| `rota_delete` | `lister/<pk>/slet` | POST | manager | delete a rota, only while nothing is signed off or inspected |

Areas and task lists are edited in the **Django admin** (`rengoering.CleaningArea` with an inline of
`CleaningTask`, ordered), gated to Inspektionen with the same `has_*_permission` pattern as
`cms.admin.ContentEditorAdmin`. That is a sortable nested list with no new UI. A custom editor can come
later if the admin turns out to be too clunky on a phone.

## Data model
```
CleaningArea   name, slug (unique), description ("Deles af 3 alumner"), order, is_active,
               kind = TextChoices(SOENDAG="soendag")      # later: KOEKKEN, with its own deadline rules
               import_aliases (text, one per line)        # "Stuen, Gang, Toilet" from the PDF headers
CleaningTask   area FK(CASCADE), zone ("Toilet og bad"), group ("Håndvask"), text, star_only (bool),
               order, is_active                           # deactivate, never delete, once inspected
Rota           name ("E26"), start_date (a Sunday), created_by, created_at
CleaningAssignment
               rota FK(CASCADE), area FK(PROTECT), date, resident FK(SET_NULL, null = "Ingen på listen"),
               is_star_week, signed_off_at (null), signed_off_by (null; ≠ resident when done on behalf),
               comment (the PDF's "Kommentar" column)
               UNIQUE(area, date)                         # two rotas can never both claim a Sunday
Inspection     assignment FK(CASCADE), round (1 = Sunday, 2+ = opfølgning), inspector FK(SET_NULL),
               checked_tasks M2M CleaningTask, note, outcome ("" while in progress | GODKENDT | MANGLER),
               missing (JSON [{zone, text}], frozen on decision), started_at, decided_at,
               deadline (MANGLER only), fixed_at (alumne pressed "udbedret")
               UNIQUE(assignment, round)
SwapRequest    assignment FK(CASCADE; the requester's Sunday), requester, recipient, message,
               state (AFVENTER | ACCEPTERET | AFVIST | TRUKKET | BORTFALDET), given_back FK(SET_NULL;
               the recipient's Sunday that went the other way, null = taken over for nothing),
               created_at, answered_at
               UNIQUE(assignment) WHERE state = AFVENTER   # one open request per Sunday
```
- **`Inspection.missing` is a snapshot.** It is written when a round is decided, so history keeps
  saying what was missing after Inspektionen edits or deactivates tasks. The live ticks
  (`checked_tasks`) are only needed while the round is in progress.
- **Zone and group are plain strings on the task**, not models. They exist only to group the checklist
  on screen (`{% regroup %}`), so separate models would add joins and admin forms for nothing.
- **Star weeks live on the assignment**, not as a formula on the rota. Generation sets them from a
  cadence (every *n*th Sunday), and import copies the PDF's `*` column. One field covers both, plus
  manual exceptions.
- **No `RotaMember` table.** The order of residents per area is an input to the generator. After
  that, the assignments are the source of truth, so later edits (swaps, moving out) never disagree
  with stored rotation data.
- **Status is derived, not stored** (`CleaningAssignment.status`, computed from the date, the sign-off
  and the latest inspection): `kommende` → `afventer` (→ `ikke_taget` after 22.00) → `taget` → `godkendt` | `mangler` →
  `udbedret` (waiting for re-check) → `godkendt` | `mangler` (new round) …, plus `overskredet` when a
  Mangler deadline passes without a later Godkendt. A stored status could drift from the rows it
  summarises.
- **Køkkenrengøring later:** a new `kind` plus kind-specific rules in `services` (dates that aren't
  Sundays, other deadlines). Nothing here assumes Sunday except the generator and the defaults.
  (`origin/feature/kitchen-duty` is køkkenvagter, a different thing, and does not overlap.)
- **`core.Cleaning` / `Residency.cleaning`** (the legacy monthly "rengøring" column in the alumneliste)
  is left alone. See open questions.

## Business rules
- **Generation:** for each chosen area, take an ordered list of residents and the start Sunday, the
  number of weeks and the star cadence. Week *i* gets `residents[i % len]`, and `is_star_week = (i+1) % n == 0`
  (E26: n = 5 → 29/11, 3/1, 7/2, 14/3, 18/4). Show a preview grid before saving. Saving is one
  transaction and fails as a whole on a UNIQUE(area, date) clash, naming the clash.
- **Import:** parse `pdftotext -layout` output (area header line, `Dato | Hvem | Stjerne | …` header,
  `dd/mm/yyyy  Name  [*]` rows) or CSV `område;dato;navn;stjerne`. Area headers are matched against
  `CleaningArea.name` + `import_aliases`. Names are matched against residents with a `Residency` in the
  active month or later (`importing.match_name`): full name → the whole thing as a first name
  ("Anton Claes") → first name + start of the last name or one of its words ("William P",
  "Gustav Skovsager") → the first name alone if it is unique ("Wilma"). The first rule with exactly
  one hit wins. Otherwise the name is *flere mulige* (with candidates) or *ikke fundet*. The match
  step shows one dropdown per **distinct** name, and nothing is saved until every one is set to a
  resident or to "Ingen". Parsed rows are kept in the session between the steps. Format detection:
  the PDF's rows start with a date and CSV rows don't (counting commas fails on "Stuen, Gang, Toilet").
  Upload accepts PDF (only if `pdftotext` is on the server), CSV or text, up to 5 MB.
- **Sign-off:** own assignment, from the Sunday 00:00 (local) onward, and only while there is no
  decided round 1. Signing after 22.00 is allowed but shown as late. Undo is allowed until inspected.
- **Inspection:** the checklist shows the area's active tasks, and `star_only` tasks only when
  `is_star_week`. "Godkend" ticks all visible tasks and decides GODKENDT. "Mangler" decides MANGLER
  with the note. Missing tasks are the visible ones minus `checked_tasks`, frozen into `missing`. Default deadline: 22.00 the
  day after the inspection evening (an inspection decided before 06.00 counts as the previous
  evening), editable. A follow-up is round + 1 and starts from the previous round's ticks. Inspecting
  is allowed whether or not the alumne signed off: "ikke taget" is a fact shown next to the outcome,
  not a separate outcome.
- **One endpoint per area form.** Autosave posts the *whole* form (all ticks + note), not one task at
  a time, so saves landing out of order can never leave a half-ticked list. Without JavaScript the
  "Gem" button does the same.
- **Undo** ("Fortryd afgørelsen") reopens the last decided round as long as no later round exists.
  It is there for mis-taps on a phone. An approved assignment is otherwise closed: autosave is refused.
- **History counts** per resident/area: Sundays, approved the first time, with mangler, *late* (a
  Mangler round not resolved by its deadline, where resolved means the alumne pressed "udbedret" or
  the next round was decided) and not signed off.
- **"Kopiér mangler"** stays as in the prototype: it builds the message (missing tasks per zone + note +
  deadline) for Den Hurtige/SMS until phase 5 sends it.
- **Swaps between alumner:** an alumne asks another to take one of their Sundays (from the
  assignment or the dashboard card: "Byt søndagen"). The recipient answers on "Min rengøring", where
  the dashboard points them: **accept** with one of their own Sundays to give back, or none ("jeg
  tager den bare"), or **decline**. The requester can withdraw while it is open. The requester never
  sees the other alumne's Sundays, so own-only holds. Who may be asked: anyone `rota_residents()`
  allows (this month's list or a later one), except the kitchen account. A Sunday can change hands
  while it is open: today or later, not ticked off, never inspected (`services.can_swap`).
  Accepting moves both residents in one transaction and checks it all again first. The
  Sunday must still be the requester's (Inspektionen may have edited it in the meantime), and
  nobody may end up with two Sundays on one date. The recipient only gets offered Sundays that pass
  those checks. Other open requests for either Sunday lapse (`bortfaldet`). The accepted request stays as the
  record, shown on both assignments' pages ("Byttet 3/11: Wilma gav 8/11 til Amalie mod 15/11"), and
  Inspektionen sees the new names everywhere else. Inspektionen does not answer requests: they
  still change any Sunday directly with `edit_assignment`. No notification yet (later phase); the
  dashboard shows "N alumner vil bytte rengøring med dig".
- **Rating:** keep **Godkendt / Mangler**. The checklist already records *what* was missing, and
  history gives counts that matter for repeat offenders (mangler, re-checks needed, deadlines
  overskredet, ikke taget). A 1–5 score would be subjective across inspectors and nobody acts on it.

## Access
| Who | May |
|---|---|
| any logged-in resident | see **their own** assignments, task lists, inspections, mangler; sign off; mark fixed |
| Inspektionen (active month) / `administrator` / superuser — "manager" | everything: rotas, import, edits, inspect, overview, history, admin for areas/tasks |
| the outgoing Inspektion, in April and October only — with the managers, "inspector" | inspect, sign off on someone's behalf, overview, history, and the assignment pages those link to. Not rotas, import, edits or admin |

- Another resident's assignment → **404** (not 403), so existence isn't confirmed. The same applies
  to sign-off/fixed POSTs.
- Manager views use `role_required(Role.INSPEKTION, Role.ADMINISTRATOR)`. Superuser and administrator
  already resolve to all roles in `real_roles`. Effective roles are used, so the preview tool works.
- An Inspektion assignment from a **previous** month grants nothing (time-bound roles), and this is tested.
  The one exception is the **handover after a generalforsamling**. The GF in March and September
  elects a new Inspektion, which gets the role the month after (April, October) but starts
  inspecting the month after that (May, November). So while April or October is the active month,
  whoever held Inspektion in March or September keeps the inspection views (`access.can_inspect`,
  `HANDOVER_MONTHS`). The new Inspektion keeps everything the role gives them, the walk included, so
  they can shadow it. A role preview switches the exception off along with the roles. Tested both
  ways, including that it ends when May or November becomes the active month.
  Confirmed with the requester: both Inspektioner may inspect in the overlapping month, and a
  re-check that falls after the month ends (a mangler from the last Sunday of April checked in early
  May) passes to the new Inspektion.

## Outputs & side effects
- Dashboard card "Din næste rengøring" (area, date, (*)-uge, signed-off state, open mangler + deadline).
- Sidebar: "Min rengøring" for everyone, "Rengøringstjek" for managers.
- No email/push in the first pass. Phase 5 adds a `rengoering` category in `notifications.categories`
  and `notify(...)` calls.

## Seed & management commands
- `manage.py load_cleaning_areas`: idempotent upsert of the 9 areas and their tasks from
  `rengoering/areas_data.py` (generated from the prototype's `AREAS`, incl. PDF header aliases).
  Existing areas are left as edited; `--reset` replaces their task lists (old tasks deactivated).
  Run by `task seed` and usable in prod.
- `manage.py import_cleaning_rota <file> --name E26 [--dry-run] [--map "William P=<email>"] [--map "Kim="]`:
  the same parser and matcher as the UI. Prints unresolved names with their candidates and saves
  nothing until each is settled (`NAVN=` = Ingen). The real E26 file is **not** committed.
- `seed_demo` (via `rengoering/demo.py`, like the other apps): areas + a generated rota over demo
  residents, with past Sundays in every state (godkendt, mangler, udbedret, overskredet, ikke taget).

## Decisions taken for the first pass
Questions 1, 3 and 4 below went unanswered when the plan was approved, so these defaults apply. Each
is a small change to revisit: residents see only their own assignments, `Residency.cleaning` is not
used, and there is no rollout gate.

## Open questions
1. **Should residents see the whole rota** (who has what this Sunday)? The paper list is public on
   varme trappe. The brief says own-only, so that's the default. Showing names without inspection
   outcomes would be harmless and handy for swaps.
2. ~~**Swaps:** should alumner swap themselves?~~ Decided: yes, see *Business rules*. Still open:
   a notification to the recipient once notifications land.
3. **`Residency.cleaning`:** what are the real legacy `intern_alumne_cleaning` names? If they are the
   areas ("2. sal"…), the generator could pre-fill each area's people from the alumneliste.
4. **Rollout gate:** ship behind a `core.rollout.Gate` (Inspektionen-only "Under test") for a round or
   two, then open it to everyone?
5. **Deadline edge cases:** is "22.00 the day after the inspection" right when the check happens late
   (e.g. Monday)? Should follow-up rounds get the same +1 day?
6. **Consequences** (Den Gyldne Bog): do repeated mangler/overskredet lead to AK-krydser or similar? If
   so, that's a later link to `ak`, not part of this feature.
7. **PDF upload** needs `poppler-utils` in the Docker image (not there today) or a pure-Python parser.
   Worth it, or is pasting enough?
