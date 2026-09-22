# Design: Køkkenvagter — kitchen cleaning shift allocation

**Status:** approved 2026-09-21. **P1 implemented** (commit `27195a2`, review-fix cycle in flight).
**Amendment 1** (2026-09-22, FCFS tiebreak + allocation look-ahead and preference locking) is at the
end of this document and **awaits sign-off**; where it changes a decision below, the section says so.
**Feature spec (to be written at implementation time):** `spec/features/koekkenvagter.md`, **unnumbered**.

> **Unnumbered on purpose**, for the reason `spec/features/begivenheder.md` gives: `F-001`–`F-015` are
> legacy-parity documents, each pointing at a PHP controller, and `99-index.md` states they cover every
> live controller in the old app. Køkkenvagter is greenfield, so an `F-016` would make every existing
> `F-0NN` citation in the code ambiguous.

## What it is, and what it replaces

Six kitchen cleaning duties per day — **morgenvagt**, **frokostvagt** and **aftenvagt**, on weekdays and
at weekends — are today filled by residents writing themselves on a list. The complaint that started this:
*"sometimes tasks are not taken and the kitchen is not getting cleaned."* This replaces the list with
allocation plus a fairness ledger, at `/intern/koekken/`.

It also replaces an **informal, manual kitchen credit scoreboard** (marks per task done, a stated 4 h/month
obligation, balances positive/negative/zero). That scoreboard is **not** AK. `ak.AkEntry` is a separate
system — monthly krydser for dorm labour — and the two must not be conflated; there is no interaction
between them.

**None of the three "existing systems" referenced while scoping this feature exist as software.** The
6-month general-cleaning rotation offered as a precedent is `intern/rengoering/index.php`, which
`00-manifest.md:315` describes as a *"Static cleaning-schedule PDF index"*; `core.Cleaning` is only a name
plus a headcount target, hand-assigned per month, whose count check
(`spec/features/F-010-alumneliste.md:100`) is *"Purely advisory... does not block writes"*. The
holiday-weeks reporting does not exist in this repo. The credit scoreboard is manual. So there is nothing
to migrate programmatically and no rotation code to reuse — this is the first such system in the codebase.

## The arithmetic that shaped the design

Durations are fixed: morgenvagt 1 h (1 person), frokostvagt 1 h (1 person), weekday aftenvagt 3 h
(**2 people**), weekend aftenvagt 2 h (1 person). Against 61 residents:

| | value |
| --- | --- |
| tier A (morgen + frokost) | 56–62 slots/month, mean 60.8 |
| tier B (aftenvagt) | 48–54 slots/month |
| total | 104–116 person-slots/month (1.85 each) |
| load | 208.7 person-hours/month = **3.42 h/resident/month** |

Four findings drove the design, and each is worth keeping because the design looks arbitrary without them.

**1. Equal hours cannot be reached in a month, or in two.** The achievable per-person bundles are chunky —
1.0 h (one morgen/frokost), 3.0 h, 4.0 h (morgen + weekday aften), 7.0 h — and none lands near 3.42 h.
Deviation from target by horizon: 1 month 10–71%, 2 months 16–27%, 3 months 14%, 6 months 1–13%,
12 months 2–5%. The original request proposed "equal per month, or failing that over two months"; neither
works. Convergence needs roughly five to six months, which is what makes a **semester-length period** the
right unit. Recomputed on the chosen semesters: Sep–Jan 1048 person-hours (17.18 h/resident), Feb–Jun 1032
(16.92 h/resident), converging to **4–5%** at four aftenvagter per resident per semester.

