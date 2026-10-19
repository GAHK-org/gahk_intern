# Notifications

The one way every feature tells residents about things: the bell and the notifications page in the app, push on each subscribed device, and personal settings per category.

## Categories
- Each category is a row on the settings page. Groups appear in this order everywhere; System and Generelt come last.
- In-app "always" means the in-app copy cannot be turned off; push and vibration can.

| Group | Category | Default importance | In-app |
|---|---|---|---|
| Den Hurtige | One per room (Den Hurtige, TV-Rezz, G. A. Sportsmann, GAHKroom, M.H.G.A): new messages | Normal | no, push only |
| Den Hurtige | Svar på dine beskeder | Normal | yes |
| Den Hurtige | Reaktioner på dine beskeder | Lav | yes |
| Opslagstavlen | Nye opslag | Normal | yes |
| Opslagstavlen | Kommentarer på dine opslag | Normal | yes |
| Opslagstavlen | Reaktioner på dine opslag | Lav | yes |
| Begivenheder | Nye begivenheder | Normal | yes |
| Begivenheder | Ændringer og aflysninger (events you said ja to) | Høj | yes |
| Begivenheder | Påmindelser og venteliste (svarfrist reminders, moved up from the venteliste) | Høj | yes |
| Begivenheder | Kommentarer på dine begivenheder (events you host) | Normal | yes |
| Reparationer | Reparationer (new and handed-over repairs for your crew) | Normal | yes |
| System | Systembeskeder: about the site itself, e.g. downtime and new features | Normal | always |
| Generelt | Generelle beskeder: everything without a category of its own | Normal | always |

## Importance
- Four levels: Lav, Normal, Høj and Kritisk. Each category has a default, and a single notification may be raised above it.
- Lists show the most important first, then the most recently updated. Høj and Kritisk are marked with a coloured edge and their level.
- Kritisk overrides every setting: it arrives in-app (where the category has an in-app copy), as a push and vibrating, even with the category turned off, and the push stays on screen until dismissed.
- Only administrators send Kritisk, from the broadcast form.

## Who is told what
- Nobody is told about something they could not open (a feature's rollout gate, a restricted room, a private event's invite list), and nobody is told about their own message, post, comment, reaction or report.
- Den Hurtige: a new message goes to everyone who can read the room. A reply goes to the message's author, and also to the room if the replier ticks "Underret alle". A new reaction goes to the author.
- Opslagstavlen: a new post goes to everyone who can open the board. A comment or a new reaction goes to the post's author.
- Begivenheder: a new event goes to everyone who can see it. A move or rename goes to those who said ja, except the organiser; an aflysning goes to everyone who said ja. Comments go to the hosts; svarfrist reminders and promotions from the venteliste go to the person concerned.
- Reparationer: the crew taking over a repair (Viceværterne for a new or returned one, Reppergruppen for one handed to them), plus administrators and superusers.
- Søg værelse: an applicant whose application Indstillingen deletes is told under Generelt, at Høj.
- Reactions notify only when one is added; changing or removing an emoji tells nobody.

## The bell and the notifications page
- The bell in the top bar shows how many notifications are unseen. It opens a panel with the 10 most important, then newest, and links to the full list and the settings. On a phone the panel spans the top bar.
- The badge and an open panel update live, so reading a notification on one device clears it on the others. Notifications sent by scheduled jobs, such as svarfrist reminders, show on the next page load.
- The notifications page (/intern/notifikationer/) lists every active notification, in one list or grouped by category.
- A notification is seen once it is clicked, in the app or as a push. Unseen ones have a dot and a highlighted background. "Markér alle som læst" marks everything seen, and going back to the page shows the one just opened as read.

## Grouping
- Replies and reactions on Den Hurtige and Opslagstavlen, and comments on events, are grouped per message, post or event. The notification is rewritten ("Anders svarede", "Anders og Bente svarede", "3 personer svarede"), marked unseen again and moved to the top.
- The newest person is named first, and the same person twice counts once. The count stops at 50.
- On the device the new push replaces the previous one instead of stacking.
- Once a grouped notification has expired, the next one starts a new count.

## Settings
- Reached from the avatar menu, the bell's panel and the notifications page.
- Every category has I appen, Push and Vibration. Push-only rooms show "—" for in-app; System and Generelt show in-app ticked and locked.
- Everything is on by default. Settings belong to the resident, not the device: every subscribed device follows them.
- Vibration can be turned off altogether, or limited to a chosen importance and above. A push without vibration arrives silently, with no sound either, but is still shown.
- Each Den Hurtige room shows 🔔 "Lyd til" or 🔕 "Lyd fra" in its header, linking to that room's row here.

## Push on a device
- A device (one browser) subscribes once and then receives every category the resident has push on for.
- The dashboard and Den Hurtige ask "Få besked på denne enhed?" while this device is not subscribed. The settings page always shows this device's state, with a button to turn push on or off.
- A browser shared by two residents belongs to whoever subscribed it last.
- A device the push service reports as gone is removed. Without push set up on the server, the prompts are hidden.

## Expiry and retention
- A notification expires after 7 days, counted as elapsed time across a change to or from summer time. One about a Den Hurtige message expires with the message, and a svarfrist reminder at the svarfrist.
- An expired notification leaves the lists at once, and a push not delivered by then is dropped.
- Expired notifications are deleted 30 days after expiry by a nightly job (03:30).

## Admin page
- Administrators only, from the site-admin front page.
- Devices: every push subscription per resident (device, created, last delivered, failures in a row), each with "Annullér". A cancelled device stops getting pushes, and its owner can subscribe it again.
- Statistics for the last 7 and 30 days: sent, seen and pushed, by category and by importance, plus push deliveries per day (delivered, failed, devices removed).
- Broadcast: a System or Generelt notification at any importance, with a title, a text and a link to a page on the site. It goes to the whole house: everyone living here or holding a role this month, anyone with a push device, and superusers.

## Decisions
- One subscription per device and one settings page replace a separate push opt-in per feature. A push that was swiped away used to be gone; the in-app list keeps it.
- System and Generelt are catch-alls. A feature gets its own category only when its notifications are recurring and plentiful enough that someone would want them off on their own (new posts, reactions, chat rooms). A one-off about a resident's own affairs, such as a deleted room application, goes under Generelt with a raised importance; something about the site itself goes under System.
- System and Generelt are always in-app: the list is where somebody with push off still sees what the house says.
- Room messages are push only. Every chat message in the in-app list would bury the notifications that are about you.
- Events and posts are several categories each, so a resident can keep cancellations or comments on their own post while dropping every announcement.
- Reparationer has its own category because its notifications matter only to the role holders.
- "No vibration" means silent: the web platform has no vibration-only switch.
- Expired notifications are kept for 30 days only so the admin statistics can count a month.
