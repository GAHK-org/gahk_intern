# Feature: Optagelse — admission/tour & sublet applications (public forms + indstilling review)

## Today (since the split of gahk.dk and intern.gahk.dk)

- The landing text is an ordinary page at gahk.dk/optagelse, edited under Hjemmeside on intern.
- The two forms, `/optagelse/ansoeg` (rundvisning) and `/optagelse/fremlej` (fremleje), are served
  by gahk.dk, which also handles spam (honeypot, Turnstile). The old POST addresses
  `/optagelse/send_rundvisning` and `/optagelse/send_fremleje` keep working there.
- gahk.dk keeps a submission only until intern fetches it, at most a couple of minutes later.
  intern checks every submission against the same field rules the forms had, stores it once
  (fetching it again changes nothing), and then lets gahk.dk delete it.
- A submission that breaks the rules is dropped and logged; the overview counts them. One of a kind
  intern does not know yet stays on gahk.dk until an intern that knows it fetches it.
- The applicant gets the same auto-reply as before, once, when intern stores the application.
- Indstillingen reviews applications on intern as before: `/optagelse/listansoegninger`, export,
  detail, received and discard. gahk.dk sends those addresses on to intern.

Everything below is the analysis of the legacy PHP site, kept for reference.

- **Feature ID:** F-001
- **Source file(s):** `application/controllers/optagelse.php`, `application/models/ansoegninger_model.php`,
  views `application/views/optagelse/{overview,rundvisning_box,fremlej_box,list_ansoegninger_box,show_ansoegninger_box}.php`
  (reads CMS text via `application/models/page_model.php`)
- **URL / route:** (route `optagelse → optagelse`; default action `index`)
  - `GET  /optagelse/` — landing (index)
  - `GET  /optagelse/ansoeg[/success]` — tour-request form (rundvisning)
  - `POST /optagelse/send_rundvisning` — submit tour request
  - `GET  /optagelse/fremlej[/eng|/success]` — sublet form (fremleje), DA/EN
  - `POST /optagelse/send_fremleje` — submit sublet application
  - `GET  /optagelse/listansoegninger[?from=N]` — **admin** list of applications
  - `GET  /optagelse/showAnsoegning/{id}[/success]` — **admin** single application
  - `GET  /optagelse/setasreceived/{id}` — **admin** mark application received (this is the link emailed to the office)
  - `GET/POST /optagelse/validateCaptcha` — form-validation callback (not a real navigable page)
- **HTTP method(s):** GET + POST
- **Access control:** **Mixed, enforced inline per action (no central guard):**
  - Public (unauthenticated): `index`, `ansoeg`, `send_rundvisning`, `fremlej`, `send_fremleje`, `validateCaptcha`.
    Write actions are gated only by **reCAPTCHA v2** (see `01-infrastructure.md` A6/A9), not auth.
  - Admin: `listansoegninger`, `showAnsoegning` require a session `username` **and `indstilling == 1`**;
    `setasreceived` requires `username` and **`!empty(indstilling)`**. ⚠ The two checks are **inconsistent**
    (`!= 1` vs `empty()`) — see findings. Auth uses the standard CI session userdata from `01-infrastructure.md` A4/A5.

**Routes / actions:**
| Action | URL | Method | Access | Purpose |
|---|---|---|---|---|
| `index` | `/optagelse/` | GET | public | landing |
| `ansoeg` | `/optagelse/ansoeg[/success]` | GET | public | tour-request form |
| `send_rundvisning` | `/optagelse/send_rundvisning` | POST | public (captcha) | submit tour request → INSERT + email |
| `fremlej` | `/optagelse/fremlej[/eng\|/success]` | GET | public | sublet form (DA/EN) |
| `send_fremleje` | `/optagelse/send_fremleje` | POST | public (captcha) | submit sublet → INSERT + email |
| `listansoegninger` | `/optagelse/listansoegninger[?from=N]` | GET | `indstilling==1` | admin list |
| `showAnsoegning` | `/optagelse/showAnsoegning/{id}[/success]` | GET | `indstilling==1` | admin detail |
| `setasreceived` | `/optagelse/setasreceived/{id}` | GET | `!empty(indstilling)` | mark received → UPDATE |
| `validateCaptcha` | (form-validation callback) | n/a | n/a | captcha verification |

## Purpose
The public face of admissions. Prospective residents request a guided tour (*rundvisning*) or apply
to sublet a room (*fremleje*) through captcha-protected forms; each submission is stored and emailed
to the admissions committee (*indstillingen*), and the applicant gets an auto-reply. The committee
(role `indstilling`) reviews submissions through an internal list/detail view and marks each one
"received" via a link in the notification email.

