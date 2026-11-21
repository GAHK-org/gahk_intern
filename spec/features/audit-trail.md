# Feature: Revisionsspor — the audit trail

One record of everything that happens on intern: who did what, when, from where, to which things,
and what every changed field held before and after. Under Administration → Revisionsspor.

## What is recorded

- Every action a person or the system takes that changes data, as one event: a name for what
  happened ("Gav rolle", "Rettede beboer"), who did it, when, the request's IP address, and every
  thing it involved.
- For each thing involved: whether it was created, changed, deleted or only took part, its name at
  the time, and each changed field before and after.
- Logging in, failed logins (with the reason, and the address typed when there is no such account),
  logging out, changing a password, asking for a reset link (with the address asked for and how
  many accounts it reached), and setting a password from a link.
- Being refused a page one's roles do not open. Not while an administrator previews another role.
- Roles given and removed, admin access approved and refused, the alumneliste copied, saved and
  added to, residents created and edited, the room round ended, and an administrator viewing the
  site as another role.
- Edits in the Django admin, and anything written from a shell, are recorded as "Uden handling":
  every field, by whoever was logged in, without a name for what was meant.
- Work done by the background workers is marked as such, with no person behind it.

## What is not recorded

- Passwords and tokens: the trail says that they changed, never what they held.
- Fields that move by themselves: last login, staff status (it follows the roles), thumbnails.
- Reactions and similar small things, each app saying which of its models it leaves out and why.

## Append-only

- Nothing in the trail can be changed or deleted, by anyone: the database refuses it.
- Deleting a resident or anything else leaves its history as it was, with the names it had then.

## The audit page

- Administrators only.
- Newest first, 50 at a time, with "Ældre" for the next 50.
- Each line: when, who, what, and the thing it was about. Unfolding it shows the details, the IP
  address, and every thing involved with its changes; long texts unfold on their own.
- Filters: free search (a person, a thing's name, an action), area (Login og adgang, Beboere og
  roller, …), source (web, worker, system), from and to dates, and only "Uden handling".
- A person's name shows everything they did; a thing's name shows its whole history.

## Pages of their own

- A feature may show its own part of the trail on its own pages (the Hjemmeside, Drev). The same
  events also appear on the audit page.

## Decisions

- One general trail for every feature, so "everything Anna did last week" is one question. A feature
  keeps a table of its own only for what the general trail cannot hold.
- Append-only in the database, not just in the code, so even an administrator with the Django admin
  cannot tidy the history. The price: personal data in the trail stays as long as the trail does.
- A change nobody claimed is still recorded, so nothing is unseen; in the test suite it is an error,
  so every screen and job names what it does.
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
