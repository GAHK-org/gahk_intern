# Design: Køkkenvagter P2 — preferences, tier-B, verification, UI

**Status: approved and implemented** (2026-09-28; code at `bdc01ad` after three review/fix cycles).
**The rollout is planned but has not happened yet — see §13, which is live until February 2027.**
Phase design; the architecture it sits inside is
`2026-09-21-koekkenvagter-design.md` (approved, with Amendments 1–3 approved and implemented, and
Amendment 4 an approved direction for a later phase). Read that first — this document assumes it and
does not restate it.

P1 shipped headless: models, services, access gate, management commands, no views or templates. P2 is
the first UI, and it also completes the allocation story by adding tier-B.

## What P2 covers

Tier-B (aftenvagt) allocation; the full preference model and its collection window; the
verification/flagging workflow; the kitchen tablet; and the resident, Køkkengruppen and Regnskab
screens. Out of scope: the summer period and holiday-weeks input (P3), and swapping (Phase 2b,
Amendment 4).

## 1. Corrections to earlier decisions

Two things settled earlier are superseded, and are recorded here rather than left for a reader to
trip over.

**Multi-assignment is not broadly mandatory.** The "no vagter left open" rule was read as forcing the
allocator to hand out second and third shifts. That was an overstatement. The 104–116 monthly slots
span **two tiers**, and one-per-tier gives a ceiling of 61 + 61 = 122, comfortably above 116 — so in
the baseline case **no resident needs more than one tier-A and one tier-B slot**. The 1.7–1.9
slots-per-resident figure is *across* both tiers, not within either. Forced doubling is real but
narrow: at most **one** weekday tier-A slot per month, in months where ~20 residents declare
`weekday_unavailable` and the weekday pool runs one short of takers — plus routinely, by choice, for
residents who opt into the aftenvagt-avoidance pattern (§4). The accurate statement of the original
decision: the allocator must be *willing* to assign a second slot where the numbers require it.

**The kitchen tablet is not read-only.** The original UI section specified a read-only display.
Marking a shift done now requires physical presence at the tablet, so it is an interaction point. See
§5.

## 2. Preferences

Four inputs per resident per periode. **Exactly one is a hard constraint; the other three are soft.**

| field | kind | meaning |
| --- | --- | --- |
| `weekday_unavailable` | **hard** | unchanged from P1. Routes to the weekend tier-A pool; the resident is never assigned a weekday morgen/frokost. |
| morgenvagt days | soft | which weekdays they would prefer for morning duty |
| frokostvagt days | soft | which weekdays they would prefer for lunch duty |
| aftenvagt days | soft | which weekdays they would prefer for evening duty |

**Why only one hard constraint**, since three of these look alike and could each have been an
availability declaration. There are 20–23 weekday morgen slots and 20–23 weekday frokost slots a
month against 43–45 weekday-available residents, so **49–51% of them must take a weekday morgenvagt
every month**. A hard "I can't do morgen" becomes infeasible the moment more than about half declare
it — and an early morning is precisely the shift a majority would decline if simply asked. Under the
no-slots-left-open rule the system would then assign it anyway, so we would have collected a
declaration, promised nothing, and overridden it routinely. That is worse than not asking: it teaches
residents the form is theatre. `weekday_unavailable` stays hard because the capacity argument behind
it (the weekend pool is a mandatory overflow of 15–21 people, sized 16–20) was built to guarantee
exactly that one promise. Aftenvagt never binds at all: 48–54 slots against 61 eligible residents.

**How a soft preference actually enters the decision** — this is the rule that keeps them meaningful
without touching fairness:

> The **ranking** decides *who* is picked next. The **preference** decides *which* of the available
> slots that person is handed.

Fairness ordering is unchanged from Amendment 1: projected balance ascending, ties by `declared_at`,
then pk. Preferences never reorder the queue. The single exception is the already-approved weekend
compensation, which does move weekend-tier-A assignees up the order for aftenvagt specifically.

### Storage

A related model, not columns:

```
PraeferenceDag(praeference, kind, weekday)   unique (praeference, kind, weekday)
```

`kind` reuses the existing `VagtRegel.Kind`; `weekday` is 0–6.