## Inputs
| Name | Source | Type | Required? | Validated/sanitized? | Used for |
|---|---|---|---|---|---|
| `fullName` | POST | string | yes (`required`) | `xss_clean` before persist; **not** escaped for email headers | stored; email subject + From-name |
| `email` | POST | string | yes (`required`; no `valid_email` rule) | `xss_clean` | stored; sender/recipient of mails |
| `age` | POST | string | yes | `xss_clean` | stored |
| `gender` | POST | string | yes (rundvisning & fremleje) | `xss_clean`; mapped to `female = (gender=="female")` then **unset** | stored as `female` tinyint |
| `studyyear` | POST | string | yes (rundvisning) | `xss_clean` | stored |
| `yearleft` | POST | string | yes (rundvisning) | `xss_clean` | stored |
| `university` | POST | string | yes (rundvisning) | `xss_clean` | stored; stats grouping |
| `fieldofstudy` | POST | string | yes (rundvisning) | `xss_clean` | stored |
| `occupation` | POST | string | yes (fremleje) | `xss_clean` | stored |
| `heardAboutUs` | POST | string | yes | `xss_clean` | stored; stats grouping |
| `motivation` | POST | text | yes | `xss_clean` | stored; email body |
| `g-recaptcha-response` | POST | string | yes (`callback_validateCaptcha`) | verified vs Google; then `unset` | captcha gate |
| *(any other POST key)* | POST | — | no | inserted verbatim if key matches a column | ⚠ **mass-assignment** — see DB writes |
| `from` | GET | int | no (default 0) | **none** | pagination offset, used raw in `LIMIT` |
| `{id}` | route segment | int | for show/setasreceived | **none** | application id, used raw in WHERE / passed to update |
| `uri->segment(3)` | route | string | no | n/a | `eng` (language) / `success` flag on `fremlej`/`ansoeg` |
| `uri->segment(4)` | route | string | no | n/a | `success` flag on `showAnsoegning` |
| session: `username`, `indstilling`, `editpage`, `administrator`, `fullname`, `akRole`, `alumne_id` | session | mixed | for admin actions | CI session | auth + view chrome; `alumne_id` recorded as receiver |

## Database interactions
**Tables touched:** `gahk_ansoegninger` (R/W), `gahk_page` (R), `intern_alumne` (R, join), `gahk_counter`/`gahk_counterdato` (W, via counter middleware).

- **Reads:**
  - `gahk_page` via `Page_model->get_page($pageId)` — CMS body + `bgpic`/`menuCat` for the landing/ansoeg/fremlej pages (pageIds 6, 7, 8, 9).
  - `gahk_ansoegninger` LEFT JOIN `intern_alumne` (`receivedByAlumneId → intern_alumne.ID`) for the admin list
    (`getNewestAnsoegninger($from,$rowsPerPage)`, `ORDER BY id DESC LIMIT $from,$rowsPerPage`) and single view
    (`getAnsoegningerById($id)`), plus a `numberOfAnsoegninger()` count for pagination.
