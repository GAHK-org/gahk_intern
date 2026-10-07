# Design: Køkkenvagter Amendment 6 — credit for party work (festkredit)

**Status: decisions approved 2026-10-07** (stakeholder answers relayed through the coordinating
session, one question at a time; recorded in §2). **This written document awaits the stakeholder's
confirmation. Status: built (`a46f6a7`).** It sits inside `2026-09-21-koekkenvagter-design.md`, whose ledger
section ("Ledger and obligation") it extends. Read that section first.

## 1. What it is

At some parties, e.g. the New Year party, around 8–10 residents cook and serve, and are rewarded
with 4–6 køkkenkrydser each. **1 kryds = 1 hour.** This amendment lets Køkkengruppen record those
awards in the kitchen ledger. Each award is **funded by the whole house**, so the ledger stays
self-balancing.

The stakeholder's own question was: *"If this extra credit comes in. Will it skew the workload enough
to care for?"* §3 answers it with numbers.

## 2. Decisions

| # | decision | stakeholder answer |
| --- | --- | --- |
| Q1 | **Funded by everyone.** Each award charges the same total, split evenly over everyone on the event month's resident list, helpers included. The ledger stays self-balancing. | Option (a) |
| Q2.1 | Køkkengruppen (and administrators) award from a form on the Køkkengruppen page, not via Django admin. | "Yes to all" |
| Q2.2 | The form takes event name, event date, the helpers, and whole hours per person (the same default for everyone, adjustable per person). | ″ |
| Q2.3 | Only current residents can receive credit (see §4 for the precise rule). | ″ |
| Q2.4 | Funding is charged to that month's published resident list. If no list has been published, the award is refused with a message. | ″ |
| Q2.5 | "Fortryd tildeling" reverses a whole award with opposite entries, credit and funding alike. Nothing is ever deleted. | ″ |
| Q2.6 | Helpers get a push on the existing Køkkenvagter topic. Residents who are only charged get none. The charge is visible on their balance as "Fest: {navn}" (§6). | ″ |
| Q2.7 | The event is recorded as free-text name plus date, with no link to the Begivenheder app. | ″ |
| Q2.8 | There is no cap on hours or on the number of awards. | ″ |

## 3. Does it skew the workload? (the stakeholder's question)

Kitchen supply is ~208.7 h/month, ~2,500 h/year with summer at full schedule (P3), across 61 residents.
Scoping: 3–5 events/year, 8–10 helpers, 4–6 krydser each.

| scenario | credit/year | share of kitchen supply | per resident/year | per resident/month |
| --- | --- | --- | --- | --- |
| low (3 × 8 × 4) | 96 h | 3.8 % | 1.6 h | 8 min |
| typical (4 × 9 × 5) | 180 h | 7.2 % | 2.95 h | 15 min |
| high (5 × 10 × 6) | 300 h | 12.0 % | 4.9 h | 25 min |

- **Workload.** The number of kitchen shifts is fixed, so credit shifts kitchen work from helpers onto
  everyone else, through the existing ranking. In the typical case each non-helper does about 15 extra
  minutes a month (~7 % on top of 3.42 h/month). One helper's 5 h is about 1.5 months of obligation: they
  rank later for roughly one or two shifts. Because helpers rotate (~36 helper slots a year over 61
  residents), most people receive similar credit over a few years, and the redistribution largely
  cancels out. This is the ledger doing what it is for: party cooking and serving is real work for the
  house, an hour for an hour. The original design rejected a "weekend hours premium" for distorting the
  hours. Festkredit does not distort them, because 1 kryds is an actual hour worked.
- **Money.** Unfunded, the house's mean balance would rise ~3 h/year, and less would be collected at
  move-out. Funding (Q1) keeps total obligation = total credit. A resident's balance moves by their
  credit minus their small share of the charge, e.g. +5 h − 44 min for a helper at a typical 45-hour
  party. Nobody's move-out money drifts. Ranking is unaffected by the funding, because adding the same
  constant to everyone present never changes an order. So the funding changes the money and not the
  workload, which is the point.

**Answer to the stakeholder:** about 15 min/month per non-helper, which evens out as helpers rotate.
With funding, it does not skew the money.

## 4. Rules