Separate per kind rather than one combined per-weekday structure, because a "for each weekday choose
morgen/frokost/either/neither" shape cannot cleanly express *"I would take Tuesday morning **and**
Tuesday lunch"* — "either" weakens it into something else — and it does not extend to aftenvagt, so
it would force one shape for two types and a different shape for the third.

A related model rather than three list columns because it keys off the enum this feature already has
(one code path instead of three named after the kinds), it gets DB-level validation from choices plus
the unique constraint, and the allocator's real query — "which days does this resident prefer for kind
K" — is one filtered prefetch. Volume is trivial: at most 61 × 3 × 7 rows per periode.

Checked against house convention rather than taste. This repo has **no `ArrayField` anywhere** despite
running Postgres, so that would be a new dependency unlike anything here. It does use
`JSONField(default=list)` (`photo_album.media_ids`, `oelkaelder.price_steps`), so three JSON fields
would have been defensible — passed on because a JSON list of ints gets no validation at all (nothing
stops `[99]` or `["mandag"]`), and because naming columns after the three kinds sits awkwardly beside
a design that deliberately made the vagt rules a table rather than constants. A bitmask was rejected
outright: unreadable in the admin and in a shell, for no gain at this size.

## 3. The preference window

**One window, ~1 week, collecting all four inputs, ending at the periode deadline that already
exists.** `services.periode_deadline()` is exactly two calendar months before a periode starts — 1
July for Sep–Jan, 1 December for Feb–Jun, 1 May for Jul–Aug.

This is not a new schedule. Taking the stated intent literally — distribution one month before the
target month, a window opening "1 month + 1 week" before that and running a week — puts the window's
close on 1 July for a September distribution. **That is the built deadline, to the day.** The window
is the week leading up to a deadline the code already computes.

Everything Amendment 1 built about locking therefore stands untouched: preferences for a periode are
editable until its deadline, freeze there, and a later edit is written to the *following* periode's
row. A resident with no row for the current periode may still **create** an initial one at any time
(Amendment 3), affecting only months not yet allocated.

**Lead times are unchanged.** The built schedule publishes each month two to four months ahead — the
deadline batch takes the periode's first three months, the monthly roll-forward takes the rest. The
requirement was *"at least 1 month"*, which this already exceeds, so nothing is torn out.

## 4. Allocation: tier-B, and no slots left open

Tier-B is **not** a claim-based UI. There is no live claiming, no first-come signup, and no separate
"auto-assign the leftovers" deadline. It is a preference round followed by a single algorithmic
decision, the same fundamental shape tier-A already has.

The monthly pass becomes: **tier-A, then tier-B, in one run**, with tier-A's outcomes feeding tier-B's
ordering (the weekend compensation mechanic). Every slot in both tiers is filled. A resident who
declared nothing is ranked and seated like anyone else, with no day preference to honour, sorting last
on ties — forced by the no-slots rule and consistent with Amendment 3's treatment of undeclared
tier-A.

**`weekday_unavailable` must NOT restrict aftenvagt eligibility.** The flag means "not home during
early-to-afternoon on weekdays" — that is morgen and frokost. Aftenvagt is the evening, and someone
out all day at work or lectures is precisely the person who *can* do it. Carrying the flag across
would exclude that entire group from evening shifts for no reason. Tier B therefore has no eligibility
constraint of its own and is never capacity-binding.

### The aftenvagt-avoidance pattern, and its cap

Selecting several morgen/frokost days while leaving aftenvagt **empty** is a deliberate signal: *I
would rather cover my hours with more morning and lunch duty than take an evening shift.* It is a soft
preference like any other — it cannot be a veto, because the aftenvagt slots still need bodies.

It has a capacity cap nobody had computed. Reaching the 3.42 h/month target using only 1-hour shifts
takes about 3.4 of them, and tier A holds only 56–62 slots a month, so **the pattern balances on hours
for at most ~17 residents — roughly 28% of the house.** Beyond that, tier-A slots are exhausted and
the arithmetic stops working:

| residents opting in | everyone else gets | their hours | |
| --- | --- | --- | --- |
| 0 | 0.98 tier-A + 0.85 aften | 3.41 h | balanced |
| 10 | 0.51 + 1.02 | 3.41 h | balanced |
| 17 | 0.05 + 1.18 | 3.41 h | balanced |
| 20 | — | — | **infeasible** |

