# Feature: Søg værelse — room-application "kvotient" lottery (internal area)

## Audit trail (today)

- Recorded (audit-trail.md): an application sent, edited and withdrawn by its applicant, or deleted
  by Indstillingen with the reason; an offer made and closed (with how many applications it
  cleared); a tie decided; and the round ended, with every move, every carried-forward place and
  every cleared application and offer in that one event.
- An application's rooms are replaced only when they changed, so an edit of the study end does not
  read as every room dropped and chosen again.

- **Feature ID:** F-004
- **Source file(s):** `application/controllers/intern/soegvaerelse.php`,
  models `application/models/{kvotient_model,kvotientoffer_model,kvotient_priority_model,kvotient_orlov_model,adminuser_model}.php`,
  views `application/views/intern/soegvaerelse/{overview,soeg,admin,kvotientDetailFrame}.php`
- **URL / route:** internal area via the wildcard `nyintern/(:any) → intern/$1` (`routes.php:76`).
  All paths below are served as `https://www.gahk.dk/nyintern/soegvaerelse/<method>/...`:
  - `GET  /nyintern/soegvaerelse[/index][/success]` — personal overview (offers + my applications)
  - `GET  /nyintern/soegvaerelse/soeg/{monthNr}[/success]` — application form for a month
  - `POST /nyintern/soegvaerelse/indsend/{monthNr}` — submit application (kvotient + priorities + orlov)
  - `POST /nyintern/soegvaerelse/getKAsJson/{monthNr}` — live K calculation (AJAX, JSON/text)
  - `GET  /nyintern/soegvaerelse/getKvotientData/{ansoegningsId}` — application-detail iframe (HTML)
  - `GET  /nyintern/soegvaerelse/admin[/success]` — **admin** offer-management page
  - `GET  /nyintern/soegvaerelse/getApplicationByRoom/{roomNr}` — **admin** ranked applicants for a room (JSON)
  - `GET  /nyintern/soegvaerelse/wonRoomAlgorithm/{roomNr}` — winner resolver (public method; called internally)
  - `GET  /nyintern/soegvaerelse/closeOffer/{id}` — **admin** close offer (cascade delete)
  - `POST /nyintern/soegvaerelse/createoffer` — **admin** create offer
- **HTTP method(s):** GET + POST. ⚠ Note state-changing actions accept GET: `indsend`/`getKAsJson` read `$_POST` but the route does not enforce method; `closeOffer` is a GET that deletes (it is wired to a plain `<form>` with no method in `admin.php:120`, i.e. GET).
- **Access control:** **logged-in member** for user views; **admin (`indstilling`)** intended for admin views — **but enforcement is broken** (see findings). All checks are inline session-userdata checks (`01-infrastructure.md` A4/A5), no central guard.
  - User views (`index`, `soeg`, `indsend`, `getKAsJson`): require session `username`; if absent → flashdata `redirectToUrlAfterLogin` + `redirect("nyintern/admin")`.
  - `getKvotientData`: guarded by an **inverted/garbled** condition (see findings) intended to allow the owning member or an admin.
  - Admin views (`admin`, `getApplicationByRoom`, `closeOffer`, `createoffer`): use the buggy idiom `!$username && !empty($indstilling)` — **only blocks when logged-OUT and `indstilling` non-empty**; allows everyone else through (`01-infrastructure.md` A5).
  - `wonRoomAlgorithm`: **no access check at all** (public method).

