# Feature: Notifikationer — in-app and push notifications

## What it is, and what it replaces

Notifications are sent for many reasons: a reply to your chat message, a new post on the board, an
event you said ja to being moved, a repair handed to your group, a message from the administrators.
Before this feature every part of the app had its own push subscription (`…/abonner`, with a
`wants_<feature>` column per topic) and its own idea of what to send; nothing was kept, so a push
that was swiped away was gone.

Now there is one `notifications` app that owns all of it:

- **In-app notifications** — a row per recipient, listed in a dropdown in the top bar and on a
  notifications page, with seen/unseen state.
- **Push notifications** — one subscription per device, used for every category.
- **Personal settings** — per category: in-app, push and vibration.

Some notifications are both in-app and push; some are **push only** (see Categories).

Features decide **who** is told and **with what wording**; they call
`notifications.services.notify(...)` or `notify_grouped(...)` and never deal with delivery.
`core.push` is the transport only (VAPID/pywebpush, device bookkeeping, daily delivery counters).

## Categories

| Group | Key | Default importance | In-app |
|---|---|---|---|
| Den Hurtige | `chat.room.<slug>` — one per room: Den Hurtige, TV-Rezz, G. A. Sportsmann, GAHKroom, M.H.G.A | Normal | **no — push only** |
| Den Hurtige | `chat.replies` — replies to your message | Normal | yes |
| Den Hurtige | `chat.likes` — reactions to your message | Lav | yes |
| Opslagstavlen | `posts.new` | Normal | yes |
| Opslagstavlen | `posts.replies` — comments on your post | Normal | yes |
| Opslagstavlen | `posts.likes` — reactions to your post | Lav | yes |
| Begivenheder | `events.new` | Normal | yes |
| Begivenheder | `events.changes` — moved or cancelled, for events you said ja to | Høj | yes |
| Begivenheder | `events.personal` — RSVP deadline reminders, moved up from the venteliste | Høj | yes |
| Begivenheder | `events.comments` — comments on events you host | Normal | yes |
| Reparationer | `repairs` — new and handed-over tasks for your group | Normal | yes |
| Køkkenvagter | `kitchen` — shifts for sale and unmanned shifts (see kitchen-duty.md) | Normal | yes |
| System | `system` — about the site itself: downtime, new features | Normal | **always** |
| Generelt | `general` — **the catch-all**: everything without a category of its own, from a notice about your own application to an announcement to the whole kollegium | Normal | **always** |

Groups are shown in this order everywhere (settings page, the by-category view). System and Generelt
come last: they are rare, and the toggles people come to the settings page for are the feature ones.

The room categories are generated from the Den Hurtige channel registry, so adding a channel adds
its toggle. A new message in a room is **push only**: putting every chat message in the in-app list
would bury the notifications that are about *you*. Replies and reactions to your own messages are
in-app *and* push.

### System and Generelt are catch-alls — don't add a category lightly

The settings page is a table every resident scrolls, and each category is another row of toggles. A
feature gets its own category only when its notifications are **recurring and plentiful enough that
somebody would want them off on their own** (new posts, reactions, chat rooms). Everything else uses
an existing one:

- **Generelt** — anything without its own category: one-off messages about the resident's own
  affairs (e.g. "din ansøgning er slettet") and announcements to everyone. Raise `importance` at the
  call site when it matters more than Normal.
- **System** — about the site itself (downtime, new features), not about a resident's own case.

A feature does not get a group just for being a feature: a deleted room application is `general`,
not a new "Søg værelse" group.

System and Generelt can **not** be turned off in-app (`Category.in_app_optional = False`): the list
is where somebody who has push off still sees what the house itself says. Push and vibration for
them can be turned off like any other category.

## Importance

Four levels: **Lav**, **Normal**, **Høj**, **Kritisk**. Each category has a default; a call may raise
it. "Someone reacted to your message" is the typical Lav.

- In the dropdown and on the page, the most important come first and are marked: Høj and Kritisk get
  a coloured left border and a level label, Kritisk also a tinted background.
- **Kritisk overrides every preference** — in-app (where the category has an in-app row), push and
  vibration — so a resident gets it even with the category turned off. The push uses `requireInteraction`, so it stays on screen until
  dismissed.
- No feature sends Kritisk on its own; administrators can, via the broadcast form on the admin page.

## Who is told what

The base audience, `services.everyone()`, is residents with a residency or role assignment in the
active period, plus anyone who owns a push device, plus superusers. Each feature narrows it by its
own access gate (rollout gate, channel roles, invite list), so nobody is notified about something
they would get a 403 on opening. Acting on your own content never notifies you.