- **Writes:**
  - **INSERT `gahk_ansoegninger`** — on successful `send_rundvisning` *and* `send_fremleje`, via
    `Ansoegninger_model->addAnsoegning($_POST)`. ⚠ Inserts the **entire (xss_clean'd) `$_POST` array**,
    then the model sets `day`,`month`,`year` (server date), `timestamp` (epoch), `female` (from `gender`,
    which it unsets), and the controller pre-sets `typeOfAnsoegning` (`"rundvisning"`/`"fremleje"`).
    Real columns: `id, fullName, age, studyyear, yearleft, university, fieldofstudy, occupation,
    motivation, heardAboutUs, typeOfAnsoegning, email, day, month, year, timestamp, receivedByAlumneId,
    female`. `receivedByAlumneId` is **not** set on insert (defaults NULL).
  - **UPDATE `gahk_ansoegninger` SET `receivedByAlumneId` = {session alumne_id} WHERE `id` = {id}** —
    on `setasreceived` (`setAnsoegningAsReceived($id,$alumneId)`, uses `db->where()` — bound/escaped).
  - **INSERT/UPDATE `gahk_counter` / `gahk_counterdato`** — side effect of `MY_Controller::counter()`,
    called from this controller's constructor on **every** action (see `01-infrastructure.md` A9).
- **Transactions / ordering:** none. `gahk_ansoegninger` is **MyISAM** (no transactions available).
  The insert-then-email sequence is not atomic, but email failure does **not** roll back the insert
  (nor is it detected — see findings).

## Business logic
- **`index` / `ansoeg` / `fremlej`** load a CMS page (`gahk_page`) and render the public form. `ansoeg`
  builds the reCAPTCHA widget; `fremlej` additionally switches DA/EN by `uri->segment(3)=="eng"` (loads
  `fremleje`/`recaptcha` language files, sets `pageId` 8 vs 9). A `success` flag (segment 3) shows a
  confirmation message after redirect.
- **`send_rundvisning`** (POST):
  ```
  validate required fields + captcha (callback_validateCaptcha)
  if invalid -> re-render ansoeg()
  else:
    $_POST = xss_clean($_POST); unset g-recaptcha-response; typeOfAnsoegning="rundvisning"
    addAnsoegning($_POST)                       # INSERT
    build plaintext $message from $_POST (incl. a "set as received" admin link)
    sendMail($message, from=email, fromName=fullName, isFremlejer=false, to=indstillingen@gahk.dk)
    sendMail(autoReply, from=autosvar@gahk.dk, to=applicant email)
    redirect /optagelse/ansoeg/success
  ```
- **`send_fremleje`** (POST): same shape, `typeOfAnsoegning="fremleje"`, DA/EN auto-reply. ⚠ **The
  notification email to the committee is commented out and replaced by `if(TRUE)`** (`optagelse.php:181-182`)
  — so the office is **never emailed** for sublet applications; only the applicant auto-reply is sent.
  The application **is** still inserted.
- **`validateCaptcha($value)`**: if `g-recaptcha-response` present, calls `recaptcha->verifyResponse(...)`
  and returns TRUE on `success===true`; returns FALSE if empty; returns **nothing (null)** if present but
  verification fails (treated as falsy by form_validation, so it still blocks — but implicitly).
- **`listansoegninger`**: if not logged in → flashdata redirect to `nyintern/admin`; elif `indstilling != 1`
  → echo "Ingen adgang"; else paginate `getNewestAnsoegninger($from,50)` and render via `showInternPage`.
- **`showAnsoegning($id)`**: same auth; renders one application by id.
- **`setasreceived($id)`**: if not logged in → redirect; elif `empty($indstilling)` → "Ingen adgang"; else
  set `receivedByAlumneId` = current `alumne_id`, redirect to `showAnsoegning/$id/success`.

## Outputs & side effects
- **Renders:** public CMS-backed forms (rundvisning/fremleje) with reCAPTCHA; admin list (DataTables-style,
  50/page) and single-application detail — both wrapped by `showInternPage()` (intern header/footer).
- **Redirects:** `/optagelse/ansoeg/success`, `/optagelse/fremlej/success`,
  `/optagelse/showAnsoegning/{id}/success`; unauthenticated admin access → `nyintern/admin` (with
  `redirectToUrlAfterLogin` flashdata).
- **Emails (via CI Email lib + SMTP from `config/email.php`):** rundvisning → committee + applicant
  auto-reply; fremleje → applicant auto-reply **only** (committee mail disabled). From address forced to
  `autosvar@gahk.dk`; subject embeds `$_POST['fullName']`.
- **Headers:** constructor sets `HTTP/1.0 200 OK` + aggressive no-cache (`Cache-Control`, `Expires`,
  `Pragma`) and `output->cache(0)`.
- **Other:** visit counter write on every hit (`gahk_counter`/`gahk_counterdato`); `MY_Controller`
  constructor also calls `sendAnsoegningPaamindelseIfTime()` (reminder mail — currently disabled).

## Dependencies
- **Cross-cutting infra used:** `MY_Controller` base (session bootstrap, visit counter, reminder),
  CI DB sessions, email config (`config/email.php`), reCAPTCHA library — see `01-infrastructure.md`
  A4/A6/A9. Reference by name; not re-described here.
- **Base:** `MY_Controller` (session, `gahk_helper`, counter, reminder) — `01-infrastructure.md` A9.
- **Models:** `Ansoegninger_model`, `Page_model`; `Pylon_calendar_model` is loaded in the constructor but
  **never used** in this controller.
- **Libraries:** `recaptcha` (the loaded one, not `recaptchassl`), `form_validation`, `email`, `session`.
- **Config:** `config/email.php` (SMTP creds), `config/recaptcha.php` (v2 keys).
- **External:** Google reCAPTCHA verify endpoint; one.com SMTP (`mailout.one.com`).
- **Language files:** `fremleje`, `recaptcha` (danish/english).

## Security findings
| Issue | Location | Severity | Note |
|---|---|---|---|
| SQL injection via `from` | `ansoegninger_model::getNewestAnsoegninger` (`LIMIT $from,$to`) | **High** | `$_GET['from']` concatenated raw into SQL |
| SQL injection via `{id}` | `ansoegninger_model::getAnsoegningerById` (`WHERE id=$id`) | **High** | route segment concatenated raw |
| Mass-assignment on insert | `ansoegninger_model::addAnsoegning($_POST)` | **High** | whole `$_POST` inserted; attacker can set `receivedByAlumneId`/`id` etc. |
| Email header injection | `optagelse.php:99,111,205` (subject/From-name/To from `$_POST`) | **Medium** | `xss_clean` doesn't strip CRLF; CI Email may mitigate — verify |
| No CSRF protection | all POST + the state-changing GET `setasreceived` | **Medium** | `csrf_protection=false` site-wide (`01-infra` A4); `setasreceived` mutates on GET |
| Inconsistent authorization | `listansoegninger`/`showAnsoegning` (`indstilling!=1`) vs `setasreceived` (`empty()`) | **Medium** | any non-empty `indstilling` can mark-received |
| Stored data shown in admin views | `show/list_ansoegninger_box` | **Medium** | output-escaping of applicant text not confirmed; `xss_clean` on input is partial defense |
| Hardcoded SMTP creds | `config/email.php` | **High** | inherited (`01-infra` A6/A10) |
| Public write gated only by captcha | `send_rundvisning`/`send_fremleje` | **Low** | acceptable, but no rate-limit → spam/DB-growth |

## Quirks, edge cases & suspected bugs
- **Committee never notified of fremleje applications** — `if(TRUE)` replaces the real `sendMail(...)`
  to `indstillingen@gahk.dk` (`optagelse.php:181-182`). Users/staff may have silently relied on (or been
  bitten by) this. **The "diff against old site" test must reproduce this exact behavior, then we decide to fix.**
- **`sendMail()` always returns TRUE**, even when `$this->email->send()` fails (`optagelse.php:240-244`:
  `if(!send()){ return true; } return TRUE;`). So the user always sees "success" and is redirected even if
  no mail went out. Applications are still saved.
- **Out-of-order month array** in `showAnsoegning` (`...Jul, Aug, Okt, Sep, Nov...` — Sep/Okt swapped,
  `optagelse.php:334`) vs the correct order in `listansoegninger`. Cosmetic but visible.
- `Pylon_calendar_model` loaded but unused.
- Dead commented-out `load->view(...)` blocks (superseded by `showInternPage`).
- `gender` mapped to boolean `female = (gender=="female")`; any other value (incl. empty) → `female=0`.
  Exact set of values the form posts is unconfirmed.
- Counter + (disabled) reminder fire on every action, including POST submits.
- `email` has no `valid_email` rule on the public forms (only `required`).
- Mojibake risk: `gahk_ansoegninger` is `latin1_swedish_ci`; Danish characters in motivation/names need
  careful latin1→utf8 handling in ETL (`01-infra` A2 / scope §6).

## Reimplementation notes (Django)
- **Views:** two public `FormView`s (rundvisning, fremleje, the latter with i18n) + two admin views
  (`ListView` paginated, `DetailView`) + a small "mark received" action (POST, not GET). **Forms** with
  explicit fields kill the mass-assignment and SQLi classes; ORM kills the raw `LIMIT`/`WHERE` injection.
- **Model:** one `Ansoegning` model over `gahk_ansoegninger` (fields above; `female`→boolean, add proper
  `received_by`/`received_at`). Captcha → Turnstile/reCAPTCHA v2 (scope §4).
- **PRESERVE:** the exact public URLs (`/optagelse`, `/optagelse/ansoeg`, `/optagelse/fremlej[/eng]`) for
  SEO 301s; the auto-reply texts; the "received via emailed link" workflow.
- **FIX (record + confirm first):** re-enable the committee notification for fremleje; make email-send
  failure actually surface; unify the admin authorization check; `setasreceived` → POST + CSRF.
- **URL patterns to keep:** all `GET /optagelse/*` paths verbatim.

## Open questions
- Is the missing fremleje committee email a **bug to fix** or has the office moved to checking the list
  view (i.e. intended)? Needs the committee's confirmation before we "fix" it.
- What values does the `gender` field actually submit (form template not yet read) — needed to preserve
  the `female` mapping faithfully.
- Are applicant fields HTML-escaped in `list/show_ansoegninger_box` on output? (Determines stored-XSS risk.)
- Is there any retention policy for `gahk_ansoegninger` (GDPR — these are unsolicited personal data with
  no expiry/rate-limit today)?
- `pageId` → `gahk_page` mapping (6/7/8/9): confirm these CMS rows exist and are the intended copy.
