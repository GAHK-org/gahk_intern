# Design: Køkkenvagter — kitchen cleaning shift allocation

**Status:** approved 2026-09-21. **P3 (summer) is now designed in `2026-10-04-koekkenvagter-p3-design.md`**, which supersedes the "Summer (Jul–Aug)" section below, the `FerieUge` data-model bullet, and Amendment 4's "awaiting sign-off" framing (Amendment 4 is now designed in `2026-10-04-koekkenvagter-a4-design.md`; its content below is kept as history). **P1 implemented** (`27195a2`) and review-fixed (`a8575fd`, F1–F5).
**Amendments 1–3 are approved** and in implementation: A1 (2026-09-22, FCFS tiebreak, allocation
look-ahead, preference locking), A2 (2026-09-22, where a three-months-out population comes from) and
A3 (2026-09-23, reconciliation eligibility and residents arriving with no preference) — note **A3.1
corrects A2.3**, so read them together. **Amendment 4** (2026-09-23, swapping vagter) is now **designed in
`2026-10-04-koekkenvagter-a4-design.md`** (approved 2026-10-04, including the written doc;
step 1 in implementation). **Amendment 5** (2026-10-01, fridage) is **approved and built**
(`fa0fca8`), and carries a **supplement of 2026-10-04** (built, `fba6616`) that replaces A5.4's
whole-month re-allocation -- read it before A5.4. All are at the end of this document; where any changes a decision above, that section
says so. A1.3 additionally carries an approved supplement (2026-10-01) on preference-window ordering.
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
- *(Superseded by `2026-10-04-koekkenvagter-p3-design.md` §3: away date ranges (`Fravaer`), not weekly presence.)* **`FerieUge`** — resident, ISO year, ISO week, `present`. Modelled generally as weekly presence so it can
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

> **Superseded by `2026-10-04-koekkenvagter-p3-design.md`:** summer is not allocated at all; residents claim shifts themselves. The text below is kept as history.

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
| 2 | Tier-B allocation, preferences, verification and flagging, kitchen tablet. **Designed in `2026-09-28-koekkenvagter-p2-design.md`** — note it supersedes two things here: tier-B is *not* signup-based, and the tablet is *not* read-only. |
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
  *(Amendment 4 adds swapping as a correction tool on top of allocation. That is not this proposal
  returning; see A4.4 for why the distinction is load-bearing rather than a technicality.)*
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

**Raised 2026-09-22. Approved 2026-09-22.** Two requirements, plus the consequences they force.

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

> **Supplement, 2026-10-01 (approved).** An **open preference window wins over everything below.** A
> declaration made while a window is open targets *that window's* periode, and this check runs **first**,
> ahead of the mid-period-arrival exemption. Without it the exemption fires first and captures the write:
> in the first real window every resident has no row for the periode they are living in, so the whole
> house's declaration lands on a periode whose deadline passed months earlier and which nothing will read
> again — while the banner truthfully reports that they have declared.
>
> This **supplements** the rules below rather than changing them. The exemption keeps its exact meaning;
> it is simply unreachable during a window, where it is **provably vacuous** anyway: it only does real work
> when the current periode still has unallocated months, and by the time any window opens the current
> periode has been fully allocated for months (Forår 2027's window opens 24 Nov 2026, by which point
> Efterår 2026 has been complete since ~1 Sep; the same holds for every window).
>
> **That vacuity is a property of this schedule, not a coincidence** — deadline two months before a periode
> starts, a batch of its first three months, roll-forward monthly thereafter. The one-month-lead
> alternative rejected in Q2 would **not** have it, and window-first would then genuinely cost a
> mid-period arrival their only chance to declare. **If the lead time is ever shortened, revisit this.**
>
> **The deadline day belongs to the resident, not the allocator.** The window is inclusive of the deadline
> date, and the deadline-triggered batch is refused until the day *after* — enforced, not conventional, so
> a declaration made on the deadline day cannot be silently overtaken by a batch run that morning.

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

---

# Amendment 2 — where a three-months-out population comes from

**Raised 2026-09-22 after review of Amendment 1. Approved 2026-09-22.**

## A2.1 The gap