**2. The weekend tier-A pool is a mandatory overflow, not an accommodation.** Tier A splits into a weekday
pool (weekdays × 2 = 40–46 slots) and a weekend pool (weekend days × 2 = 16–20). The weekday pool caps at
46 against 61 residents, so **15–21 residents must take a weekend morgen/frokost every month whether they
want one or not** — a standing quota of a quarter to a third of the house. The request framed the weekend
as a minority accommodation for people who cannot do weekdays; arithmetically it is nothing of the kind.
**Consequence:** the weekday-unavailable signal is a *priority ordering on a pool that must be filled
anyway*, not scarce relief to be granted. Under the 16–20 cap every declarer gets a weekend slot **and the
allocator must still draft others** to fill the rest; above the cap, some declarations must be refused. A
boolean "unavailable → honour it" would silently produce infeasible allocations.

**3. "Everybody gets a morgen or frokost each month" is infeasible in half the year.** Tier A is short by
1 in eight months and by 5 in both Februaries — infeasible in 5 of the 10 semester months: Sep(−1),
Nov(−1), Feb(−5), Apr(−1), Jun(−1). The floor is therefore a **soft objective, not a constraint**; the
ledger compensates whoever misses out. Implementing it as a hard rule would fail allocation every
February. Relatedly, allowing residents to take *more* than one tier-A shift cannot absorb this shortfall
— surplus-taking adds demand, not supply — so it is simply an allowed option, not a mechanism.

**4. The old 4 h/month obligation exceeded the work that exists.** Supply is 3.42 h/resident/month against
a stated obligation of 4.00 — a gap of 0.58 h/resident/month, or 35.3 person-hours/month of obligation with
no work in existence to satisfy it. Per semester: 20.00 h owed against 17.18/16.92 available. **Every
resident accrued debt even under perfect distribution and perfect attendance**, so the scoreboard could not
help drifting house-wide negative; this is the most likely explanation for the observed spread of positive,
negative and zero credit, and it means much of today's debt reflects an accounting error rather than
shirking. Accumulated structural debt runs to 7.0 h after a year, 13.9 h after two, 27.8 h after four.
Hence: **obligation = actual period supply**, and a **launch rebase** so that artifact never converts to
money.

## Decisions

| | decision |
| --- | --- |
| Fairness metric | Equal weighted **hours**, as a running per-resident balance. Not a shift count. |
| Period | University semesters: **Sep–Jan**, **Feb–Jun**, plus **Jul–Aug** as a separate short period. |
| Fill mechanism | Two-tier hybrid: tier A auto-allocated, tier B signup with an auto-assign backstop. |
| Tier-A eligibility | A per-resident **weekday-unavailable** signal routes residents to the weekend pool. |
| Tier-A floor | Soft per-month objective. Missing it is a ledger debit, not an error. |
| Obligation | = actual period supply (~3.42 h/month), replacing the old 4 h/month. |
| Weekend compensation | Weekend-tier-A assignees get **aftenvagt preference priority**. Not an hours premium — hours accounting is untouched. |
| Verification | Self-report, plus anyone may flag a no-show, adjudicated by Køkkengruppen. |
| Launch seed | Rebase known informal balances so the **house average is zero**. |
| Move-out penalty | Proportional to the **absolute** balance. Out of scope to calculate or collect. |
| Work-off cap | Extra shifts for a departing resident capped at **2× normal monthly obligation**. |

Two of these were decided against my recommendation or with an explicit trade-off, and both are recorded
here so nobody re-litigates them by accident:

- **Penalty uses the absolute balance, not balance relative to the house mean.** I recommended relative,
  because the mean drifts as residents leave with unsettled credit (simulated at one departure/month with
  leavers skewed toward credit: −0.8 h after a year, −2.6 h after five), and under an absolute rule that
  drift silently becomes money for whoever leaves later. This was **overridden deliberately**, for
  legibility: a resident should be able to look at "−3" and know they pay 3. The drift risk was accepted
  as informed, on the bet that the launch rebase plus a correct obligation rate keeps it small. **If it
  ever does grow uncomfortable, re-running the launch rebase as an admin action resets the anchor while
  keeping the numbers exactly as legible.** That escape hatch should be left available.