An award is refused (Danish message, nothing written) unless all of these hold:

- the event date is **today or earlier**: credit is for work done;
- at least one helper, each with a **positive whole number of hours**. There is no upper limit (Q2.8);
- no resident appears twice in the same award;
- every helper **lives here now**: `move_out_date` is null or not before today. This is the approved
  "no move-out before the event", tightened by the ledger's standing rule that *no entry is ever written
  for a resident after their `move_out_date`*. Awards are posted after the event, so the rule is checked
  against the day of posting;
- the event month has a **published `Residency` list** (Q2.4). It is never projected: the A2.2
  projection is for allocation only, exactly as P3 §11 notes for `post_obligation`.

**Who is charged:** everyone on the event month's `Residency` list, excluding anyone whose
`move_out_date` is before today (the same set `post_obligation` charges, by the same rule). Helpers are
not on that list by definition, so a helper who moved in after that month gets credit and pays no share.

**The split:** total = Σ helper hours × 60 minutes. It is divided over the charged residents with the
**same largest-remainder method `post_obligation` uses**, ordered by pk, so the charges sum to exactly
the credit. Extract that arithmetic into one helper shared by both. Do not copy it.

## 5. Data model

```
FestKredit                       -- one award
  navn          CharField(100)   -- "Nytårsfest"
  dato          DateField        -- the event date
  created_by    FK Resident      SET_NULL, null
  created_at
  fortrudt_at   DateTime, null   -- set by "Fortryd tildeling"
  fortrudt_by   FK Resident      SET_NULL, null
```

`KoekkenPost` gains:

- `festkredit` FK `FestKredit`, null, **PROTECT** (an award with ledger rows must never be deletable, the
  same reasoning as `KoekkenPost.periode`);
- two kinds:
  - `FESTKREDIT = "festkredit", "Festkredit"` (+, helpers);
  - `FESTBIDRAG = "festbidrag", "Festbidrag"` (−, everyone charged).

**Reversal** reuses `TILBAGEFOERSEL`, with `festkredit` set. Every row it writes is the exact negative
of one of the award's rows.

Unchanged and not re-opened:
- `month` stays null for these kinds (the partial unique constraint is for `FORPLIGTELSE` only);
- `periode` is the periode containing `dato`;
- `vagt` is null;
- `created_by` is the awarding officer.

No other model changes. `KoekkenPost`'s append-only invariant holds: an award is never edited, only
reversed.

## 6. Mechanics

`award_festkredit(navn, dato, timer_by_resident, *, by) -> FestKredit` runs in one
`transaction.atomic()`. It:

1. checks every §4 rule;
2. creates the `FestKredit` row;
3. writes one `FESTKREDIT` row per helper;
4. writes one `FESTBIDRAG` row per charged resident;
5. pushes each helper, after commit (`core.push.send`, default background mode). The audience is
   narrowed through `access.allowed_subscribers`, exactly as `resolve_anmeldelse` does. Message:
   *"Du har fået {timer} t køkkenkredit for {navn}."*

`undo_festkredit(festkredit, *, by)` runs in one transaction. It:

1. locks the `FestKredit` row (`select_for_update`);
2. refuses if `fortrudt_at` is already set, so a double-click cannot reverse twice;
3. writes one `TILBAGEFOERSEL` per linked row with the negated delta;
4. sets `fortrudt_at` and `fortrudt_by`.

There is no push on undo. It was not among the approved defaults, and adding one later is a one-liner.

**Concurrency:**
- Ledger writes are append-only inserts, so there is no shared row to race on except the award row
  during undo, which is locked.
- Two simultaneous submissions of the same award (a double-click) would create two awards. The form's
  submit button is disabled on submit, and either award can be undone.
- Nothing here touches `VagtTildeling`, `Vagt` or the Amendment 4 lock order.

**Interactions:**
- Ranking (`bulk_projected_balances`) and `house_mean` read the ledger and need no change.
- `post_obligation`'s stale-row cleanup deletes only `FORPLIGTELSE` rows, so it never touches these
  kinds. Verify this with a test.
- The Regnskab export and the move-out penalty read the balance, so they include festkredit
  automatically, as intended.

## 7. UI

