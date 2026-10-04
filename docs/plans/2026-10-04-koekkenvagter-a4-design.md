# Design: Køkkenvagter Amendment 4 — hand-off and trading of vagter

**Status: decisions approved 2026-10-04** (stakeholder answers relayed through the coordinating
session, one question at a time; recorded in §2). **This written document awaits the stakeholder's
confirmation. Not yet built.** It sits inside `2026-09-21-koekkenvagter-design.md` (architecture,
Amendments 1–5), `2026-09-28-koekkenvagter-p2-design.md` (P2) and
`2026-10-04-koekkenvagter-p3-design.md` (P3). Read those first. This document assumes them.

**This replaces the sketch in the main design doc's "Amendment 4" section (A4.1–A4.6).** A4.1
(swapping is for what the system *could not know*), A4.4 (this is not the rejected swap market) and
A4.5 (the ledger does not care who works a shift) still stand as reasoning. A4.3's rules and A4.6's
sketch are replaced by §3–§5 below.

**Prerequisite:** the Amendment 5 supplement of 2026-10-04 (main design doc, start of "Amendment 5").
A late fridag no longer reshuffles the whole month. Without that change, every December fridag would
undo December's hand-offs. It is built first, as its own step.

## 1. What it is

A resident who cannot work one of their future shifts **offers** it. Another resident then either
**takes it over** straight away, or **proposes a trade** with one of their own future shifts, which
the offerer accepts or declines. On a two-person weekday aftenvagt, the offerer's partner on that
same shift can **take over the whole shift** and earn the credit for both places.

There is still **no self-service unclaim anywhere** (P3 §2). An offer moves nothing until somebody
accepts it, and until then the offerer is responsible. Every move goes from one resident to another,
and both consent: the offerer by offering, the taker by accepting.

It applies to every `TILDELT` assignment that has not started yet: allocated shifts (from Feb 2027)
and summer claims (from ~May 2027) alike, because both are ordinary rows.

## 2. Decisions

| # | decision | stakeholder answer |
| --- | --- | --- |
| Q1 | The holder posts the offer. **Anyone can take it immediately**: posting is the consent, and nobody confirms afterwards. Anyone can propose a trade, which **the holder approves**. No unsolicited requests for shifts that have not been offered. | Option (a) |
| Q2a/b | A late fridag removes only that day's shifts, with no reshuffle and no lead-time guard. The report names whoever is removed. | **Amendment 5 supplement** (main design doc), not this document |
| Q2c | **Completed hand-offs and trades survive a deliberate force re-run**, as marked-done shifts already do. The success message reports how many were kept. | Option (a) |
| Q3 | An offer nobody takes **expires when the shift starts, and the offerer stays responsible**. Køkkengruppen sees open offers for the next 14 days. No reminder, no escalation. | Option (a) |
| Q4 | Køkkenvagter sends **no broadcast push of its own**. Instead, an offer can be **posted to Den Hurtige** in a new "Køkkenvagter" channel, under the resident's own name. There is a tick-box, ticked by default. Built last. | Option (a) |
| Q5 | On a two-person shift, **the partner can take over the whole shift through the other's offer, before the shift, and gets the credit for both places** (6 h on a weekday aftenvagt). No claim after the fact for a partner who did not show up: that stays a manual Køkkengruppen adjustment. | Option (a) |
| — | **No eligibility check** on take-over or trade beyond population and move-out (§3). **Any trade is allowed**, unequal ones included. **No cap** on open offers. Build order: take-over → trade → Den Hurtige. | "Yes to all four" |

### Why no eligibility check, given P2/P3's reasoning

`weekday_unavailable` limits what the **allocator may impose**. It does not limit what a resident may
choose (A4.3, P3 §6). The question was whether taking over someone else's assigned shift differs
from claiming an empty one. It does not, in any way that matters: both residents act voluntarily,
and the offerer gives up a shift instead of having one imposed. Allocator-only constructs (the
weekend draft, aftenvagt avoidance, the 2× work-off cap, weekend compensation) also stay with the
allocator. One consequence is accepted knowingly: a weekend tier-A assignee who hands off their
weekend shift keeps the aftenvagt priority they already received that month. That priority only
decides which day a person gets, and it is never re-evaluated within a month.

### Why no cap

Offering is self-limiting. Nothing moves unless someone volunteers. Every shift given away lowers
the giver's projected balance (A1.2), so the next allocation hands them more. A resident who offers
everything ends up working the same total over time, only later.

## 3. Rules