- **Aftenvagt's two weekday slots are independent.** No partner matching, no don't-pair rules. Nothing in
  the request suggests pairing matters, and it is the largest available source of avoidable complexity.
  Straightforward to add later if it turns out to matter.

## Data model

New app `koekken`.

- **`VagtRegel`** — the six rules (kind × weekday/weekend) holding hours and headcount. A table rather than
  constants, following the `AkMonthlyCharge` precedent of an officer-editable schedule. Seeded: morgen 1 h
  / 1 person, frokost 1 h / 1 person, weekday aften 3 h / 2 people, weekend aften 2 h / 1 person.
- **`Periode`** — kind (`EFTERAAR` Sep–Jan, `FORAAR` Feb–Jun, `SOMMER` Jul–Aug), year, start, end. The
  ledger's key. Periods are calendar-anchored, **not** a rolling window.
- **`Vagt`** — date + kind, unique together, with headcount copied from the rule at generation time (so
  later edits to a rule cannot retroactively change a published month).
- **`VagtTildeling`** — FK `Vagt`, FK `Resident`, status (`TILDELT` / `UDFOERT` / `ANMELDT` /
  `IKKE_UDFOERT`), unique on (vagt, resident). **Stored, not derived** — unlike `events.Rsvp`, where
  `app/events/models.py` deliberately derives seating from claim order because claims are voluntary. Here
  assignment is authoritative output of an allocator, so it is a row.
- **`KoekkenPost`** — the append-only ledger, mirroring `ak.AkEntry`: resident, `delta_minutes`, kind
  (`FORPLIGTELSE` / `ARBEJDE` / `JUSTERING` / `STARTSALDO` / `TILBAGEFOERSEL`), periode, vagt (nullable),
  `created_at`, `created_by`. Balance is `SUM(delta_minutes)`.

  **Integer minutes, not float and not `Decimal`.** This balance converts to money at move-out, so
  accumulated rounding is not acceptable; integers make every balance exact by construction.
- **`Praeference`** — resident, periode, `weekday_unavailable`, preferred aftenvagt weekdays.
- **`FerieUge`** — resident, ISO year, ISO week, `present`. Modelled generally as weekly presence so it can
  later cover exchange, internship and long illness, but **collected and used for the summer period only**
  in v1 — shaped to generalise without building a second model, and without speculative UI now.

`Residency` remains the source of who is eligible in a month. Note its limit, which is why `FerieUge`
exists: `Residency` records who **holds a room**, not who is **in the building**. Close enough for ten
months of the year; exactly wrong in July and August.

## Allocation

Runs per month. **Tier A runs strictly before tier B** — this ordering is load-bearing, not incidental,
because tier-B preference priority depends on who tier A routed to a weekend slot.

1. **Tier A.** Population from `Residency` rows for the month. Weekend pool capacity is weekend days × 2.
   Seat weekday-unavailable declarers there first, ranked by balance ascending (most behind first); if they
   exceed capacity the excess are refused with a clear message rather than silently dropped. Then **draft**
   further residents into any remaining weekend slots — required, per finding 2. Then fill weekday slots,
   balance ascending. Residents left over get no tier-A slot this month: the soft floor, absorbed by the
   ledger.
2. **Tier B.** Aftenvagter open for signup. Preference matching orders weekend-tier-A assignees first (the
   compensation mechanic), then by balance ascending. At a deadline N days before the month starts,
   unclaimed slots are auto-assigned by balance ascending — this is the backstop that fixes the original
   complaint.
3. **Departing residents.** Anyone with a known `move_out_date` inside the period is biased toward extra
   shifts to work down a negative balance, capped at 2× the normal monthly obligation.

Ranking on raw balance is deliberate and load-bearing: ranking is invariant under adding a constant, which
is what makes arrivals, departures and historical artifacts unable to distort who gets priority.

> **Amended by Amendment 1.** Ties in the balance ranking are now broken by who declared their preference
> first, and the balance used is a *projected* balance that accounts for shifts already assigned in
> look-ahead months. See "Amendment 1" below.

