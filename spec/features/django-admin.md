# Feature: Django admin — mostly for looking things up

Django's own admin at /django-admin/. Superusers and administrators whose Django-admin access a
second administrator has accepted can open it (see admin-summary.md for who that is each month).
Everything in it can be read; very little can be changed.

## What can be changed

- **Rooms:** add, fix floor, side and note, delete one nobody lives in. A room's number and old index
  are fixed once it exists.
- **Embedsgrupper and rengøringer:** add, rename, and set the required size per year. Not delete.
- **Photo album and GAHKFlix tags:** rename and delete (neither feature does it).
- **Residents:** only *active* (closes or reopens a login) and *kitchen account*. The password is not
  shown.
- **Begivenheder answers:** only the time of an answer, which decides the order of the waitlist. No
  "you got a seat" notice goes out for a change made here.
- **Opslag pictures:** only which opslag a picture belongs to.
- **Scheduled jobs:** only switching one off or on.

## What can be deleted

- Den Hurtige messages and replies — the only way to remove an archived one.
- Begivenheder and their comments — the only way anyone can reach a private event.
- Calendar subscription links — revokes it; the resident's next visit makes a new one.
- Applications — erasing an applicant before the one-year retention removes them.

## Everything else is read-only

Opslag, comments and reactions; repairs; GAHKFlix videos, series and requests; albums and pictures;
notifications and their settings; push devices; the monthly list and roles; schedules; groups. The
feature's own pages are where these change.

Arkiv keeps its own admin (root folders have no other screen) until Drive replaces it.

## Decisions

- **Read-only unless nothing else can do it.** An edit here skips what the feature does around it:
  history, votes, notifications, the calendar update, removing stored files. A row changed here is a
  row the feature never saw change.
- **New models start read-only.** A test lists every exception, so a newly registered model is
  read-only until someone adds it there on purpose.
- **No superuser switch, groups or permissions on residents.** An administrator who could tick them
  on their own row would not need a second administrator to accept them.
- **Roles are given on the roles page only.** It records who gave `administrator`, which the
  two-person rule depends on.
- **Schedules live in the code.** Beat writes them to the database every time it starts, so a
  schedule changed here would quietly be put back at the next deploy; pausing a job is the exception.
- **Embedsgrupper are not deleted.** Every past month's list that named the group would lose it.
