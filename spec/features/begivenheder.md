# Feature: Begivenheder — events and tilmelding

**Unnumbered on purpose.** `F-001`–`F-015` are legacy-parity documents: each carries a "Source
file(s)" header pointing at a PHP controller, and `99-index.md` states they cover every live
controller in the old app. Begivenheder is greenfield, so an `F-016` would make every existing
`F-0NN` citation in the code ambiguous. Same reasoning as `opslagstavle.md`, and this file exists
for the same reason: the **rejected alternatives** at the end are worth writing down somewhere, and
a module docstring is the wrong place for "here is what we decided not to do".

## What it is, and what it replaces

The kollegium plans parties, fællesspisninger and excursions in a **Facebook group**, and signs
people up on **a paper sheet in the kitchen** — opslagstavlen's own demo fixture still reads
*"Tilmelding på sedlen i køkkenet"*. This replaces both: an event has a time, a place, a
description, an optional picture, and a guest list that people answer **ja** or **nej** to. At
`/intern/begivenheder/`.

**Three things are called "begivenhed" in this codebase, and the collision is deliberate rather
than accidental.** They divide like this, and each model's docstring says so:

| | What it is |
| --- | --- |
| `cms.Event` (public `/begivenheder/`) | Marketing copy on the public site. A `DateField`, no time, no attendance, developer-edited. Import it as `CmsEvent` anywhere both appear. |
| `opslagstavle.Category.BEGIVENHED` | An announcement *about* an event, with comments and reactions. |
| `events.Event` (this) | The thing with a time and a guest list. |

Announce on opslagstavlen; sign up here. Doing both is fine.