**Routes / actions:**
| Action | URL | Method | Access | Purpose |
|---|---|---|---|---|
| `index` | `/nyintern/soegvaerelse[/index][/success]` | GET | logged-in (`username`) | personal overview: offers + my applications |
| `soeg` | `/nyintern/soegvaerelse/soeg/{monthNr}[/success]` | GET | logged-in (`username`) | render application form for a month |
| `indsend` | `/nyintern/soegvaerelse/indsend/{monthNr}` | POST | logged-in (`username`) | validate + INSERT kvotient/priorities/orlov |
| `getKAsJson` | `/nyintern/soegvaerelse/getKAsJson/{monthNr}` | POST | logged-in (no explicit check) | live K/a/b calc → JSON or validation errors |
| `getKvotientData` | `/nyintern/soegvaerelse/getKvotientData/{ansoegningsId}` | GET | owner-or-admin (⚠ broken) | application-detail iframe HTML |
| `admin` | `/nyintern/soegvaerelse/admin[/success]` | GET | admin `indstilling` (⚠ broken) | offer-management page |
| `getApplicationByRoom` | `/nyintern/soegvaerelse/getApplicationByRoom/{roomNr}` | GET | admin (⚠ broken) | ranked applicants + winner flag (JSON) |
| `wonRoomAlgorithm` | `/nyintern/soegvaerelse/wonRoomAlgorithm/{roomNr}` | GET | **none** | resolve winning `alumne_id` for a room |
| `closeOffer` | `/nyintern/soegvaerelse/closeOffer/{id}` | GET | admin (⚠ broken) | cascade-delete applications + delete offer |
| `createoffer` | `/nyintern/soegvaerelse/createoffer` | POST | admin (⚠ broken) | INSERT a room offer |

## Purpose
The internal room-lottery. When the indstillingen (admissions/allocation committee) puts rooms "in
offer" (*udbud*) for a future month, current residents apply by selecting one or more rooms in
priority order and entering when they expect to finish studying plus any past leave-of-absence
(*orlov*) periods. From this the site computes a "kvotient" `K` that ranks applicants per room; the
member sees their live K while filling the form, and the committee sees a per-room ranked list with
the algorithm's winner highlighted, then closes offers once allocated.

## Inputs
| Name | Source | Type | Required? | Validated/sanitized? | Used for |
|---|---|---|---|---|---|
| `{monthNr}` | route seg 3 | int (months-since-epoch-as-int = month + year*12) | yes for soeg/indsend/getKAsJson | **none** (passed raw, but escaped at SQL layer) | offer lookup; stored as `moveMonth` |
| `{ansoegningsId}` | route seg 3 | int | yes for getKvotientData | **none** (escaped at SQL layer in `getKvotientDataFromAnsoegningsId`) | load application detail |
| `{roomNr}` | route seg 3 | int | yes for getApplicationByRoom/wonRoomAlgorithm | **none** (escaped in `getApplicationsByRoom`; used as array key in `wonRoomAlgorithm`) | per-room ranking/winner |
| `{id}` | route seg 3 | int | yes for closeOffer | **none** (escaped in `deleteAnsoegningByOfferId`; **but `getOfferById`/`deleteOfferById` are unescaped/bound respectively**) | offer id to close/delete |
| `uri->segment(4)` | route seg 4 | string `"success"` | no | n/a | success banner flag on index/soeg/admin |
| `priority[]` | POST | int array (0-indexed) | `priority[0]` required | `xss_clean` (in `validateAnsoegInput`); each cast `intval` | priority list → `intern_kvotient_priority_nyintern` rows |
| `leaveMonth` | POST | int (0–11, month index) | yes (`required`) | `xss_clean`; arithmetic | `doneStudyingMonth = leaveMonth + leaveYear*12` |
| `leaveYear` | POST | int (calendar year) | yes (`required`) | `xss_clean`; arithmetic | combined into `doneStudyingMonth` |
| `orlovMoveOutMonth[]` | POST | int array (0–11) | optional (per index) | `xss_clean`; loop-validated | orlov start = `month + year*12` |
| `orlovMoveOutYear[]` | POST | int array | required **if** matching `orlovMoveOutMonth[i] != ""` | `xss_clean` | orlov start year |
| `orlovMoveInMonth[]` | POST | int array | required if out-month set | `xss_clean` | orlov end month |
| `orlovMoveInYear[]` | POST | int array | required if out-month set | `xss_clean` | orlov end year |
| `month` (createoffer) | POST | int (0–11) | yes (`required`) | `xss_clean` | offer month: `month + year*12` |
| `year` (createoffer) | POST | int | yes (`required`) | `xss_clean`; then `unset` | combined into `month`, then dropped |
| `vaerelses_id` (createoffer) | POST | int (1–61) | yes (`required`) | `xss_clean`; used as array key into room map | offer room id; derives `vaerelses_num` |
| session: `username`, `alumne_id`, `fullname`, `akRole`, `indstilling` | session | mixed | for auth/ownership | CI session (`01-infra` A4) | auth, ownership check, view chrome |