- **Den Hurtige**
  - A new message → the room's category, to everyone who may read the room, except the author.
  - A reply → grouped `chat.replies` to the message's author. If the replier ticks "underret alle",
    the room is notified too (except the replier and the author, who already got the reply).
  - A reaction → grouped `chat.likes` to the message's author only, and **only when a reaction is
    created** — changing or removing an emoji never notifies.
- **Opslagstavlen**
  - A new post → `posts.new` to everyone who can open the board, except the author. The body is
    the post as plain text, not Markdown.
  - Comments and reactions → grouped to the post's author only. There is no "underret alle": a
    noticeboard has no single shared audience the way a chat room does.
- **Begivenheder**
  - New → `events.new` to the event's audience (the invite list for invite-only events), except the
    organiser.
  - Moved or renamed → `events.changes` to those who said ja, except the organiser. Cancelled → the
    same, titled "Aflyst: …".
  - Comments → grouped `events.comments` to each host (organiser and co-organisers).
  - RSVP deadline reminders and promotion from the venteliste → `events.personal`.
- **Reparationer** → `repairs` to current holders of the responsible role (Viceværterne for a new
  report or one sent back, Reppergruppen for one handed over), plus administrators and superusers.
  The reporter is not told about their own new report.
- **Køkkenvagter** → `kitchen`. A shift put for sale within 7 days → this month's residents except
  the seller, gone when the shift starts. Unmanned shifts → this month's residents (within 24 hours)
  or those with a negative balance (72 hours; under -10 weekly, 7 days), gone when the last starts.

## In-app notifications

### The dropdown

A button with an unseen-count badge in the top bar opens a dropdown (loaded with htmx on click) with
the **10** most important, then most recent, active in-app notifications, and a link to the page.

### The notifications page — `/intern/notifikationer/`

Lists all active in-app notifications, in two views:

- `?visning=kronologisk` (default) — one list;
- `?visning=kategori` — one section per group, in the group order above. A row whose category has
  since been retired is listed under Generelt rather than erroring.

Both are ordered **importance first, then most recently updated**. `updated_at`, not `created_at`: a
grouped notification that gains another like is news again, so it moves back up and becomes unseen.

### Seen / unseen

A notification is marked seen when it is clicked — in the app or as a push.

- Unseen items have a dot and a highlighted background.
- Clicking an item goes through `/intern/notifikationer/<pk>/`, which marks it seen and redirects
  to its target.
- A push names its fan-out (`batch`), not a row, because one payload is shared by every recipient.
  Tapping it goes through `/intern/notifikationer/aabn/<batch>/`, which finds the tapping resident's
  own row in that batch, marks it seen and redirects — this works for push-only rows too.
- "Markér alle som set" on the page marks everything seen.
- Redirect targets must be paths on this site (`services.is_site_path`, Django's
  `url_has_allowed_host_and_scheme`), so a stored or broadcast link can never become an open
  redirect.

## Grouping

Some notifications are grouped: you only see the latest state. If Anders likes your message and then
Bente does too, the first notification is replaced by "Anders og Bente reagerede på din besked"; from
three people on it reads "3 personer reagerede på din besked".

- One row per (recipient, `group_key`), e.g. `chat.likes:<post pk>`, rewritten in place, marked
  unseen again and moved to the top.
- The newest actor is named first; the same person acting twice counts once. The 50 most recent
  actors are kept, so the count stops at 50.
- The push carries `tag = group_key`, so the phone replaces the earlier push instead of stacking a
  new one.

Grouped: replies and reactions on Den Hurtige and Opslagstavlen, and comments on events.

## Settings page — `/intern/notifikationer/indstillinger`

Personal settings for each resident. **They apply to the resident, not the device**: what you get
told is chosen once, and every device you have subscribed follows it.

- A card per group, each with a table: category, **I appen**, **Push**, **Vibration**. Push-only
  rooms show "—" for in-app; System and Generelt show their in-app box ticked and disabled.
- **By default, everything is enabled.** Preferences are stored as exceptions: a category set back
  to all-on has its row deleted.
- **A toggle per chat room.** Each room shows an indicator in its header — 🔔 "Lyd til" when the
  room's push is on, 🔕 "Lyd fra" when it is off — that links to `…/indstillinger#rum-<slug>`, where that room's row is
  highlighted.
