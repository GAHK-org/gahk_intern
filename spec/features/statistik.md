# Statistik

An internal statistics page on applications and front-page visits, plus the counter that records those visits.

## Access
- Every logged-in resident can view `/intern/statistik/`; it is linked under *Ressourcer* in the sidebar.
- The page is read-only, and its data is not available anywhere without logging in.

## The page
- *Ansøgninger fordelt på type*: a pie of tour (Rundvisning) versus sublet (Fremleje) applications on file. Applications are deleted after a year, so this covers roughly the last twelve months.
- *Hvor hørte de om os?*: a pie of where this year's tour applicants heard about the kollegium; blank answers are left out.
- *Rundvisninger pr. universitet*: the top 15 universities among this year's tour applicants.
- *Besøg pr. dag*: a line chart of front-page visits for the latest 30 days that had visits.
- "This year" is the current calendar year.
- Pie tooltips show percentages.
- A panel with no data shows a short message instead of an empty chart.

## Front-page visit counter
- Only successful views of the public front page are counted; no other page is.
- A visitor is counted again only if 30 minutes or more have passed since their last counted visit.
- Visitors are identified by a keyed hash of their IP address; raw IPs are never stored.
- Counts are kept indefinitely.
- A failure in the counter never affects the front page.

## Decisions
- The data is only ever rendered into the logged-in page, with no separate data feed that could be left open.
- Visits are counted after the page is built, so the counter can never delay or break the front page.
- IPs are hashed so visitor counts can be deduplicated without storing personal data.
