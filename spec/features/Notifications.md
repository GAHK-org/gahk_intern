# Feature: notifications

Notifications may be sent for many reasons.
Currently, subscription to push notifications is scattered around the app.

Notifications should include both in-app notifications, as well as push notifications.
**Some notifications are push only, whilst other might be both.**

## In-app notifications
Include a notifications page and a notification dropdown in the nav bar.

The dropdown should show the 10 most recent notifications.

The page should show all active notifications.
For the page, add both a chronologic view and a view grouped by category.

### Seen/unseen
If a user has clicked a notification (in-app or push), mark the notification as seen.
Add a visual cue to indicate if a notification is seen or not.


## Categories
Current categories include (but not limited to):

- System
- General
- Events
- Chat (den hurtige)
    * Likes
    * Replies
- Posts
    * Replies
    * Likes


## Grouping

For notifications and push notifications, some are grouped, i.e., you will only see the most recent.
For instance, if you get a notification for "user-A" liked your message, then "user-B" likes the same message too, the initial notification will be replaced with "user-A and user-B liked your message". If more users like it, it might end up saying "x users liked your message".


## Settings page

Include a personal settings page for each user, where they can manage they may manage their notification settings.
These settings apply for the user, not the device.

On the settings page, users may toggle if they want to receive notifications for different categories.

Enable separate toggle for each chat room (den hurtige, tv-rezz, sportsman, gahkroom and m.h.g.a).
Add an indicator directly in each chat room, that indicates if the room is toggled on. Clicking the indicator takes the user to this section of the settings page.

For each category, the user may toggle for in-app notifications and push notifications separately.

By default, everything is enabled.


Include setting for vibration.
- On/off in general
- By category
- By importance level. E.g., don't vibrate for lowest importance level



## Prompt for subscription to push notifications
Subscription to push notifications should be once per device, i.e., not for each different part of the app that sends notifications. A single point per device.

Prompt the user to subscribe at the dashboard, at the chat (den hurtige) and in the settings page.


## TTL
When notifications are created, they should have a TTL.
For time limited messages in the chat, set the TTL to the message expiration time.

For other notifications, make the default TTL 7 days.


## Importance

Add 4 levels of importance to notifications.
In the dropdown and on the notifications page, show the most important first, and with visual cues that they are important (color/icons etc...).

Allow the most important notification level to overrule notification settings. That is, a user will still receive a notification, even though the category is disabled.

Example of lowest importance is "user liked your message".


## Admin page

On the admin page, admins should be able to view active push subscriptions for each user.
Enable admin to cancel a subscription.

Show statistics and metrics for notifications.



---

## As built (feature/notification-settings)

This section records what was implemented and where it deliberately differs from, or makes concrete, the request above.

### Architecture
- New Django app `notifications` owns the rows, preferences, grouping and fan-out. `core.push` is transport only (VAPID/pywebpush, device bookkeeping, daily delivery counters).
- Features call `notifications.services.notify(...)` / `notify_grouped(...)`; they decide **who** and **what wording**, never how it is delivered.
- `Notification` rows: recipient, category, importance, title, body, url, batch (uuid shared by one fan-out), group_key, actors, `in_app`, `pushed`, created/updated/expires/seen timestamps.
- `NotificationPreference` (per resident × category: in_app / push / vibrate) only stores rows that differ from the all-on default. `NotificationSettings` holds the global vibration switch and the minimum importance that vibrates.

### Categories (keys)
| Group | Key | Default importance | In-app |
|---|---|---|---|
| System | `system` | Normal | yes |
| Generelt | `general` | Normal | yes |
| Chat | `chat.room.<slug>` — one per room (Den Hurtige, TV-Rezz, G. A. Sportsmann, GAHKroom, M.H.G.A) | Normal | **no — push only** |
| Chat | `chat.replies` (replies to your message) | Normal | yes |
| Chat | `chat.likes` (reactions to your message) | Lav | yes |
| Opslag | `posts.new`, `posts.replies`, `posts.likes` (Lav) | Normal | yes |
| Begivenheder | `events.new`, `events.changes` (Høj), `events.personal` (Høj — deadline reminders, moved off the waiting list), `events.comments` | | yes |
| Reparationer | `repairs` (new / handed-over tasks for your group) | Normal | yes |

The request listed "Events" and "Posts" as single categories; they were split so a resident can, e.g., keep cancellations but drop new-event announcements. Reparationer was added because it already sent pushes.

**Push-only:** new messages in a chat room. Putting every chat message in the in-app list would bury the notifications that are about *you*; replies and reactions to your own messages are in-app *and* push.

