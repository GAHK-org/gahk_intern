# Access

Who may open which pages and do what on the intranet. One table decides it; the rest of the site
only asks for a permission by name.

## Features and grades

The site is cut into features: Den Hurtige, Opslagstavlen, Begivenheder, Reparationer, Fotoalbum,
GAHKFlix, AK, Ølkælder, Værelsestjek, Søg værelse, Alumneliste, profiles, Stamtræ, Statistik,
notifications, the wiki, and the group tools (Optagelse, Hjemmeside, Regnskab, Site-admin, Django
admin, Adgang). Every page is in exactly one feature.

Access to a feature is one of these grades, each including the ones before it:

| Grade | Means |
|---|---|
| view | open its pages, but send nothing |
| use | what a member of the house does there: post, book, add krydser |
| manage | what the group that runs it does: moderate, edit, approve |
| administer | what only administrators do inside it: merge albums, delete a page of gahk.dk |

A feature can also have a named power between use and manage for one group: Viceværterne may move a
repair (`reparationer.move`) without managing the board.

## Roles

An account holds a set of roles:

- **Its stage**, from the alumneliste (the month in effect is the newest published list that has
  started):

  | Stage | Who |
  |---|---|
  | Flytter ind | On a coming month's list, not the one in effect (also someone moving back in) |
  | Beboer | On the list in effect |
  | Fraflyttet < 1 måned | Last on the list the month before the one in effect |
  | Fraflyttet 1–3 måneder | Last on the list 2–3 months before |
  | Fraflyttet 3–12 måneder | Last on the list 4–12 months before |
  | Fraflyttet > 1 år | Last on the list more than 12 months before |

  Never on any list: no stage.
- **Each embedsgruppe held in the month in effect**: Indstillingen, Inspektionen, AK, Ølkælderen,
  Regnskab, PR, Repperne, Viceværterne, Fotogruppen, Køkkengruppen. Holding one makes someone
  Beboer, whatever their months say.
- **Administrator**: Netværksgruppen, and every superuser (who is also Beboer).
- **Grants**: a role given by hand, from a first day through a last one (or until ended).
  Køkkenet is only ever a grant.

An account with no role at all has no access: the front page says so and offers to log out.

## What each role gives

- **Beboer**: use of every feature except the group tools.
- **Flytter ind**: everything Beboer has except Fotoalbum and GAHKFlix.
- **Fraflyttet < 1 måned**: everything except Søg værelse.
- **Fraflyttet 1–3 måneder**: everything except Søg værelse, Værelsestjek and Reparationer. AK and
  Ølkælder are view only: their AK page and Ølkælder balance, but no new krydser.
- **Fraflyttet 3–12 måneder**: Den Hurtige, Opslagstavlen, Begivenheder and Fotoalbum, with their
  notifications and the @-picker.
- **Fraflyttet > 1 år**: Stamtræ.
- **Køkkenet**: use of Den Hurtige, Opslagstavlen, Reparationer and notifications. Inside them the
  kitchen sees only the Køkkenet channel and its own opslag.
- **Indstillingen**: manages Optagelse, Alumneliste, Søg værelse, Hjemmeside and Adgang.
- **Køkkengruppen**: adds and ends the kitchen staff's accounts on Adgang.
- **Inspektionen**: manages Den Hurtige, Opslagstavlen, Reparationer and Hjemmeside.
- **AK**: manages AK and Værelsestjek.
- **Ølkælderen**: manages Ølkælder. **Regnskab**: manages Regnskab. **PR**: manages Hjemmeside.
- **Repperne**: manage Reparationer. **Viceværterne**: may move repairs.
- **Fotogruppen**: manages Fotoalbum and GAHKFlix.
- **Administrator**: administers everything.

Someone with several roles gets the highest grade any of them gives. No role takes anything away.

## Grants and the kitchen staff

- Each of the kitchen staff has an account of their own, made on the Adgang page by Køkkengruppen or
  the Indstilling: name and e-mail, and the account gets a mail to set its password. Ending their
  access there ends the grant; the account stays, with no access.
- The old shared kitchen login became a grant when this shipped, and keeps working until somebody
  ends it on Adgang.
- The Indstilling gives any other role by hand on a resident's edit page, with a reason and a
  period: someone who moved out but finishes their group's accounts, a guest. Administrator cannot
  be given this way. The edit page shows the roles the account has now.
- Ending a grant makes it hold through yesterday; one that has not begun is removed.

## Behaviour

- A page of a feature the account lacks answers 403, recorded in the audit trail. With view only,
  the page opens but any form post answers 403.
- Nobody logged in is sent to the login page, except for the account pages and the public ones
  (install page, service worker, calendar feeds, the Ølkælder till, the photo import script).
- A page's poll or live re-draw gets an empty answer instead of a login page or a 403.
- The front page sends an account without the dashboard to its first feature: Den Hurtige, then
  Opslagstavlen, Begivenheder, Fotoalbum, Stamtræ. The kitchen lands on its channel.
- The sidebar, the search box and live updates offer only what the account's roles open.
- Nobody is notified about a page they cannot open, whatever the notification's audience.
- A calendar feed is empty once its owner has no Begivenheder.
- Everything follows the date: a list made ahead takes effect on the 1st, the moved-out stages
  pass with the months and a grant ends after its last day, with nothing to run.
- Administrators can see the site as any role, any stage, as never listed or as Køkkenet ("Se side
  som").

## Decisions

- **Months on the list, not the move-out date.** The list is kept every month and already decides
  who lives here everywhere else.
- **Worked out when asked, not stored.** One query reads an account's months, groups and grants, and
  the same rule runs in SQL for audiences, so no signal or nightly job keeps a stored copy current.
- **Grants add, never take away.** An exception the Indstilling makes is one more role; nobody's
  access is cut by hand.

- **Permissions, not roles, in the code.** A view asks "may this request manage Ølkælder?", never
  "is this person in Ølkælderen?". Who may is a row in the table, so changing it is one edit.
- **One table in code, not database rows.** The rule is reviewed and tested like code and the same
  in every environment. People, months and roles are in the database.
- **A page in no feature is closed to everyone.** A test fails while one exists, so a new page is
  only opened by someone deciding whose it is.
- **Not Django's groups and permissions.** They cannot follow the monthly roles, and a superuser
  passes every Django check, so "Se side som" could not show an administrator the site as a
  resident. Django admin keeps its own permissions.
- **Group tools rely on the gate.** Their features are given only to their groups, so their views
  need no check of their own; a later read-only role for one of them needs no code change.