Danish, P2 §10 conventions. Køkkengruppen actions use a plain POST with redirect and messages, like
`allokering`. They are not htmx "closed actions".

**`/intern/koekken/gruppe/festkredit/`**, for `access.can_manage` only (template gate and view
re-check). It has three parts:

1. **The form:**
   - "Begivenhed" (text);
   - "Dato" (date);
   - "Hjælpere" (a multi-select of current residents, sorted by name);
   - "Timer pr. person" (default, whole number).

   Submitting it renders step 2.
2. **The confirmation step:**
   - each chosen helper with an editable hours field, prefilled with the default;
   - a summary computed by the same pure split helper that the write uses: *"I alt 45 t. Fordeles
     som festbidrag på 61 beboere på alumnelisten for december 2026 (ca. 44 min hver)."*;
   - a "Tildel" button, with the submit disabled on click.

   Any §4 refusal shows here as an error message. Nothing is written until "Tildel".
3. **"Tidligere tildelinger":**
   - each award shows name, date, helpers with hours, total, who awarded it, and "fortrudt" if undone;
   - a "Fortryd tildeling" button only on awards not yet undone, with a confirm: *"Hjælpernes kredit og
     alles festbidrag tilbageføres. Det kan ikke gøres om."*

The Køkkengruppen page links to it.

**Resident index page:** a small **"Seneste posteringer"** card listing the resident's last 10 ledger
entries, with date, Danish label and ± hours. Festkredit, festbidrag and their reversals are labelled
**"Fest: {navn}"**. This is how Q2.6's "visible on their balance" is met: today residents see only the
balance total. The card is read-only, and it also makes every other ledger entry legible, e.g.
"Forpligtelse december", "Udført: Aftenvagt 3. dec".

**Push opt-in wording:** the `success_text` in `index.html` and the `wants_koekken` comment in
`core/models.py` gain "køkkenkredit for en fest".

**Explanation page:** one paragraph explaining:
- party credit exists, 1 kryds = 1 hour;
- it is paid for by a small charge on everyone that month;
- it means helpers get slightly fewer kitchen shifts later, which rotates around the house.

## 8. Tests

- **Award:**
  - helpers' `FESTKREDIT` rows equal their hours;
  - the `FESTBIDRAG` rows over the event month's list sum to exactly minus the total, with no rounding
    drift for a total that doesn't divide evenly;
  - the house's total ledger change is exactly 0;
  - the helpers are pushed via `allowed_subscribers`, and nobody else is.
- **Refusals (each writes nothing):**
  - a future date;
  - no helpers;
  - zero or negative hours;
  - a duplicate resident;
  - a helper who has moved out;
  - a month with no published list.
- **Funding set:** it is the event month's list minus residents who have moved out by today. A helper
  not on that list gets credit and is not charged. The split helper is shared with `post_obligation`, and
  that function's existing tests still pass.
- **Undo:**
  - one `TILBAGEFOERSEL` per linked row with the negated delta;
  - every affected balance is back to its pre-award value;
  - `fortrudt_at` is set;
  - a second undo is refused;
  - the button is absent on an award that has been undone.
- **Ranking:** after an award, a helper ranks later than an otherwise identical non-helper in
  `allocate_tier_a`'s ordering. The funding debit alone does not change any relative order.
- **`post_obligation` re-run** for the event month leaves `FESTKREDIT` and `FESTBIDRAG` rows untouched.
- **Access:** non-Køkkengruppen users get 403 on every view and do not see the link.
- **Confirmation step:** the summary matches what is written.
- **Resident view:** the "Seneste posteringer" card shows "Fest: {navn}" for all three kinds.
- **Demo:** one award (and one undone award) in `demo.py`.

## Rejected alternatives

- **Credit only, unfunded** (Q1 b). The workload effect is the same, but the house mean drifts up
  ~3 h/year and becomes uncollected move-out money.
- **Reusing `JUSTERING` via Django admin.** It is staff-only, has no event record, no funding, and no
  undo.
- **Linking to the Begivenheder `Event`** (Q2.7). Not every party is registered there, and the coupling
  buys nothing.
- **An hours premium or multiplier.** 1 kryds is 1 real hour, and the ledger must keep meaning hours.
