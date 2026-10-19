# Worker-jobs

The administrators' dashboard of everything the background workers do: what waits, what runs, what ran and how it went, and what is scheduled next.

## Who

- Administrators only (the effective role, so a preview as another role loses access), from the
  site-admin front page.

## The top of the page (refreshes every 10 seconds while the tab is in view)

- **Tiles:** jobs in the queue (and how long the oldest has waited), jobs running now, jobs failed
  and jobs completed in the last 24 hours (with the average run time). Each tile links to its list.
- **Kører nu:** every running job — what it works on, which worker, how long it waited, how long it
  has run, and how much of its time limit that is.
- A job still marked as running past its time limit is not running: its worker died under it. Such
  jobs are counted separately, in red, with a link to them.
- **I kø:** waiting jobs grouped by queue and job type, with the count of each and the first five
  in the order they will be taken, each with its sent time.

## The rest of the page

- **Activity:** finished jobs per hour over the last 24 hours, completed and failed stacked, with
  the numbers available as a table.
- **Opgavetyper:** every job type with its count in the queue, running, completed, failed and other,
  how its latest run ended and when, and its average run time. A type links to its history; a
  failure count links to its failures.
- **Historik:** every job kept, newest first, filterable by type, status, period (24 hours, 7 days,
  30 days, everything kept) and with or without sub-jobs, sortable by newest, oldest or longest run.
  The status filter shows how many jobs each status has under the other filters. 25 per page.
- **Planlagte opgaver:** every active schedule — its plan in words where it has a usual shape, when
  it last sent its job, when it will next, and how its job last ended. A schedule well past its next
  time is marked "Forsinket": Beat is probably not running.

## A job's page

- Sent, started and finished on one line, with the wait and the run time between them; a running
  job's share of its time limit.
- Queue, worker, retries, arguments, parent and root job (linked), ETA, expiry, origin.
- Its sub-jobs: how many in each status, then the first 200 — those still queued included.
- Its result, its traceback, and what it logged.

## Times

- All times are shown in Danish local time, whichever clock stored them.
- Every job's sent time is shown: it is stamped on the job when it is sent and kept with the job.
  Jobs from before this was recorded show "–".

## Retention

- Successful jobs are kept for 30 days. Any other job is kept until there are more than 1000 of the
  same type and status; then the oldest beyond the 1000 go once they are 60 days old.

## Decisions

- **The worker keeps its own record of every job**, not only of the jobs that log something. The
  broker's copy of a message — where the sent time, the parent job and the arguments live — is
  swept after a day, and Celery's own result row has none of them.
- **Only the top refreshes itself.** The history stays as loaded, so it does not move under somebody
  reading it.
- **Unsuccessful jobs are kept much longer than successful ones**, because those are the ones
  somebody may need to investigate.
- **Sub-jobs are hidden by default.** A photo import queues one job per picture; shown, they bury
  the jobs anybody started.