## Ledger and obligation

Obligation is posted monthly, **per month of presence**, at that month's supply divided by present
residents. Two properties follow: the system is self-balancing by construction (total obligation equals
total work), and mid-period arrivals and departures need no special case — someone present for two months
of a five-month period owes two months' worth.

Posting is idempotent, guarded by a `UniqueConstraint` on (resident, periode, month, kind=`FORPLIGTELSE`),
exactly as `AkEntry` guards its monthly charge. It runs from a **scheduled task plus a lazy
first-page-load backstop**, following `ak.services.ensure_active_month_applied`: cron fails silently, and a
skipped month leaves every balance wrong.

Balances **carry across semester boundaries**. Rebasing happens once, at launch, and never again as routine
— rebasing at each boundary would undo the seeding.

**No ledger entry is ever written for a resident after their `move_out_date`.** The obligation task skips
them and their balance freezes. Historical rows are retained, matching the append-only shape.

## Verification and no-shows

The assignee self-reports `UDFOERT`, which posts the work credit. Anyone may flag a shift, moving it to
`ANMELDT` for Køkkengruppen — the embedsgruppe already responsible for noticing whether people take their
vagt — to adjudicate. A shift ruled `IKKE_UDFOERT` has its credit reversed by a `TILBAGEFOERSEL` entry,
**never by deleting a row**.

This matters more than it looks. Allocation guarantees every slot has a *name* against it; it does not
guarantee the cleaning happened. Without completion recording, the ledger would credit work that may never
have occurred, the scoreboard would become fiction, and the original complaint would survive the rewrite
intact — while the balance now carries a cash value at move-out, which makes a gameable ledger worth
gaming. Self-report plus flagging turns the signal the house already generates socially (people notice an
uncleaned kitchen) into the system's error-correction path, without putting ~113 confirmations a month on
anyone.

No separate no-show punishment exists: withholding the credit **is** the consequence, and it flows into the
move-out penalty.

## Summer (Jul–Aug)

Its own `Periode`. Slots are generated only for weeks with reported presence, and obligation remains supply
divided by present residents — so the same rule that fixes the obligation rate everywhere else
automatically sizes the reduced holiday load, with no bespoke "how much smaller" constant to tune.

This is necessary rather than cosmetic: at full slot volume July and August each carry 62 tier-A slots, 53
aftenvagter and 212 person-hours, *identical* to any other month. The load per **present** resident is
acutely sensitive to occupancy — 61 present gives 3.48 h, 40 gives 5.30, 30 gives 7.07, 20 gives 10.60 — so
if the house half-empties while the kitchen needs the same cleaning, equal hours does not degrade
gracefully, it inverts onto whoever stayed.

## UI

- **Resident:** my shifts, my balance (displayed against the house average), preferences, holiday weeks,
  aftenvagt signup.
- **Køkkengruppen:** allocation run and preview, the flag queue, overrides.
- **Regnskab:** a balance export for the move-out penalty.
- **Kitchen tablet** at `/intern/koekken/idag/` — read-only, no login, large type, showing today's shifts
  and who is responsible, so a no-show is visible in person.

The tablet view reuses the **IP-gating pattern** already proven by the ølkælder till: `_client_ip()` in
`app/oelkaelder/views.py` trusts the **last** hop of `X-Forwarded-For` (the entry Traefik appended, which a
client cannot spoof — safe only because gunicorn is reachable solely via the proxy), and `_is_kiosk()` gates
on an IP whitelist from settings. This feature gets its own `KOEKKEN_KIOSK_IPS`.

It reuses **only the gating**, not `kiosk_base.html`. That template is deliberately iOS-10/WebKit-602-safe —
no Tailwind, no JS bundle — because the till runs an ancient iPad. The kitchen tablet is a modern device, so
this view uses the normal `base.html` and frontend stack.

