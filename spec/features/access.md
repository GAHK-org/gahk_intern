# Access

Who may open which pages and do what on the intranet. One table decides it; the rest of the site
only asks for a permission by name.

## Features and grades

The site is cut into features: Den Hurtige, Opslagstavlen, Begivenheder, Reparationer, Fotoalbum,
GAHKFlix, AK, Ølkælder, Værelsestjek, Søg værelse, Alumneliste, profiles, Stamtræ, Statistik,
notifications, the wiki, and the group tools (Optagelse, Hjemmeside, Regnskab, Site-admin, Django
admin). Every page is in exactly one feature.

Access to a feature is one of these grades, each including the ones before it:

| Grade | Means |
|---|---|
| view | open its pages, but send nothing |
| use | what a member of the house does there: post, book, add krydser |
| manage | what the group that runs it does: moderate, edit, approve |
| administer | what only administrators do inside it: merge albums, delete a page of gahk.dk |

A feature can also have a named power between use and manage for one group: Viceværterne may move a
repair (`access.reparationer_move`) without managing the board.

## Permissions

Each feature and action is an ordinary Django permission, `access.<feature>_<action>`
(`access.oelkaelder_manage`), asked the standard way: `has_perm`, `permission_required`,
`perms.access.…` in a template. Grades are spelled out: a role that manages a feature holds its view,
use and manage permissions.

## Roles

Each role is a Django group named by its code, holding exactly the permissions the table gives it.
An account holds a set of roles:

- **Beboer**: every account except the kitchen staff's.
- **Each embedsgruppe held in the month in effect**: Indstillingen, Inspektionen, AK, Ølkælderen,
  Regnskab, PR, Repperne, Viceværterne, Fotogruppen, Køkkengruppen.
- **Administrator**: Netværksgruppen, and every superuser.
- **Køkkenet**: the kitchen staff, and nothing else.

## What each role gives

- **Beboer**: use of every feature except the group tools.
- **Køkkenet**: use of Den Hurtige, Opslagstavlen, Reparationer and notifications. Inside them the
  kitchen sees only the Køkkenet channel and its own opslag.
- **Indstillingen**: manages Optagelse, Alumneliste, Søg værelse and Hjemmeside.
- **Inspektionen**: manages Den Hurtige, Opslagstavlen, Reparationer and Hjemmeside.
- **AK**: manages AK and Værelsestjek.
- **Ølkælderen**: manages Ølkælder. **Regnskab**: manages Regnskab. **PR**: manages Hjemmeside.
- **Repperne**: manage Reparationer. **Viceværterne**: may move repairs.
- **Fotogruppen**: manages Fotoalbum and GAHKFlix.
- **Administrator**: administers everything.

Someone with several roles gets the highest grade any of them gives. No role takes anything away.

## Behaviour

- A page of a feature the account lacks answers 403, recorded in the audit trail. With view only,
  the page opens but any form post answers 403.
- Nobody logged in is sent to the login page, except for the account pages and the public ones
  (install page, service worker, calendar feeds, the Ølkælder till, the photo import script).
- A page's poll or live re-draw gets an empty answer instead of a login page or a 403.
- The front page sends an account without the dashboard to its first feature: Den Hurtige, then
  Opslagstavlen, Begivenheder, Fotoalbum, Stamtræ. The kitchen lands on its channel.
- The sidebar, the search box and live updates offer only what the account's roles open.
- Administrators can see the site as any role, as plain Beboer or as Køkkenet ("Se side som").
- An account's groups follow the facts: synced when a role is given or taken back, an account is
  saved or a list makes a newer month the one in effect, just after midnight (a list made ahead takes
  effect on the 1st), and after every migrate, which also writes each group's permissions.

## Decisions

- **Permissions, not roles, in the code.** A view asks "may this request manage Ølkælder?", never
  "is this person in Ølkælderen?". Who may is a row in the table, so changing it is one edit.
- **One table in code, not database rows.** The rule is reviewed and tested like code and the same
  in every environment. People, months and roles are in the database.
- **A page in no feature is closed to everyone.** A test fails while one exists, so a new page is
  only opened by someone deciding whose it is.
- **Django's groups and permissions, kept in step by a sync.** Roles come from facts (the month's
  list, the kitchen flag), so groups are written from them rather than edited by hand; a nightly and
  on-change sync is simpler than a permission system of our own. The table stays in code and is
  written into the groups at every migrate.
- **"Se side som" answers from the previewed roles.** Account.has_perm reads the preview's
  permissions when one is set, so a superuser can see the site as a resident; Django admin and the
  editor hint ask the account's own permissions.
- **Group tools rely on the gate.** Their features are given only to their groups, so their views
  need no check of their own; a later read-only role for one of them needs no code change.
