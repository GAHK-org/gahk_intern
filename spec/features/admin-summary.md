# Feature: Monthly admin summary — the administrators' mail on the 1st

A plain-text mail on the 1st of every month, at 09:05, about the month that just ended and about who
holds access now.

## Who gets it

- Everyone with Django-admin access: superusers, and administrators of the current month whose
  access a second administrator has accepted. One mail, all of them as recipients.
- An administrator still waiting for acceptance does not get it; nor does any other role.
- Nobody with access means no mail.

## Background jobs (the month that just ended)

- How many jobs ran: completed and failed, by Danish local month, each job counted in the month it
  ended. Cancelled jobs are counted on their own line when there were any.
- The failed jobs by type, most first, with a link to the failures on the worker-jobs page.
- Sub-jobs (one per picture in a photo import) count like any other job.

## Access (as the mail goes out)

- Superusers.
- Administrators of the current month, each with where their Django-admin access stands: accepted,
  awaiting acceptance, denied, or superuser.
- The other roles of the current month, with their holders. Roles nobody holds are left out.
- The Django-admin grants decided during the month just ended: when, whom, accepted or denied, and
  by whom.
- A link to the admin-access page.

## Decisions

- **It replaced the twice-daily test mail.** That mail only proved Beat and the worker were alive in
  production; a summary arriving on the 1st proves the same and is worth reading.
- **Access is shown as it is now, jobs as they were last month.** Who can get in is the question
  that matters on the day the mail is read; a list of last month's holders would answer an older one.
- **Successful jobs are kept 35 days, not 30.** The nightly cleanup on the 1st runs before the mail,
  and at 30 days the first day of a 31-day month was already gone.
- **In the day, not with the night's sweeps:** people read it.