Duplicating `_client_ip`/`_is_kiosk` rather than extracting them to `core` is deliberate, and follows this
repo's own documented rule. `core/rollout.py`'s docstring records that the rollout gate was copied at n=2 on
purpose and extracted only at n=3, because *"a core/… both apps imported would outlive the reason it
existed."* This is the second copy, so it is copied, with a comment pointing at the ølkælder original.

## Access

Køkkengruppen (`Role.KOKKENGRUPPE`) for overrides and flag adjudication; Regnskab (`Role.REGNSKAB`)
read-only on balances; `administrator` implies everything; `indstilling` continues to own the monthly
`Residency` list, unchanged by this feature. Ships behind `core.rollout.Gate` with `ACCESS_ROLES` set,
opened to the house by setting it to `None`.

## Phasing

| phase | contents |
| --- | --- |
| 1 | Slot model, generation, tier-A allocation, ledger, obligation posting, launch seeding command. |
| 2 | Tier-B signup, preferences, verification and flagging, kitchen tablet view. |
| 3 | Summer period and the holiday-weeks input. |

Staged behind the rollout gate, because fairness only becomes observable over a full semester — a two-week
trial cannot tell anyone whether the allocation is fair.

## Launch migration

A management command with `--dry-run` takes the known informal balances, subtracts the mean so the house
average is exactly zero, and writes one `STARTSALDO` entry per resident. This is what prevents the old
obligation rate's artifact (up to ~28 h for a long-standing resident) from converting into a real monetary
penalty at move-out. Relative standing between residents is preserved exactly; only the anchor moves.

## Rejected alternatives

- **Full auto-allocation of both tiers with a swap market.** Strongest fairness guarantee, but the most
  code by a wide margin (allocator plus swap workflow plus appeals) and the largest behaviour change.
  Rejected because the ledger, not per-month control, is what delivers fairness — see finding 1.
- **Pure assisted signup for both tiers.** Rejected because tier A has essentially no slack (60.8 slots for
  61 residents), so "signing up" for it is a fiction; there is close to one satisfying assignment.
- **Equal shift counts instead of equal hours.** Rejected: a weekday aftenvagt is 3 h against a morgenvagt's
  1 h, so equal counts produce a 2–4× spread in actual work.
- **Adding kitchen capacity so every resident can have a tier-A slot every month.** Would mean putting a
  second person on weekend slots purely to preserve a monthly invariant the ledger makes unnecessary.
- **Folding July–August into the Sep–Jan period.** Arithmetically tidy (a seven-month period at 24.13
  h/resident) but imports summer hours into a period with a different population, breaking the very metric
  the design is built on.
- **An hours premium for weekend tier-A slots.** Considered, then dropped in favour of aftenvagt preference
  priority: compensation belongs in allocation, not in the accounting, because distorting the hours would
  make the ledger mean something other than hours.
- **Treating assignment as completion.** The option under which the system looks tidy and changes nothing
  about the actual complaint.
- **Integrating with `ak.AkEntry`.** Explicitly out of scope; the kitchen obligation is a separate system.

## Open details (do not block implementation)

- The exact tier-B auto-assign deadline (N days before month start).
- Whether the general-cleaning rotation should later adopt this machinery — it has none of its own today.
- Whether weekly presence is extended beyond summer to cover exchange, internship and long illness. The
  model is already shaped for it.

---

# Amendment 1 — FCFS tiebreak, allocation look-ahead, preference locking

**Raised 2026-09-22. Awaiting sign-off.** Two requirements, plus the consequences they force.

## A1.1 First-come-first-served, as a tiebreaker only

