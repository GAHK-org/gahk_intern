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
- **Its embedsgruppe's role**, read from the list in effect: Indstillingen, Inspektionen, AK,
  Ølkælderen, Regnskab, PR, Repperne, Viceværterne, Fotogruppen, Køkkengruppen. A list made ahead
  gives its roles when its month starts; nothing is copied from month to month.
- **Roles given by hand**, each from a first day through a last one (or until ended): administrator
  (Netværksgruppen), a role from the role page, the kitchen staff's, an exception the Indstilling
  made. Køkkenet is only ever given this way.
- **Administrator**: by hand, and every superuser. An administrator is Beboer whatever their months
  say.

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
  kitchen sees only what is addressed to it: the Køkkenet channel, and posts for the house and the
  kitchen (its own always are).
- **Indstillingen**: manages Optagelse, Alumneliste, Søg værelse, Hjemmeside and Adgang.
- **Køkkengruppen**: adds and ends the kitchen staff's accounts on Adgang.
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
- The role page (Site-admin → Roller) gives a role from today until removed, and shows each
  resident's embedsgruppe role from the list, which is changed on the list. Giving administrator
  opens the two-person approval, and only an administrator role gives Django admin's staff flag.

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
  who lives here everywhere else; AK, Regnskab, statistics and the room rounds all count its months.
  So living here stays monthly, and only roles carry dates.
- **An embedsgruppe's role is read from the list, not copied.** The list already says who is in
  which group each month, so a second record per month could only disagree with it.
- **Worked out when asked, not stored.** One query reads an account's months, groups and grants, and
  the same rule runs in SQL for audiences, so no signal or nightly job keeps a stored copy current.
- **Channels stay in code, with their audience beside them.** Notification settings are made from
  the channel list, so a channel is still added by a deploy; who reads it is a line of data.
- **Audiences are role sets, not lists of people.** They follow the alumneliste by themselves: a
  post for those living here stops reaching someone the month they move out.
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
