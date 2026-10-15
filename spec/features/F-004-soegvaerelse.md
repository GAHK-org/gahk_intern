# Feature: Søg værelse — the room-application lottery (kvotient, priorities, room offers)

**Feature ID:** F-004

## What it is

When a room becomes free, Indstillingen puts it *i udbud* (creates a room offer) for next month.
Current residents apply with a ranked list of the offered rooms they want, plus when they expect
to finish studying and how many months of orlov (leave) they have had. From that the site computes
a *kvotient* `K`; the highest K gets their highest-priority free room. Indstillingen watches the
live projection, settles exact K-ties by the dorm's coin flip, and ends the round, which writes the
result straight onto next month's alumneliste (F-010).

At `/intern/soegvaerelse/`, namespaced `soegvaerelse:`. Code: `app/rooms/views_soegvaerelse.py`,
`app/rooms/kvotient.py`, `app/rooms/models.py`, templates in `app/templates/soegvaerelse/`.

**What it replaces.** The PHP controller of the same name. Its applications, priorities and offers
were not migrated — the module docstring calls this a "fresh-start build" because the legacy data
had already been destroyed by a cascade-delete bug in `closeOffer`. The K formula and the
0-indexed month numbering were kept deliberately.

## Access

| Action | Who |
| --- | --- |
| Apply / edit own application, see own K, list own applications | every logged-in resident |
| Withdraw an application | its owner only (the lookup is scoped to `current_resident`) |
| View an application's detail page | its owner, or `indstilling` |
| Offer admin: create/close offers, see applicants, resolve ties, end round | `indstilling` |

`administrator` and superusers pass every `indstilling` check through the all-access rule in
`residents.permissions`. Checks use *effective* roles, so the preview tool applies. The sidebar
shows "Søg værelse" to everyone and "Værelsesudbud" only to `indstilling`
(`core.context_processors._nav_intern`).

## Routes

| URL (under `/intern/soegvaerelse/`) | Method | Who | Purpose |
| --- | --- | --- | --- |
| `` | GET, POST | resident | Kvotient calculator + application form (`soeg`) |
| `mine` | GET | resident | Own applications (`my`) |
| `kvotient` | GET | resident | htmx fragment: live K for the form's inputs |
| `ansoegning/<pk>` | GET | owner or indstilling | Application detail |
| `ansoegning/<pk>/slet` | POST | owner | Withdraw application |
| `admin` | GET | indstilling | Offers, live leader per room, last round's summary |
| `admin/opret-tilbud` | POST | indstilling | Create an offer (room number) for next month |
| `admin/afslut-runde` | POST | indstilling | End the round (see below) |
| `admin/tilbud/<id>/ansoegere` | GET | indstilling | Ranked applicants for one offer |
| `admin/tilbud/<id>/luk` | POST | indstilling | Close one offer |
| `admin/tilbud/<id>/afgoer` | POST | indstilling | Record the coin-flip winner of a tie |

## Data model