Balance-ascending ranking **stays primary**. FCFS decides only exact ties: when two residents have the
same balance, whoever declared their preference earlier wins. Today the tie falls to `Resident.pk`
(the population queryset is `.order_by("pk")` and Python's sort is stable), which is arbitrary and, worse,
*stably* arbitrary — the same low-pk resident wins every tie forever.

**Comparator becomes `(projected_balance ASC, declared_at ASC, pk ASC)`.** `pk` stays as the final
fallback so the ordering is always total and allocation is deterministic under test.

A resident with no `Praeference` row has no `declared_at`. They sort **last** among tied residents —
declaring is what earns priority, so a non-declarer cannot outrank a declarer on a tie. Implement as a
sentinel (`date.max`, or `NULLS LAST`), never as a null that sorts first by accident.

## A1.2 Allocation look-ahead, and what it forces

The requirement is that residents can see two to three months of their shifts in advance, rather than
allocation happening one month at a time whenever Køkkengruppen triggers it.

**Mechanism: a preference deadline two months before each period starts, then a batch plus a monthly
roll-forward.**

1. Each `Periode` has a **preference deadline two calendar months before its start** — derived, not a
   stored field (Feb–Jun's deadline is 1 December; Sep–Jan's is 1 July; Jul–Aug's is 1 May).
2. At the deadline, Køkkengruppen runs a **batch allocating the period's first three months**. This stays
   a deliberate human action: the first run of a new period is the one worth eyeballing.
3. Thereafter a **scheduled monthly job rolls the window forward one month**, allocating the next
   not-yet-allocated month inside the current period.

Visibility under this scheme never drops below two months and is usually three. Worked through Feb–Jun:
the Jul–Aug deadline falls on 1 May, so standing in June — the period's last month — July and August are
already allocated, giving two months ahead; standing in May gives three (June, July, August).

**Why not allocate a whole semester at once.** It looks simpler and it is worse. Ranking is by balance,
and balances move as shifts are completed, so a five-month batch ranks months four and five on data that
is months stale — exactly defeating the mechanism that gives priority to whoever is currently behind. A
three-month window is the compromise the requirement asks for anyway.

**Why the deadline is two months out, not one.** A rolling three-month window reaches across a period
boundary: allocating three months ahead in November touches February, which belongs to Feb–Jun. Those
months must be allocated from Feb–Jun preferences, so Feb–Jun preferences have to exist before the window
arrives. Two months is the smallest offset that keeps visibility at two-or-more everywhere. It also has a
practical benefit for the summer period: holiday weeks become due on 1 May rather than in March, which is
a far more reasonable thing to ask people.

**Consequence — the balance used for ranking must be projected, not the raw ledger balance.** With a
look-ahead window, months N+1 and N+2 are allocated but not yet worked, so their hours are not in the
ledger. Ranking on the raw balance would show the same residents as equally behind each time and hand them
the next month's shifts too, compounding across the window.

    projected_balance = ledger balance + Σ hours of TILDELT assignments not yet credited

`TILDELT` is exactly the set of assigned-but-uncredited rows (credit is posted on `UDFOERT`), so the
projection needs no extra bookkeeping. Note that the monthly obligation debit is deliberately **not**
projected: it is uniform across everyone present in a month, so it cannot change a relative ranking, and
projecting it would mean guessing at presence three months out.

**Consequence — an allocated month must not be silently re-shuffled.** `allocate_tier_a` currently
deletes the month's `TILDELT` rows and re-allocates, which is correct for a single-month manual run and
wrong the moment residents are relying on a published schedule. Re-running must **refuse** when a month
already has `TILDELT` rows, unless an explicit `--force` is passed for a genuine correction. Without this
the look-ahead promise — "people know which vagter they get" — is not actually kept.

## A1.3 Preference locking

Preferences for a period are editable until that period's deadline. **At the deadline they freeze**, and a
later edit is written to the *following* period's row instead, taking effect then. This is directly
expressible because `Praeference` is already keyed `(resident, periode)` with a unique constraint: the
form simply targets a different `periode`. The UI must say plainly which period an edit will apply to —
silently redirecting an edit to a future period would be worse than refusing it.

Locking on **the deadline** rather than on "any allocation has run" is deliberate. The two are nearly the
same moment by construction, but a date is something a resident can be told in advance and reminded about,
whereas "whenever an officer happens to press the button" is not.

**Edge cases and their resolutions:**

- **Mid-period arrival.** A resident with no `Praeference` row for the current period may **create** one at
  any time, deadline passed or not. They have never had the chance to state a preference, and defaulting
  them to weekday-available could assign shifts they cannot do. The rule is narrower than "no changes
  within a period": *you may always create an initial preference; you may not edit one that has been
  used.* A new preference affects only months not yet allocated, and if none remain it simply becomes the
  next period's.
- **Departure inside the window.** Months already allocated may contain shifts for someone who then leaves.
  Køkkengruppen reassigns via the override path; nothing about the ledger changes, and per the approved
  design no entry is ever written after `move_out_date`.
- **Look-ahead crossing a period boundary.** Each month is allocated using the preferences of the period
  *that month* belongs to — never the period the allocation run was triggered from. The two-month deadline
  offset is what guarantees those rows exist.
- **Missed deadline.** Not an error and must not block allocation. A resident with no row for the new period
  carries forward the previous period's value as the default, and sorts last on FCFS ties (A1.1) since they
  declared nothing.

## A1.4 What this changes in already-built P1, and what it does not

**Touches committed P1 code** (`27195a2` plus the in-flight review fixes):

| where | change |
| --- | --- |
| `models.py` `Praeference` | Add `declared_at`. New migration. |
| `services.py` `allocate_tier_a` | Composite sort key in all three rankings (declarers, weekend draft, weekday pool); switch `bulk_balances` to the projected balance; add the already-allocated guard. |
| `services.py` | New helper for the projected balance, and period-deadline derivation alongside `_periode_bounds`. |
| `management/commands/allocate_koekkenvagter.py` | `--force`; a batch mode for a period's first three months. |
| `tasks.py` + `CELERY_BEAT_SCHEDULE` | New scheduled roll-forward job. **This reverses P1's deliberate decision to leave allocation manual-only** — the monthly roll-forward becomes a cron job, while the period-opening batch stays a Køkkengruppen action. The P1 reasoning ("a Køkkengruppen decision, not a cron job") was sound for one-month-at-a-time allocation and does not survive a rolling window. Extend the parametrised table in `app/tests/test_scheduled_tasks.py`. |
| `demo.py` | Show a locked preference, a three-month allocated window, and a tie broken by `declared_at`. |

**Explicitly NOT affected — do not re-open these:**

- **`post_obligation` is unchanged.** Obligation is still posted per month of presence, monthly, for
  whoever is actually on that month's list. It must *not* move to the look-ahead schedule: presence three
  months out is a guess, and posting ahead would write entries that could fall after a `move_out_date`.
- **The ledger, `KoekkenPost`, integer minutes, the rebase and the penalty basis** are all untouched.
- **`generate_vagter`, `VagtRegel`, `Vagt`, `VagtTildeling` shapes** are untouched; only *when*
  `allocate_tier_a` runs and *how it ranks* change, not what it writes.
- **Tier-A capacity arithmetic, the soft floor, and the weekend-overflow draft** are untouched. February
  still legitimately leaves five residents without a tier-A slot.

## A1.5 Additional tests this requires

- Two residents on identical balances: the earlier `declared_at` is seated first; reversing the declaration
  order reverses the outcome; a non-declarer sorts last on a tie.
- Allocating three consecutive months in sequence spreads load rather than compounding it onto the same
  residents — the projected-balance regression, and the one most likely to be got wrong.
- Re-running allocation for an already-allocated month refuses without `--force` and proceeds with it.
- An edit after the deadline writes the *next* period's row and leaves the current period's allocation
  untouched.
- A mid-period arrival can create a first preference after the deadline, and it affects only
  not-yet-allocated months.
- A month in the look-ahead window that falls in the next period reads that period's preferences.
- A resident with no row for a new period inherits the previous period's value and allocation still runs.
- Visibility invariant: stepping `DevClock` month by month across a full period boundary, at least two
  months are always allocated ahead.