Over-subscription needs **no refusal mechanism**: a soft preference is best-effort by definition, so
the allocator honours as many as capacity allows, ordered by the same ledger ranking as everything
else, and the rest simply get an aftenvagt that month. This is also the one place where
multi-assignment within tier A is routine rather than exceptional (§1).

**Worth saying to residents plainly in the UI, because it is what they are choosing:** leaving
aftenvagt empty means roughly **one morning or lunch shift every week** instead of a single evening.
Same hours, a very different rhythm. Nobody should discover that cadence by being assigned it.

**Distinguishing an explicit avoidance signal from someone who never engaged** needs no new field. It
is derivable from what Amendment 1 already built:

```
declared_at IS NOT NULL      (they engaged with the form at all)
AND >= 1 morgen-or-frokost day selected
AND 0 aften days selected
```

The middle clause is the discriminator: someone who declared and selected nothing anywhere is
genuinely no-opinion, not avoidance. Someone who never declared has no `declared_at` and sorts last on
ties, unchanged.

## 5. Verification: marking done at the tablet

**Marking a shift done requires physical presence — it works only from the kitchen tablet.**

### Identification

**Tap the button on your own row. No PIN, no login.** The tablet already lists today's shifts with the
responsible person's name against each, so there is no separate identification step to design, and the
candidate list is the handful of people on duty today rather than all 61.

This matches the ølkælder till exactly, which identifies buyers by tapping a name from a list —
`oelkaelder/views.py:purchase` reads `request.POST.getlist("shopper")` and the template renders each
shopper as a tappable row. No credential of any kind, **for real money**. Requiring a PIN here would
apply more security to marking a shift done than this house applies to buying beer, and would add a
credential someone must issue, forget and reset.

The threat model, stated rather than assumed:

- Marking **someone else's** shift done gives *them* credit — a benefit, so there is no malice
  incentive. The till is strictly worse here and the house already tolerates it: tapping someone
  else's name there *charges* them.
- Marking **your own** shift done without doing the work — tapping and leaving — is real, and physical
  presence does not prevent it. Being in the room is not evidence of having cleaned. That case is what
  flagging is for (§6), so it is covered rather than ignored.

Net: this tablet is less abusable than the till already in service, with no new credential.

### The marking window

Opens at **the shift's own start time**, closes at **the end of the following day**:

| kind | opens |
| --- | --- |
| morgenvagt | 06:00 |
| frokostvagt | 12:00 |
| aftenvagt | 17:00 |

Not midnight-of-that-day: a tablet that lets you mark a morning shift done at 00:05 is a worse honour
system than one that waits until the duty has actually begun. Not in advance, for the same reason.

**Store the start time as a column on `VagtRegel`.** That table already holds the per-kind hours and
headcount and exists precisely so the rules are officer-editable data rather than constants; a start
time is the same kind of fact. **Read it live — do not snapshot it onto `Vagt`.** Headcount *is*
snapshotted, deliberately, because it is an allocation input and editing a rule must not retroactively
change a published month. A start time is an operational value that should track reality: if the
kitchen moves lunch duty to 13:00, future shifts should follow, and past shifts are unaffected because
their window was evaluated when the button was tapped. The distinction is worth a comment in the code,
because the snapshot invariant is documented nearby and someone will otherwise apply it reflexively.

Times are local wall-clock (Europe/Copenhagen). Compare against `core.clock.current_datetime()`, never
`django.utils.timezone` directly.

## 6. Flagging and adjudication

**One uniform action:** *"denne vagt blev ikke udført"*, available on any shift dated today or earlier,
whatever its status. One button, one queue. Only the ledger consequence differs:

| flagged shift was | upheld ruling does |
| --- | --- |
| `UDFOERT` | reverses the credit with a `TILBAGEFOERSEL` entry — never a delete |
| still `TILDELT` | nothing on the ledger; no credit was ever posted |

Making it uniform dissolves a question that would otherwise need answering — whether disputing a
claimed completion is still worth building now that physical presence is required. It is still a real
case (tapping and leaving), and keeping it costs nothing, because it is the same button and the same
queue.