- **Vibration**
  - on/off in general;
  - per category;
  - by importance: "vibrate only for importance X and above".

  Vibration is implemented as a **silent push** (`silent: true` in the service worker): browsers do
  not let a page choose "sound but no vibration", so "no vibration" means no sound either. The
  notification is still shown.
- The page also shows this device's push subscription (see below).

## Push subscription

**One subscription per device**, not one per part of the app. A `PushSubscription` (one browser on
one device) receives every category the resident has push on for.

The resident is prompted to subscribe in three places:

- the **dashboard** and **Den Hurtige** — a prompt shown only while this device is not subscribed;
- the **settings page** — always shown, with the current state and an unsubscribe button.

The widget asks the server for the device's state, because the browser can only say that *some*
subscription exists: *subscribed for you*, *subscribed for another resident* (a shared browser —
subscribing takes it over), or *not subscribed*. Endpoint: `POST /intern/notifikationer/enhed`
(subscribe / unsubscribe / status). Unsubscribing is scoped to the resident, so one person cannot
remove another's phone by replaying its endpoint.

A device that the push service reports as gone (404/410) is deleted; other failures are counted
(`failure_count`), and a success resets the count and stamps `last_success_at`.

## TTL and retention

Every notification has a TTL, used both as the push's TTL and as the in-app row's `expires_at`:

- **default: 7 days**;
- a Den Hurtige message, and replies and reactions to it: the **message's own expiry**;
- an RSVP deadline reminder: the deadline.

Expiry is a comparison, not a sweep, so a row leaves the lists the moment its TTL runs out. The rows
themselves are deleted **30 days after expiry** by a nightly Celery beat task (03:30), so the admin
statistics still have a month to count them.

## Admin page — `/admin/notifikationer`

Administrators only.

- **Devices** — active push subscriptions grouped per resident (device, created, last delivered,
  failures in a row), with **Annullér** to cancel (delete) a subscription.
- **Statistics** for the last 7 and 30 days: notifications sent, seen and pushed — by category and by
  importance — plus push deliveries per day (delivered, failed, devices removed).
- **Broadcast** — send a System or Generelt notification, at any importance, with a title, body and
  on-site link, to everyone.

## Routes

`/intern/notifikationer/` (page), `seneste` (dropdown), `set` (mark all seen, POST),
`indstillinger` (settings), `enhed` (device subscription, POST), `<pk>/` (open an item),
`aabn/<uuid>/` (open a push). Namespaced `notifications:`. Admin: `siteadmin:notifications` and
`siteadmin:notification_device_cancel`. The sidebar has a **Notifikationer** entry.

## Data model

- `Notification` — recipient, category, importance, title, body, url, `batch` (uuid shared by one
  fan-out), `group_key`, `actors` (JSON, for grouped titles), `in_app`, `pushed`, `created_at`,
  `updated_at`, `expires_at`, `seen_at`. A row is written for a recipient who gets it in-app or by
  push; one who would get neither gets no row.
- `NotificationPreference` — resident × category: `in_app`, `push`, `vibrate`. Exists only when
  different from all-on.
- `NotificationSettings` — per resident: `vibrate` and `vibrate_min_importance`.
- `core.PushSubscription` — user, endpoint, keys, user agent, `created_at`, `last_success_at`,
  `failure_count`. No topic columns.
- `core.PushDeliveryDay` — per day: delivered, failed, removed.

One fan-out is at most two push deliveries — the residents whose push vibrates and those whose push
is silent — sent from a single background thread, however many recipients there are.

## Migration from per-feature subscriptions

- The per-feature topic columns (`wants_den_hurtige`, `wants_opslagstavle`, `wants_begivenheder`,
  `wants_reparationer`) were dropped. Existing devices now get every category, per the all-on
  default.
- Den Hurtige's per-room mute (`ChannelMute`) was copied into room preferences with push off, then
  the table was dropped.
- The per-feature `…/abonner` endpoints and Den Hurtige's `lyd/<slug>` mute endpoint are gone; their
  slugs stay reserved in the channel registry.

## Decisions

- **Events and posts are several categories, not one each.** A resident should be able to keep
  cancellations while dropping new-event announcements, or keep comments on their own post while
  dropping every new post.
- **Reparationer is its own category.** It was not in the original list, but it sends pushes too,
  and they matter only to the role holders.
- **Room messages are push only.** See Categories.
- **Reactions notify on create only.** Changing your emoji, or removing it, is not news to anyone.
- **Silent instead of "no vibration".** The web platform has no vibration-only switch.
- **System and Generelt are always in-app.** See Categories.
