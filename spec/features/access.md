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

- **A stage**, from the months its resident is on the alumneliste. The month in effect is the newest
  published list that has started.

  | Stage | When |
  |---|---|
  | Flytter ind | on a coming list only |
  | Beboer | on the list in effect |
  | Fraflyttet for under 1 måned siden | the first month off the list |
  | Fraflyttet for 1–3 måneder siden | months 2–3 off the list |
  | Fraflyttet for 3–12 måneder siden | months 4–12 off the list |
  | Fraflyttet for over et år siden | after that |

  An account never on a list has no stage.
- **Its embedsgruppe's role** on the list in effect: Indstillingen, Inspektionen, AK, Ølkælderen,
  Regnskab, PR, Repperne, Viceværterne, Fotogruppen, Køkkengruppen. Changed on the list, nowhere
  else.
- **Every role given by hand** that holds today: a grant with a first day, a reason and an optional
  last day. Administrator is always given this way; the others when the list says nothing or the
  wrong thing (someone finishing their group's accounts after moving out).
- **Administrator and Beboer** for every superuser.
- **Køkkenet**: the kitchen staff, by a grant. Each has an account of their own and a person, but no
  resident: they do not live here.

An account with none of these has no access at all; the front page says so.

## What each role gives

- **Beboer**: use of every feature except the group tools.
- **Flytter ind**: as Beboer, but no Fotoalbum or GAHKFlix yet.
- **Under 1 month out**: as Beboer, but no Søg værelse.
- **1–3 months out**: as that, without Værelsestjek and Reparationer; AK and Ølkælder to look at,
  but no more krydser.
- **3–12 months out**: Den Hurtige, Opslagstavlen, Begivenheder, Fotoalbum, notifications and
  mentions.
- **Over a year out**: Stamtræ.
- **Køkkenet**: use of Den Hurtige, Opslagstavlen, Reparationer and notifications. Inside them the
  kitchen sees only what is addressed to it: the Køkkenet channel, and posts for the house and the
  kitchen (its own always are).
- **Køkkengruppen**: adds and ends the kitchen staff's accounts on the Adgang page.
- **Indstillingen**: manages Optagelse, Alumneliste, Søg værelse and Hjemmeside, and the Adgang
  page, where it sees and ends roles given by hand (not administrator).
- **Inspektionen**: manages Den Hurtige, Opslagstavlen, Reparationer and Hjemmeside.
- **AK**: manages AK and Værelsestjek.
- **Ølkælderen**: manages Ølkælder. **Regnskab**: manages Regnskab. **PR**: manages Hjemmeside.
- **Repperne**: manage Reparationer. **Viceværterne**: may move repairs.
- **Fotogruppen**: manages Fotoalbum and GAHKFlix.
- **Administrator**: administers everything.

Someone with several roles gets the highest grade any of them gives. No role takes anything away.

## Audiences

A Den Hurtige channel, an opslag and an open event each say who they are for: a set of roles.
Someone sees one when any of their roles is in it, and the feature is open to them as well. The
audience narrows who; the roles still decide what.

| Audience | Roles |
|---|---|
| Hele huset | every stage (living here, moving in, moved out) |
| Kun dem, der bor her nu | Flytter ind, Beboer |
| Kun tidligere beboere | the moved-out stages |
| Hele huset og køkkenet | every stage and Køkkenet |

- Every channel is for the whole house except Køkkenet, which is for the house and the kitchen. A
  channel for former residents or for one embedsgruppe is a channel with that audience.
- A post or an open event is for the whole house unless its author picks another audience they are
  in themselves. One that is not for the whole house shows its audience. The author always sees
  their own.
- Invite-only events are their invitees', as before.
- New posts, events and channel messages notify the audience only, and the notification settings
  list only the channels and features the account can be sent.
- Administrators are in every audience: they moderate what they could not otherwise see.

## Behaviour

- A page of a feature the account lacks answers 403, recorded in the audit trail. With view only,
  the page opens but any form post answers 403.
- Nobody logged in is sent to the login page, except for the account pages and the public ones
  (install page, service worker, calendar feeds, the Ølkælder till, the photo import script).
- A page's poll or live re-draw gets an empty answer instead of a login page or a 403.
- The front page sends an account without the dashboard to its first feature: Den Hurtige, then
  Opslagstavlen, Begivenheder, Fotoalbum, Stamtræ. The kitchen lands on its channel.
- The sidebar, the search box and live updates offer only what the account's roles open.
- Administrators can see the site as any role, as plain Beboer, as Køkkenet, as any stage or as
  someone never on a list ("Se side som").
- Nobody is notified about a page they cannot open. A calendar feed is empty once its owner has no
  Begivenheder.
- The role page (/admin/roles) gives a role from today until removed; removing it ends it
  yesterday. A role from the list shows there but is changed on the list.
- The Adgang page (/access/): Køkkengruppen and Indstillingen add one of the kitchen staff (a person,
  an account, the kitchen role from today, and a mail with a link to set the password) and end their
  access. The old shared kitchen login became a grant and works until someone ends it there.
- Django admin lets in superusers and holders of an administrator grant, once a second
  administrator has approved it.
- An account's groups follow the facts: synced when a grant is given or ended, an account is saved
  or its resident's months on the list change, just after midnight (a list made ahead takes
  effect on the 1st, the months pass, grants start and end), and after every migrate, which also
  writes each group's permissions.

