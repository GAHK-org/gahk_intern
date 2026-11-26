# Feature: Kalenderabonnement — the resident's calendar subscription

Each resident has a personal calendar address to subscribe to in Google Kalender, Outlook or Apple
Kalender. It is the resident's calendar, not begivenheder's: today it carries events and birthdays,
later kitchen and cleaning shifts. Settings page at `/calendar/settings` on intern.gahk.dk, linked from the
user menu, the month calendar and every event page.

## The settings page

- Shows the address in a field with a **Kopiér** button, an **Åbn i kalender-app** link (`webcal://`,
  which subscribes in Apple Kalender and Outlook on a computer) and **Tilføj i Google Kalender**.
  Nothing on the page downloads the file.
- Short instructions for Google Kalender, Outlook and Apple Kalender (iPhone/iPad and Mac).
- What goes in, all on by default:
  - Begivenheder: **all events the resident can see**, or **only those they said ja to**.
  - Fødselsdage on the active alumneliste.
  - Køkkenvagter and rengøringsvagter: shown greyed out until those features exist.
- A small note says when a calendar app last fetched the address. If it has not been fetched for
  **a week**, a warning says the subscription is probably not set up right or was removed.
- **Lav en ny adresse** replaces the address; the old one stops working at once (404).

## The feed

- `/calendar/feed/<token>.ics`, no login: the token in the path is the credential. Unknown tokens 404.
  Served inline, `no-store`, `noindex`, `no-referrer`.
- Events come from begivenheder's visibility rules (never a private event you were not invited to),
  from 90 days back. Where you stand is in the title:
  - said ja and seated: the plain title, blocks the time;
  - not answered: `Ikke svaret: <titel>`; said nej: `Sagt nej: <titel>`;
  - on the venteliste: `Venteliste: <titel>`, tentative;
  - cancelled: `Aflyst: <titel>`.
  Only a seated ja blocks the time.
- Every event entry links to its page, in both `URL` and the description.
- Birthdays are all-day entries ("Mette Hansen fylder 23"), never busy, from 90 days back to a year
  ahead, linking to the resident's profile.
- Every fetch is counted (`fetch_count`, `last_used_at`), shown per resident in the Django admin. A
  new address starts at zero.

## Addresses

- The feed used to be `/kalender/<token>.ics` and the page `/begivenheder/kalender/abonnement`; both
  redirect permanently.
- Most subscriptions still point at `gahk.dk/kalender/<token>.ics`; gahk.dk passes them through to
  intern, which counts them as fetches like any other.

## Audit trail

- Recorded: a subscription made on the first visit (*Fik en kalenderadresse*), settings saved with
  a change (*Rettede sit kalenderabonnement*) and a new address (*Lavede en ny kalenderadresse*).
- The address itself is redacted: the trail says it changed, never what it was or became.
- A calendar app fetching the feed records nothing; the fetch count and last fetch are bookkeeping.

## Decisions

- **Its own app (`calendars`)**, not part of begivenheder: the calendar gathers several features,
  each of which renders its own entries. The model moved from begivenheder with every token intact.
  The app is where calendars are put together, not just this feed: a calendar view at `/calendar/`
  and a shared house calendar (everyone's shifts, all events) belong here too, with the subscription
  as one way to see them. Plural because an app named `calendar` would shadow Python's own module.
- **The token lives in its own model**, never on `Resident`, which is loaded everywhere and exported
  by the alumneliste; a secret there leaks through code that does not know it holds one.
- **A prefix in the title** rather than a status or category: the title is the only thing every
  calendar app shows.
- **The feed under `/calendar/feed/`, the page at `/calendar/settings`**: `/calendar/` stays free
  for a calendar view of one's own, a page under `/calendar/` can never collide with a token, and
  the kitchen account's allowlist names the feeds by prefix.
- **Every fetch writes**: one UPDATE per request is nothing for a house of sixty, and the count is
  what tells whether subscriptions are in use.
- **A week before the warning**: every calendar app fetches at least daily (Google is the slowest).
- **Birthdays are on by default.** Names and ages then reach the subscribers' calendar providers
  (Google, Apple, Microsoft); the dates are already visible to every resident on the profile page
  and in the alumneliste, and a subscriber can switch them off.
