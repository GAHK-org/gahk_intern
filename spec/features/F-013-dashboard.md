# Feature: Dashboard — internal-area landing page (nyintern forside)

- **Feature ID:** F-013
- **Source file(s):** `application/controllers/intern/dashboard.php`,
  `application/views/intern/dashboard.php`,
  base `application/core/MY_Controller.php` (`showInternPage()`),
  helper `application/helpers/gahk_helper.php` (`insideGAHK()`).
  (`application/models/internuser_model.php` is loaded in the constructor but **never used** by this feature — see Quirks.)
- **URL / route:** (route `nyintern → intern/dashboard`, default action `index`; the generic
  `nyintern/(:any) → intern/$1` route also resolves `nyintern/dashboard`)
  - `GET /nyintern/` — dashboard (index)
  - `GET /nyintern/dashboard/` — dashboard (index, via generic route)
- **HTTP method(s):** GET only (no form submitted from this page; `index()` ignores POST).
- **Access control:** **logged-in**, enforced **inline** in `index()`:
  `$this->session->userdata('username')` must be truthy, else the request is redirected to
  `nyintern/admin` (login). Uses the standard CI session userdata auth pattern (see `01-infrastructure.md` A4/A5).
  ⚠ No role checks — any logged-in user sees the page. A second, **non-auth** gate inside the
  view, `insideGAHK()` (campus-IP check, see `01-infrastructure.md` A9), governs the secret blocks.

**Routes / actions:**
| Action | URL | Method | Access | Purpose |
|---|---|---|---|---|
| `index` | `/nyintern/` | GET | logged-in (`username`) | render the internal landing page; on-campus IP additionally sees WiFi + calendar credentials |
| `index` | `/nyintern/dashboard/` | GET | logged-in (`username`) | same (generic route alias) |

## Purpose
The forside ("front page") a resident lands on after logging into GAHK Intern. It is a static welcome
page: a link to change one's password, a contact mail for the network group, links to the alumne-list
and GAHK-Intern mobile apps, and an embedded Google calendar of house events. For users connecting from
the GAHK campus network it additionally reveals the shared Wi-Fi password and a shared Google-calendar
login so on-site residents can join the network and add events.

## Inputs
| Name | Source | Type | Required? | Validated/sanitized? | Used for |
|---|---|---|---|---|---|
| `username` | session userdata | string | yes (auth gate) | n/a (read-only) | truthiness check in `index()` (`dashboard.php:17,19`); if falsy → redirect |
| `current_url()` | derived (CI URL helper) | string | n/a | n/a | stored as `redirectToUrlAfterLogin` flashdata before redirect (`dashboard.php:20`) |
| `$_SERVER['REMOTE_ADDR']` | request (client IP) | string | implicit | compared to a hardcoded allow-list of 6 IPs | `insideGAHK()` (`gahk_helper.php:4-6`) — gates the two secret blocks in the view (`dashboard.php:47,78`) |
| `fullname`, `alumne_id`, `akRole`, `indstilling`, `inspektion`, `kokkengruppe`, `oelkaelder`, `administrator` | session userdata | mixed | no | n/a | read by `showInternPage()` (`MY_Controller.php:14-32`) and passed to header/footer views for menu/chrome — **not** used by the dashboard body itself |
| `pagename` = `"dashboard"`, `pageheader` = `"Forside"` | controller literals | string | n/a | n/a | view data set in `index()` (`dashboard.php:23-24`) |

There are **no** GET query params, POST fields, or route segments consumed by this feature.

## Database interactions
- **Tables touched:** **none** by this feature's own code path.
- **Reads:** none. (The controller loads `Internuser_model` in `__construct` but never calls it; the
  body reads only session userdata, which is CI's DB-session lookup handled by the session library, not
  by this feature.)
- **Writes:** **none.** ⚠ Unlike the public controllers (`admin`, `optagelse`, `news`, `page`, `pylon`),
  `intern/dashboard.php` does **not** call `$this->counter()` in its constructor. The visit counter
  (`gahk_counter`/`gahk_counterdato` via `MY_Controller::counter()`) therefore does **NOT** run on
  dashboard hits — contrary to the task's stated assumption. See Quirks.
- **Transactions / ordering:** none.

## Business logic
- `index()` reads `username` from the session.
  - If falsy: set flashdata `redirectToUrlAfterLogin = current_url()` and `redirect("nyintern/admin")`
    (login page). Nothing rendered.
  - Otherwise: set `pagename="dashboard"`, `pageheader="Forside"` and call
    `showInternPage('intern/dashboard', $data)`, which wraps `intern/header` + the dashboard body +
    `intern/footer` (with session-derived chrome variables).
