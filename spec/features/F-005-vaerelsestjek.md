# Feature: Værelsestjek — room condition inspection with scores, comments and photos

**Feature ID:** F-005

## What it is

A condition report for each room, typically made when someone moves in or out. A resident picks a
room on a floor plan and scores it against a fixed set of criteria (walls, ceiling lamp, closet
doors, …), adding a comment and optionally a photo per criterion. Each submission becomes the
room's current report; earlier reports are kept as history. The AK group has a matrix view of every
room's current scores, with CSV/Excel export.

At `/intern/vaerelsestjek/`, namespaced `vaerelsestjek:`. Code: `app/rooms/views.py`,
`app/rooms/models.py`, templates in `app/templates/vaerelsestjek/`, client-side photo downscaling
in `frontend/src/imageupload.ts`.

**What it replaces.** The PHP `vaerelsestjek` controller. Its reports were imported (criteria,
comments and image paths were stored as delimited blobs and are normalised into one row per
criterion here). Two legacy behaviours are called out in the `rooms/views.py` docstring as bugs the
port inverts: fields that were `disabled` until JavaScript re-enabled them (so nothing posted
without JS), and an AK overview that filled cells by position, putting scores under the wrong
heading.

## Access

| Action | Who |
| --- | --- |
| Overview, view a room's report, submit a new report | every logged-in resident |
| AK matrix + export (`akoverview`) | `ak` (and `administrator`/superuser via all-access) |

Inspection is deliberately **not** gated to `inspektion`: "room checks are done by whoever is
around, and gating them behind an embedsgruppe just meant the work stalled" (`rooms/views.py`).
Attribution comes from each report recording who wrote it. The sidebar lists "Værelsestjek" for
everyone (`core.context_processors._nav_intern`). Tested in
`app/tests/test_features.py::test_vaerelsestjek_is_open_to_every_resident`.

## Routes

| URL (under `/intern/vaerelsestjek/`) | Method | Who | Purpose |
| --- | --- | --- | --- |
| `` | GET | resident | Floor plans with room buttons, plus a table of all rooms and last check |
| `se/<room number>` | GET | resident | The room's current report, photos and score legends |
| `besvar/<room number>` | GET | resident | Inspection form, prefilled; `?rapport=<id>` prefills from an older report |
| `besvar/<room number>` | POST | resident | Save a new report |
| `akoverview` | GET | ak | Matrix of current scores; `?format=csv` / `?format=xlsx` to export |

Rooms are addressed by their human **room number** (`Room.number`), not the primary key.

**`akoverview` is linked from nowhere.** No template, sidebar entry or other view links to it (the
AK sidebar item "AK-oversigt" is `/intern/ak/admin`, a different page). It is reachable only by
typing the URL. See Open questions.

## Data model

- `RoomCriterion` — `code` (unique ASCII id, from legacy), `name`, `description` (the score legend
  shown under the field), `options`. `options` is a **scale-shape selector, not a maximum**,
  mirroring the legacy form exactly: `3 → 0..2`, `> 2 → 1..options` (so `5 → 1..5`), otherwise
  `0..1`. `score_values`, `score_min`, `score_max` and `accepts_score` expose it; `None`
  (unanswered) is always acceptable.
- `RoomCondition` — one report: room, `resident` (`SET_NULL`), `recorded_by_name` (name snapshot),
  `recorded_at`, `is_current`. Indexed on `(room, is_current)`.
- `RoomConditionScore` — condition, criterion, `score` (nullable), `comment`, `photo`
  (`FileField`, `upload_to="roomimages/%Y/"`) and `image` (legacy `;`-separated paths, read-only;
  `image_urls` turns them into `MEDIA_URL` links). Unique on `(condition, criterion)`.

Criteria are ordered by `code` everywhere, not by name: the legacy listed them in code order, which
clusters them by object, and ASCII codes avoid an æ/ø/å collation difference between SQLite and
Postgres (`room` view comment).

There is no admin registration or management screen for `RoomCriterion`; the set comes from the
legacy import (and `seed_demo` in development).

## Submitting a report (`besvar`, POST)

In one transaction: every current report for the room is flagged `is_current=False`, and a new
`RoomCondition` is created with the submitter and the current time. For each criterion:

- **Score** is parsed by `_parse_score` (plain ASCII decimals, optional `-`, max six digits; anything
  else is treated as blank). An off-scale score is dropped for that criterion only, with a warning
  message — never clamped ("a 5 silently stored as 2 is indistinguishable from a real 2"), and the
  rest of the submission is kept because file inputs cannot be re-populated.
- **Photo** is checked by `core.uploads.check_image_upload` against `ROOM_PHOTO_MAX_MB` (default 5,
  env-configurable, `config/settings.py`). A rejected photo produces a warning and is not saved;
  the rest is. The shared check exists so the rule cannot drift from the CMS and Den Hurtige; it
  replaced a looser `image/*` test that let SVG through.
- A score row is written only if there is a score, a comment or a photo.

The browser downscales photos to 1600px / JPEG 0.82 before upload (`frontend/src/imageupload.ts`);
the server cap is the backstop.

**Submitting always creates a new report.** Viewing an older one via `?rapport=` only prefills the
form; the old report is never mutated (tested in
`test_vaerelsestjek_history_prefills_without_mutating_the_old_report`).

In the form each row starts read-only behind an "Opdater" button, using Alpine `:readonly` rather
than `disabled`, so the form still posts all fields with JavaScript off.

## Viewing

- **Overview**: one card per floor with the static plan image (`Room.plan_image`, files under
  `static/legacy/image/intern/`) and room buttons, greyed out when the room has never been checked;
  then a table with floor, side and last check.
- **Room page**: the current report's scores with the scale range, comments, new photos and legacy
  images, and a collapsed "Forklaring af skalaerne" with each criterion's legend.
- **AK matrix**: one row per room with a current report, one column per criterion. Cells are looked
  up by criterion id, never by position, so a missing criterion cannot shift later scores under the
  wrong heading — in the page and in the export (`test_ak_overview_matrix_and_export_align_…`).

Photos are served through `/media/`, which requires a session for everything outside `cms/`
(`core/media.py`).

## Decisions

- **Open to every resident**, with attribution instead of a role gate (see Access).
- **History is kept** (`is_current=False`) rather than overwritten, so reports stay attributable.
- **Legacy blobs normalised** into `RoomConditionScore` rows; legacy image paths kept as a raw
  string next to the new `photo` field instead of being rewritten.
- **Drop, don't reject or clamp** an off-scale score.
- **Lock-by-readonly, not disabled**, so the form works without JavaScript.

## Open questions

- **`akoverview` has no inbound link.** Either add it to the AK sidebar section next to
  "AK-oversigt" (or link it from `/intern/ak/admin`), or decide it is no longer needed.
- **Photos are not carried over on a re-submit.** A new report only contains photos uploaded in that
  submission; prefilling from the current report copies scores and comments but not photos, so
  updating one field drops every earlier photo from the *current* report (they remain on the
  superseded one).
- **The `rooms/models.py` module docstring says "only the current condition per room is kept"**, but
  the view keeps every report as history. The docstring is stale.
- **No move-in / move-out distinction** is recorded on a report, though that is when checks happen.
- **No way to manage criteria** in the app or the Django admin.
