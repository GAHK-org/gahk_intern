# Feature: Revisionsspor — the audit trail

One record of everything that happens on intern: who did what, when, from where, to which things,
and what every changed field held before and after. Under Administration → Revisionsspor.

## What is recorded

- Every action a person or the system takes that changes data, as one event: a name for what
  happened ("Gav rolle", "Rettede beboer"), who did it, when, the request's IP address, and every
  thing it involved.
- For each thing involved: whether it was created, changed, deleted or only took part, its name at
  the time, and each changed field before and after.
- Logging in, failed logins (with the reason), logging out, changing a password, asking for a reset
  link (with the account it reached, if any), and setting a password from a link.
- An address typed into the login or reset form that matches no account is kept only as its domain
  and a keyed hash, never as typed: people type their password into that box. Searching the page for
  the address still finds every try with it.
- Being refused a page one's roles do not open. Not while an administrator previews another role.
- Roles given and removed, admin access approved and refused, the alumneliste copied, saved and
  added to, residents created and edited, the room round ended, and an administrator viewing the
  site as another role.
- Sending mail by hand: failed mails sent again from the Django admin, the alumneregninger sent, and
  one sent again because it did not arrive.
- Edits in the Django admin, and anything written from a shell, are recorded as "Uden handling":
  every field, by whoever was logged in, without a name for what was meant.
- Work done by the background workers is marked as such, with no person behind it.

## What is not recorded

- Passwords and tokens: the trail says that they changed, never what they held.
- Fields that move by themselves: last login, staff status (it follows the roles), thumbnails.
- Reactions and similar small things, each app saying which of its models it leaves out and why.
- The outbox's own rows: queued, tried, sent, failed, emptied. The mail page shows them.

## Append-only, for a fixed time

- Nothing in the trail can be changed, by anyone, and nothing can be deleted before its time is up:
  the database refuses it.
- Logins, failed logins, logouts and reset requests are kept for a year; everything else for five
  years, as long as bookkeeping is kept. Every night the events past their time are deleted, and the
  trail records how many.
- Deleting a resident or anything else leaves its history as it was, with the names it had then,
  until that history's time is up.
- A request to delete one's data does not reach the trail before its time is up: it is the record of
  who did what.

## The audit page

- Administrators only.
- Newest first, 50 at a time, with "Ældre" for the next 50.
- Each line: when, who, what, and the thing it was about. Unfolding it shows the details, the IP
  address, and every thing involved with its changes; long texts unfold on their own.
- Filters: free search (a person, a thing's name, an action by its name on the page or its code, an
  address typed into the login form), area (Login og adgang, Beboere og
  roller, …), source (web, worker, system), from and to dates, and only "Uden handling".
- A person's name shows everything they did; a thing's name shows its whole history.
- What a feature keeps from administrators (the names in a Delt mappe, say) is kept in the trail in
  full but not shown here: such a thing is named by a tag ("Fil 3f2c9a", the same in every event), its
  private fields read "ændret (vises ikke her)", so do the event details a feature keeps for its own
  pages, and searching does not find it by name. The
  feature's own pages show them to whoever may see them there.

## Pages of their own

- A feature may show its own part of the trail on its own pages (the Hjemmeside, Drev). The same
  events also appear on the audit page.

## Decisions

- One general trail for every feature, so "everything Anna did last week" is one question. A feature
  keeps a table of its own only for what the general trail cannot hold.
- Append-only in the database, not just in the code, so even an administrator with the Django admin
  cannot tidy the history. The price: personal data in the trail stays as long as the trail does,
  which is why that is a fixed time (decided 2026-11-23), and why a typed address is only hashed.
- The retention rule lives once, in the database (audit migration 0002): the trigger and the nightly
  purge ask the same function. Changing a period is a migration.
- A change nobody claimed is still recorded, so nothing is unseen; in the test suite it is an error,
  so every screen and job names what it does.
- Private names are hidden on the page, not in the database: administrators run the server and can
  read it anyway. The page only keeps from showing what a feature has promised not to show them, and
  a feature's own history can still read its part of the one trail.
- Names are kept as they were at the time: a resident who changes name or is deleted still reads
  right in old events. Related things in a change are shown by their name today.

## Development seed data

- A demo database gets a made-up history of the five weeks before the day it was seeded, with every
  kind of line the page shows: logins and failed logins (a night-time burst against admin@ among
  them), a password reset, refused pages, roles given and removed, an administrator approved and one
  refused, residents and the alumneliste edited, the room round, a role preview, and "Uden handling"
  from the Django admin and from a shell. By the documented logins and others, from several addresses.
- Loading the demo data itself is not in the trail. The history is written afterwards, on purpose,
  and its before and after values are real: it ends at the seeded data.