**What a flag accomplishes when it reverses nothing.** It is an **operational alert, not an accounting
event**. The ledger already handles the fairness consequence by itself and silently: no report, no
credit. What the ledger cannot do is tell anyone **the kitchen is dirty right now**. That is the whole
job of a flag on an unreported shift — it reaches Køkkengruppen today, while it still matters, rather
than surfacing in an end-of-month sweep when the only thing left to decide is bookkeeping. This
feature exists because *"sometimes tasks are not taken and the kitchen is not getting cleaned"*; the
flag is the part that closes that loop, and it was never really an accounting mechanism. Secondary
benefit: a pattern of flags against one resident is visible to Køkkengruppen even though each
individual flag is ledger-neutral.

A flag carries an optional free-text reason — an adjudicator with no idea why something was flagged
cannot do much with it.

**Flagger identity is fully visible, including to the flagged resident.** One consequence is recorded
here without arguing it: in a 61-person dorm where everyone knows everyone, this will suppress
flagging — people will not accuse a neighbour over a dirty counter. The cost is bounded rather than
fatal, and it is worth knowing why: the **automatic** side still catches the accounting case without
anyone having to accuse anybody, since an unreported shift is already uncredited. The two mechanisms
split cleanly — the silent sweep keeps the ledger honest, the visible flag handles urgency. If
flagging goes unused, the ledger is unaffected; only the "get it cleaned today" path degrades.

The assignee is **notified** when a ruling goes against them, because it reverses credit and that
credit is money at move-out. Nobody should discover that only when they leave. This uses the existing
`core.push` per-topic rails (explicit per-topic consent, defaulting off) — a new køkken topic, not a
new mechanism.

## 7. UI surfaces

Four audiences. Every view carries the rollout gate, including partials.

**Resident** — my shifts (upcoming and past), my balance shown against the house average, the
preference form, and a page explaining what my preferences bought me. Nothing here is a claim action;
allocation is not interactive.

**Køkkengruppen** — the flag queue (open flags, with reason and flagger, and an uphold/dismiss control
pair); an "ikke rapporteret" list of past shifts nobody marked done, which is the silent-sweep half of
§6; allocation run and preview; and overrides.

**Regnskab** — a read-only balance export for the move-out penalty.

**Kitchen tablet** at `/intern/koekken/idag/` — today's shifts, who is responsible, and a
mark-done button per row inside its window (§5). IP-gated through a new `KOEKKEN_KIOSK_IPS`, reusing
the `_client_ip`/`_is_kiosk` pattern from `oelkaelder/views.py` — **the last hop of
`X-Forwarded-For`**, safe only because gunicorn is reachable solely via the proxy. Duplicate those two
helpers with a comment pointing at the ølkælder original rather than extracting to `core`: this is the
second copy, and `core/rollout.py`'s docstring documents the house rule of copying at n=2 and
extracting at n=3.

**Not `kiosk_base.html`** — that is deliberately iOS-10/WebKit-602-safe for the till's ancient iPad.
The kitchen tablet is a modern device, so this page uses the normal `base.html` and frontend stack,
following `den_hurtige/feed.html`: a `{% block viewport %}` override, a `{% block body_class %}`, and
its own section in the stylesheet.

## 8. Prompts

Two, because they differ in urgency.

**The preference window** gets a site-wide banner, extending the proven `base.html` mechanism (a third
conditional block plus a context-processor key) to a resident audience for the first time. A one-week
window that comes round two or three times a year and governs five months of shifts justifies
something hard to miss.

**A resident who has never declared** gets a todo card on the dashboard. That is a standing task rather
than a deadline, and the dashboard already has the layout for it.

## 9. Access and rollout

Køkkengruppen for overrides and flag adjudication; Regnskab read-only on balances; `administrator`
implies everything; `indstilling` continues to own the monthly `Residency` list. Role tuples as
documented module constants in `views.py`, with **both** a template gate and a view re-check — the
template never renders a button you may not press, *and* the view refuses a replayed POST. Permission
booleans are computed in the view and passed into the template, because a Django template cannot call
an access predicate with arguments.

**Resolved — see §13.** `access.py` now sets `ACCESS_ROLES = None`: the gate opened to the whole
house on 2026-09-30, at the stakeholder's explicit direction, ahead of the originally-planned §13
phase-3 date (~20 Nov 2026). It was `(Role.KOKKENGRUPPE,)` through phase 1's dry run, matching this
section's original open item — every P2 surface except the tablet is for ordinary residents, and
under that narrower gate the preference round had almost nobody to collect from and the ledger
nothing to balance.

