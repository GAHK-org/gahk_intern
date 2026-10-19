# Administration

The area where Netværksgruppen (the administrator role) assigns roles, approves Django-admin access, previews the site as other roles, and oversees notifications and background jobs.

## Access
- The administration area (`/admin/`) is open only to residents who hold the administrator role in the active month, and to superusers.
- The administrator role includes every other role on the site.
- Roles belong to a month. A resident's access comes from the roles they hold in the active month, which is the newest monthly resident list that has already started. Access changes when a new month's list takes effect, without anyone logging in again.
- The front page links to roles, admin access, role preview, notifications, worker jobs, applications, statistics and Django admin. It also lists any administrators who are waiting for approval.

## Roles
- The roles screen lists every resident on the active month's list, with that month's roles.
- An administrator can add or remove any role for a resident on that list, for the active month only.
- Roles tied to an embedsgruppe are also set automatically from the monthly resident list. Saving a resident on that list resets their embedsgruppe roles to match their group. Administrator is not tied to an embedsgruppe and carries over from month to month on its own.
- A resident who has ever held a role can sign in to Django admin. They only see what their role allows there, which is usually nothing.

## Django-admin access (two-person rule)
- A new administrator gets full Django-admin permissions only after a second person approves them.
- When a resident first gets the administrator role, by any route, an approval request opens and every possible approver is emailed.
- Approvers are superusers and administrators whose own access has already been approved.
- Neither the new administrator nor the person who gave them the role can decide the request.
- The admin-access page lists pending requests with approve and refuse buttons where the viewer may decide, plus the last 20 decisions.
- An approved administrator keeps access as long as they hold the role. Holding it again in a later month does not need a new approval.
- A resident who loses the administrator role in every month loses their approval. If they get the role again, they need to be approved again.
- A refused administrator keeps the role without Django-admin permissions. Granting them administrator again on the roles screen reopens the request and emails the approvers; the monthly list carrying the role forward does not.
- The site's content sections in Django admin are open by role to Indstillingen, Inspektionen, PR and administrators, independently of this approval.
- Django admin's resident form cannot make anyone superuser or give them groups or permissions, and does not show the password, only whether one is set. Superuser status is granted on the server.
- Only a superuser can edit or delete a superuser in Django admin.

## View site as role
- An administrator can view the members area as if they held one chosen role, or no role at all ("beboer"). Menus and access change to match, but they stay logged in as themselves.
- A banner on every page shows the active preview, with a button to end it. Logging out also ends it.
- The preview switcher stays available while previewing, so an administrator previewing as "beboer" can always switch back.
- The preview only affects the members area. Django admin and site-content editing keep the administrator's real access.

## Notifications and worker jobs
- The notifications page (push devices, statistics and broadcasts to the house) is described in notifications.md.
- The worker-jobs dashboard (queue, running jobs, history, schedules and each job's page) is described in worker-jobs.md.

## Development
- Developers can set a simulated date to test month rollover. This is not available in production.

## Decisions
- Django-admin rights are checked against the current month's role on every request, not stored on the account. Roles expire when the month rolls over, and nothing runs at that moment that could revoke a stored right.
- The approval request opens whenever the administrator role is created, by any route. The role editor, the monthly list and the søg-værelse roll-forward all create roles, and a check that only one of them applied would not protect anything.
- An administrator is approved once, not every month. Approving the same people twelve times a year would teach the group to click accept without reading.
- The person who grants the role cannot approve it. Otherwise one administrator could grant and accept in two clicks. A majority of the group can still admit a newcomer between them.
- Only a deliberate re-grant on the roles screen reopens a refused request. The monthly roll-forward also creates the role, and must not turn a refusal into a new request every month.
- The resident form hides superuser and permissions, and only a superuser can edit a superuser, because an approved administrator holds every permission and could otherwise make themselves a lasting superuser, or take one over by changing its email and resetting the password.