## The access page

- Every account has "Min adgang" in its account menu, the kitchen staff's and one with no access
  too. The Indstilling opens anyone's from their edit page, and gives a role by hand there: an
  embedsgruppe's role with a reason, a first day and an optional last day (not administrator, not
  the kitchen's, not a stage).
- It lists each role with where it comes from (the list and month, the embedsgruppe, who gave it,
  why and until when), each part of the site with its grade and the roles that give it, and what
  will change: the list made ahead, a grant ending, and for someone not on the newest list the
  moved-out stages as they come. Nothing is projected past the newest list for someone on it.
- An administrator can see the site with one person's roles from their page ("Se siden med Annas
  roller"). It shows what the roles open, not that person's own messages and posts.

## Decisions

- **Channels stay in code, with their audience beside them.** Notification settings are made from
  the channel list, so a channel is still added by a deploy; who reads it is a line of data.
- **Audiences are role sets, not lists of people.** They follow the alumneliste by themselves: a
  post for those living here stops reaching someone the month they move out.
- **An audience is stored as its groups' names, not a link to each group.** No join table per post,
  and the database fills the default; the names are the stable role codes.

- **Permissions, not roles, in the code.** A view asks "may this request manage Ølkælder?", never
  "is this person in Ølkælderen?". Who may is a row in the table, so changing it is one edit.
- **One table in code, not database rows.** The rule is reviewed and tested like code and the same
  in every environment. People, months and roles are in the database.
- **A page in no feature is closed to everyone.** A test fails while one exists, so a new page is
  only opened by someone deciding whose it is.
- **Django's groups and permissions, kept in step by a sync.** Roles come from facts (the month's
  list, the grants), so groups are written from them rather than edited by hand; a nightly and
  on-change sync is simpler than a permission system of our own. The table stays in code and is
  written into the groups at every migrate.
- **"Se side som" answers from the previewed roles.** Account.has_perm reads the preview's
  permissions when one is set, so a superuser can see the site as a resident; Django admin and the
  editor hint ask the account's own permissions.
- **Group tools rely on the gate.** Their features are given only to their groups, so their views
  need no check of their own; a later read-only role for one of them needs no code change.
- **Stages from months, not move-out dates.** The list is kept every month and already decides who
  lives here everywhere else; dates on a resident are often missing or late.
- **An embedsgruppe's role is read from the list; every other role has dates.** The monthly role
  rows were mostly copies of the list, and administrator had to be carried forward each month.
- **A grant is on an account, not a person.** A kiosk or an agent may need a role and is no person.