- The view body is **mostly static HTML** with two conditional secret blocks, both gated by
  `insideGAHK()` (returns true only when `$_SERVER['REMOTE_ADDR']` is one of 6 hardcoded campus IPs:
  `130.225.243.26`, `192.38.116.242`–`.246` — `gahk_helper.php:4`):
  - **Block 1 (`dashboard.php:47-55`):** prints the **plaintext Wi-Fi password**
    `IAlleDeRigerOgLande1908` plus links to the WiFi wiki page and the MAC-address page.
  - **Block 2 (`dashboard.php:78-87`):** prints a **shared Google-calendar login** in plaintext —
    E-mail `gahkkalender`, password (Adgangskode) `nokolugter` — plus a Google ServiceLogin link.
- Always shown (no gate): welcome header, "change your password" link
  (`http://gahk.dk/nyintern/admin/changepassword`), `mailto:it@gahk.dk`, alumne-list app links
  (web + Google Play), GAHK-Intern Google Play badge, and the Google-calendar **iframe**.
- **Branching:** the only branch is `insideGAHK()` (on/off, per request IP). No per-user/per-role
  branching in the body.

## Outputs & side effects
- **Renders:** the internal forside HTML inside the intern header/footer chrome (via `showInternPage()`).
- **Secret leakage (on-campus IPs only):**
  - Wi-Fi password `IAlleDeRigerOgLande1908` in cleartext (`dashboard.php:49`).
  - Google-calendar credentials `gahkkalender` / `nokolugter` in cleartext (`dashboard.php:83-84`).
  These are **hardcoded in the view source** regardless of IP; `insideGAHK()` only decides whether they
  are *echoed*. The secrets are present in the repository/source for anyone with file access.
- **Embeds:** a Google Calendar `<iframe>` to `google.com/calendar/embed` for calendars
  `gahkkalender@gmail.com` and `mnic13suhuvarq6ffitg2j30m4@group.calendar.google.com` (`dashboard.php:76`).
- **Redirect:** unauthenticated → `redirect("nyintern/admin")` with `redirectToUrlAfterLogin` flashdata.
- **Session/headers:** `session_start()` is called in the constructor (`dashboard.php:6`) in addition to
  CI's own session library — see Quirks. No custom headers, no cache directives (this controller does
  **not** set the no-cache headers that, e.g., `optagelse` does).
- **Visit-counter write:** **none** (see Database interactions — `counter()` is not invoked here).

## Dependencies
- **Cross-cutting infra used:** `MY_Controller` base + `showInternPage()` (intern header/footer chrome,
  session-derived view vars) — `01-infrastructure.md` A9; CI DB sessions / session userdata auth —
  `01-infrastructure.md` A4/A5; `insideGAHK()` campus-IP gate — `01-infrastructure.md` A9. Referenced by
  name; not re-described here.
- **Helpers/libraries loaded in constructor:** `form` helper, `gahk_helper` (for `insideGAHK()`),
  `session` library, `Internuser_model` model. ⚠ Of these, only `gahk_helper`/`session` are actually
  used; `form` helper and `Internuser_model` are loaded but unused on this page.
- **External:** Google Calendar embed (iframe) + Google account ServiceLogin link; Google Play / app
  store image badges; `gahk.dk` wiki & app pages. All are hardcoded URLs in the view.

## Security findings
| Issue | Location | Severity | Note |
|---|---|---|---|
| Hardcoded plaintext Wi-Fi password in source/view | `views/intern/dashboard.php:49` | **High** | `IAlleDeRigerOgLande1908` committed in the template; in repo and served to on-campus clients |
| Hardcoded plaintext Google-calendar credentials in source/view | `views/intern/dashboard.php:83-84` | **High** | user `gahkkalender` / pass `nokolugter` committed in the template |
| Secret display gated only by client-IP allow-list | `helpers/gahk_helper.php:4-6`, `dashboard.php:47,78` | **Medium** | `insideGAHK()` trusts `$_SERVER['REMOTE_ADDR']`; spoofable behind a misconfigured proxy / `X-Forwarded-For` rewrite, and bypassable by anyone with repo/source access regardless of IP |
| Auth check is inline, not centralized; no role enforcement | `dashboard.php:17-22` | **Low** | any logged-in `username` sees the page (acceptable for a landing page, but auth is duplicated per-controller) |
| No CSRF token / no output escaping concerns | view is static | **Low/Info** | no user input is reflected, so no reflected XSS here; CSRF N/A (no state change). `csrf_protection=false` site-wide (`01-infra` A4) but irrelevant for this GET-only page |
| Redundant `session_start()` alongside CI session lib | `dashboard.php:6` | **Low** | manual PHP `session_start()` plus CI `session` library; possible double session handling (inherited pattern across intern controllers) |