## 10. Conventions this must follow

- **One shared context builder** per partial, used by both the full page and any htmx swap, so the
  landed page and the swapped fragment can never disagree (`events.views._rsvp_context`). An htmx POST
  returns the re-rendered partial, not JSON, and not a redirect — *"an htmx swap has nowhere to show a
  redirect"*, so no messages framework on htmx actions.
- **A closed or out-of-window action removes its button rather than disabling it**, and a test asserts
  the action URL is absent from the page. A greyed-out button with a live POST target behind it is a
  lie.
- Filters and queue views are **plain GET**, deliberately not htmx, so they stay bookmarkable.
- `frontend/src/koekken.ts` plus one side-effect import line in `main.ts`, with a root-selector no-op
  guard so it does nothing on every other page, and server-computed `data-can-*` flags so the script
  wires up nothing at all for someone who may not act. htmx CSRF is automatic site-wide.
- Templates are project-level (`app/templates/koekken/`), `_`-prefixed partials, forms hand-rendered
  field by field with Danish labels and no `label_tag`.
- Sidebar entry in `_nav_intern()` in `core/context_processors.py`, where line 125 already carries the
  marker *"kokkengruppe: no dedicated screen today (documented gap; no menu item)"*.
- Danish for everything user-facing; English identifiers; a module docstring on each new file stating
  what it is for and which alternative was rejected.

## 11. Tests

- A hard `weekday_unavailable` declarer is never assigned a weekday morgen/frokost; a soft day
  preference is honoured when capacity allows and silently overridden when it does not.
- Preferences do not reorder the queue: given two residents, the one with the worse projected balance
  is picked first regardless of preferences; the preference only changes which slot they receive.
- Tier-A runs before tier-B, and a weekend-tier-A assignee outranks a weekday assignee for aftenvagt.
- After a run, **no slot in either tier is unfilled**.
- An undeclared resident is still assigned.
- `weekday_unavailable` does **not** exclude anyone from aftenvagt.
- The avoidance signal is recognised only with `declared_at` set, ≥1 tier-A day and 0 aften days; a
  resident who declared nothing anywhere is treated as no-opinion, not avoidance.
- Marking done is refused before the kind's start time, accepted after it, and refused after the end of
  the following day — one test per kind, plus a `DevClock` walk across the boundary.
- Marking done from a non-whitelisted IP is refused; from a whitelisted one it succeeds without login;
  the last-hop `X-Forwarded-For` rule holds (mirror `test_features.py::test_oelkaelder_kiosk_gate_uses_forwarded_ip`).
- Flagging an `UDFOERT` shift and upholding it writes a `TILBAGEFOERSEL` and leaves the original row
  intact; flagging a `TILDELT` shift and upholding it writes no ledger entry at all.
- Out-of-window and unpermitted actions render no button and no action URL.
- The rollout gate opens views, sidebar, banner and tablet together when `ACCESS_ROLES` is `None`.

## 12. Open items

- ~~The rollout decision in §9, before ship.~~ **Decided 2026-09-30 — see §13.**
- Whether the preference-window banner is dismissible. A banner you can dismiss weakens a deadline
  that comes round twice a year; one you cannot is aggressive for a week. Worth deciding with the
  first real window rather than in the abstract.

**Per-resident persistence — decided, not open (the code comments citing this section for it are about
this, not the dismissibility item above):** the banner persists for each resident individually until
they have personally declared for whatever periode their own submission would currently target, OR
the window's time runs out — not a single site-wide on/off switch that stays lit for everyone
regardless of who has already acted. This is a distinct stakeholder decision from dismissibility
above (which is still open): non-dismissible means "no close button"; per-resident persistence means
the banner's own on/off condition is evaluated per viewer, not once for the whole window. Landed
alongside the fix for the banner/write-path periode mismatch (`koekken.services.
preference_target_periode`, `resident_has_declared_for`) — see those functions' docstrings for the
mechanism.

## 13. Rollout runbook

**Decided 2026-09-30: a clean start at the Feb–Jun 2027 periode.** No code changes beyond two
settings values. This section is the operational plan; it is live until February 2027 and is history
after that.

### Why February, and not sooner