## Database interactions
- **Tables touched:** `intern_kvotient_nyintern` (R/W), `intern_kvotient_priority_nyintern` (R/W/D),
  `intern_kvotient_orlov_nyintern` (W/D), `intern_kvotient_offer_nyintern` (R/W/D), `intern_alumne` (R, joins),
  `gahk_dk_sessions` (R/W, session lib). *(Correction: `soegvaerelse` does **not** call `$this->counter()`, so it writes no `gahk_counter`/`gahk_counterdato` — see `99-index.md` §2.)*
- **Reads:**
  - **Offers:** `getOffers()` (`SELECT * FROM intern_kvotient_offer_nyintern ORDER BY month, vaerelses_id ASC`),
    `getMonthsWithOffers()` (`SELECT DISTINCT month ... ORDER BY month ASC`),
    `getOffersByMonthNr($monthNr)` (escaped `WHERE month = …`).
  - **K-ranking (per room):** `getApplicationsByRoom($roomNr)` —
    `intern_kvotient_nyintern` INNER JOIN `intern_kvotient_priority_nyintern` (on `ID = ansoegnings_id`)
    INNER JOIN `intern_alumne` (on `kvotient.alumne_id = alumne.ID`), `WHERE vaerelse_id = <escaped roomNr>`,
    `ORDER BY K DESC, applyDatetime ASC, priority.priority ASC`. ⚠ Bug: missing space before `ORDER BY`
    (`kvotient_model.php:21-22`) — rendered SQL is `…<roomNr>ORDER BY` (`01-infra` A3).
  - **All applications (winner algo):** `getApplications()` — kvotient INNER JOIN priority,
    `ORDER BY K DESC, priority.priority ASC` (no room filter).
  - **My applications:** `getApplicationsByAlumneId($alumneId)` — kvotient INNER JOIN priority,
    `WHERE kvotient.alumne_id = <escaped>`, `ORDER BY K DESC, priority.priority ASC`.
  - **Application detail:** `getKvotientDataFromAnsoegningsId($ansoegningsId)` —
    `intern_kvotient_nyintern` LEFT JOIN `intern_kvotient_orlov_nyintern` (on `applica.ID = orlov.ansoegnings_id`),
    `WHERE applica.ID = <escaped>` (returns one row per orlov period).
  - **Member data:** `Adminuser_model->getAlumneOnId($alumneId)` —
    `SELECT firstName, lastName, moveInDay FROM intern_alumne WHERE ID = $alumneId` ⚠ **unescaped**
    (`adminuser_model.php:46`), but `$alumneId` comes from the trusted session, not the request.
  - `Kvotientoffer_model->getOfferById($id)` — `WHERE id = $id` ⚠ **unescaped raw interpolation**
    (`kvotientoffer_model.php:35`); **not called by this controller** (dead read here).
