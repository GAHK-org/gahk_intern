# Feature: Worker-jobs — the background jobs dashboard

Administrators' overview of everything the background workers do: what waits, what runs, what
ran and how it went, and what is scheduled next. Under Administration → Worker-jobs.

## Who

- Administrators only (the effective role, so a preview as another role loses access).

## The top of the page (refreshes every 10 seconds while the tab is in view)

- **Tiles:** jobs in the queue (and how long the oldest has waited), jobs running now, jobs failed
  and jobs completed in the last 24 hours (with the average run time). Each tile links to its list.
- **Kører nu:** every running job — what it works on, which worker, how long it waited, how long it
  has run, and how much of its time limit that is. A job that reports its progress (a video's
  conversion, a GAHKFlix film, an album ZIP imported or built for download, an album merge) also
  shows how far it has got and what it is doing.
- A job still marked as running past its time limit is not running: its worker died under it. Such
  jobs are counted separately, in red, with a link to them, until the hourly cleanup marks them as
  failed (ten minutes past their limit at the earliest).
- A photo-album media job names the file in full, extension included, with its size and album. A
  GAHKFlix encode names the uploaded file with its size and the video's title; a frame redraw names
  the video.
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
- Its peak memory: the worker's own, and that of the largest program it ran (ffmpeg), with the
  program's name. Measured as it runs, so a job killed for running out of memory shows what it had
  reached. Only measured on the server; "–" where it was not.
- Its sub-jobs: how many in each status, then the first 200 — those still queued included.
- Its result, its traceback, and what it logged. While it runs, the page refreshes itself every 10
  seconds (while the tab is in view), showing its progress and what it has logged so far, at most
  10 seconds old; it stops refreshing when the job ends. A job killed outright keeps what it logged
  up to then. A job that succeeded shows 100 %; one that failed shows how far it got and what it was
  doing.

## Stuck jobs

- A job that reports its progress must get at least 10 percentage points further every hour, or
  finish within the hour when less than that is left, counted from its first report (an import's
  download of its ZIP comes before that and is not held against it). One that does not is stopped,
  as at its time limit, with the progress it made in the last hour in its log; a video's conversion
  counts it as a failed try. Its page says so while it runs.
- A job at 100 % that has not finished is left to its time limit: what it does after its last
  report (an import's commit, a merge's moves) is not measured, and stopping it there would throw
  away the whole job.
- So such jobs may run for up to twelve hours: a video conversion, an album ZIP (imported, built
  for download) or merge, and a GAHKFlix film. Jobs that report no progress keep their own limits.

## Times

- All times are shown in Danish local time, whichever clock stored them.
- Every job's sent time is shown: it is stamped on the job when it is sent and kept with the job.
  Jobs from before this was recorded show "–".

## Retention

- Successful jobs are kept for 35 days, so the monthly summary can count a whole month. Any other
  job is kept until there are more than 1000 of the same type and status; then the oldest beyond the
  1000 go once they are 60 days old.

## Audit trail

- A job's own record here, and the workers' heartbeats, are not in the audit trail (audit-trail.md):
  they are the worker's bookkeeping. What a job changes is, as done by nobody, under the job's own
  name.

## Demo data

- The demo seed fills the page: jobs running (one with progress), one whose worker died, failed
  ones (an error, the watchdog, a worker out of memory), an import with its sub-jobs, and the
  routine Beat jobs over the last days, with peak memory on all but the oldest. The queue stays
  empty: the local worker would take and run a seeded message at once.

## Decisions

- **The worker keeps its own record of every job**, not only of the jobs that log something. The
  broker's copy of a message — where the sent time, the parent job and the arguments live — is
  swept after a day, and Celery's own result row has none of them.
- **Only the top refreshes itself.** The history stays as loaded, so it does not move under somebody
  reading it.
- **A stuck job is told by its progress, not its age.** A 5 GB video can honestly take more than
  four hours, so a fixed limit short enough to free the worker from a stuck job soon would also stop
  the honest ones. The worker itself watches the pace, since nothing outside it can stop a job.
- **Sub-jobs are hidden by default.** A photo import queues one job per picture; shown, they bury
  the jobs anybody started.