Amendment 1 assumes allocation can run two to three months ahead. It never says where the *population*
for those months comes from, and the answer turns out to be: nowhere.

`allocate_tier_a` draws its population from `Residency` rows, and **nothing in this codebase ever creates
a `Residency` row more than one month ahead.** `residents.views._target_period` is hard-capped to the
current month or the next one, and says so deliberately: *"Deliberately only those two... nothing in the
kollegium's workflow needs it."* So the deadline batch allocates months with no roster at all, and the
roll-forward job walks straight into the same wall one month later — raising, inside a Celery task, in
direct contradiction of its own docstring promise that it must not raise.

This is a design gap Amendment 1 opened, not an implementation slip.

## A2.2 Resolution: project the population in memory

When a target month has no `Residency` rows, the allocator **projects** one:

> the most recent published `Residency` list at or before the target month, minus any resident whose
> `Resident.move_out_date` falls before that month begins.

The subtraction matters and costs nothing: `move_out_date` is already on `Resident`, and departures are
normally known well in advance, so the largest and most predictable source of projection error is removed
by reading a field we already have. Arrivals cannot be predicted and are handled by reconciliation (A2.3).

**The projection is computed in memory and never written.** `Residency` is *indstilling's* table — roles,
`active_period`, the alumneliste, the kvotient lottery and the stamtræ all read it — and a kitchen feature
that writes rows into it would become a silent producer of the roster every other feature trusts. This is
worth stating explicitly because the codebase makes the wrong version easy: `rooms.views_soegvaerelse.
_carry_roster_forward` already carries a roster forward non-destructively, and calling it from here would
work, look tidy, and quietly make køkken a co-owner of the resident list. Do not.

When projection is impossible — no published list at all — allocation **logs and no-ops**. It must not
raise: a scheduled job that dies on an empty database is a monthly page for no reason, and this is exactly
the promise the roll-forward's docstring already makes.

## A2.3 Reconciliation, when the real list arrives

Projection is an approximation, so the real list must be allowed to correct it. But Amendment 1 forbids
silently re-shuffling an allocated month, and that rule exists for the same reason the look-ahead exists:
people are relying on what they were shown.

Both hold at once, because reconciliation is **additive only**:

- A resident on the projection who is **not** on the real list has their assignments **vacated** (they are
  not here) and those slots are refilled from the real population, balance ascending.
- A resident on the real list who was **not** on the projection — a new arrival — is assigned to any
  **unfilled** slots, balance ascending.
- **Every other existing assignment is left exactly as it was.**

This yields a guarantee worth stating in the resident-facing UI: **an assignment shown to a resident who
is still living in the dorm is never revoked by reconciliation.** Only slots belonging to someone who has
left, or slots nobody held, can move.

Reconciliation runs monthly for the month whose list was most recently published — i.e. next month — and
is idempotent: if the population already matches the assignments it does nothing. It is **not** a `--force`
re-run. `--force` keeps its Amendment 1 meaning: a deliberate Køkkengruppen re-shuffle for a genuine
correction, which *may* move anyone.

## A2.4 Accepted failure modes

- **A new arrival may get no shifts in their first allocated month**, because nothing could have predicted
  them. They still accrue that month's obligation, so they end the month behind. This self-corrects — the
  projected balance ranks them most-behind, so they take priority at the next allocation — but the
  correction is up to three months out, because that is how far ahead allocation runs. Reconciliation
  shortens this whenever unfilled slots exist to give them.
- **A departure not recorded as a `move_out_date`** is invisible to the projection and is caught only at
  reconciliation, one month before the month in question. That is still a month of lead time for
  Køkkengruppen to act, and the slot is refilled automatically rather than silently going unstaffed.
- **The ledger is never wrong because of projection.** Obligation is posted monthly from the *real* list,
  per month of presence, so a projected-but-departed resident never receives an entry, and the move-out
  rule ("no entries after `move_out_date`") is untouched. Projection affects assignments only.

## A2.5 Rejected alternatives

- **Ask indstilling to publish rosters three months ahead.** Pushes this feature's requirement into
  another workgroup's workflow, against an editor that deliberately refuses the case, and asks them to
  guess room assignments that depend on the kvotient lottery and actual move-ins. If it ever becomes real,
  projection simply stops being used — the allocator prefers a real list whenever one exists — so nothing
  here blocks it.
