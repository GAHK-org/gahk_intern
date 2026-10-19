# Stamtræ

The kollegium's family tree: every resident placed under their fylgje, the older resident who introduced them.

## Access
- Every logged-in resident can view it (under Ressourcer in the sidebar).
- Indstillingen records the fylgje, either when creating a new resident on the monthly list or later in the resident's core data.
- A fylgje that would form a loop (a resident ending up as their own fylgje, directly or through others) is refused, in the core data and in Django admin.

## The tree
- One tree that grows to the right and scrolls sideways, starting from a single root, «Hagemanns Ånd».
- Each person is shown with their full name and two-digit move-in year (e.g. '19), and links to their profile.
- Everyone who has ever been a resident is included, whether they live here now, have moved out or are inactive.
- Residents with no linked fylgje sit directly under «Hagemanns Ånd». That includes residents whose fylgje is known only as free text.
- Residents caught in a fylgje loop from older data are still shown: the loop is broken at one of them, who sits directly under «Hagemanns Ånd».
- Siblings are ordered by move-in date (unknown dates first), then by name.
- The tree is static: no collapsing, zooming or searching.

## Decisions
- The fylgje is asked for when a new resident is created, because a separate edit afterwards is the step that gets skipped and leaves holes in the tree. It stays optional because the oldest residents in a lineage have no fylgje.
- A fylgje that could not be matched to a resident is kept as free text instead of being thrown away, so Indstillingen can still link it by hand later.
- Residents whose fylgje is missing, unknown or part of a loop are shown at the top level instead of being dropped.
- The connecting lines are drawn with plain CSS, so the page needs no JavaScript or charting library.
