# Feature: Værelsestjek — room move-in/move-out condition inspection

## Audit trail (today)

- Recorded (audit-trail.md): each room check as one event by the resident who did it, with every
  answer and photo, naming the report it replaced. Changes to the checkpoints themselves (Django
  admin) are recorded too.
- Left out: that the replaced report stopped being the newest (bookkeeping), and the copy of the
  checker's name on the report (the event's person says it).
- Importing the old reports is not in the trail.

- **Feature ID:** F-005
- **Source file(s):** `application/controllers/intern/vaerelsestjek.php`,
  `application/models/roomcondition_model.php`, `application/models/roomcriteria_model.php`,
  views `application/views/intern/vaerelsestjek/{overview,besvar,akoverview}.php`
- **URL / route:** (routes `nyintern → intern`; `nyintern/(:any)[/(:any)] → intern/$1[/$2]`, `config/routes.php:75-77`)
  - `GET  /nyintern/vaerelsestjek` — personal overview / room map (index)
  - `GET  /nyintern/vaerelsestjek/besvar/{roomId}` — inspection form for one room
  - `POST /nyintern/vaerelsestjek/indsend/{roomId}` — submit inspection (multi-file image upload)
  - `GET  /nyintern/vaerelsestjek/akoverview` — AK committee overview (all newest conditions)
- **HTTP method(s):** GET + POST
- **Access control:** **logged-in (CI session `username`), enforced inline per action.** No central guard;
  each user-facing method re-checks `username` from CI session userdata (`01-infrastructure.md` A4/A5).
  ⚠ `akoverview` uses a **broken guard** (`!$username && !empty($ak)`) that effectively enforces nothing
  for normal users — see findings. `indsend` checks **nothing** itself (no `username` check) — it relies on
  `besvar()` being called for invalid input and on session values for the writer identity.

**Routes / actions:**
| Action | URL | Method | Access | Purpose |
|---|---|---|---|---|
| `index` | `/nyintern/vaerelsestjek` | GET | logged-in (`username`) | render room-map overview; pick a room |
| `besvar` | `/nyintern/vaerelsestjek/besvar/{roomId}` | GET | logged-in (`username`) | show inspection form + history for a room |
| `indsend` | `/nyintern/vaerelsestjek/indsend/{roomId}` | POST | ⚠ **none enforced in-method** | validate, upload images, INSERT new condition |
| `akoverview` | `/nyintern/vaerelsestjek/akoverview` | GET | ⚠ **broken guard** (intended AK only) | tabular overview of all rooms' newest condition |