- **Skip months with no population and rely on a later catch-up run.** Since `Residency` never exists more
  than one month ahead, "skip" is not an edge case, it is every month: the window would sit permanently at
  one month while appearing to function. That fails the requirement Amendment 1 exists for, quietly rather
  than loudly. Retained only as the genuine-impossibility no-op in A2.2.
- **Write projected `Residency` rows** (e.g. via `_carry_roster_forward`). See A2.2.

## A2.6 What this changes

| where | change |
| --- | --- |
| `services.py` | A population resolver: real `Residency` rows when present, otherwise the projection in A2.2. `allocate_tier_a` calls it instead of querying `Residency` directly. |
| `services.py` | New `reconcile_month()` implementing A2.3. |
| roll-forward task | No-op-and-log instead of raising when no population can be resolved, honouring its docstring. |
| `tasks.py` + beat | Reconciliation added to the monthly chain, after the roster for next month exists. Same stagger discipline as every other job here. |
| `demo.py` | A projected month, and a reconciliation that vacates a departure and seats an arrival. |

**Unchanged, and not to be re-opened:** `post_obligation` (still monthly, from the real list, per month of
presence); the ledger, rebase and penalty basis; the `Vagt`/`VagtTildeling`/`VagtRegel` shapes; tier-A
capacity arithmetic and the soft floor; and Amendment 1's ranking, locking and `--force` semantics.

## A2.7 Tests

- Allocating a month with no `Residency` rows uses the latest list and excludes anyone whose
  `move_out_date` precedes that month.
- No published list at all: the roll-forward logs and returns rather than raising.
- Reconciliation vacates a projected resident absent from the real list and refills the slot.
- Reconciliation seats a new arrival into an unfilled slot.
- **Reconciliation never moves the assignment of a resident present on both lists** — the guarantee in
  A2.3, and the one a regression here would hurt most.
- Reconciliation is idempotent when projection and reality already agree.
- No `Residency` row is created by any køkken code path.

## A2.8 Two implementation-level bugs bundled with this

Both were found in the same review and are fixes, not design questions:

- **`--batch` must clamp to the period's actual length.** It walks a fixed three months from a period's
  start, so for the two-month summer period it steps into autumn — allocating a month before its own
  preference deadline has passed, which inverts Amendment 1's locking rule. Clamp to the period.
- **`test_missing_preference_row_falls_back_to_previous_periode_value` does not test what it claims.**
  It passes with the fallback deliberately broken, because the resident it checks would have been seated
  anyway by the mandatory weekend draft. It needs an assertion that the resident arrived *via the
  fallback* specifically.

---

# Amendment 3 — reconciliation eligibility, and residents who arrive with no preference

