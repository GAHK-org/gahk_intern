# Feature: Intern auth — members-area login, logout, password change/reset and own profile

**Feature ID:** F-014

## What it is

The front door to the members area at `/intern/`. Residents log in with **email and password**,
log out, change their password, recover a forgotten one by email, and edit their own profile. A
newly created resident gets a welcome mail with a link to set their first password. The views are
Django's stock auth views (`django.contrib.auth.views`), configured in `app/residents/urls.py` under
the legacy `/intern/admin/...` paths. The user model is `residents.Resident`
(`AUTH_USER_MODEL`, `USERNAME_FIELD = "email"`).

**What it replaces.** The PHP `intern/admin` controller under `/nyintern/admin/` authenticated
against unsalted SHA-256 hashes, kept its own remember-me token table, decided admin status at login
from a join on `gahk_admin_user`, and reset a forgotten password by mailing a link to a page that
*displayed* a new temporary password. Now Django sessions and hashers do the work, roles are read per
request from monthly assignments (F-010, see F-002), and reset links let the resident choose a
password. `/nyintern/<rest>` 301-redirects to `/intern/<rest>` (`app/config/urls.py`).

## Access

| Action | Who |
| --- | --- |
| Log in, request a reset link, use a reset link | anyone |
| Log out | any session (POST) |
| Change password, edit own profile | logged-in resident |
| View any resident's profile page | logged-in resident |

Every members-area view carries its own `login_required` or `role_required` decorator. There is no
site-wide login middleware. `LOGIN_URL = /intern/admin/login`, `LOGIN_REDIRECT_URL = /intern/`,
`LOGOUT_REDIRECT_URL = /intern/admin/login` (`app/config/settings.py`).

## Routes

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `/intern/admin/login` | GET, POST | anyone | `LoginView` + `EmailAuthenticationForm`; honours `next` |
| `/intern/admin/logout` | POST | session | `LogoutView`; flushes the session, redirects to login |
| `/intern/admin/password-change` | GET, POST | logged in | `PasswordChangeView` (old + new twice) |
| `/intern/admin/password-change/done` | GET | logged in | Confirmation |
| `/intern/admin/password-reset` | GET, POST | anyone | "Glemt kodeord": mails a reset link |
| `/intern/admin/password-reset/done` | GET | anyone | "Link sent" (shown whether or not a mail went) |
| `/intern/admin/reset/<uidb64>/<token>/` | GET, POST | link holder | Choose a new password |
| `/intern/admin/reset/done` | GET | anyone | Confirmation; the resident then logs in |
| `/intern/beboer/min-profil/rediger` | GET, POST | logged in | Edit own profile (`views.edit_profile`) |
| `/intern/beboer/<pk>/profil` | GET | logged in | A resident's profile page (`views.profile`) |

URL names are Django's defaults (`login`, `logout`, `password_change`, `password_reset`,
`password_reset_confirm`, …). The dashboard links "Skift kodeord", the login page links "Glemt
kodeord?", and the logout button in `base.html` is a POST form.

## Login

- **The email is matched case-insensitively.** `ResidentManager.get_by_natural_key` tries an exact
  match first, then `iexact`.
- **Legacy hashes are upgraded on login.** Migrated residents keep their old unsalted SHA-256 hash,
  stored as `gahk_sha256$$<hex>`. `core.hashers.GahkLegacySHA256PasswordHasher` (last in
  `PASSWORD_HASHERS`) verifies it in constant time, and `must_update` returns True, so Django
  re-hashes with PBKDF2 on the first successful login. No forced reset.
- **Deactivated residents cannot log in** (Django's `ModelBackend` rejects `is_active=False`).
- **Bad credentials** show a single generic "Forkert e-mail eller kodeord."
- **Roles are not decided at login.** They are read per request from the active month's
  `RoleAssignment`s (`residents.permissions`). A month rollover changes access without a re-login.
- **Session cookie:** `Secure`, scoped to `.gahk.dk` in production (`SESSION_COOKIE_DOMAIN`).
  Lifetime is Django's default.

## Password change and reset

- **New passwords** must pass Django's four standard validators (user-attribute similarity, minimum
  length, common passwords, numeric-only) for both change and reset.
- **Reset links** are Django's token links and expire after **2 hours**
  (`PASSWORD_RESET_TIMEOUT = 7200`). The mail is `registration/password_reset_email.html` with the
  subject "Nulstil dit GAHK Intern-kodeord". A token also stops working once the password changes.
- **Reset lookup** uses `ResidentPasswordResetForm` (`app/residents/forms.py`). It finds active
  residents by case-insensitive, Unicode-normalised email, **including residents with no usable
  password**. Deactivated residents are skipped. The "done" page shows either way, so the form does
  not reveal which addresses exist.

## Welcome mail for new residents

When Indstillingen adds a new resident in the next-month list editor (F-010), the resident is created
with an **unusable password**. `residents.views._send_welcome_email` then sends "Velkommen til GAHK
Intern" with a set-password link: a reset token, so it also expires after 2 hours. It includes a
fallback link to the reset form. The send is best-effort. A failure is logged, and the editor shows
a warning telling Indstillingen to point the resident at "glemt kodeord".

## Own profile

`ProfileEditForm` covers profile picture (an image, at most 5 MB, checked by `core.uploads`), bio
(max 500 characters), Facebook URL and Instagram handle. **Name, email and phone are not
self-editable.** They are on `ResidentEditForm`, which only `indstilling` reaches via
`/intern/beboer/<pk>/rediger`. The profile page shows any resident's room, groups and badges for the
active month, plus their 10 latest opslag.

## Decisions

- **Reset reaches residents who never set a password.** Django's stock `get_users` skips them. A new
  resident who missed the 2-hour welcome window got the "link sent" page and no mail, and only
  `manage.py changepassword` could let them in. Dropping the filter is safe because the token still
  goes to the account's own address, and GAHK has no LDAP/SSO accounts that the filter protects
  (`ResidentPasswordResetForm` docstring).
- **Case-insensitive login.** Residents typing a capital first letter were locked out. Exact match
  goes first to avoid `MultipleObjectsReturned` if two rows differ only by case.
- **Upgrade-on-login instead of a forced global reset.** The legacy hasher must stay until no
  `gahk_sha256` hashes remain (`core/hashers.py`, which notes that the wiki checks the same format).
- **2-hour reset links** (settings comment: "F-014 decision, 2026-06").
- **Welcome-mail failures are reported, not swallowed.** A wrong `DEFAULT_FROM_EMAIL` (one.com
  refuses senders the account is not an alias of) must not look like success.

## Open questions

- **Nothing in the app deactivates residents.** Only Django admin sets `is_active=False`. Moved-out
  residents keep a working login and can still request reset links, unless someone deactivates them
  by hand.
- **No login throttling** or lockout on the login or reset forms.
- **`/django-admin/login/` is a second login form.** Django's own admin login, with no reset link
  or styling. It goes through the same backend and only admits `is_staff`. Undecided whether it
  should redirect to `/intern/admin/login`.
- **`ResidentAdmin` shows the password hash** as an editable text field (see F-002). Its comment
  defers a proper set-password form to "the auth feature build (F-014)", which has not happened.
- **Legacy self-service contact edit is gone.** The PHP `editinfo` let residents change their own
  email and phone; now only Indstillingen can. Unclear whether that is intended.