**What it is not:** Den Hurtige (minutes to hours, hard-deleted on expiry) or opslagstavlen (weeks
to forever — the board is the kollegium's archive). The dividing question is whether the thing has *state* — a list that
fills up, a deadline that closes it, an audience that may be a subset of the house. A noticeboard
post with a date column cannot express "fuldt".

## Access

**Gated to a trial group** — `ACCESS_ROLES = (administrator, inspektion)` in `events/access.py`,
the same pair opslagstavlen uses. "Netværk" is spelled `administrator` because the network group is
not an embedsgruppe with a `Workgroup` row and so has never had a role of its own (see
`residents.models.WORKGROUP_ROLE`, where `administrator` is deliberately absent). Setting
`ACCESS_ROLES = None` opens every view, the sidebar entry, the "Under test" chip and the push
audience together.

One thing the trial cannot reach, worth knowing before the gate comes off: the **calendar feed**
only becomes testable once several people have real answers in it, so half a dozen testers can
exercise creating, answering, the venteliste, the deadline, invites and both `.ics` paths, but not
"does a month of real events look right in Google Calendar six weeks from now".

The feed endpoint itself sits **outside** the gate deliberately — somebody not in the trial group
simply has no answers, so their feed is a valid empty VCALENDAR rather than a 403 a calendar client
cannot explain.

Per-event rights:

| Action | Who |
| --- | --- |
| See an **åbent** event | every resident |
| See a **kun inviterede** event | the organiser, co-organisers, and invitees — nobody else |
| Answer ja/nej | anyone who can see it, until answers lock |
| Edit, aflys, invite, uninvite | the organiser **and** co-organisers ("hosts") |
| Add or remove co-organisers | the **original organiser** only |
| Delete outright | a host, and only while nobody has said ja |

**Two different refusals, and the split is the rule:**

```
404  you may not know it exists       (a private event you were not invited to)
403  you know it exists, but not this (someone else's event you cannot edit)
```

A private event must be **absent**, not forbidden. A 403 confirms that an event with that id
exists, which is precisely the fact the event is hiding.

**Moderators get no special read access.** Inspektionen moderate opslagstavlen and Den Hurtige, and
deliberately not this: a private event's whole promise is that a non-invitee cannot see it, and
"except Inspektionen" makes that promise false in exactly the case anyone would care about. A
reported private event is a superuser job in the Django admin, which has always seen every table.

`access.visible_to(resident)` is the chokepoint every view, the calendar and both `.ics` endpoints
start from. It is enforced structurally: `events/views.py` contains **zero** occurrences of
`Event.objects` outside the purge, checked by a test that parses the module, and a second test walks
`events.urls.urlpatterns` so a pk-taking route added without a leak test fails CI rather than
production.

## Routes

Mounted at `/intern/begivenheder/`, namespaced `events:`. Fixed segments come **before** `<int:pk>`
so they are never read as an id.

```
""                       index          GET       upcoming only — there is no archive
"opret"                  create         GET/POST
"abonner"                save_subscription  POST  push consent for the `events` topic
"kalender"               calendar       GET       month grid; ?maaned=, ?dag=
"kalender/abonnement"    feed_settings  GET       shows the subscribe URL
"kalender/nyt-link"      rotate_token   POST      invalidates the old feed URL
"<int:pk>"               detail
"<int:pk>/rediger"       edit           GET/POST
"<int:pk>/slet"          delete         POST
"<int:pk>/aflys"         cancel         POST
"<int:pk>/svar"          answer         POST      htmx -> returns the RSVP partial only
"<int:pk>/ics"           event_ics      GET       single-event download
```

One route lives **outside** `/intern/`, in `config/urls.py`:

```
"/kalender/<token>.ics"  events_feed    GET       NO auth decorator
```

Mounted at the site root on purpose. If `/intern/` is ever gated at the proxy, or someone adds
Django's `LoginRequiredMiddleware`, a feed under it dies **silently** — and silence is the entire
failure mode of a calendar subscription. The token is the credential; see below.

## Data model

```
Event         organiser FK, co_organisers M2M, title, description (Markdown), image, location,
              starts_at, ends_at?, visibility, capacity?, rsvp_deadline_at?,
              sequence, created_at, edited_at?, cancelled_at?, reminder_sent_at?
EventInvite   (event, resident, invited_by?, invited_at)      unique (event, resident)
Rsvp          (event, resident, answer, answered_at, promoted_at?, created_at)
                                                              unique (event, resident)
CalendarFeedToken   OneToOne(resident), token, created_at, rotated_at?, last_used_at?
```

Shape choices, in short — each is argued at length where it lives:

- **`image` is one `FileField`**, not opslagstavlen's `NoticeImage` table. That table exists because
  a Markdown body can reference many images uploaded before the post exists. One event, one picture.
- **`capacity` and `rsvp_deadline_at` are nullable, and NULL means "no limit"** — no companion
  boolean. One column answers "is there a cap" and "what is it".
- **Invited and replied are two tables.** An invite is "you may see this"; an RSVP is "here is my
  answer". Collapsing them into one row with a nullable answer makes "invited but hasn't replied"
  and "not invited" the same absence — and the deadline reminder is exactly a query for the first.
- **`co_organisers` is a plain M2M while `EventInvite` is explicit.** An invite carries payload that
  gets queried (who, when); a co-organiser link carries none.
- **`edited_at` and `answered_at` are set explicitly, never `auto_now`.** Promotions, reminder
  claims and sequence bumps all `save()` these rows. For `edited_at` that would stamp "Redigeret" on
  someone's behalf; for `answered_at`, which is the **waitlist ordering key**, it would silently
  move the person at the head of the queue to the back of it.
- **`sequence` is a stored integer** (RFC 5545), bumped only when the time, title, place or status
  changes. A typo fix in the description must not re-notify sixty phones.
- **`ical_uid` is derived from the pk** (`begivenhed-{pk}@gahk.dk`), never stored and never
  host-derived: the same event must present the same UID in the download and in every feed, forever.
- **`CalendarFeedToken` is its own model, not a column on `Resident`.** `Resident` is the auth
  principal, loaded by every request and dumped by the alumneliste export; a secret there is one a
  stray `.values()` can leak from code that has no idea it is handling a credential. Revocation is
  a row you delete rather than a column you remember to null.

## Tilmelding, capacity and the venteliste

Answers are **ja or nej**. No "måske": a maybe cannot hold a seat and cannot release one, so every
downstream question would grow a third branch that always resolves to "treat as nej".

With a `capacity` set, **seating is derived, not stored**: the first `capacity` ja rows ordered by
`(answered_at, pk)` are in, the rest are the venteliste, and queue position is the index in that
set. Nothing is written when someone is promoted except the cosmetic `promoted_at`.

That is only sound because of one guarantee, which is why it has a test of its own:

> **Nobody can change their answer after the deadline.** `services.set_answer` refuses *every*
> answer once `answers_locked` is true — drop-outs included.

So the set of ja rows, and the ordering over it, is already frozen when the deadline passes.
Deriving needs no state of its own to be final; it inherits finality from the rows it reads. An
earlier draft stored a `seat_taken_at` and took `SELECT … FOR UPDATE` on the parent Event row,
arguing that a derived seat could never be made final. That was wrong, and the column, both
CheckConstraints and the lock went with it. What it buys: there is no "at most `capacity` seats"
invariant, so two people racing for the last seat have nothing to violate — both get a row and the
ordering decides — and the code behaves identically on SQLite, where `select_for_update` is a silent
no-op.

**The trade, stated plainly:** after the deadline someone who genuinely cannot come has no way to
say so, and a host cannot free their seat. That is what "endelig" means here, and it is the price of
a list that does not shuffle under people after they were told they were in.

Raising the capacity seats more people immediately, because the derivation simply reads further down
the same ordering. **Lowering it below the seated count is refused by the form**, naming the number:
silently demoting someone who was told they had a seat is not something a form does.

## Deadline

`rsvp_deadline_at` is optional. **With none set, answers close when the event starts** — a ja
arriving after the party keeps mutating an attendee list and every subscriber's `.ics`. That makes
`answers_locked` *total*: there is no state in which this feature is open forever.

The boundary is `<=` — at exactly the deadline instant, closed. Danish "svarfrist kl. 18.00" reads
as *senest* 18.00, and there is a test on each side of a microsecond.

The check lives in `services.set_answer`, which raises `RsvpClosed`, not in a view or a form: the
same transition is reachable from the answer view, the host's controls, `demo.py` and the admin, and
a view-level check protects one of them. After the deadline the form is **removed from the DOM, not
disabled** — a disabled button with a live POST target is a lie, and the test asserts the action URL
is absent rather than that a `disabled` attribute is present.

A host reopens by moving or clearing the deadline. That is an edit, not a separate flag: a reopen
boolean would be a second source of truth for one question, and changing the date the page shows is
the honest way to tell people they can answer again.

Everything reads `core.clock.current_datetime()`, never `timezone.now()` directly, so the dev clock
can fast-forward past a deadline locally and in tests.

## Invite-only

`visibility` is an explicit toggle, never derived from "does it have invites" — an open event may
carry invites as a nudge, and deriving would mean removing the last invitee silently opened a
private party to the whole house.

An invite-only event needs **at least 4 invitees besides the organiser**, so the smallest private
event is five people; it keeps the feature from quietly becoming a DM channel. Enforced in the
form, because a minimum across related rows is not expressible as a `CheckConstraint` without a
trigger — which means it has to run on **edit and on uninvite too**, not only on create. The
create-only version is the obvious bug and has its own test.

The minimum is weak as specified: four people who will never come satisfy it, and it makes a
genuinely small event permanently un-shrinkable. Kept as decided, flagged here.

## The calendar, and the phone

A month grid, server-rendered, `?maaned=YYYY-MM`. No JS library and no htmx: a month is a place you
can be, so it should be a URL you can send somebody, reload and press back out of.

**The grid queries its own span, not the month.** It pads out to whole weeks, so late August shows
the first days of September — and querying the month rendered those cells empty while the events
they should carry sat one click away. A calendar that draws a day and hides what is on it is worse
than one that does not draw the day.

**Two renderings of every cell, and the width picks one.** Seven columns wide enough to read on a
phone leave about forty pixels per day: room for a dot, nowhere near enough for "18.00
Fællesspisning". So each cell carries both a titled chip per event *and* a row of coloured dots plus
a whole-cell tap link, and the `max-width: 720px` query hides one half or the other. They are
siblings, never nested — an `<a>` wrapping the chips would be an anchor inside an anchor — and
whichever is hidden is `display:none`, so it leaves the accessibility tree with it.

**A tap opens the day under the grid** (`?dag=YYYY-MM-DD`), where the events get their titles back.
A URL parameter rather than JavaScript, for the same reason the month is one. A day outside the
visible grid is treated as absent, so `?dag=2019-04-01` cannot print a heading for a date nowhere on
the page; an empty day says "der sker ikke noget den dag" rather than rendering nothing, because a
panel that fails to appear reads as a broken tap. Not defaulted to today: it would be redundant
beside the desktop chips.

The previous phone rendering scrolled the grid sideways at 660px wide, which showed three days at a
time — no month at all, which is the one thing a month view is for.

## Linked from opslagstavlen

`opslagstavle.Notice.event` points a post at an event, and the event page lists the posts about it
("Omtalt på opslagstavlen"). Announce there, sign up here — which both features' docstrings have
said all along with no way to actually get from one to the other.

Both directions are gated on **the reader's** access to the feature being linked *into*, because
both features are behind their own rollout gate: the event page hides its "Omtalt" section from
somebody who cannot open the board, and the noticeboard hides the chip — and drops the "Handler om"
field from the compose form — from somebody who cannot open begivenheder. The gates hold the same
roles today and are separate globals whose whole purpose is to move apart. A link that 403s whoever
taps it is worse than no link. Only *åbne*
events can be linked at all, so a private event never appears on the noticeboard. The rest of the
argument — `SET_NULL`, the string reference, the second visibility check — lives in
`spec/features/opslagstavle.md`, since the column is on that side.

## Retention

**The shortest in the app**, and it shapes the feature more than it looks.

| | Deleted |
| --- | --- |
| Held | 7 days after `ends_at`, or after `starts_at` if there is no end |
| Aflyst | 30 days after `cancelled_at` |

**A week, widened from the day this shipped with.** A day covered the question people ask the
morning after — "var det i går?" — and nothing else. It missed everything on the scale the house
actually runs on: somebody back from a weekend away wanting to know what they missed, an organiser
checking the guest list before settling up, and the calendar feed, where an event you had said ja to
vanished from your own calendar the next day rather than staying where you could see what the week
held. A week is still a tail rather than a record; if it ever grows past a month, that is the point
to admit an archive is being built and design one on purpose.

A cancelled event gets its own, longer clock because it never happens — the first rule would either
purge it the moment its start passed or hold it to a date that no longer means anything. Thirty days is what gives every subscribed calendar time
to poll and pick up `STATUS:CANCELLED`.

Consequences to build around rather than bolt on: **there is no past-events archive**, so the list
view is only ever "what is coming up" — no history pagination, no year filter, no "tidligere
begivenheder" tab. The calendar view is forward-looking for the same reason.

The obvious objection, answered: RSVPs are deleted with the event, so **there is no record of who
came to anything**. That is deliberate and matches the promise. If the house wants a record, that is
opslagstavlen's job.

Mechanics: `manage.py purge_events` nightly (DEPLOY.md §4b) **plus** a lazy purge on the list view.
Both, unlike opslagstavlen. Nothing about a missed sweep shows on the list — that only ever displays
what is coming up — but a held event stays in every subscriber's `.ics` until it is deleted, so a
cron that has silently stopped leaves last month's dinners sitting in people's calendars. Images go with the row through the `post_delete` receiver, which
is a signal precisely because a bulk queryset delete never calls `Model.delete()`.

## Aflys, not delete

Delete stays available only while **nobody has said ja**. After that the control is **Aflys**: it
sets `cancelled_at`, notifies everyone who said ja, greys the card, freezes editing, refuses new
answers, and keeps emitting the VEVENT with `STATUS:CANCELLED` until the purge takes it 30 days
later.

The reason is the calendar: a hard delete vanishes from every subscribed feed with no explanation,
and the per-event `.ics` already imported into other people's calendars stays there **forever** —
once the row is gone there is nothing left to emit `STATUS:CANCELLED` from.

## Notifications

Web Push through `core.push`, topic `events`, consent per device.

| Trigger | Audience |
| --- | --- |
| New **åbent** event | everyone opted in, minus the organiser |
| New **kun inviterede** event | the invitees only — never the house |
| Promoted off the venteliste | that one person |
| Deadline approaching (24 h) | people who can answer and have not, once per event |
| Aflyst | everyone who said ja |
| Significant edit | everyone who said ja |

"Significant" is the same set of fields that bumps `SEQUENCE` — time, title, place, status. A typo
fix in the description notifies nobody.

The reminder runs as `manage.py remind_rsvp_deadlines` (DEPLOY.md §4b), with a lazy backstop on the
list view. Two things about it are load-bearing:

- **The claim happens before the send.** `reminder_sent_at` is a compare-and-swap, so a crash
  between the two loses one reminder rather than pushing the whole house twice. Losing one is
  recoverable — somebody opens the page — and double-notifying sixty people is not.
- **Delivery is inline** (`background=False`). `core.push` hands the fan-out to a daemon thread by
  default, which is right in a request and fatal in a command: `handle()` returns, the interpreter
  shuts down, and Python kills the thread mid-send. That would deliver to a different fraction of
  the house every night with no error anywhere. Adding the flag to `core.push.send` was a
  prerequisite commit for this feature.

## Calendar export

Two paths, both from `visible_to`:

- **Per event** — `<pk>/ics`, `Content-Disposition: attachment`.
- **Per resident** — `/kalender/<token>.ics`, a subscribable feed, rendered inline (a subscription
  that downloads is not a subscription), plus `X-WR-CALNAME` and
  `REFRESH-INTERVAL;VALUE=DURATION:PT1H`.

The iCalendar output is **hand-rolled**, ~120 lines in `events/icalendar.py`. No dependency: CI runs
`pip-audit` over a frozen lockfile, and this codebase has already made exactly this call once —
`core/push.py` explains dropping `django-webpush` for ~30 lines of direct code.

What the format decisions are, and why:

- **UTC timestamps, no `VTIMEZONE`.** A correct one for Europe/Copenhagen means shipping RRULE-based
  DST transition rules, and a wrong one shifts every event by an hour twice a year — a bug
  introduced in March and reported in April. Clients render UTC instants in the viewer's own zone,
  which they already solve. Pinned by the test that matters: 17:00 local in July -> `150000Z`, 17:00
  local in January -> `160000Z`.
- **Fold at 75 _octets_, not characters.** `æ ø å` are two bytes each, so folding by character
  overshoots and slicing bytes naively splits a continuation byte into mojibake in Apple Calendar.
  The single most likely bug in the feature; it has a dedicated round-trip test.
- **CRLF everywhere**, and escape TEXT with the backslash first, then `;` `,` and newline. Backslash
  last double-escapes everything.
- **No `METHOD`.** `METHOD:PUBLISH` turns the file into an iTIP *message*, and Outlook then treats a
  subscribed feed as an invitation to accept, producing ghost entries.
- **No `ATTENDEE`, no `ORGANIZER`.** `ATTENDEE` lines would publish every attendee's email address
  into a file that lands on Google's and Apple's servers, and `ORGANIZER;mailto:` makes some clients
  try to send iTIP replies to it. The guest list stays on the dorm's site. This has a test.
- **No `VALARM`** — a subscriber's alarms are their choice, and pushing one is how a feed gets
  unsubscribed.
- **No `ends_at` still gets a `DTEND`**, two hours out. A VEVENT with neither `DTEND` nor
  `DURATION` is a zero-length instant and renders as a sliver you cannot read. `DTEND` rather than
  the equivalent `DURATION:PT2H` so there is one branch instead of two.
- **`DTSTAMP` is the event's own last-modified, not wall-clock now**, so the document is a pure
  function of the data — which is what lets the feed answer 304 to a conditional GET instead of
  resending to a client polling hourly, and makes "did editing the description change the bytes?" a
  question a test can ask.
- **`URL:` points at `gahk.dk`, hard-coded**, for the same reason as the UID: the link lands in
  somebody's calendar and must still work from a phone that has never heard of `localhost:8000`.
- **`DESCRIPTION` carries `plain_text`, never the Markdown source or HTML.** A calendar has no more
  use for `**bold**` than a lock screen does, and `X-ALT-DESC` is Outlook-only.

### The feed token

The feed view carries **no auth decorator** — a calendar client cannot log in — so the token *is*
the credential. Everything follows from that: `secrets.token_urlsafe(32)` via a named module
function so migrations can serialise the default; minted **lazily on first use**, never backfilled
by a migration (that would mint sixty secrets for people who may never use the feature, and bake a
non-deterministic data step into a history that could then never be squashed cleanly); an unknown
token **404s**; the page offers **forny link**, which invalidates the old one; the feed contains
only what `visible_to` allows; and `last_used_at` is written at most once an hour, because sixty
phones polling hourly would otherwise turn a read endpoint into a write storm.

## Decisions and rejected alternatives

| Decision | Rejected alternative, and why it lost |
| --- | --- |
| A new app | A `Category.BEGIVENHED` post with a date column. A post cannot express "fuldt", and a noticeboard whose rows are invisible to some readers is one nobody can reason about. |
| Ja / nej | Adding "måske". It cannot hold or release a seat, so every downstream question grows a branch that resolves to "treat as nej". |
| Derived seating | A stored `seat_taken_at` + `select_for_update` on the parent row. Reversed once the deadline guarantee was pinned: with no answer changeable after the deadline the derivation is already final, and the stored version cost a column, two constraints, a lock and a SQLite/Postgres divergence. |
| Derived queue position | A stored `position`. Every withdrawal would rewrite every row behind it, and a trustworthy version needs a partial unique constraint whose shuffle-down UPDATE collides with itself — fixable only with `DEFERRABLE`, which Postgres has and SQLite does not. |
| Deadline closes at `starts_at` when unset | "Open forever". A ja arriving after the party keeps mutating an attendee list and every subscriber's `.ics`. |
| Aflys, keeping the row 30 days | Hard delete. Nothing is left to emit `STATUS:CANCELLED` from, so the event sits in people's calendars forever. |
| No archive | Keeping events, with a "tidligere" tab. Retention is the shape the list and calendar are built around, not a policy bolted on afterwards. |
| Moderators cannot see private events | "Inspektionen sees everything". It falsifies the one promise a private event makes. The admin is the escape hatch. |
| Subqueries in `visible_to` | `Q(invites__resident=r)` joins. An event you are both co-organiser of and invited to comes back twice — a duplicate card, and two VEVENTs sharing one UID, which some clients resolve by dropping both. `.distinct()` patches it and silently stops being enough the moment anyone adds an `annotate(Count(...))`. |
| Hand-rolled iCalendar | A library. CI runs `pip-audit` over a frozen lockfile; `core/push.py` made the same call for the same reason. |
| Feed at the site root | Under `/intern/`. A proxy gate or `LoginRequiredMiddleware` would kill every subscription **silently**. |
| `core.rollout` extracted | A third copy of the gate. `opslagstavle/access.py` said verbatim that a third feature wanting it is the point to extract it. This was the third. |

## Open questions, flagged not answered

- **Recurring events** (fællesspisning every Wednesday) are out of scope and would change the model
  substantially — a recurrence rule interacts with RSVP, capacity and retention at once. Worth
  knowing whether it is wanted *before* the schema sets, because retrofitting is expensive.
- **The 4-invitee minimum** is weak (see above) and may be worth dropping or replacing.
- **Waitlisted events in a subscriber's feed** are emitted as `STATUS:TENTATIVE` +
  `TRANSP:TRANSPARENT` — they show, but do not mark you busy, so queuing for a trip does not block
  an afternoon you may not get. That makes the same event render differently in two people's feeds,
  which is correct (status is per attendee), but it is worth a look once people are using it: the
  simpler alternative is to omit them until they are seated.
- **The shared Google Calendar** on the dashboard — whose username and password are printed for
  every logged-in resident (`F-013-dashboard.md` flags it High) — is what this feature is eventually
  meant to replace. It stays this round: removing it needs someone to first export what is live in
  the Google account, which nobody has done. Its own PR, noted so it is not forgotten.