Record, do not fix.

## Quirks, edge cases & suspected bugs
- **Visit counter does NOT run here.** The task brief assumed `MY_Controller` runs the counter on every
  hit, but `MY_Controller::__construct()` (`MY_Controller.php:5-10`) does **not** call `counter()`;
  each controller calls `$this->counter()` itself, and `intern/dashboard.php` does **not**. So dashboard
  views are **not** counted. (Contrast `optagelse.php:23`, `admin.php:8`, `news.php:9`, `page.php:10`,
  `pylon.php:9`, which do.) ⚠ Surfaced as a finding, not corrected.
- **Secrets live in version-controlled template source.** Even off-campus, the Wi-Fi password and
  calendar credentials are readable by anyone who can see the repo or the raw PHP; `insideGAHK()` only
  controls runtime echo.
- **`insideGAHK()` ignores IPv6 and proxies.** Pure exact-match against 6 IPv4 literals; behind a reverse
  proxy `REMOTE_ADDR` may be the proxy's IP, so on-campus users could *lose* the blocks, or a spoofed
  forwarded address scenario could *gain* them — depends on deployment.
- **Unused loads:** `Internuser_model` and the `form` helper are loaded in the constructor but never used
  by the dashboard. The model is fully analyzed only because the brief named it; this feature touches none
  of its methods.
- **Hardcoded `http://` (non-TLS) links** to `gahk.dk/nyintern/admin/changepassword` and the wiki
  (`dashboard.php:41,50`) — mixed-content / downgrade risk if the site is HTTPS.
- The "Sep/Okt"-style month-array bug noted in F-001 is not present here (no date logic).
- `pagename`/`pageheader` are set but the body view does not reference them; they feed header/footer chrome.

## Reimplementation notes (Django)
- **View type:** a simple `TemplateView` (login-required via `LoginRequiredMixin`, redirecting
  unauthenticated users to the nyintern login). **Template:** a static `dashboard.html` extending the
  intern base layout; keep the calendar `<iframe>`.
- **FIX (record + confirm first):** move the Wi-Fi password and Google-calendar credentials **out of the
  template** into a secret store / settings (env or a `Setting`/`SecretConfig` model), and reconsider the
  IP gate — replace `insideGAHK()` reflection-of-secrets with either an authenticated role or a server-side
  config lookup, so secrets are never literals in source. **PRESERVE:** the page content/links, the
  on-campus-only visibility behavior (if still desired), and the URLs `/nyintern/` and `/nyintern/dashboard/`.
- **URL pattern:** route both `/nyintern/` and `/nyintern/dashboard/` to the dashboard view (301/alias as
  in CI).
- Decide explicitly whether the dashboard should now increment the visit counter (it currently does not).

## The shared Google calendar is now replaceable

`app/events/` (see `spec/features/begivenheder.md`) gives the kollegium its own events feature with
tilmelding, a month calendar and a subscribable `.ics` feed. That makes the embedded Google calendar
— and the **shared Google account whose username and password are printed on the dashboard for every
logged-in resident** — replaceable rather than load-bearing, which it was not before.

It is deliberately **not** removed yet, and the missing step is not code: somebody has to export
what is actually live in `mnic13suhuvarq6ffitg2j30m4@group.calendar.google.com` first, and nobody
has. Deleting the embed before that loses whatever is in there. The order is therefore:

1. Export the Google calendar (`.ics`) and read what is in it.
2. Recreate anything still relevant as begivenheder, or accept the loss deliberately.
3. Remove the embed, the credentials and the ServiceLogin link from `dashboard.html`, and the
   assertions in `test_features.py` that currently pin them.
4. Retire the shared Google account, or at least rotate it.

Its own PR. Recorded here so that it does not get forgotten now that the blocker is gone.

## Open questions
- Where should the Wi-Fi password and shared Google-calendar credentials live going forward? (Secrets
  manager / env / DB-backed editable setting?) Who owns rotating them, given they are currently
  source-committed?
- Is the **campus-IP gating** still the intended access model for these secrets, or should it become a
  proper authenticated/role check? The current allow-list (6 IPv4 addresses) is likely stale.
- Is the **absence of the visit counter on the dashboard** intentional, or an oversight that should be
  reproduced then fixed?
- The shared Google-calendar account credentials suggest a single shared Google login — is that account
  still in use, and should the embed/credentials be replaced by a proper integration?
- Confirm whether the redundant manual `session_start()` matters for session behavior or is dead code
  inherited across intern controllers.