- **Writes:**
  - **INSERT `intern_kvotient_nyintern`** (one row) — `indsend`, via `addKvotientApplication($kvotient)`.
    Columns set from `getKvotientDataFromPOST` + `calculateK`: `alumne_id` (session), `moveMonth` (= `monthNr`),
    `moveInMonth` (derived from `intern_alumne.moveInDay`: `(month-1)+year*12`), `doneStudyingMonth`
    (= `leaveMonth + leaveYear*12`), `K` (computed), `applyDatetime` (= `time()`). `ID` auto. Returns insert id.
  - **INSERT `intern_kvotient_priority_nyintern`** (N rows) — `indsend`, via `addPriority($priorityData)`,
    one per non-zero `priority[i]`: `ansoegnings_id` (= new kvotient ID), `alumne_id` (session),
    `priority` (= `i+1`), `vaerelse_id` (= `priority[i]`). `month` column **not set** (defaults 0).
  - **INSERT `intern_kvotient_orlov_nyintern`** (M rows) — `indsend`, via `addOrlov($orlovData)`,
    one per non-empty `orlovMoveOutMonth[i]`: `ansoegnings_id`, `orlov_start` (= out month+year*12),
    `orlov_end` (= in month+year*12), `numberOfMonths` (= end − start).
  - **INSERT `intern_kvotient_offer_nyintern`** — `createoffer`, via `addOffer($_POST)`. ⚠ Inserts the
    **whole (xss_clean'd) `$_POST`** after deriving: `month` (= `month + year*12`), `year` unset,
    `vaerelses_num` (= room-map number for `vaerelses_id`). Real columns: `id, month, vaerelses_id, vaerelses_num`.
    Mass-assignment surface — any extra POST key matching a column would be written.
  - **CASCADE DELETE** — `closeOffer`, via `deleteAnsoegningByOfferId($id)`: a single multi-table
    `DELETE prio, ansoeg, orlov FROM intern_kvotient_priority_nyintern AS prio INNER JOIN
    intern_kvotient_nyintern AS ansoeg … LEFT JOIN intern_kvotient_orlov_nyintern AS orlov …
    INNER JOIN intern_kvotient_offer_nyintern AS offer ON prio.vaerelse_id = offer.vaerelses_id
    WHERE offer.id = <escaped id>`. Deletes priorities, applications, and orlov rows for **every
    application that prioritized the room belonging to that offer** (matched by `vaerelses_id`, not by month).
  - **DELETE `intern_kvotient_offer_nyintern`** — `closeOffer`, via `deleteOfferById($id)` →
    `$this->db->delete('intern_kvotient_offer_nyintern', array('id' => $offerId))` (bound/escaped).
  - *(No `gahk_counter`/`gahk_counterdato` write — this controller does not call `counter()`; correction per `99-index.md` §2.)*
- **Transactions / ordering:** ⚠ **None.** `indsend` performs the multi-table submit (1× kvotient INSERT,
  N× priority INSERT, M× orlov INSERT) **non-atomically** — a failure mid-loop leaves a partial application
  (a kvotient row with missing priorities/orlov, or orlov referencing an `ansoegnings_id` whose priorities
  were not all written). All tables are **MyISAM** (schema confirms `ENGINE=MyISAM`), so **transactions are
  not available** even if added. `closeOffer` does the cascade delete then the offer delete as two separate
  statements (also non-atomic). The cascade also matches by `vaerelses_id` across ALL months — see quirks.

## Business logic
- **`index`**: load offers + months-with-offers + my applications. Builds `monthWithOffers` as pairs
  `[mn2m(month), month]`. `mn2m(n) = n % 12`, `mn2y(n) = int(n/12)` — a "month number" packs year*12 + month
  (0-indexed month). Renders `overview`.
- **`soeg($monthNr)`**: loads offers for that month + the member's `intern_alumne` row (for `moveInDay`).
  Computes `monthOfferAsTime = mn2y($monthNr)."-".(mn2m($monthNr)+1)."-01"` for the date display. Renders `soeg`.
- **`indsend($monthNr)`** (POST):
  ```
  load models; alumneId = session.alumne_id
  if $_POST: validateAnsoegInput()      # xss_clean $_POST; require leaveMonth, leaveYear, priority[0];
                                         # for each orlovMoveOutMonth[i] != "" require the matching in/out year+month
  if (!$_POST || form_validation fails): soeg($monthNr)        # re-render form with errors
  else:
    kvotient = getKvotientDataFromPOST(alumneId, monthNr, $_POST)
       doneStudyingMonth = leaveMonth + leaveYear*12
       moveMonth         = monthNr
       moveInMonth       = (month(moveInDay)-1) + year(moveInDay)*12
       applyDatetime     = time()
    kvotient.K = calculateK(kvotient, $_POST)
    ansoegnings_id = INSERT kvotient
    for i in priority[]:                 # intval; skip zeros
       INSERT priority {ansoegnings_id, alumne_id, priority=i+1, vaerelse_id=priority[i]}
    for i in orlovMoveOutMonth[]:        # skip empty
       INSERT orlov {ansoegnings_id, orlov_start, orlov_end, numberOfMonths}
    redirect /nyintern/soegvaerelse/index/success
  ```
- **K (kvotient) algorithm** (`calculateK`/`calculateA`/`calculateB`, `:262-286`):
  ```
  a = moveMonth - moveInMonth                       # months lived at GAHK at the sought move-in
      for each orlov period in POST:
          a -= (orlovMoveInMonthNr - orlovMoveOutMonthNr)   # subtract leave length
  b = doneStudyingMonth - moveMonth                 # months from sought move-in to study end
  K = number_format( a*100.0 / (a + b + 12), 2 )    # 2-decimal string
  ```
  Larger `a` (longer tenure) and smaller `b` (closer to finishing) raise K. The `+12` is a fixed
  denominator offset (intent unconfirmed — see Open questions). ⚠ `calculateK` returns a `number_format`
  **string** ("12.34"); it is stored into the `K` float column and used for `ORDER BY K DESC`.
- **`getKAsJson($monthNr)`** (AJAX): if POST present → validate; if valid, echo `json_encode({K,a,b})`;
  else echo `validation_errors()` (plain text/HTML, **not** JSON). ⚠ Condition `!$this->form_validation->run() == FALSE`
  parses as `(!run()) == FALSE` due to precedence — see quirks. The `soeg` form posts `form.serialize()` here on
  every change to show live K. Also called by an Easter-egg in `admin.php:175` only when `alumne_id === 254`.
- **`getKvotientData($ansoegningsId)`**: loads the application + its orlov rows. "Hack" (per the code's own
  comment): sums `numberOfMonths` across orlov rows into one synthetic period, then reuses `calculateK/A/B`
  to recompute and display K/a/b plus formatted move-in/move/done-studying dates and the orlov table.
  Renders the **iframe** view `kvotientDetailFrame` (full HTML document, no intern layout).
- **`admin`**: render offer-management page (`admin` view): existing offers (each with a per-room ranked
  table fetched via AJAX) + a "create offer" form + floor plan.
- **`getApplicationByRoom($roomNr)`** (JSON): `getApplicationsByRoom($roomNr)` (the ranked list), then for
  each application set `won = 1` **if** `application->alumne_id == wonRoomAlgorithm($roomNr)`. Echoes JSON.
  ⚠ Calls `wonRoomAlgorithm($roomNr)` **once per application row** (re-runs the whole allocation each time).
- **`wonRoomAlgorithm($roomNr)`** — greedy global allocation:
  ```
  applications = getApplications()        # all apps, ORDER BY K DESC, priority ASC
  applications[0]->won = 1                # (set but unused)
  for each application in order:
      if room not yet occupied AND this alumne has not yet gotten a room:   # $alumneGotRoom undefined first iter
          roomOccupied[vaerelse_id]   = alumne_id
          alumneGotRoom[alumne_id]    = vaerelse_id
          if roomOccupied[$roomNr] set: return roomOccupied[$roomNr]   # early exit once target room filled
  return roomOccupied[$roomNr] ?? -1
  ```
  Walks all applications best-K-first; the first applicant (by K, then priority) who can take a room without
  doubling up wins it; one room per applicant. Returns the winning `alumne_id` for `$roomNr`, or `-1`.
- **`createoffer`** (POST): validate `month`/`year`/`vaerelses_id`; pack `month = month + year*12`, drop
  `year`, set `vaerelses_num` from the room map; `addOffer($_POST)`; redirect to `admin/success`.
- **`closeOffer($id)`**: cascade-delete applications touching the offer's room, then delete the offer;
  redirect to `admin/success`.

## Outputs & side effects
- **Renders (intern layout via `showInternPage`):** `overview` (personal), `soeg` (form), `admin` (offer mgmt).
- **Raw view (no layout):** `getKvotientData` → `kvotientDetailFrame` — a **standalone HTML document** loaded
  as an `<iframe src='https://gahk.dk/nyintern/soegvaerelse/getKvotientData/{id}'>` from overview/admin.
- **JSON endpoints:** `getKAsJson` echoes `json_encode({K,a,b})` (or plain validation-error text on failure);
  `getApplicationByRoom` echoes `json_encode(applications)` with a `won` flag; `wonRoomAlgorithm` returns a
  value but is normally called internally. `closeOffer`/`createoffer`/admin echo `json_encode("Ikke adgang")`
  on the (broken) auth-fail branch.
- **Redirects:** `indsend` → `/nyintern/soegvaerelse/index/success`; `createoffer`/`closeOffer` →
  `nyintern/soegvaerelse/admin/success`; unauthenticated user views → `nyintern/admin` (with flashdata).
- **Cross-origin AJAX:** `admin`/`overview` views hit absolute `https://www.gahk.dk` / `https://gahk.dk` URLs
  (mixed host: `www.` vs bare) for the JSON/iframe fetches.
- **No emails.** **Headers:** intern layout + `MY_Controller` constructor headers. **Session:** read-only here
  (plus flashdata on redirect). **Other:** none — this controller does **not** call `counter()`, so no visit-counter write (`99-index.md` §2).
- **Easter egg:** `admin.php:175` inlines `<?=$this->session->userdata('alumne_id')?>` into JS as
  `if (<id>===254)` — for that one user, posts a hardcoded `priority[0]=1&leaveMonth=1&leaveYear=2020` to
  `getKAsJson` and tints the table aquamarine/coral. ⚠ Renders raw into JS (breaks/XSS if id is non-numeric/empty).

## Dependencies
- **Cross-cutting infra used:** `MY_Controller` base (session bootstrap, `gahk_helper`; **does not** call
  `counter()` — no visit-counter write) — `01-infra` A9; CI DB sessions `gahk_dk_sessions` — A4; ad-hoc per-controller authz reading
  session role flags — A5; query layer is the **escaped exception** (`kvotient_model`) plus raw-interpolation
  elsewhere — A3/A7. **Note:** the constructor calls `session_start()` directly **and** loads CI's `session`
  library (double session init) — `soegvaerelse.php:6,10`.
- **Models:** `Kvotient_model`, `Kvotientoffer_model`, `Kvotient_priority_model`, `Kvotient_orlov_model`,
  `Adminuser_model` (only `getAlumneOnId`).
- **Libraries/helpers:** `form` helper, `form_validation`, `session`, `security` (`xss_clean`).
- **Client-side:** `public/js/soegvaerelse/vaerelse.js` (`drawMap` canvas room plan), jQuery, Bootstrap,
  Raphael (CDN), Morris, tablesorter. External CDN: `cdnjs.cloudflare.com/.../raphael`.
- **External services:** none server-side (no email, no captcha).

## Security findings
| Issue | Location (file:line) | Severity | Note |
|---|---|---|---|
| Auth bypass — inverted idiom on all admin actions | `soegvaerelse.php:389,417,468,491` | **High** | `!$username && !empty($indstilling)` only blocks logged-out users who have a role; any logged-in non-admin (and arguably anyone with a session) reaches `admin`/`getApplicationByRoom`/`closeOffer`/`createoffer` |
| No auth check at all | `wonRoomAlgorithm` `:432` | **Medium** | public method; runs full allocation query, leaks winner; reachable directly via URL |
| Broken ownership check on detail view | `getKvotientData` `:327` | **Medium** | `if(!$username && (!empty($indstilling) || alumne_id != session))` — only triggers when logged-OUT; logged-in users can read **any** application's detail by id |
| No CSRF protection | all POST + GET `closeOffer` | **Medium** | `csrf_protection=false` site-wide (`01-infra` A4/A10); `closeOffer` mutates (cascade delete) on **GET** |
| Mass-assignment on offer insert | `createoffer` → `addOffer($_POST)` `:502` | **Medium** | whole `$_POST` inserted after xss_clean; extra keys matching columns persist |
| SQLi — unescaped `getOfferById` | `kvotientoffer_model.php:35` | **High** (latent) | `WHERE id = $id` raw; **not called by this controller** but the model is shared (`01-infra` A3) |
| SQLi — unescaped `getAlumneOnId` | `adminuser_model.php:46` | **Low (here)** | `WHERE ID = $alumneId` raw; `$alumneId` is session-sourced in this feature, but model is shared/injectable elsewhere |
| Stored XSS via member text in detail/ranked views | `overview.php`, `admin.php`, `kvotientDetailFrame.php` | **Medium** | `firstName`/`lastName` injected into JS-built table rows and HTML; output-escaping not applied; `xss_clean` on the *application* form does not cover names from `intern_alumne` |
| Raw session value into JS | `admin.php:175` | **Low** | `if (<?=alumne_id?>===254)` — could break JS / inject if id non-numeric |
| Insecure session/transport | `01-infra` A4 | inherited | `cookie_secure=false`, `sess_encrypt_cookie=false`, weak hardcoded key |
| `ORDER BY` spacing bug renders `<roomNr>ORDER BY` | `kvotient_model.php:21-22` | **Low** | works only because `escape()` quotes the value; brittle (`01-infra` A3) |

## Quirks, edge cases & suspected bugs
- **Undefined variable in `wonRoomAlgorithm`** (`:441`): `empty($alumneGotRoom[...])` reads `$alumneGotRoom`
  before it is ever initialized (first iteration). Under `error_reporting(0)` (`01-infra` A1) this is silently
  treated as "empty/true", so the first applicant always passes; functionally tolerated but fragile.
- **`applications[0]->won = 1`** (`:437`) is set then never read by the caller (caller recomputes via
  `wonRoomAlgorithm`); dead assignment / leftover debug.
- **`getApplicationByRoom` calls `wonRoomAlgorithm($roomNr)` once per row** (`:423`) — O(n²) re-allocation
  per request; only the last row's `won` reflects the true winner because the comparison `==` matches exactly
  one alumne, but each call re-runs the global allocation.
- **Cascade delete matches by room, ignoring month** (`deleteAnsoegningByOfferId`, `kvotient_model.php:43-52`):
  joins offer→priority on `prio.vaerelse_id = offer.vaerelses_id`, so closing an offer deletes applications for
  that room **across all months/offers**, not just the closed one. Likely unintended data loss.
- **ORDER BY spacing bug** (`kvotient_model.php:21-22`): missing space → `…<roomNr>ORDER BY` (works only due to
  `escape()` quoting; `01-infra` A3).
- **`getKAsJson` precedence bug** (`:296`): `!$this->form_validation->run() == FALSE` evaluates as
  `(!run()) == FALSE`, i.e. true iff validation **passes** — so it happens to work, but the expression is
  accidental and inverted-looking; on invalid input it echoes HTML validation errors that the JS then tries to
  `JSON.parse` (silently fails → no K shown).
- **`mn2m` inconsistency across files:** controller `mn2m` returns `n % 12` (no remap of 0→12); `admin.php`'s
  local `mn2m` remaps `0→12` and `mn2y` uses `(n-1)/12`. Month display can differ between controller-computed
  values and view-computed values.
- **`K` stored as a `number_format` string** into a float column; ranking relies on MySQL coercing the string.
- **`getKvotientData` "hack":** collapses all orlov periods into one synthetic period (start month 1, length =
  total) purely to reuse `calculateA`; the per-period display still uses the real rows. Self-described as a hack.
- **Easter egg** for `alumne_id === 254` in `admin.php` (see Outputs).
- **`indsend`/`getKAsJson` are not method-guarded** — they branch on `if($_POST)`; a GET simply re-renders.
- **Priority form caps at 9 visible / `sizeof($offers)` rows; orlov capped at 7 periods** in the view, but the
  server loops over `sizeof($_POST[...])` with no cap — more rows can be submitted via crafted POST.
- **`createoffer` view `year` select** uses `set_select('leaveYear', …)` (wrong field name) on the year option.

## Reimplementation notes (Django)
- **Views:** member `ListView` (overview) + `FormView` (soeg/indsend) + JSON views (`getKAsJson`,
  `getApplicationByRoom`) + a `DetailView`-style iframe/partial (`getKvotientData`); admin actions as
  permission-gated views. Use a Django `Form`/`Formset` for `priority[]` and orlov periods (kills
  mass-assignment); ORM kills the SQLi/spacing-bug classes.
- **Models:** `KvotientApplication` (`intern_kvotient_nyintern`), `KvotientPriority`
  (`intern_kvotient_priority_nyintern`), `KvotientOrlov` (`intern_kvotient_orlov_nyintern`), `KvotientOffer`
  (`intern_kvotient_offer_nyintern`); `intern_alumne` is the user/profile table (`01-infra` C4).
- **Atomicity:** wrap the `indsend` multi-table submit in `transaction.atomic()` (requires moving these
  tables off MyISAM to InnoDB/Postgres). Make `closeOffer` POST + CSRF and scope the cascade to the specific
  offer (month + room), not room-across-all-months.
- **FIX (record + confirm first):** the admin auth idiom (`!$username && !empty(...)`) and the
  `getKvotientData` ownership check — replace with proper `indstilling`-permission and owner-or-admin checks;
  add auth to `wonRoomAlgorithm`; make `getKAsJson` always return JSON. **PRESERVE:** the exact K formula
  `a*100/(a+b+12)`, the ranking order `K DESC, applyDatetime ASC, priority ASC`, and the greedy winner
  algorithm until the committee confirms intent.
- **URL patterns to keep:** all `/nyintern/soegvaerelse/*` paths verbatim (member bookmarks + the iframe/JSON
  absolute URLs).

## Open questions
- **Exact K / winner intent.** Is `K = a·100/(a+b+12)` (with the fixed `+12`) the committee's canonical
  formula, and is the greedy *global best-K-first, one-room-per-person* allocation in `wonRoomAlgorithm` the
  intended lottery rule? Both need the committee's sign-off before we lock the spec.
- **Cascade-delete scope:** is deleting applications for a room **across all months** on `closeOffer` intended,
  or should it be scoped to the closed offer's month? (Current behavior is almost certainly a bug.)
- **Who may view an application's detail** (`getKvotientData`)? Owner only, or any logged-in member, or admins?
  The current (broken) check resolves to "anyone logged in".
- **Admin role:** is `indstilling` the sole role that should manage offers, or also `administrator`/`akRole`?
- **`moveInMonth` derivation** uses `month(moveInDay) - 1` (0-indexed) while `moveMonth`/offer months come
  straight from the form's 0-indexed month — confirm the two are on the same 0-indexed footing (off-by-one risk
  in `a`).
- Should partial submissions be possible at all, or must a member always provide a priority + study-end before
  K is computed/stored?