- `KvotientApplication` — resident, `move_month` (the round's target month), `move_in_month`,
  `done_studying_month`, `k` (float, computed at submit), `apply_datetime`. Unique on
  `(resident, move_month)`: **one application per resident per round**. Default ordering
  `-k, apply_datetime`, but `apply_datetime` is explicitly *not* an allocation tiebreak.
- `KvotientPriority` — application, room, `priority` (1 = first choice). Unique on
  `(application, priority)`. Also stores a legacy `month` column ("kept for fidelity").
- `KvotientOrlov` — application, `start_month`, `end_month`; `number_of_months` is the difference.
  The form only ever writes one row, starting at the target month and spanning the declared count.
- `RoomOffer` — room, `month`, and `awarded_application` (nullable, `SET_NULL`) for a manually
  resolved tie. Unique on `(room, month)`.

**Months are absolute 0-indexed integers:** `month_index(year, month) = year*12 + (month-1)`
(`rooms.kvotient`). The `maaned` template filter (`rooms/templatetags/room_extras.py`) renders them
as "Måned ÅÅÅÅ".

## The kvotient

`K = a·100 / (a + b + 12)`, rounded to two decimals; `0.0` if the denominator is not positive.

- `a` = months from move-in to the target month, minus orlov months.
- `b` = months from the target month to the study-end month.

`compute_k_parts` is the single source of truth and returns `a`, `b` and `k` so the page can *show*
the formula, not just the result. The live preview (`kvotient` view) is computed server-side
against the resident's real `move_in_date` so no formula is duplicated in JavaScript. It is
reachable whether or not any room is on offer, so a resident can check their K at any time. Without
a `move_in_date` the page says to contact Indstillingen and submitting is refused.

**Suggested orlov** (`computed_orlov_months`): every month between the resident's move-in month and
the active period in which they have no `Residency` row counts as leave. It pre-fills the field for
a new application (the resident may change it), and the detail page shows Indstillingen the
declared value next to the computed one, highlighted when they differ.

## Applying

The target month is always `next_period()` — the month after the active alumneliste month
(`residents.models.active_period`) — and the form has no month input. The form offers five
priority selects listing every current offer's room; at least one priority is required.

Submitting is one transaction: `update_or_create` on `(resident, move_month)`, then priorities and
orlov are deleted and rewritten wholesale. Re-submitting therefore **edits** the application and
keeps the original `apply_datetime`. The form pre-fills from an existing application.

## Allocation (`allocate_round`)

A global greedy port of the legacy `wonRoomAlgorithm`, run per offer month:

1. Offers with an `awarded_application` are honoured first; that resident is placed.
2. All priorities for offered rooms are walked in order `-k, priority`. Each unplaced resident gets
   their highest-priority still-free room; each resident wins at most one room.
3. **Ties are contested, not resolved by time.** If the leading contender for a free room has an
   equal-K rival (another unplaced resident who also listed it), the room is marked contested, and
   *all* tied residents are held out of the rest of the run, so nobody tied on their first choice
   is cascaded into a lower choice before the coin flip.

The admin page and the applicants page call it live, showing the current leader (🏆) or the tie
(⚖️). `resolve_tie` stores the chosen application on the offer; re-running can then relieve other
contests.

## Ending a round (`end_round`)

For every month that has offers, in one transaction:

1. **Carry the roster forward** (`_carry_roster_forward`): everyone on the previous month's list who
   is not already on the target month's list is copied with room, embedsgruppe, rengøring and all
   their role assignments. Non-destructive towards rows already present. Without this, every
   resident not in the round would vanish when the target month became active.
2. **Place the winners**: `Residency.update_or_create` keyed on `(resident, year, month)`, which
   *moves* a winner's carried-forward row into the won room.
3. **Evict departing occupants**: anyone still in a won room who is not a winner is removed from the
   target month, along with that month's role assignments. Done after all placements so a winner
   relocating out of another won room is never deleted ("musical chairs").
4. **Clear**: with no contests, all applications and offers for the month are deleted. Otherwise
   only settled offers and the winners' applications are removed, so contested rooms can be
   resolved and the round run again.

A summary (moves "from → to", new residents, leavers removed, rooms still tied) is stored in the
session and shown once on the admin page.

**Closing a single offer** (`close_offer`) deletes the offer and every application *for that month*
that listed the room — the whole application, including its priorities for other rooms.

## Dev-only clock

`active_period` reads the date via `core.clock.current_date`. Under `DEBUG` only, a `DevClock` row
can override "today" (`siteadmin:dev_clock_set`, a bar shown to logged-in users by
`core.context_processors.navigation`) so month rollover can be tested locally. In production the
override is never read and the view returns 404.

## Decisions

- **Submission time does not break ties.** The kollegium settles equal K by coin flip; the code
  records Indstillingen's choice instead of inventing an order (`allocate_round` docstring).
- **Tied residents are held, not cascaded**, so the coin flip decides their first choice rather than
  the algorithm quietly giving one of them a worse room.
- **The target month is never chosen by hand**, for both offers and applications: always the next
  month, which matches how the dorm runs rounds.
- **`close_offer` is scoped to the offer's month** — the legacy delete spanned all months.
- **End-of-round writes the alumneliste directly**, with the carry-forward and eviction steps above,
  because a round that only wrote winners emptied next month's list.

## Open questions

- **Submitted priorities are not validated against the current offers.** `soeg` accepts any room id
  from POST: a room that is not on offer is stored and silently ignored by `allocate_round`; a
  non-existent id would fail the foreign key. The same room may also fill several slots.
- **Negative orlov is accepted on submit.** The live preview clamps it at 0, but `soeg` passes
  `orlov_months` straight to `compute_k`, so a crafted negative value inflates `a` and therefore K.
  A non-numeric value raises `ValueError` (500).
- **Stale offers.** The apply form lists offers of every month, but applications always target
  next month. If a round is not ended before the month rolls over, residents can pick rooms whose
  offers belong to the old month, and those priorities never count.
- **`resolve_tie` does not check that the chosen application is one of the tied ones**, or even that
  it listed the room — only that it is for the offer's month. The UI only offers tied applicants.
- **Closing an offer removes applicants from every other room too.** Inherited from legacy; worth
  confirming that this is still wanted.
