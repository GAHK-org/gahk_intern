# Access levels

Every account has an access level, which decides which parts of the intranet exist for it. Roles are
separate and add powers on top (moderate, edit gahk.dk, the embedsgrupper's tools); the level decides
the pages.

## Levels

The level follows the monthly alumneliste. "The month in effect" is the newest published list that has
started.

| Level | Who |
|---|---|
| Beboer | On the list in effect, or holds a role this month, or is a superuser |
| Flytter ind | On a coming month's list, not the one in effect |
| Fraflyttet < 1 måned | Last on the list the month before the one in effect |
| Fraflyttet 1–3 måneder | Last on the list 2–3 months before |
| Fraflyttet 3–12 måneder | Last on the list 4–12 months before |
| Fraflyttet > 1 år | Last on the list more than 12 months before |
| Køkkenet | The kitchen staff's shared login (ticked in Django admin) |
| Ingen adgang | Never on any list, and none of the above |

Someone moving back in is Flytter ind until their first month starts.

The Indstilling can make an exception on the resident's edit page: another level (not Køkkenet),
through a last day or until removed. It replaces what the list says; roles, superusers and the
kitchen still come first. The edit page shows the level the account has now.

## What each level gets

- **Beboer**: everything.
- **Flytter ind**: everything except Fotoalbum and GAHKFlix.
- **Fraflyttet < 1 måned**: everything except Søg værelse.
- **Fraflyttet 1–3 måneder**: everything except Søg værelse, Værelsestjek and Reparationer. AK and
  Ølkælder are read-only: they can see their AK page and Ølkælder balance, but not add krydser.
- **Fraflyttet 3–12 måneder**: Den Hurtige, Opslagstavlen, Begivenheder and Fotoalbum (with their
  notifications and the @-picker).
- **Fraflyttet > 1 år**: Stamtræ.
- **Køkkenet**: Den Hurtige's Køkkenet channel, its own opslag, Reparationer and its notifications.
- **Ingen adgang**: nothing. The front page says so and offers to log out.

Every level can log in and out and change or reset its password. The role tools (Optagelse,
Hjemmeside, Regnskab, Site-admin, Django admin) are for residents only.

## Behaviour

- A page a level cannot open answers 403. Read-only means a level can open the pages but sends
  nothing (no form posts).
- The front page sends a level without it to its first feature: Den Hurtige, then Opslagstavlen,
  Begivenheder, Fotoalbum, Stamtræ. The kitchen lands on its channel.
- The sidebar, the search box, the bell and the account menu show only what the level can open.
- Nobody is notified about a page they cannot open, whatever the notification's audience. The
  notification settings leave out the groups the level never gets.
- Live updates reach only the features the level can open.
- A calendar feed is empty once its owner's level has no Begivenheder.
- Administrators can see the site as any level under "Se side som".
- A level changes the moment someone's months, roles, kitchen flag or exception change, and for
  everyone when a list makes a newer month the one in effect. Just after midnight every level is
  worked out again, so a list made ahead takes effect on the 1st, the moved-out limits pass with the
  months and an exception ends after its last day (and is cleared). Every deploy does the same.

## Decisions

- **Months on the list, not the move-out date.** The list is kept every month and already decides who
  lives here everywhere else. Levels count whole months, like the list.
- **Roles and superusers are residents.** Administrators need not live here.
- **Stored, not worked out per request.** The level is a column on the account: a request reads it
  with the account, the wiki and the admin lists can read it, and an exception is a field beside it.
  The rule still decides it, so the stored level is kept current from the same rule by signals, a
  nightly run and every migrate; a page that writes a whole month's list works it out once at the end.
- **Not Django groups and permissions.** They would replace only the table and the stored column:
  the gate and the notification filtering stay. The table would become database rows changed by data
  migrations rather than code, and Django would not keep an account in one level only.
- **Its own app (`access`).** The table and the URLconf marking (`access.policy`) know nothing about
  the alumneliste; whose level is what (`access.levels`) reads the list through the residents app.
- **One table.** Each level's access to each feature is one table in code, so a rule is a row or a
  column, not a check spread over the apps. A feature is a part of the URLconf; a page in no
  feature is for residents only, so a new page stays closed to other levels until it is put in one.
- **In-feature limits come later.** Separate channels in Opslagstavlen, Begivenheder and Den Hurtige
  for former residents are planned; for now a level either has a feature or not.
- **The wiki** has its own entry in the table (only the levels with near-full access), for the
  wiki's account sync to follow.
