# Feature: Stamtræ — the fylgje lineage of every resident, as one side-scrolling tree

**Feature ID:** F-011

## What it is, and what it replaces

Every newcomer at GAHK has a *fylgje* (fadder): the older resident who introduced them. Chaining
those links gives the kollegium's family tree. `/intern/stamtree/` draws it as one horizontal tree
under a synthetic root, «Hagemanns Ånd», with every resident who ever had a row in `Resident` hanging
somewhere beneath it. Each node is the resident's full name plus a two-digit move-in year (`'19`),
and links to their profile.

The PHP version stored the fylgje as a free-text name, matched names against names, and drew the
tree with a D3 v3 script fed by unescaped inline JSON. The rewrite replaces the name match with a
real foreign key and the D3 chart with server-rendered nested lists styled by CSS; there is no
JavaScript on the page.

## Access

| Action | Who |
| --- | --- |
| View the stamtræ | every logged-in resident (`@login_required`, no role check) |
| Set a resident's fylgje when creating them | `indstilling` (next-month list, "add new") |
| Change a resident's fylgje afterwards | `indstilling` (resident edit form) |

The page is listed in the sidebar under **Ressourcer**, not Oversigt
(`core.context_processors._nav_intern`): Oversigt is for things that change between visits; the
stamtræ is reference material you open to look something up.

## Routes

| URL | Method | Who | Purpose |
| --- | --- | --- | --- |
| `/intern/stamtree/` | GET | logged in | Render the whole tree (`residents.views.stamtree`, name `stamtree`) |

The legacy `/nyintern/stamtree/` is caught by the generic `nyintern/<rest>` → `/intern/<rest>`
permanent redirect in `app/config/urls.py`.

## Data model

Two fields on `Resident` (`app/residents/models.py`):

- `sponsor` — nullable self-FK, `on_delete=SET_NULL`, `related_name="proteges"`. This is the
  fylgje. Deleting a resident makes their protégés roots rather than deleting them.
- `fylgje_raw` — free text, kept for a fylgje that could not be resolved to a resident (the
  original legacy name string). It is shown on the profile and in the alumneliste when `sponsor`
  is empty, but **the stamtræ ignores it**: a resident with only `fylgje_raw` is drawn as a root.

There is no separate tree table; the tree is computed on every request.

## Building the tree

`residents.views._fylgje_forest()` loads every `Resident` in one query (`select_related("sponsor")`)
and groups them by `sponsor_id`:

- A resident whose `sponsor_id` is null, or points at an id not in the loaded set, is a **root**
  (a child of «Hagemanns Ånd»).
- Everyone else is a child of their sponsor.
- Siblings, and the roots, are ordered by `move_in_date` (missing dates first, via `date.min`),
  then first name, then last name.
- Recursion carries a `seen` set and skips any child already on the current path, so a cycle
  cannot recurse forever.

The result is a list of `{"resident", "children"}` dicts. The «Hagemanns Ånd» node is not data;
it exists only in `app/templates/stamtree/stamtree.html`, which wraps the forest in one root `<li>`.

All residents are included: current, moved out, inactive. Nothing filters on `move_out_date` or
`is_active`.

## Rendering

- `app/templates/stamtree/stamtree.html` widens the content area (`max-w-full`) and puts the tree
  in a horizontally scrollable card.
- `app/templates/stamtree/_node.html` renders one node and recursively `{% include %}`s itself for
  the children. The name is autoescaped by the template engine, closing the legacy stored-XSS hole.
- The connector lines are pure CSS (`.lineage` rules in `frontend/src/styles.css`): each `<ul>` is
  a flex column, children fan out to the right, and `::before`/`::after` pseudo-elements draw the
  horizontal stubs and the shared vertical bar. No collapse/expand, no zoom, no search.

## Recording the fylgje

The tree is only as good as the `sponsor` links, so the code makes them easy to set at the moment
the information is known:

- **At creation.** The "add new resident" action in the next-month list
  (`residents.views.next_month_list`, `action=add_new`) takes an optional `sponsor` id and saves it
  on the new `Resident`. An unknown or blank id leaves it null.
- **Afterwards.** `ResidentEditForm` (`app/residents/forms.py`) offers a non-required `sponsor`
  picker over every *other* resident (the resident being edited is excluded) plus the free-text
  `fylgje_raw` field.

Tests: `test_stamtree_shows_lineage`, `test_fylgje_can_be_set_when_the_resident_is_created`,
`test_creating_without_a_fylgje_still_works`, `test_the_create_form_offers_a_fylgje_picker`
(`app/tests/test_next_month.py`), `test_indstilling_can_edit_resident_core_data`
(`app/tests/test_resident_edit.py`).

## Decisions

- **Fylgje is set when the resident is created, not left for a later edit.** Whoever adds a
  newcomer knows who introduced them right then; a separate trip through the edit form "is exactly
  the step that gets skipped, leaving holes in the stamtræ" (comment in `next_month_list`; test
  docstring in `test_fylgje_can_be_set_when_the_resident_is_created`). It stays optional because
  the oldest residents in a lineage have none.
- **A foreign key, with the legacy text kept beside it.** `sponsor` replaces matching on name
  strings; `fylgje_raw` keeps the original value when it could not be resolved, so nothing is lost
  and indstilling can still fix it by hand.
- **CSS connectors instead of a charting library.** The `.lineage` comment in
  `frontend/src/styles.css`: connectors are pseudo-elements "so there is no JS/CDN dependency".
- **Unresolved and missing sponsors become roots** rather than being dropped (docstring of
  `_fylgje_forest`). The legacy code silently dropped some children and printed `FEJL!!`.

## Open questions

- **Cycles hide people.** The recursion is cycle-safe, but a cycle is never reached at all:
  residents only enter the tree through a root, and every member of a cycle (A→B→A, or a resident
  who is their own sponsor) has a sponsor in the set, so none of them is a root. They and all their
  descendants are missing from the page with no warning. `ResidentEditForm` prevents only a
  self-reference; longer cycles, and anything set through Django admin, are not validated.
- **`fylgje_raw` is invisible here.** A resident whose fylgje is known only as text appears as a
  root with no hint of the name. Should the node show it, or should there be a list of unresolved
  fylgjer for indstilling to fix?
- **No scale strategy.** The whole tree renders on every request with one recursive template
  include per resident. That is fine for a few hundred residents; it has not been measured with
  the full alumni history.
- **Should moved-out and inactive residents be distinguished** (e.g. greyed) rather than drawn
  identically to current residents?

## Note on other `F-011` citations

Some comments cite `F-011` for unrelated things: the admissions list and the dropped per-request
rundvisning mail (`app/admissions/views.py`, admissions tests in `app/tests/test_features.py`) and
the front-page visit counter (`app/stats/`, `VISIT_COUNTER_HMAC_KEY`). Those are not part of the
stamtræ; the visit counter is described under F-013 and the admissions list belongs with F-001.