Checked in the service on **every** write, after locking, never only in the view.

A row may be **offered** when:
- it is `TILDELT`, and its shift has not started. Start time is read live from `VagtRegel.start_time`, the value `marking_window` and P3's `claim_vagt` use;
- it has **no `VagtAnmeldelse` at all**, in any status. A shift dated today can be flagged before it starts, dismissed back to `TILDELT`, and then offered. Moving such a row would attach someone else's flag history to the taker;
- it has no open offer already. A partial unique constraint enforces this.

An offer may be **taken over** when:
- the offer is open and its shift has not started;
- the taker is not the offerer and does not already hold that `Vagt`. The unique `(vagt, resident)` constraint backs this up, and an `IntegrityError` from a race becomes the same refusal;
- the taker **may hold the shift**: they are in that month's population (the real `Residency` list, or the A2.2 projection via `_resolve_population`), and their `move_out_date` is not before the shift date. This check is one helper, `may_hold(resident, vagt)`. **P3 step 3's `claim_vagt` must reuse it** and not write its own copy.

A **whole-shift take-over** (Q5) is the take-over action when the taker is the offerer's partner on
that same `Vagt`. It additionally requires:
- `vagt.headcount == 2`. Only the seeded weekday aftenvagt qualifies today. The rule is generic so that a `VagtRegel` edit keeps it correct, but a three-person shift is refused (§5.3 explains why);
- the partner's own row is `TILDELT`, and the partner has **no open offer on it**. Otherwise they would be offering half of a shift that has just become whole.

A **trade** proposal (step 2) is allowed when:
- the proposer's own row meets every "may be offered" rule above;
- both directions meet the take-over rules: the proposer may hold X, the offerer may hold Y, and neither already holds the other's `Vagt`. That last check also excludes trading places on the same two-person shift, which would change nothing.

Any two shifts may be traded: weekday for weekend, 1 h for 3 h. An unequal trade corrects itself
through the ledger (A4.3).

Deliberately **not** checked: `weekday_unavailable`, the avoidance pattern, the 2× cap, away ranges
(`Fravaer`), a per-resident cap, time overlap (morgen 06–07, frokost 12–13 and aften 17–20 cannot
overlap, P3 §6).

## 4. Data model

One migration for step 1, one for step 2, one for step 3. **`VagtTildeling` gets no new column.**

```
VagtBytte                          -- one offer of one assignment
  tildeling      FK VagtTildeling  CASCADE, related_name="byttetilbud"
  tilbudt_af     FK Resident       CASCADE   -- the offerer, frozen at offer time
  status         AABEN | OVERTAGET | OVERTAGET_HEL | BYTTET | TRUKKET | BORTFALDET
  overtaget_af   FK Resident       null, SET_NULL   -- the taker / the trade partner
  hurtig_post    FK den_hurtige.QuickPost  null, SET_NULL   -- step 3 only
  created_at, closed_at
  constraint: at most one AABEN per tildeling (partial unique, the VagtAnmeldelse pattern)

VagtBytteForslag                   -- step 2: "I'll give you my Y for your X"
  bytte          FK VagtBytte      CASCADE, related_name="forslag"
  modydelse      FK VagtTildeling  CASCADE   -- the proposer's own row, Y
  foreslaaet_af  FK Resident       CASCADE
  status         AABEN | ACCEPTERET | AFVIST | TRUKKET | BORTFALDET
  created_at, closed_at
  constraint: at most one AABEN per (bytte, modydelse)
```

- **Rows are moved, never deleted and recreated.** A take-over or trade changes `resident` on the existing `VagtTildeling`. Its pk, and the offer pointing at it, survive as history. The one exception is the whole-shift collapse (§5.3), which deletes the vacated row and **re-points the offer at the surviving row first**, so the history survives there too.
- **Expiry is derived, never written.** An `AABEN` offer whose shift has started is "expired" whenever it is read: not shown, not acceptable. No scheduled task exists to flip a status, the same lazy shape the feature already prefers. Expired rows stay `AABEN` in the database harmlessly. Nothing can be done with them, and the partial unique constraint no longer matters for a row that cannot be offered again.
- **Completed hand-offs are identified by query, not by a flag.** A row is a *completed hand-off* when a `VagtBytte` with status `OVERTAGET`/`OVERTAGET_HEL`/`BYTTET` points at it, or a `VagtBytteForslag` with status `ACCEPTERET` has it as `modydelse`. One helper, `handed_off_tildeling_filter()`, returns this as an `Exists` expression, for §5.5.

