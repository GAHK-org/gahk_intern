# Feature: The GAHK Intern app — installing the intranet on the home screen

The intranet can be installed as a web app. On the home screen it is called **GAHK Intern** and
opens on Den Hurtige. Push notifications on a phone (iPhone in particular) need the installed app.

## Why everyone installs it again

- The app used to be installed from gahk.dk/intern/ as "Den Hurtige". An installed web app belongs
  to the site it was installed from and cannot follow the intranet to intern.gahk.dk, so residents
  install it again from intern.gahk.dk and turn notifications on again there.
- The old app on gahk.dk shows a notice that points to intern.gahk.dk/install (built in gahk_web).

## The install page (/install)

- Open to everyone, logged in or not: the old app sends people here, and they are not logged in on
  the new address yet. The kitchen account may open it too.
- Steps for iPhone/iPad (Safari: Del → Føj til hjemmeskærm → Tilføj; from the old app, open the page
  in Safari first), Android (Chrome's menu ⋮ → Installér app / Føj til startskærm) and a computer
  (the install icon in Chrome/Edge's address bar, Arkiv › Føj til Dock in Safari).
- The section for the device in hand comes first and open; the others stay reachable below.
- Where the browser offers its own install prompt (Chrome, Edge, Samsung Internet), an "Installér
  appen" button opens it; the manual steps stay below it.
- Inside the installed app the steps give way to "Du bruger allerede appen".
- After installing: log in in the app, turn notifications on again (Den Hurtige or the
  notification settings), and delete the old "Den Hurtige" app.

## The banner

- "Installér GAHK Intern som app på din telefon — Sådan gør du" at the top of the intranet's pages,
  linking to /install.
- Only for a logged-in resident on a phone or tablet, in the browser rather than the installed app.
  Never on a computer, on /install itself, in GAHKFlix, on the ølkælder till or for the kitchen
  account.
- Closing it hides it on that browser for longer each time: 1 day, then 3 days, then 30 days.
- A close within 7 days after a 30-day hide ended hides it for another 30 days; a later close starts
  over at 1 day (then 3, then 30).
- Where the browser keeps no site data, it shows on every page, and closing it hides it on that page
  only.

## Decisions

- **The name is GAHK Intern, not Den Hurtige.** The app is the whole intranet; it only opens on Den
  Hurtige. Everyone reinstalls anyway, so this was the moment to rename it.
- **The app's identity (its start page, /den-hurtige/) is unchanged.** Changing it would make
  every installed copy a different app once more.
- **The page is public.** It holds no residents' data, and asking for a login first would stop the
  people it is for.
- **The kitchen account may read it but gets no banner.** A public page should not answer 403 to one
  account, and the kitchen tablet may carry the old app itself; but the kitchen's shared screen is
  not nagged to install anything.
- **The banner appears only after the page has checked the device**, so a computer never sees it
  flash.
- **Escalating hides rather than one long one.** A first close is often "not now"; a resident who
  keeps closing it is asked less and less, and one who closes it promptly after every 30 days has
  clearly decided, so stays at 30.