**Raised 2026-09-23 in review of Amendment 2. Approved 2026-09-23.** Amendment 2's reconciliation rule
("a resident on the real list who was not on the projection is seated into unfilled slots only, balance
ascending") is **wrong as written**, and separately it assumes a preference that a new arrival cannot
have had the chance to give. Both are fixed here; the rest of Amendment 2 stands.

## A3.1 Reconciliation must re-derive eligibility, not hand over a slot

As written, Amendment 2's seating rule is pool-blind: it says "unfilled slots", not "unfilled slots this
resident is eligible for". In the exact scenario that exposed it — one resident moves out holding a
**weekday** slot, one resident moves in — the arrival would mechanically inherit that weekday slot no
matter what they can actually do. **That is a real defect in the design as written, not an omission.**

**A vacated slot is never inherited.** It returns to the pool of unfilled slots, and reconciliation seats
people into it by the same eligibility rules normal allocation uses. The resident who takes it may well not
be the arrival at all — an existing resident who is further behind is the more likely taker.

The rule this hangs on is an asymmetry established by finding 2 and already honoured by `allocate_tier_a`,
which refuses to force a declarer onto "a weekday slot they said they can't do":

| | may take a weekend slot | may take a weekday slot |
| --- | --- | --- |
| weekday-**unavailable** | yes — their only option | **never** |
| weekday-**available** | yes — draftable, the pool is mandatory overflow | yes |

So the two pools are not interchangeable and the constraint runs one way only. A vacated **weekend** slot
can go to anyone; a vacated **weekday** slot can go only to a weekday-available resident. If no eligible
candidate exists, **the slot stays unfilled and surfaces in Køkkengruppen's queue** rather than being
forced on someone who cannot work it. An unfilled slot that somebody knows about is a better outcome than
an assigned slot that silently will not happen — the latter is the original complaint wearing a disguise.

**Implementation shape, and the point of it:** reconciliation must not hand-roll its own seating. Extract
the seating core of `allocate_tier_a` — declarers to weekend first, draft the remainder of the weekend
pool, then weekday, all on the Amendment 1 ranking — and have both paths call it, reconciliation passing a
restricted slot set (only unfilled) and a restricted population (only unseated). Two hand-written copies of
this logic would drift, and the direction they would drift in is precisely this bug.

Reconciliation also prefers residents **without** a tier-A slot this month. Residents may take more than
one voluntarily (that was the Q4 answer), but reconciliation must not force a second shift on someone who
has already done their share; if nobody unassigned is eligible, the slot goes to the queue.

## A3.2 Residents who arrive with no preference at all

Amendment 1's fallback — no row for this period, carry forward the previous period's value — has nothing
to carry forward for a first-ever residency. And new residents are created close to move-in, so the
two-month deadline machinery gives them no opportunity at all.

**Decision: an undeclared resident is treated as weekday-available**, which is already the model's default
(`weekday_unavailable = False`) and already what the built fallback produces for someone with no history.
No new field and no new default are needed. Three reasons:

1. **It is the right statistical guess.** Weekday-unavailability is a minority condition by construction:
   the weekend pool holds only 16–20 of 61, and it has to be *drafted* into, which means fewer people
   declare it than the pool needs. Most residents can do weekdays.
2. **The other default is systemically harmful, not merely wrong.** Defaulting arrivals to
   weekday-*unavailable* would spend scarce weekend capacity on an assumption, and since declarers beyond
   capacity are refused, assumed non-declarers would push genuine declarers out of the only pool they can
   work. A wrong guess in this direction costs one person an awkward month; a wrong guess in the other
   corrupts the allocation for people who did declare.
3. **Being wrong is recoverable and visible.** The resident asks Køkkengruppen to move them, via the
   override path that already exists, or flags the shift. It is a one-month inconvenience, not a
   structural failure.

**What makes this acceptable rather than merely defensible is that the gap is closed at the other end:**
a resident with no `Praeference` row for the current period is **prompted to declare on first login**, and
Amendment 1 already permits creating an initial preference at any time, deadline or not. The declaration
is a single yes/no question — the two-month deadline exists to stop preferences being changed *after*
people see their allocation, not because answering takes two months. There is real lead time to use:
records are created around move-in, reconciliation runs for the following month, so days to weeks are
available in which one question can be answered.

**Assumed and declared are already distinguishable, with no schema change.** Amendment 1 added
`declared_at`; a resident with no row has none, so `declared_at IS NULL` *is* the "we are guessing" marker.
It already sorts them last on ties (A1.1), and it lets Køkkengruppen see at a glance who is on a guess
rather than a statement. Køkkengruppen's month view should show that, and the move-in process is the
natural place to ask the question.

## A3.3 One interaction worth recording, not changing

A new resident starts at a ledger balance of **0**, while the house mean drifts *negative* over time (the
departure drift the absolute-balance decision knowingly accepted: −0.8 h after a year, −2.6 h after five).
So a newcomer is relatively "ahead" of the house and ranks **last** for shifts until they accrue enough
obligation to sink toward the mean.

Two consequences, and both are fine:

- It is *helpful* here: arrivals are not at the front of the queue, which is exactly the breathing room an
  undeclared newcomer needs to answer the question before being handed anything.
- It means each cohort does slightly less work than the incumbents it joins, for as long as the mean sits
  below zero. **Do not "fix" this by seeding newcomers at the house mean.** That would hand a resident who
  has done nothing wrong a negative balance, which under the approved rule is a monetary penalty at
  move-out. Starting at 0 is correct precisely because the balance converts to money; the mild work
  inequity is the price of that correctness, and it is the smaller of the two errors.

## A3.4 What this changes

| where | change |
| --- | --- |
| `services.py` | Extract the tier-A seating core; `allocate_tier_a` and `reconcile_month` both call it. Reconciliation passes unfilled slots + unseated residents and **must not** contain its own seating rules. |
| `services.py` | Reconciliation prefers residents with no tier-A slot this month; leaves a slot unfilled and queued rather than forcing a second shift or an ineligible one. |
| views/templates | Prompt a resident with no `Praeference` row for the current period to declare, on first login. Show `declared_at IS NULL` as "assumed" in Køkkengruppen's view. |
| `demo.py` | An arrival with no preference row seated correctly, and a vacated weekday slot that an ineligible arrival does *not* inherit. |

**Unchanged:** everything else in Amendments 1 and 2, the ledger, the obligation, and the newcomer's
starting balance of 0.

## A3.5 Tests

- A weekday-unavailable arrival is **never** seated into a vacated weekday slot, even when it is the only
  unfilled slot in the month — the defect this amendment exists to fix.
- A vacated weekday slot with no eligible unassigned candidate stays unfilled and is surfaced, not forced.
- A vacated weekend slot can be filled by either kind of resident.
- A departing resident's slot may go to an existing resident who is further behind, not automatically to
  the arrival — "no inheritance".
- An arrival with no `Praeference` row anywhere is treated as weekday-available and may take either pool.
- Reconciliation does not give a second tier-A slot to a resident who already has one while an unassigned
  eligible resident exists.
- Reconciliation seating and `allocate_tier_a` produce the same choice given the same slots and population
  — the shared-core guarantee, and the regression test that stops the two drifting apart.

---

# Amendment 4 — swapping vagter

> **Status update (2026-10-04):** now **designed in `2026-10-04-koekkenvagter-a4-design.md`**, which
> replaces A4.3's rules and A4.6's sketch below (A4.1, A4.4 and A4.5 still stand as reasoning; A4.6's
> open question is answered there: an untaken offer expires at shift start and the offerer stays
> responsible). Its prerequisite is the Amendment 5 supplement of 2026-10-04. The content below is
> unchanged history.

**Raised 2026-09-23. Awaiting sign-off.** A resident marks a shift they cannot take; another resident
either takes it over outright or offers one of their own in trade.

## A4.1 It is a good idea, but it answers a different question than the one it was offered for

Swapping was proposed as a way to avoid needing reconciliation's seating to be eligibility-aware: seat
people mechanically, and let anyone mis-seated trade their way out. **That specific substitution should not
be made**, and the reason is a distinction worth stating plainly:

> Swapping is the remedy for what the system **could not know**. Eligibility is the remedy for what it
> **already knows**. A swap market is not a licence to ignore information you are holding.

A resident who has declared themselves weekday-unavailable has told the system a fact. Seating them into a
weekday slot anyway, and requiring them to find a counterparty to undo it, fails in two ways at once if the
market happens to be thin that week: the shift goes unworked (the original complaint), and they take the
ledger debit for it, which under the approved rule is money at move-out. That is a system-generated error
whose cost lands on the resident. Honouring the declaration instead costs a filter on a list.

**So A3.1 stands unchanged.** What swapping genuinely improves is A3.2 — the *undeclared* case, where there
is no information to honour and a default guess is unavoidable. Swapping makes that guess cheap to be wrong
about, which is exactly the reassurance A3.2 needed and did not have. The two amendments compose: the
allocator never assigns against a declaration, and any resident whose circumstances the system could not
know has a self-service way out.

## A4.2 Scope: a separate phase, not part of the reconciliation fix

This is a **general-purpose feature**, far broader than new arrivals — sickness, travel, exam periods, a
weekend away. It carries its own UI surface (an offer board, propose/accept, notifications), its own
states, and its own abuse question (someone offering every shift they are given). Meanwhile the
reconciliation gap is already closed by A3.1 and A3.2 without it.

So: **Phase 2b, after P2.** It depends on P2 existing anyway — there is no resident-facing shift UI to
swap from, and no `UDFOERT`/flag state machine to interact with, until P2 ships. Folding it into the
reconciliation fix would delay a correction that is ready in order to start a feature that is not.

## A4.3 Rules

**A voluntary swap may cross the eligibility boundary.** A weekday-unavailable resident may choose to take
a weekday slot; the declaration constrains what the *allocator may impose*, not what the resident may opt
into. Any trade of any two shifts is permissible — weekday for weekend, weekend for weekend, unequal
durations included.

What a swap must respect has nothing to do with pools:

- **Only `TILDELT`, future-dated shifts.** Never one already self-reported, adjudicated, or in the past.
- **Capacity is preserved exactly.** A take-over transfers one assignment; it must re-check the slot is
  still open, since two residents may claim the same offer concurrently. A trade is a 1:1 exchange. The
  existing `unique (vagt, resident)` prevents the degenerate case.
- **No double-booking** — a resident must not end up holding two shifts that overlap in time on one day.
- **Nothing lands after a resident's `move_out_date`.**
- **A departing resident may exceed the 2× work-off cap by choice.** The cap exists to stop the *allocator*
  dumping unrealistic load; volunteering is not that.

**Unequal trades are allowed and self-correcting.** Trading a 1 h morgenvagt for a 3 h aftenvagt moves both
balances by 2 h, and that is simply true: one resident did two hours more work than the other. Future
allocation compensates automatically through the ranking.

## A4.4 Why this is not the rejected "swap market" returning

The rejected alternative was a **fill mechanism**: allocate everything up front and let a market sort out
who actually works. That makes the market load-bearing for *fairness* — if trading is thin, the allocation
stays unfair, and it is unfair in a way that converts to money.

This is a **correction tool on top of an allocation that is already fair by construction**. If nobody ever
swaps, the distribution is exactly as fair as it was; only individual convenience is lost. The market
carries convenience here, never fairness, and that is the whole difference. It also could not carry
fairness even if asked to: with roughly 113 slots across 61 residents, most people hold one or two shifts a
month, so the pool of tradeable shifts at any moment is thin by construction.

## A4.5 The ledger is indifferent, and that is not an accident

Credit is posted on `UDFOERT`, against the assignment row, so **whoever actually works the shift receives
the hours**. Swapping is invisible to the ledger. This falls out of an earlier decision — crediting on
completion rather than on assignment — and it is what makes swapping safe to add without touching any
accounting.

One better-than-neutral interaction: because the allocator ranks on the *projected* balance (ledger plus
uncredited `TILDELT` hours, per A1.2), a swap immediately moves both parties' standing — the resident who
gave a shift away looks more behind, the one who took it looks more ahead — so future allocations
compensate without anything extra being written. Obligation is untouched: it is per resident per month of
presence and has never depended on assignments.

## A4.6 Sketch of what it needs

A `VagtBytte` offer record (offered assignment, optional requested assignment for a trade, state, who took
it), views for offering, browsing and accepting, a notification when an offer is taken, and Køkkengruppen
visibility of shifts offered but unclaimed as the deadline approaches — an unclaimed offer is an early
warning of exactly the failure this whole feature exists to prevent.

**Open for the human, when this phase comes up:** whether an unclaimed offer eventually reverts to the
offerer (who then owes the shift or the debit), or escalates to Køkkengruppen to reassign. Not needed to
approve the direction; needed before building it.

---

# Amendment 5 — fridage (days where shifts are not generated)

**Approved 2026-10-01. Built (`fa0fca8` plus review fixes).** Køkkengruppen can mark specific dates on
which specific shift types are not generated at all — juleaften, nytårsaften, and any other day the
kitchen does not need covering.

> **Supplement, 2026-10-04 (approved; stakeholder answers Q2a/Q2b of the Amendment 4 round, relayed
> through the coordinating session). Built (`fba6616`).** It **replaces A5.4's re-allocation step and its
> notification rule**. A5.1–A5.3, A5.5 and A5.6 are unchanged.
>
> 1. **A fridag on an already-allocated month never re-allocates.** `declare_fridag` deletes the
>    matching `Vagt` rows (their assignments cascade, A5.6), re-posts obligation under the existing,
>    independent gate (the month already had `FORPLIGTELSE` posted), and notifies **exactly the
>    residents whose assignment was deleted**. They get the existing "bortfaldet" wording
>    (`_fridag_notification_message(for_date, reason)`), which becomes the only message.
>    `allocate_month` is never called. This is what P3 §4 already specified for SOMMER, now for every
>    periode, so the SOMMER branch becomes the only branch.
> 2. **Compensation needs no code.** Losing a `TILDELT` assignment lowers the resident's projected
>    balance (A1.2), so the next allocation ranks them further behind and gives them more: *"removed
>    from the shift and gets an extra shift in another month"*, in the stakeholder's words. The
>    alternative offered, *"get the points anyway"*, was rejected. It would credit hours nobody
>    worked, so total credit would exceed supply, and the ledger would no longer mean hours worked.
> 3. **No lead-time guard.** The stakeholder first proposed one ("not less than a month away"), then
>    dropped it once a late fridag only removes that day's shifts. Instead, **the command's report
>    opens with an explicit notice whenever the day is already allocated**, on both `--dry-run` and a
>    real run, naming every resident being removed and their shift, e.g. *"[dry-run] 2027-03-25 er
>    allerede allokeret: 2 beboer(e) fjernes fra deres vagter: Anna Hansen (morgenvagt), Bo Lund
>    (aftenvagt). De får besked."* It stays **non-interactive**. No management command in this
>    codebase prompts, and `--dry-run` is the preview everywhere. The help text says plainly that a
>    fridag on an allocated day removes residents, and recommends `--dry-run` first. If a fridag UI is
>    ever built, the same notice belongs in an htmx `hx-confirm` (P3 §7's convention).
> 4. **Why.** A5.4's whole-month reshuffle moved residents who had nothing to do with the fridag
>    date, and it would silently undo hand-offs residents had agreed under Amendment 4 (December is
>    allocated in August, its fridage are declared in November, and its hand-offs exist by then).
>    Removing only that day's shifts touches only the people on that day. The A5.4 notification
>    wording, which took four review rounds to settle, was a symptom of the reshuffle and goes with it.
>
> **Code consequences:**
> - `FridagResult` drops `reallocated_months`, `moved_residents` and `gained_residents`, and
>   `lost_residents` becomes `removed`: `(resident, vagt)` pairs, captured before the delete.
> - `_month_tildelt_by_resident` loses its only caller and is deleted.
> - The "omfordelt" message variant is deleted.
> - `declare_koekken_fridag`'s report and help text change as in point 3.
> - The A5.5 guards (no `UDFOERT`/`ANMELDT` assignment on the date, no past date) are unchanged.
> - The existing reshuffle and notification-diff tests are rewritten against the new behaviour
>   (A5.7), not deleted.

## A5.1 Scope

**Per shift type, not per day.** A date is excluded for a chosen set of kinds: aftenvagt may be
dropped on juleaften while morgen and frokost still run, or the whole day may go. Whole-day is simply
every kind for that date, so it needs no separate concept.

**Concrete dates, entered per periode. No recurring rules.** Danish holidays split awkwardly:
juleaften, nytårsaften and grundlovsdag are the same date every year, but påske, Kristi himmelfart and
pinse all *move* — and they all fall in Forår, so a month-and-day rule would be wrong for exactly the
periode with the most holidays in it. A rule engine that handles movable feasts is wildly
disproportionate here, and the movable ones need a human with a calendar regardless. The burden is
about four dates per periode, three times a year, at a moment Køkkengruppen are already doing periode
admin. If re-entry ever grates, a "copy last year's fridage" helper is a small later addition; it is
not worth building now.

## A5.2 Data model

```
Fridag(date, kind, reason)        unique (date, kind)
```

One row per excluded `(date, kind)`. This mirrors `Vagt`'s own key exactly, and is the same shape as
`PraeferenceDag(praeference, kind, weekday)` from P2 — the app's existing idiom rather than a new one.
`reason` ("Juleaften") is shown on the tablet and in Køkkengruppen's list; the UI writes the same text
across a date's rows and the tablet groups by date, so the duplication is harmless and has a single
writer.

## A5.3 The generation seam — **and its second user**

`generate_vagter` consults one exclusion check and skips excluded `(date, kind)` pairs. That is the
entire happy path.

**Build this as a seam, not as an `if Fridag.objects…` inline.** P3's summer work is the same question
with a different source: its design says summer slots are generated only for weeks with reported
presence, which is "should this date generate shifts?" answered from `FerieUge` and per-week, rather
than from `Fridag` and per-kind. P3 is not built yet, so making generation ask **one** question now
costs nothing and avoids a second, independent skip path growing beside the first. **Do not build the
summer source here** — only make sure there is one place for it to plug into.

## A5.4 The officer action — and why it is the normal path

> **Superseded in part by the 2026-10-04 supplement above:** the re-allocation step
> (`allocate_month(force=True)`) and the whole-month "lost, moved or gained" notification below no
> longer apply. A fridag deletes only that day's shifts, re-posts obligation, and notifies only the
> removed holders. The rest of this section is kept as history.

A fridag declared for a month whose `Vagt` rows already exist cannot be handled by skipping at
generation. The action is therefore: **delete the matching `Vagt` rows → re-allocate the affected
months with the existing `force` flag → re-post obligation for those months.** One officer action
doing all three, with a preview of what it will change before it commits.

**This is the normal case, not an edge case.** December 2026 is allocated on **1 August 2026**, 122
days ahead. Anyone who thinks about Christmas in October or November is declaring a fridag against an
already-allocated month. A design that only skipped rows at generation time would be wrong most of the
time it was used.

There is still a clean happy path, and it belongs in an existing routine rather than a new ritual:
`generate_koekkenvagter` takes `--date`, so Køkkengruppen generate and batch-allocate a periode at its
deadline, three times a year. *"Set this periode's fridage"* goes there. A5.4's action is for when
somebody remembers late.

**The obligation re-post is bundled deliberately.** `post_obligation` derives a month's total from
`Sum(headcount × duration_minutes)` over actual `Vagt` rows, so removing rows reduces that month's
obligation correctly with **no new ledger logic** — but only if it is re-run. It reconciles properly on
re-run (stale rows deleted, then `update_or_create`), so this is mechanical; it simply will not happen
by itself, and must not be left to be remembered.

**Every resident whose held shifts in the month changed at all -- lost, moved or newly gained -- is notified**, with one generic message that names the affected month but no specific shift or date. (Notifying only residents who "lose a shift" went through four rounds of precision bugs -- who lost what, which shift moved where -- each fixing one wrong case and exposing the next; one always-true message to anyone affected replaced it.) Residents who lose a shift were shown it. Their projected balance drops with the
assignment, so they rank further behind and the allocator compensates them later without anything
extra being written.

## A5.5 Two guards

- **Refuse a date carrying any `UDFOERT` or `ANMELDT` assignment.** You cannot retroactively un-hold a
  shift somebody actually worked, and its ledger entry would be left pointing at nothing.
- **Refuse a date in the past.** Pointless, and it only disturbs history.

## A5.6 Verified mechanics

Two foreign keys decide what deletion costs, and they differ:

- **`KoekkenPost.vagt` is `on_delete=SET_NULL`.** Deleting a `Vagt` nulls the reference and **leaves the
  ledger intact** — a resident who already worked a shift keeps their credit. The append-only ledger is
  not at risk here.
- **`VagtTildeling.vagt` is `on_delete=CASCADE`.** Assignments vanish with the shift. Correct, and the
  reason A5.4 notifies.

## A5.7 Tests

- An excluded `(date, kind)` is not generated; other kinds on that date still are.
- A whole-day exclusion removes every kind for that date and nothing on adjacent dates.
- *(Revised by the 2026-10-04 supplement.)* Declaring a fridag on an already-allocated month deletes
  only that day's shifts and **never re-allocates**. Every other assignment in the month is identical
  before and after, a completed Amendment 4 hand-off on another day included. Obligation is re-posted,
  and that month's total obligation falls by exactly the removed supply. Only the removed holders are
  notified, with the "bortfaldet" message. The command's report names them on both `--dry-run` and a
  real run, and says so when nobody is affected. `allocate_month` is never called.
- The ledger is unchanged by the deletion: an existing `ARBEJDE` credit survives with a null `vagt`.
- Both guards refuse, with a clear Danish message, and change nothing.
- Generation asks the exclusion seam exactly once per `(date, kind)` — the regression that would let a
  second skip path grow beside it.