Admin registration for both. Regenerate `erd.md`, as `004577a` did for `Fridag`.

## 5. Mechanics

Each state-changing service runs in one `transaction.atomic()`. It locks with `select_for_update`
the offer, any proposal, and every `VagtTildeling` involved, **in ascending pk order** so that two
concurrent actions cannot deadlock. It then re-checks every §3 rule against the locked rows. If
someone got there first, it raises `KoekkenAllocationError` with a Danish message, the same pattern
as P3's claim.

### 5.1 Offer and withdraw

- `offer_tildeling(tildeling, by)` creates an `AABEN` `VagtBytte`. Only the row's own holder may offer it.
- `withdraw_offer(bytte, by)` sets the offer to `TRUKKET`, and every open proposal on it to `BORTFALDET`. This is **not** an unclaim: the offerer simply keeps the shift.

### 5.2 Take-over

`take_over(bytte, by)`: lock, re-check, set `tildeling.resident = by`, set the offer to `OVERTAGET`
with `overtaget_af = by`, then **close everything the move invalidated** (§5.6). Credit, the flag
workflow and the tablet follow the row automatically: whoever holds it marks it done and earns it.

### 5.3 Whole-shift take-over (Q5) — collapsing the shift

A two-person shift is **two `VagtTildeling` rows** on one `Vagt`. Credit is posted **per row**:
`mark_udfoert` credits `vagt.duration_minutes`, 180 on a weekday aftenvagt, so each partner earns
3 h. "Full credit" for working it alone is therefore 6 h. That is also exactly what keeps the ledger
balanced, because obligation charges `headcount × duration` per place.

`take_over_whole(bytte, by)`, in one locked transaction:

1. Re-point the offer at the partner's own row (`bytte.tildeling = partner_row`), set its status to `OVERTAGET_HEL` and `overtaget_af = by`.
2. Delete the vacated row.
3. Change the `Vagt` from `headcount=2, duration_minutes=180` to **`headcount=1, duration_minutes=360`**.