The calendar decided this, not preference. The preference window is the seven days ending at
`periode_deadline()`, and the current Sep–Jan 2026/27 periode passed its deadline on **1 July 2026 —
before this system existed**, so its preferences are locked and unreachable. The first window this
system can honestly run is the next one, and the first shifts it governs are February's.

The alternative considered and rejected was a one-time off-cycle preference round to take over in
December/January. It would have gained about two months, but Amendment 1's locking deliberately
prevents collecting preferences for a periode past its deadline, so it needed new code plus a
deliberate exception to a rule written on purpose. Also rejected: opening now and allocating with no
preferences at all, which is fastest and makes a resident's first experience a schedule they were
never asked about, with every day-preference unhonoured and the weekend pool filled entirely by
drafting. The shape below is chosen so that **the first thing a resident ever meets is being asked
something**, and the first shifts they see are ones their own answers shaped.

### Timeline

| phase | when | what happens | gate state |
| --- | --- | --- | --- |
| 1 | 30 Sep → mid-Nov 2026 | **Køkkengruppen dry run.** Generate a periode, run an allocation, work the flag queue, mark a shift done on the tablet, try an override. Doubles as their training. | `ACCESS_ROLES = (KOKKENGRUPPE,)`, `KOEKKEN_JOBS_ENABLED = 0` |
| 2 | before 20 Nov 2026 | **Seed the balances.** Enter the informal scoreboard figures; run `seed_koekken_balances` (idempotent per resident via `uniq_koekken_startsaldo_per_resident`). | unchanged |
| 3 | ~20 Nov 2026 | **Open the house.** A few days before the window, so people can look round before the banner appears. | **`ACCESS_ROLES = None`** |
| 4 | 24 Nov → 1 Dec 2026 | **First real preference window.** Banner up house-wide; everyone declares all four inputs. | unchanged |
| 5 | 2 Dec 2026 | **First real allocation**, the day after the deadline (the deadline-timing guard refuses to run on or before 1 Dec, the deadline day itself), covering February, March and April 2027. | **`KOEKKEN_JOBS_ENABLED = 1`** |
| — | 1 Feb 2027 | First shift anyone is expected to actually work. | steady state |

### The two settings changes

```
koekken/access.py   ACCESS_ROLES = (Role.KOKKENGRUPPE,)  ->  None      ~20 Nov 2026
config/settings.py  KOEKKEN_JOBS_ENABLED = 0             ->  1          2 Dec 2026
```

That is the entire code delta. `ACCESS_ROLES = None` opens the views, the sidebar entry, the banner
and the tablet together, which is what `core.rollout.Gate` was built for.

**Order matters between them, and the reason is the bug this feature exists to fix.**
`post_obligation` derives each month's charge from actual `Vagt` supply and defaults to zero, so a
month with no generated shifts charges nobody — the flag is therefore harmless to flip late, and
dangerous to flip early. Turning it on while phase 1 has dry-run shifts generated would accrue real
obligation for a trial nobody outside Køkkengruppen could even see, which is precisely the *"everyone
drifts into debt through no fault of their own"* failure the settings comment on that flag warns
about. Leave it at 0 until the house is live.

**Seeding before access, not after** (phase 2 before phase 3), so that no resident ever sees a balance
of zero that later jumps for reasons they did not cause. Their first view of their balance should be
the real one.

### Two prerequisites that are not code, and are not optional

**1. Tell residents the ledger converts to money.** A negative balance becomes a monetary penalty at
move-out. People are being enrolled in that, and must be told plainly **before it starts accruing** —
in the announcement, not buried in a help page. This is a fairness point first and the cheapest
dispute-avoidance available second.

**2. Show each resident their seeded starting balance, and how to contest it.** The figure comes from
an informal paper scoreboard, rebased so the house average is zero. Two things follow. People should
see their own number before it starts converting to money, with some route to dispute it. And the
announcement should say explicitly that the rebase already removed the old 4 h/month rate's
structural artifact — otherwise someone opening the page to a negative number will reasonably read it
as an accusation, when in fact much of the old scoreboard's drift was an accounting error rather than
anybody shirking.

### Køkkengruppen readiness

Not a separate gate: phase 1 *is* the training. They should have run an allocation, adjudicated at
least one flag and marked at least one shift done on the tablet before phase 3 opens the house.
