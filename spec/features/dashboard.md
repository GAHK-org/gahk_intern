# Dashboard

The internal area's landing page at /intern/, and the sidebar and top bar shared by every internal page.

## Access
- Only logged-in residents; anyone else is sent to the login page. Logging in lands on the dashboard.
- Old /nyintern links redirect to the matching /intern page.
- Everything shown depends on the resident's roles for the active month. An administrator previewing the site as another role sees exactly what that role would see.

## The dashboard
- Greets the resident by first name, with a "Søg værelse" button.
- Shows the active month and the resident's roles for it ("Ingen særlige denne måned" when none).
- "Begivenheder på GAHK": the next 6 upcoming events the resident can see (aflyste ones marked), plus any mærkedag in the next 30 days, in date order, with a link to the month calendar. With nothing coming up it offers to create an event; a resident without access to Begivenheder is told it is not open to them yet.
- The mærkedage are the founders' birthdays, Gustav Adolf Hagemann's (16 May) and Mathilde Hagemann's (27 September), every year.
- Shortcuts for everyone: Alumneliste, Mine AK-krydser, Min ølkælder-saldo. Plus Ølkælder-admin for the Ølkælder role, AK-oversigt for the AK role and Ansøgninger for Indstilling.
- Shows the house WiFi password, the shared calendar account's login, and a link to change your own password.
- Shows "Dagens billeder": 3 photos from the photo album picked for the day, the same for everyone all day, and a photo is not picked again for a year. Below, "På denne dag": up to 6 photos taken on this date in earlier years, at most one per year before any year gets a second. Each opens in its album. The block is hidden when there is nothing to show.
- Asks "Få besked på denne enhed?" while this device is not subscribed to push (see Notifications).
- Opening the dashboard also books last month's AK charge if the nightly job missed it.

## Top bar
- A resident search (searches the alumneliste), a link to the public front page, the notification bell (see Notifications) and the resident's avatar.
- The avatar opens an account menu: Min profil, Notifikationsindstillinger, Skift kodeord and Log ud. Without JavaScript a plain Log ud button is shown instead.
- On a phone the top bar slides away while scrolling down and comes back on scrolling up, so the menu button is always within reach.
- While previewing, a banner names the previewed roles with a button to end the preview.

## Sidebar
- Grouped in sections; a section with no visible entries is hidden. The current page's entry is highlighted.
- **Oversigt** (everyone): Dashboard, Min profil, Den Hurtige, Opslagstavle, Begivenheder, Alumneliste, Fotoalbum.
- **Værelser**: Søg værelse and Værelsestjek for everyone; Værelsesudbud for Indstilling.
- **Grupper & konti**: AK-krydser and Ølkælder for everyone; AK-oversigt for AK, Ølkælder-admin for Ølkælder, Regnskab for Regnskab.
- **Værktøjer**: Reparationer for everyone.
- **Administration**: Ansøgninger for Indstilling; Rediger indhold for the website editors (Administrator, Indstilling, Inspektion, PR); Site-admin and Roller for Administrator.
- **Ressourcer**: Stamtræ, Statistik and Arkiv, then the external Wiki and "Fejl & ønsker" links, which open in a new tab.
- Den Hurtige, Opslagstavle, Begivenheder, Fotoalbum and Arkiv are currently open to every resident; if one is later limited to certain roles, its sidebar entry disappears for everyone else.
- On a phone the sidebar opens from a menu button.

## Decisions
- The WiFi password and calendar login are shown to any logged-in resident from anywhere; logging in replaced the old site's campus-network check.
- The dashboard doubles as a backstop for the monthly AK charge, so a missed nightly run fixes itself on the next visit instead of leaving every balance wrong.
- The agenda comes from Begivenheder rather than an embedded Google Calendar: the kollegium's own calendar is where events are made now.
- Notifications are not in the sidebar: the bell is where they are read and the avatar menu holds their settings, while the sidebar is for features.
- Roles shown and links offered follow the previewed roles, so the preview tool shows the real experience.
- Sidebar entries for features behind a rollout gate stay conditional even while open to all, so narrowing a feature again hides its link too instead of advertising a page that refuses access.
- Stamtræ and Statistik sit under Ressourcer, not Oversigt: Oversigt is what changes between visits, Ressourcer is for looking things up.