## Purpose
Alumni (residents) document the physical condition of a room when someone moves in/out. From the floor-plan
overview a user picks a room number, fills in a per-criterion score (0–N), a free-text comment per criterion,
and uploads photos per criterion. Each submission becomes the "newest" condition record for that room
(superseding the previous one, which is kept for history). The AK (residents' committee) views a table of the
current newest condition for every room and can drill into a room's full history.

## Inputs
| Name | Source | Type | Required? | Validated/sanitized? | Used for |
|---|---|---|---|---|---|
| `{roomId}` | route segment 3 | string (intended int) | yes (`besvar`/`indsend`) | **none** — concatenated raw into SQL & filesystem paths | room key; SQL `WHERE room_id=$roomId`; upload dir name; stored `room_id` |
| `selectedwalls` | POST | string | required by validation rule | `xss_clean` (whole `$_POST`); rule `required` | ⚠ **only field validated**; not stored directly (no `walls` UI — dead) |
| `selected{critId}` | POST | string (score) | not enforced | `xss_clean`; **not** range-checked vs `options` | per-criterion score; concatenated into `criteria` string |
| `comment{critId}` | POST | string | no | `xss_clean` | per-criterion comment; concatenated into `comments` string |
| `savedImages{critId}` | POST | string (`;`-joined paths) | no | `xss_clean` | retained existing image paths (built client-side); prefixed into `images` string |
| `userfile{critId}[]` | FILE (multiple) | uploaded files | no | type `jpg\|png\|jpeg`, `max_size='0'` (**= unlimited**), filename run through `remove_danish_letters` only | photos saved to disk; full paths appended to `images` string |
| session `username` | session | string | for index/besvar | CI session | auth gate |
| session `alumne_id` | session | int | read in `indsend` | CI session | stored as `alumne_id` of the report |
| session `fullname` | session | string | read in `indsend` | CI session | stored as `alumne_fullname` |
| session `akRole` | session | mixed | read in `akoverview` | CI session | (mis)used in broken guard |
| session keys for chrome | session | mixed | n/a | CI session | `showInternPage` reads `fullname/alumne_id/akRole/indstilling/inspektion/kokkengruppe/oelkaelder/administrator` (`MY_Controller.php:14-23`) |

Note: criteria ids come from `intern_room_criteria.id` which is a **`varchar(50)`**, not an int (`gahk_dk.sql`).
So `userfile{id}`, `selected{id}`, `comment{id}`, `savedImages{id}` field names are keyed by arbitrary string ids.

## Database interactions
- **Tables touched:** `intern_room_condition` (R/W), `intern_room_criteria` (R). Session storage table written by CI sessions. *(Correction: `vaerelsestjek` does **not** call `$this->counter()`, so it writes no `gahk_counter`/`gahk_counterdato` — see `99-index.md` §2.)*
- **Reads:**
  - `intern_room_criteria`: `RoomCriteria_model->getCriteria()` → `SELECT * FROM intern_room_criteria`
    (no order). Drives the form rows (`besvar`), the table columns (`akoverview`), and the upload loop (`indsend`).
  - `intern_room_condition`:
    - `getConditionsByRoom($roomId)` → `SELECT * ... WHERE room_id=$roomId ORDER BY date DESC` (history for `besvar`, also JSON-encoded into the page for the JS "load previous" feature).
    - `getAllNewestConditions()` → `SELECT * ... WHERE is_newest=1` (the AK overview table).
    - `getNewestConditionByRoom($roomId)` exists (`is_newest=1 limit 1`) but is **not called** by this controller.
- **Writes (all in `RoomCondition_model->addCondition`, `roomcondition_model.php:10-14`):**
  1. **UPDATE** `intern_room_condition` SET `is_newest`='0' WHERE `room_id`=$roomId AND `is_newest`=1 — demotes the previous newest record for the room.
  2. **INSERT** `intern_room_condition` (`alumne_id`, `alumne_fullname`, `room_id`, `criteria`, `date`, `is_newest`, `comments`, `images`) VALUES (… , 1, …) — the new record, flagged `is_newest`=1.
  - All values are **string-interpolated directly** into the raw SQL (no binding, no escaping) — see findings.
  - `date` is the controller-supplied `date("YmdHis")` (e.g. `20260626134501`) stored into a `datetime` column — ⚠ format mismatch (see quirks).
  - *(No `gahk_counter`/`gahk_counterdato` write — this controller does not call `counter()`; correction per `99-index.md` §2.)*
- **Transactions / ordering:** The unflag-old + insert-new pair (steps 1–2) **should be atomic** but is not wrapped in a transaction. `intern_room_condition` is **InnoDB** (so transactions are *available* but unused), unlike most legacy MyISAM tables. If the INSERT fails after the UPDATE, the room is left with **no** `is_newest=1` record. Image uploads happen **before** the DB write, so a partial/failed DB write can leave orphaned files on disk.

## Business logic
- **`index`**: if no session `username` → set `redirectToUrlAfterLogin` flashdata + `redirect("nyintern/admin")`; else render `overview` (floor-plan images + JS that generates room buttons linking to `besvar/{n}`).
- **`besvar($roomId)`**: same login gate; loads room history (`getConditionsByRoom`) + all criteria; renders form. Each criterion renders a `<select>` whose option count depends on `crit->options` (==3 → 0/1/2; >2 → 1..options; else → 0/1), a comment textarea, an image modal, and a hidden `savedImages{id}` field. All inputs start `disabled` and are only enabled by JS on "Opdater"/submit.
- **`indsend($roomId)`** (POST):
  ```
  if $_POST: validateFormInput()      # xss_clean whole $_POST; set rule selectedwalls=required
  if (!$_POST || form_validation->run()==FALSE):
        besvar($roomId)               # re-render form (NO redirect; same URL)
  else:
        alumneId  = session.alumne_id
        fullname  = session.fullname
        criterias = getCriteria()
        criteriaString=commentString=imageString=""
        foreach crit in criterias:
            target_dir = "./public/image/intern/roomimages/{roomId}/{crit->id}"
            files = $_FILES
            count = count($_FILES["userfile{id}"]['name'])   # 0 if first name == ""
            for i in 0..count:
                # repack the i-th multi-upload entry into the single-file shape CI expects
                name = remove_danish_letters(orig_name)       # lowercases, æøå→ae/oe/aa, strips spaces, dot-mangle
                if !exists(target_dir): mkdir(target_dir, 0777, recursive)
                config: upload_path=target_dir, allowed_types=jpg|png|jpeg, max_size='0', overwrite=FALSE
                if upload->do_upload("userfile{id}"):
                    append trim(target_dir,'.')+'/'+name to $data (';'-separated)
                else:
                    var_dump(upload->display_errors())          # ⚠ dumps to response, keeps going
            criteriaString .= "{id}:{selected{id}};"
            commentString  .= "{id}:{comment{id}};"
            imageString    .= "{id}:{savedImages{id}}{uploaded paths}|"
        addCondition(alumneId, fullname, roomId, date("YmdHis"), criteriaString, commentString, imageString)
        redirect('nyintern/vaerelsestjek')
  ```
  - **is_newest handling:** handled entirely in `addCondition` — demote all current `is_newest=1` rows for the room to 0, then insert the new row with `is_newest=1`. There is no `id`/PK on the table, so "the newest" is identified purely by the `is_newest` flag + `date`.
  - **Encoding format:** the three serialized blobs use delimiter conventions: `criteria`/`comments` are `id:value;` repeated; `images` is `id:path1;path2;…|` repeated (note `|` separates criteria, `;` separates images, `:` separates id from value). The JS in `besvar` parses these back out. ⚠ A `:`, `;`, or `|` inside a comment would corrupt parsing.
- **`akoverview`**: guard `if(!$username && !empty($ak))` → redirect; **else** render the table from `getAllNewestConditions()`. Because of the broken guard, the else branch runs for essentially everyone (see findings). The table explodes `criteria` on `;` and shows `explode(":",$crit)[1]` per cell.
- **`validateFormInput`**: loads `form_validation`, `xss_clean`s `$_POST`, sets one rule: `selectedwalls` `required`. There is no `selectedwalls` field in the current `besvar` view (the `walls` block is commented out, `besvar.php:140-150`), so the rule references a **non-existent field** — see quirks.

## Outputs & side effects
- **Renders:** `overview` (room-map, logged-in users), `besvar` (inspection form + JSON history, logged-in users), `akoverview` (DataTables table with CSV/Excel export + column-visibility, anyone past the broken guard). All wrapped by `showInternPage()` (intern header/footer, `01-infrastructure.md` A4).
- **Redirects:** unauthenticated `index`/`besvar` → `nyintern/admin` (with `redirectToUrlAfterLogin` flashdata); successful `indsend` → `nyintern/vaerelsestjek`.
- **Files written:** uploaded photos to `./public/image/intern/roomimages/{roomId}/{critId}/{sanitized-filename}` (relative to app root). Filenames only de-Danished/space-stripped; collisions avoided by `overwrite=FALSE` (CI appends a counter). Stored paths in DB are referenced at display time as `https://www.gahk.dk/{path}` (`besvar.php:271`).
- **Dirs created:** `mkdir($target_dir, 0777, true)` — **world-writable**, recursive, dir name built from raw `{roomId}` and `{critId}` — see findings.
- **Debug leakage:** on a failed upload, `var_dump($this->upload->display_errors())` is echoed into the HTTP response mid-request (`vaerelsestjek.php:179`).
- **Emails / external calls:** none. (`https://www.gahk.dk/` prefix is only a display URL in JS.)
- **Headers/session:** standard `MY_Controller` no-cache headers. (No counter write — `counter()` not called here.)

## Dependencies
- **Cross-cutting infra used:** `MY_Controller` base (session bootstrap, `showInternPage` chrome; **does not** call `counter()` — no visit-counter write) — `01-infrastructure.md` A4/A5/A9. CI DB sessions. Reference by name; not re-described here.
- **Models:** `RoomCondition_model`, `RoomCriteria_model`.
- **Helpers/libraries:** `form` helper, `session`, `form_validation`, `upload` (CI Upload library), `security` (`xss_clean`).
- **External services:** none.
- **Note:** constructor calls `session_start()` *before* `parent::__construct()` — PHP native session start in addition to CI session library (redundant/unusual).

## Security findings
| Issue | Location | Severity | Note |
|---|---|---|---|
| SQL injection via `{roomId}` | `roomcondition_model.php:12,13,17,22` (`WHERE room_id=$roomId`, INSERT) | **High** | route segment concatenated raw into UPDATE/INSERT/SELECT; not even cast to int |
| SQL injection via stored strings | `roomcondition_model.php:13` (criteria/comment/image strings) | **High** | `$criteriaString`/`$commentString`/`$imageString` (built from `$_POST`) interpolated raw into INSERT; `xss_clean` does not neutralize SQL |
| Insecure file upload — world-writable dirs | `vaerelsestjek.php:156` (`mkdir(...,0777,true)`) | **High** | 0777 perms; recursive create under web-served `public/` |
| Path traversal in upload dir | `vaerelsestjek.php:130` (`target_dir` from raw `$roomId` + `$crit->id`) | **High** | `roomId` is unvalidated route input; `../` could escape the intended tree |
| No real file-size limit | `vaerelsestjek.php:162` (`max_size='0'`) | **Medium** | `'0'` means *unlimited* in CI Upload → DoS / disk-fill |
| Auth bypass (broken guard) | `vaerelsestjek.php:73` (`!$username && !empty($ak)`) | **High** | only blocks logged-out users who somehow carry `akRole`; any logged-in user (and even anonymous users with no `akRole`) reaches AK overview |
| Missing auth on `indsend` | `vaerelsestjek.php:108-200` | **High** | no `username` check; writer identity taken from session (`alumne_id`/`fullname`) which may be empty → record attributed to nobody; CSRF makes this exploitable |
| No CSRF protection | all POST (`indsend`) | **Medium** | `csrf_protection=false` site-wide (`01-infrastructure.md` A4); combined with missing auth, cross-site forced submissions possible |
| Stored XSS in serialized blobs | `besvar.php` (`val.comments`/`val.criteria` injected via JS), `akoverview.php:42` | **Medium** | `xss_clean` on input is partial; comments rendered into DOM/`alumne_fullname` echoed unescaped |
| Debug info disclosure | `vaerelsestjek.php:179` (`var_dump(display_errors())`) | **Low** | leaks server paths/upload internals to client on failure |
| Content-type spoofing | `vaerelsestjek.php:161` (`allowed_types` only) | **Low** | CI checks extension + MIME but not actual image content; a `.jpg` containing script is accepted (served from `public/`) |

## Quirks, edge cases & suspected bugs
- **Validation rule targets a non-existent field.** `validateFormInput` requires `selectedwalls`, but the `walls`/`selectedwalls` control is commented out in `besvar.php:140-150`. So on a normal POST `form_validation->run()` returns FALSE → `indsend` falls into the `besvar($roomId)` re-render branch and **never writes**. ⚠ This strongly suggests the submit path is currently **broken** for the live form (or only ever "works" when the rule somehow passes). Must be reproduced against the old site before deciding to fix.
- **No `is_newest=1` row possible after a failed insert.** The UPDATE demotes the old record first; if the INSERT then fails (e.g. SQL error from an injected/quote-containing comment), the room ends up with zero current records and the AK overview silently drops it. Not atomic.
- **`date` format mismatch.** `date("YmdHis")` yields `20260626134501`, inserted into a `datetime` column expecting `Y-m-d H:i:s`. MySQL may coerce or store `0000-00-00`. Existing dump rows should be inspected to see what actually landed.
- **No primary key / auto-increment** on `intern_room_condition`; rows are addressed only by `room_id` + `is_newest` + `date`. Two submissions in the same second collide on `date` (history dropdown keyed on `date`).
- **Criteria id is `varchar(50)`**, so field names like `selected{id}`/`userfile{id}` and the serialized `id:value` pairs depend on string ids; a delimiter character (`:`/`;`/`|`) in an id or comment corrupts the blob.
- **`getNewestConditionByRoom`** is defined but never used.
- **Inputs disabled until JS enables them.** All form controls render `disabled`; `#btnSave` click re-enables them client-side. With JS disabled, nothing posts.
- **`$files = $_FILES` then mutating `$_FILES[$userfiles]`** in the loop is a fragile repack of the HTML `multiple` array into CI's single-file shape; `$files` keeps the original.
- Counter (and any base-controller reminders) fire on every action including the POST.

## Reimplementation notes (Django)
- **Views:** `index`/`akoverview` as `ListView`/`TemplateView` (login-required), `besvar` a `DetailView`+form, `indsend` a `FormView`/POST handler. Use a `ModelForm` + `MultipleFileField` so file type/size/path are validated server-side; ORM kills the raw-SQL injection.
- **Model:** `RoomCondition` over `intern_room_condition` — **add a surrogate PK**, replace the three delimited blobs with a proper child table (`RoomCriteriaAnswer`: condition FK, criteria FK, score, comment) and an `Image` table (FK + stored path), and make the unflag-old + insert-new a single atomic transaction (or derive "newest" from max date instead of a flag). `RoomCriteria` over `intern_room_criteria` (PK `id` varchar).
- **PRESERVE:** the public URLs `/nyintern/vaerelsestjek[/besvar/{roomId}|/akoverview]`; the floor-room numbering (stuen 001–010, floors 1–4 with 14/14/14/9 rooms); per-criterion score/comment/photo data model.
- **FIX (record + confirm first):** the broken `akoverview` guard → proper AK role check; add auth to `indsend`; the dead `selectedwalls` validation rule; 0777 dirs / unlimited size / path from raw input; atomic is_newest transition.

## Open questions
- Is `indsend` actually reachable end-to-end on the live site given the `selectedwalls` required-rule with no such field? (Does the old form somehow post `selectedwalls`, or is submission silently broken?) Needs verification against production behavior/data.
- Who is `akoverview` *meant* for — what role/session key marks AK members? `akRole` is read but the guard misuses it; confirm the intended membership signal (`akRole` truthy? a specific value?).
- What format are existing `date` values in the live `intern_room_condition` (YmdHis vs proper datetime)? Determines ETL parsing.
- Are stored image paths consistent (`/public/image/intern/roomimages/...`) and do the files still exist for migration?
- Should historical demoted records (`is_newest=0`) be retained as a full audit trail, or only the current state?
- What is the valid range/meaning of each criterion score, and is the `options==3 → 0/1/2` vs `>2 → 1..N` UI split intentional (asymmetric)?
