# Wiki (wiki.gahk.dk)

MediaWiki 1.43 on its own host. Residents sign in on the wiki with their intranet e-mail and
password. Nobody can read anything without signing in.

## Accounts

- One wiki account per resident, tied to them by their resident id.
- The account is named after the resident's e-mail address (`Anders@example.dk`) and carries
  their e-mail and real name from the intranet. The e-mail counts as confirmed.
- When the e-mail changes in the intranet, the next login renames the account, and its user page
  and talk page move with it. History, watchlist and contributions stay with the account. The old
  user pages redirect to the new ones.
- If someone else's account still holds the address, it is renamed aside to
  `<address> (konto <id>)` until its owner logs in again.
- Residents cannot change their e-mail or real name in the wiki; the intranet is the source.
- Accounts from the old login (named after the e-mail in any capitalisation, or the old numeric
  id) are adopted at the resident's first login: the one already named right, else the one with
  the most edits. Further duplicates stay as they are.

## Who may sign in, and for how long

- Anyone the intranet lets in (`is_active`). The kitchen login is not a resident and is refused.
- The wiki checks each resident against the intranet at most every five minutes, and at every
  login. Changes take effect within five minutes. The resident is signed out when:
  - they are deactivated or deleted. All their sessions and "remember me" cookies stop working,
    including after a reactivation.
  - their e-mail or name changed. Logging in again updates the account.
- Sessions from before this feature are signed out once; the next login links the account.
- If the intranet's database is down, signed-in residents carry on with what the wiki last knew
  about them, admin rights included, for up to a day. Someone the wiki knows nothing about (the
  wiki restarted during the outage) stays signed in without admin rights. Nobody can log in
  ("Wikien kan ikke tjekke logins lige nu").

## Administrators

- **Netværksgruppen** (`administrator` in the active month, approved by a second administrator, as
  for the intranet's admin) are wiki administrators: the sidebar and other MediaWiki: pages, site
  CSS and JavaScript, protection, deletion, blocks. A Django superuser counts as one.
- The rights come and go with the role, within five minutes.
- Accounts given rights by hand in the wiki keep them.

## Decisions

- **The wiki asks, and remembers the answer for five minutes.** Nothing in the intranet tells the
  wiki when someone leaves. Asking at most every five minutes keeps the intranet's database out of
  almost every page load, and the remembered answer carries signed-in residents through an outage.
- **The intranet's rules, not new ones.** Access is the intranet's own login rule; admin needs the
  same second approval as Django admin, since wiki admins can change the JavaScript every resident
  runs.
- **Admin rights are never stored in the wiki,** so they cannot outlive the role. The cost: the
  wiki's user list does not show them.
- **Renames happen at login, before the account is loaded.** A rename resets the account's session
  token; done mid-login it would sign out the session being created.
- **An outage keeps sessions instead of signing everyone out.** Only logins need the intranet's
  database; an outage stops them, and nothing else.
