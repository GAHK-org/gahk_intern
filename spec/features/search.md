# Søg — the site-wide search

The search field in the top bar searches the whole intranet, not only the alumneliste. Typing
shows suggestions under the field; Enter opens every result at `/search/?q=…`.

## What it finds

Each kind of result is a section, in this order:

- **Sider**: the intranet's pages and tools that this reader's sidebar shows, plus the account
  pages (Rediger profil, Notifikationsindstillinger, Skift kodeord, Installér appen). A page is
  found by its name or by other words for it ("kalender" finds Begivenheder, "øl" finds the
  ølkælder), with or without æ/ø/å ("oelkaelder" works too).
- **Alumnelisten**: residents on this month's list, by name, study, e-mail, embedsgruppe or room
  number. They link to their profile.
- **Opslagstavlen**: posts, using the board's own search (Danish word forms, parts of compound
  words, the author's name). The result shows the words around the match.
- **Begivenheder**: events that are not over yet, by title, description or place.
- **Fotoalbum**: albums by name, tag or year.
- **GAHKFlix**: published films, using GAHKFlix's own search.
- **Reparationer**: tickets by title, place, description or reporter. Open tickets come first,
  then archived ones.
- **Hjemmeside**: gahk.dk's pages by heading or address. They open in the editor.

Every word typed must match. The search starts at two characters. Matches are highlighted.
The suggestions show up to 4 results per section, and the results page shows up to 10. Where the
feature has its own search (alumnelisten, opslagstavlen, GAHKFlix, reparationer), a link opens it
with the same words.

## Who sees what

- A result only shows what the reader could open on the feature itself:
  - The pages follow the sidebar's roles.
  - Private events are found only by their organisers and guests.
  - GAHKFlix follows its rollout gate.
  - Hjemmeside results are only for its editors (administrator, indstilling, inspektion, pr).
  - A feature closed to the reader leaves out its whole section.
- The admin preview applies: viewing the site as a beboer searches as a beboer.
- The kitchen account searches too, but finds only what its gate lets it open: its menu's pages,
  opslag, reparationer and notification settings. No alumner, events, albums or films.

## Keyboard

- `/` focuses the field from anywhere outside a text field.
- The arrow keys move through the suggestions. Enter opens the highlighted one, or every result
  when none is highlighted. Escape closes the suggestions.
- On a phone the top bar has no room for the field, so a magnifier opens the results page.

## Decisions

- **Each feature decides what its results may show.** The search asks the feature's own rules
  (the sidebar builder, the event list's visibility, the rollout gates) rather than keeping its
  own. A result's title is already the leak, so a search that drifted from the feature's rules
  would show what the feature hides.
- **No search index.** Postgres answers each source in milliseconds at the kollegium's size, and
  opslagstavlen already has its full-text indexes. A separate search server would be another
  service to run and keep in sync.
- **Alumnelisten and reparationer now match word by word**, the same way as here. So
  "anders hansen" finds Anders Hansen on the list itself too.
- **Not searched**: Den Hurtige, which is a chat and not something to look up later, and the
  Arkiv, which the Drive replaces.