### Who is told what
- **Chat:** a new message → the room's category, to everyone who may read the room, except the author; TTL = the message's expiry. A reply → grouped `chat.replies` to the message's author; if the replier ticks "underret alle", the room is notified too. A reaction → grouped `chat.likes` to the author only, and **only when a reaction is created** — changing or removing an emoji never notifies.
- **Opslagstavle:** a new post → `posts.new` to everyone who can open the board; comments and reactions → grouped to the post's author only.
- **Begivenheder:** new → audience (invite list for invite-only events) except organiser; changes → RSVP "ja" except organiser; cancellation → RSVP "ja"; comments → grouped to each host; deadline reminders → `events.personal` with TTL = the RSVP deadline.
- **Reparationer:** current role holders + administrators + superusers; the reporter is not told about their own task.
- The base audience (`everyone()`) is residents with a residency or role in the active period, plus anyone who owns a push device, plus superusers — always narrowed by the feature's own access gate.

### Ordering, seen/unseen
- Lists are ordered **importance first, then most recently updated** (a grouped notification that gains a new like moves back to the top and becomes unseen again).
- Unseen items have a dot and a highlighted background; importance Høj/Kritisk get a coloured left border and a level label, Kritisk also a tinted background.
- Clicking an in-app item goes through `/intern/notifikationer/<pk>/`, which marks it seen and redirects. Tapping a push goes through `/intern/notifikationer/aabn/<batch>/`, which finds the tapping resident's row in that fan-out and does the same. "Markér alle som set" is on the page.

### Grouping
- One row per (recipient, `group_key`), e.g. `chat.likes:<post pk>`, rewritten in place. Titles: "Anders reagerede…", "Anders og Bente reagerede…", from three actors "3 personer reagerede…" (the 50 most recent actors are kept, so the count stops at 50). The newest actor is named first; an actor repeating does not count twice; acting on your own content never notifies.
- The push carries `tag = group_key`, so the phone replaces the earlier push instead of stacking.

### Settings page (`/intern/notifikationer/indstillinger`)
- A table per group; each category row has in-app / push / vibration checkboxes (push-only rooms show "—" for in-app). Everything is on by default.
- Each chat room shows a 🔔/🔕 link in its header pointing at `…/indstillinger#rum-<slug>`; the target row is highlighted.
- Vibration: global on/off, per category, and "vibrate for importance X and above". **Vibration is implemented as a silent push** (`silent: true` in the service worker) — browsers do not let a page choose "sound but no vibration", so "no vibration" means no sound either. The notification is still shown.
- The old per-room mute (`ChannelMute`) was migrated into room preferences with push off, then the table was dropped.

### Importance
Four levels: Lav, Normal, Høj, Kritisk. **Kritisk overrides every preference** (in-app, push and vibration) and uses `requireInteraction`, so it stays on screen until dismissed. No feature sends Kritisk on its own; administrators can, via the broadcast form.

### Push subscription
- One `PushSubscription` per device (browser), used for every category — the per-feature topic columns (`wants_*`) were dropped, so existing devices now get everything per the defaults.
- The device widget is shown on the dashboard and in Den Hurtige (only while this device is not subscribed) and on the settings page (always, with an unsubscribe button). It asks the server for the device's state: *subscribed for you* / *subscribed for another resident* (a shared browser — subscribing takes it over) / *not subscribed*.
- Endpoint: `POST /intern/notifikationer/enhed`. The per-feature `…/abonner` endpoints and the Den Hurtige `lyd/<slug>` mute endpoint were removed.

### TTL and retention
- Default TTL 7 days; chat messages use the message's expiry; deadline reminders the deadline. The TTL is used both as the push TTL and as `expires_at` for the in-app row — expired rows disappear from the lists immediately.
- Rows are deleted 30 days after expiry by a Celery beat task (03:30), so the admin statistics can still count them.

### Navigation
- A bell with an unseen-count badge in the top bar opens a dropdown (htmx, loaded on click) with the 10 most important/recent visible notifications.
- `/intern/notifikationer/` has two views: `?visning=kronologisk` (default) and `?visning=kategori`. The sidebar has a "Notifikationer" entry.

### Admin page (`/admin/notifikationer`, administrators only)
- Devices grouped per user (user agent, created, last success, failures) with a "Annullér" button that deletes the subscription.
- Statistics for the last 7 and 30 days: total / seen / pushed, by category and by importance; plus push deliveries per day (delivered / failed / removed devices).
- A broadcast form (category Generelt or System, any importance, title, body, link) that sends to everyone.