Everything else follows with no new code:
- supply `headcount × duration` is unchanged at 360, so `post_obligation` is untouched;
- every capacity count (`_fill_slots`, tier B's open slots, `override_assign`, P3 claiming, `still_unfilled`) reads 1 of 1, which is full;
- `mark_udfoert` credits 360;
- an upheld flag on a marked-done shift reverses 360;
- the projected balance counts 360.

Alternatives considered and rejected:
- **a `pladser` (places held) column**: every capacity count in the most-reviewed code would have to sum places instead of counting rows, six or more sites;
- **dropping the unique `(vagt, resident)` constraint**: it backs every race guard in the feature;
- **deleting the vacated row and adding a bonus at marking time**: the shift would look half-empty, and reconciliation, re-runs, overrides and claiming would all try to refill it.

**This amends a documented invariant**, `koekken.models`' "`Vagt` snapshots `headcount` and
`duration_minutes`". That invariant exists so that *editing a `VagtRegel`* cannot rewrite a
published month, and it still holds for that purpose. The amendment is narrow: **an accepted
whole-shift take-over is the one operation that may change a generated `Vagt`'s snapshot, and only
from (2, d) to (1, 2d).** It is recorded by the `OVERTAGET_HEL` offer. `generate_vagter` uses
`get_or_create`, so regeneration never resets it. The models docstring and the invariant list must
say this.

Why only `headcount == 2`: with three places, collapsing one would have to give `3d/2` to *each* of
the two remaining holders, crediting someone who never agreed to do more.

A collapsed shift is an ordinary one-person shift from then on. If its holder later offers it, the
taker takes the whole 6 h shift alone.

### 5.4 Trade (step 2)

- `propose_trade(bytte, modydelse, by)` creates an `AABEN` proposal. The offerer gets a push (§6).
- `withdraw_proposal` → `TRUKKET`. `decline_proposal` → `AFVIST`, by the offerer.
- `accept_trade(forslag, by)`, by the offerer only, in one locked transaction: swap the residents on the two rows **as one atomic two-row exchange** (never two take-overs, which could leave one person holding both shifts if the second step failed). Then set the proposal to `ACCEPTERET`, the offer to `BYTTET` with `overtaget_af` = the proposer, and close everything invalidated (§5.6). No transient unique-constraint conflict is possible, because §3 already refused the case where either side holds the other's `Vagt`.

### 5.5 Surviving a force re-run (Q2c)

`allocate_tier_a`, `allocate_tier_b` and `allocate_month` each clear a month's `TILDELT` rows before
reseating. **Each of those three delete statements excludes completed hand-off rows**, using
`handed_off_tildeling_filter()` **inside the `DELETE` itself** (`.exclude(Exists(...))`), not as a
pk list computed beforehand. A hand-off committed between the two steps would otherwise be deleted.

After the delete, the existing survivor logic already does the right thing, because it reads
*whatever rows remain*:
- surviving rows occupy headcount;
- tier A excludes their holders from that tier's seating;
- tier B's `held` set prevents a second row on the same `Vagt`;
- their minutes stay in the holder's projected balance, so the holder ranks later, as for any shift they hold.

**The already-allocated guards are unchanged.** A remaining `TILDELT` hand-off row still counts as
"allocated", so `force` is still required.

The Køkkengruppen allocation form's success message, and `allocate_koekkenvagter`'s report, add:
*"N aftalte byttehandler bevaret, M åbne tilbud bortfaldet."* An open offer on a non-handed-off row
is cascaded away with that row. The offerer may be reseated anywhere. Consistent with every manual
force re-run today, no resident is notified, and this stays the deliberate escape hatch for a
genuine correction (A1.2).

The same three sites are where the Amendment 1/P2 idempotency tests live. The force-twice idempotency
test must gain a hand-off case.

### 5.6 Closing what a move invalidated

After any take-over, whole-shift take-over or trade, in the same transaction:
- every **other** `AABEN` `VagtBytte` on a moved row → `BORTFALDET`. The row now belongs to someone who never offered it;
- every `AABEN` proposal on an offer just closed → `BORTFALDET`;
- every `AABEN` proposal that uses a moved row as `modydelse` → `BORTFALDET`;
- whole-shift take-over only: open proposals using the **deleted** row as `modydelse` disappear with it by cascade. That is acceptable, because the proposer still holds nothing different.

### 5.7 Interactions with existing machinery

| | effect |
| --- | --- |
| **Ledger / obligation** | None. Credit follows the row on `UDFOERT` (A4.5). `post_obligation` never reads assignments. A whole-shift collapse preserves supply exactly (§5.3). |
| **Ranking** | A taken shift is an ordinary `TILDELT` row. It raises the taker's projected balance (they rank later) and lowers the giver's (they rank earlier). This is A4.5's compensation, and nothing is written for it. |
| **Fridag** (with the A5 supplement) | Deletes only that day's `Vagt` rows. Assignments, offers and proposals on them cascade. The holders, offerers included, get the fridag message. Hand-offs elsewhere in the month are untouched. |
| **Reconciliation** | **Unchanged and not one of the three excluded deletes.** It vacates only residents absent from the real list. A present resident's hand-off is never touched (A2.3). A taker who turns out to have left is vacated, which is correct. |
| **`override_remove`** | Unchanged. Køkkengruppen may remove any `TILDELT` row. Offers on it cascade. No notification, as today. |
| **Flags** | A row with any flag history cannot be offered (§3). After a take-over, the taker is fully responsible: a no-show flag lands on them, and the offerer is off the hook. |
| **Tablet** | No change. It shows the current holder. |
| **Summer (P3)** | Summer claims are ordinary rows, so everything here applies. P3's page stops telling residents to "contact Køkkengruppen" (P3 §7) and points to offering instead. `claim_vagt` reuses `may_hold` (§3). |

## 6. Notifications

Sent on the existing `koekken` topic. The audience is narrowed through
`access.allowed_subscribers(subscribers(TOPIC).filter(user=…))`, exactly as `resolve_anmeldelse`
does. Messages go out via `core.push.send` from the view, which defaults to dispatch after commit.
The person who pressed the button never gets a push.

| moment | to | message (Danish, final wording at build time) |
| --- | --- | --- |
| take-over | offerer | "{navn} har overtaget din {vagt}. Du er ikke længere på vagten." |
| whole-shift take-over | offerer | "{navn} har overtaget hele {vagt}. Du er ikke længere på vagten." |
| trade proposal received | offerer | "{navn} foreslår at bytte din {vagt X} med {vagt Y} — svar i app'en." |
| trade accepted | proposer | "{navn} har accepteret byttet: du har nu {vagt X} i stedet for {vagt Y}." |

No push for: a declined or lapsed proposal (the page shows it), an expired offer (Q3), or a new
offer (Q4: Den Hurtige is the broadcast, §8).

The opt-in wording must be updated. Both the push bar's `on_label`/`success_text` in
`templates/koekken/index.html` and the `wants_koekken` comment in `core/models.py` currently promise
flag rulings only.

## 7. UI

All of this follows P2 §10: one shared context builder per partial, htmx POSTs return the
re-rendered partial (errors render inside it, never through the messages framework), an unavailable
action's button is **absent** rather than disabled, forms are hand-rendered with Danish labels, and
every view is under `access.access_required`.

**Resident index page.** One new partial, `_bytte.html`, with context builder
`_bytte_context(request)`. It absorbs the existing "Kommende vagter" list and contains:

- **Mine kommende vagter.** Each row has "Tilbyd vagten" where offering is allowed, with an htmx confirm: *"Du er stadig ansvarlig for vagten, indtil nogen tager den."* An offered row shows "Tilbudt — ingen har taget den endnu" and a "Træk tilbud tilbage" button. A row received through a hand-off is labelled "(overtaget fra {navn})" or "(hele vagten)".
- **Forslag til mig** (step 2). Incoming trade proposals with "Acceptér bytte" / "Afvis".
- **Vagter til overtagelse.** Everyone else's open, unexpired offers:
  - "Tag vagten" for anyone who may take it;
  - "Tag hele vagten (6 t)" instead, for the offerer's partner on that shift;
  - step 2: "Foreslå bytte", a `<select>` of the viewer's own offerable rows.

Each button is computed by predicates mirroring the services: `can_offer`, `can_take`,
`can_take_whole`, `can_propose`, batched as `open_flag_tildeling_ids` already is.

**URLs** (all POST):
- `vagt/<pk>/tilbyd`
- `bytte/<pk>/traek-tilbage`, `bytte/<pk>/tag`, `bytte/<pk>/tag-hele`, `bytte/<pk>/foreslaa`
- `forslag/<pk>/accepter`, `forslag/<pk>/afvis`, `forslag/<pk>/traek-tilbage`

**Køkkengruppen page.** A read-only list, "Åbne byttetilbud — de næste 14 dage": shift, offerer, and
when it was offered. This is the early warning A4.6 asked for. It needs no actions: overrides
already exist.

**Explanation page.** One paragraph: offering is not unclaiming, the offerer stays responsible until
someone takes the shift, credit goes to whoever works it, and giving shifts away means getting more
later.

**`demo.py`**: an open offer, a completed take-over, a whole-shift take-over on a weekday
aftenvagt, a pending trade proposal (step 2), and a Den Hurtige-posted offer (step 3).

## 8. Den Hurtige post (step 3)

- **A new channel** in `den_hurtige/channels.py`: `Channel(slug="koekken", name="Køkkenvagter", ...)` with `default_duration=2880` and an existing sprite icon. It is a tuple entry and **needs no migration** (that module's own docstring). `checks.py` E007–E010 validate it. Residents can mute it on its own with the existing channel mutes.
- **One public service function in `den_hurtige.services`** (e.g. `publish_post(author, content, channel, expires_at)`) creates the `QuickPost` and calls `notify_new_post`. `den_hurtige.views.create_post` is refactored to call it, so manual posts and kitchen posts behave identically. **Den Hurtige never imports `koekken`**: the dependency runs one way only. This is the first time any feature posts into Den Hurtige, and both modules' docstrings should say so.
- **Author** is the offering resident. `QuickPost.author` is the user model, which is `Resident`, and no system account exists or is needed.
- **Consent**: a "Del i Den Hurtige" checkbox on the offer action, ticked by default. Unticked, nothing is posted.
- **Content**, generated: e.g. *"Jeg kan ikke tage min aftenvagt tirsdag 12. jan. Kan du? Tag den under Køkkenvagter: /intern/koekken/"*. A feed message cannot carry a take button, so the link is essential. Check at build time whether Den Hurtige renders URLs as links, and use an absolute URL if it does not.
- **Expiry**: the earlier of the shift's start and now + 2 døgn (the longest duration Den Hurtige offers). When the offer closes for any reason (taken, whole-shift taken, traded, withdrawn, lapsed), the post is archived immediately by setting `expires_at = now`. Den Hurtige already defines "expired" as "archived", so this needs no new delete semantics. If the resident has deleted the post themselves, the `SET_NULL` link simply finds nothing.
- **Push**: Den Hurtige's own `notify_new_post` delivers to that channel's subscribers, minus mutes. Køkkenvagter sends nothing extra.
- **Access**: both features are open to the whole house (`ACCESS_ROLES = None`). If either is ever re-gated, a post could reach people who cannot open the link. A code comment records this, and no mechanism is added.

## 9. Build order

| step | contents | target |
| --- | --- | --- |
| 0 | **Amendment 5 supplement** (main design doc): fridag removes only the day's shifts, and the report names who is removed. | before step 1 |
| 1 | `VagtBytte`; offer / withdraw / take-over / whole-shift take-over; `may_hold`; the §5.5 exclusion in the three deletes plus the report line; §6 notifications (take-over rows); §7 UI except trading; Køkkengruppen list; explanation; admin; demo; `erd.md`. | ideally 1 Feb 2027 |
| 2 | `VagtBytteForslag`; propose / withdraw / decline / accept; the two trade notifications; trade UI. | after step 1 |
| 3 | Den Hurtige channel, `publish_post`, checkbox, archive-on-close. | after step 2 |

Each step is independently shippable, and step 3 can be cut without touching steps 1–2.

## 10. Tests

- **Rules.** Every §3 refusal: started shift, non-`TILDELT` row, row with any flag history, a second open offer, taking your own offer, taker already on the `Vagt`, taker outside the month's population, taker moved out before the shift date. Each fails with a Danish message and changes nothing. A `weekday_unavailable` resident **may** take a weekday tier-A offer (the deliberate non-check).
- **Take-over.** The row's pk is unchanged, and its resident is now the taker. The offer is `OVERTAGET`. Marking done credits the taker. The giver's and taker's projected balances move by the shift's minutes.
- **Concurrency.** Two takers on one offer produce exactly one move and one clean refusal. A take-over racing `allocate_month(force=True)` yields either the preserved hand-off or a clean refusal, never a half state.
- **Expiry.** An offer whose shift has started is not listed and cannot be taken. The offerer still holds the row.
- **Whole-shift.** Only the partner sees the button. The `Vagt` becomes (1, 360). Month supply and `post_obligation` totals are unchanged. Marking done credits 360, and an upheld flag on a marked-done shift reverses 360. A `headcount=3` shift is refused. A partner with their own open offer is refused. The offer record survives, re-pointed. Regeneration does not reset the shift.
- **Survival (Q2c).** After a hand-off, `allocate_month(force=True)`, `allocate_tier_a(force=True)` and `allocate_tier_b(force=True)` all leave the handed-off row in place, and it still occupies headcount. A force re-run run twice with no data change is still idempotent with a hand-off present. Open offers on other rows lapse. The report counts both.
- **Reconciliation.** A present resident's hand-off is untouched. A departed taker's handed-off tier-A row is vacated.
- **Fridag** (with step 0). Deleting a day cascades its offers. The offerer is notified via the fridag message. Hand-offs on other days of the month are untouched.
- **Trade (step 2).** The swap is atomic: a forced failure on the second row leaves both rows unchanged. Every overlapping offer and proposal is closed. Proposals on a withdrawn offer lapse. Trading places on the same `Vagt` is refused.
- **Notifications.** Each §6 moment pushes to exactly the right resident, narrowed through `allowed_subscribers`. Nothing is pushed to the actor, on decline, on expiry, or for a new offer.
- **UI.** Each button is absent wherever its service would refuse (P2 §10), with the action URL asserted absent. No unclaim URL exists anywhere. The partial and the full page render identically.
- **Den Hurtige (step 3).** Ticked: one `QuickPost` in `koekken`, authored by the offerer, with `expires_at` = min(start, +2 døgn), and `notify_new_post` called. Unticked: none. Closing the offer archives the post. Manual `create_post` behaviour is unchanged after the refactor.

## Rejected alternatives

- **Køkkengruppen approval for each hand-off.** Both residents have already consented. Approval would add a bottleneck and no information.
- **The offerer confirms each taker** (Q1 c). It is slower close to the shift, and posting the offer is already consent.
- **Unsolicited requests for shifts that have not been offered** (Q1 b). Residents can ask in person, and the holder then posts the offer.
- **A trade as two separate take-overs.** If the second fails, one person holds both shifts.
- **An unclaimed offer reverting to Køkkengruppen** (Q3 c), **a reminder push** (Q3 b). Rejected as decided.
- **A Køkkenvagter broadcast push for new offers** (Q4 b/c). Den Hurtige does this job, with its own per-channel mutes.
- **A `pladser` column, dropping the unique constraint, or a credit bonus at marking time** for whole-shift take-over. See §5.3.
- **An after-the-fact claim for a no-show partner** (Q5 b). Køkkengruppen handles it with a manual adjustment.
