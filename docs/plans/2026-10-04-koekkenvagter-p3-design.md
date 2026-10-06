# Design: Køkkenvagter P3 — summer self-signup and away ranges

**Status: approved 2026-10-04** (stakeholder decisions relayed through the coordinating session, one
question at a time; recorded in §2). **Not yet built.** Phase design inside
`2026-09-21-koekkenvagter-design.md` (architecture, Amendments 1–5) and
`2026-09-28-koekkenvagter-p2-design.md` (P2). Read those first; this document assumes them.

**This replaces the architecture doc's "Summer (Jul–Aug)" section and its `FerieUge` model.** Both were
forward references written before P3 was designed, and neither survives: summer is not allocated at
all, and presence is recorded as away date ranges, not weekly checkboxes.

## 1. What P3 is

During the summer periode (`SOMMER`, Jul–Aug) there is **no allocation algorithm**. Residents claim
shifts themselves, as on the old paper list, but every shift is still a real `Vagt` and every claim a
real `VagtTildeling`, so the tablet, marking done, flagging and the ledger all work unchanged.
Separately, residents can record when they are away, and the whole house can see it.

How summer *should* work is to be **voted on at a house meeting** (stakeholder's explicit
requirement). P3 is therefore deliberately the smallest machinery that does not pre-decide anything
the vote may decide — see §9 for what is left open.

## 2. Decisions

| | decision |
| --- | --- |
| Summer allocation | **None.** Self-signup only. Algorithmic summer allocation may come later if it makes sense. |
| Presence input | **Away date ranges** ("væk fra–til"), add and delete only, no edit. |
| Presence effect | **Informational only.** Does not affect obligation and does not restrict claiming. |
| Presence visibility | **The whole house** sees who is away each week. |
| "Has declared" marker | **Not built.** Nothing in v1 would read it. Add it if the vote makes declaring mandatory. |
| Note on an away range | **Not built.** |
| Summer obligation | **Exactly as any other month.** Supply / everyone on the `Residency` list, home or away. |
| Summer credit | **Exactly as any other month.** A claimed shift marked done earns normal `ARBEJDE` credit. |
| Claims | **Binding.** A resident can never unclaim. Only Køkkengruppen can remove one (existing override). |
| Unclaim, system-wide | **Not allowed anywhere** — summer or not. Swapping/hand-off is Amendment 4 (§8). |

### Two consequences accepted knowingly — do not "fix" these by accident

- **A resident away all summer is charged the full monthly obligation with no realistic way to earn it
  back during summer.** Intentional: the stakeholder's rule is that everyone is charged the same each
  month, home or away. It self-corrects for that individual afterwards — they rank as most behind, so
  autumn allocation hands them more shifts.
- **Every unclaimed summer hour becomes house-wide debt that cannot be worked off.** Obligation equals
  the full generated summer supply, but credit exists only for shifts actually claimed and done, and
  every autumn/spring hour is already matched by its own month's obligation. The house mean therefore
  falls each summer by (unclaimed hours / residents) and does not recover; under the absolute-balance
  penalty rule that is money at move-out. This is the same shape as the architecture doc's finding 4,
  and it was explained and **accepted** rather than escalated to the house meeting. If it ever grows
  uncomfortable, re-running the launch rebase (the architecture doc's existing escape hatch) resets
  the anchor.

`post_obligation` needs no summer special case at all: with the full `VagtRegel` schedule generated,
supply / roster lands at the normal ~3.4 h/resident/month. It is computed, not a constant.

## 3. Data model

```
Fravaer(resident, start_date, end_date, created_at)     check: start_date <= end_date
```

- One row per away range. Overlapping ranges for the same resident are **refused** in the service with
  a Danish message (this repo uses no Postgres exclusion constraints; validation lives in the service,
  the form calls it).
- General by shape (it could later carry exchange, internship, illness — the architecture doc's own
  open item), but **collected and shown for summer only** in v1: the input lives on the summer page and
  a range must intersect a `SOMMER` periode.
- A resident may add ranges and delete their own ranges that have not yet ended. No edit: delete and
  re-add.
- A range may cross a periode boundary (e.g. 28 Jun–3 Jul); it is stored as entered, and the summer
  page shows only its summer days.
- **Implementation notes (step 2).** Ranges are inclusive; adjacent ranges (1-10 Jul, 11-20 Jul) are not an
  overlap (`a.start <= b.end and b.start <= a.end`) and are never merged. An already-ended range cannot be
  added (mirrors the delete rule); an ongoing one can. No row locking: the only race is one resident
  double-submitting, whose worst case is a cosmetic duplicate row.
- **No other schema change.** A claim is an ordinary `TILDELT` `VagtTildeling`. `Vagt`, `KoekkenPost`,
  `VagtRegel`, `Fridag` and `Praeference` are unchanged.

**The A5.3 generation seam gets no second source.** Amendment 5 built `is_fridag` as a seam
specifically so P3's summer presence could plug in. P3 no longer needs it: summer generates the full
schedule and presence does not affect generation. The seam stays exactly as it is, and its docstrings
are updated so nobody goes looking for a summer source that was never built.

## 4. Summer is shut out of allocation

One predicate — "is this periode allocated" (`kind != SOMMER`) — used by every guard below, so the
rule has one definition.

| where | behaviour for a SOMMER month |
| --- | --- |
| `allocate_tier_a`, `allocate_tier_b` | Raise `KoekkenAllocationError` with a Danish message. Lowest level, so every path inherits it. |
| `allocate_month`, `allocate_batch` | Same refusal, checked **before** the deadline-timing guard so the message is the right one. Reaches the Køkkengruppen allocation form and `allocate_koekkenvagter` through their existing error handling. |
| `roll_forward_allocation` | Log and return `None` before trying anything. It currently catches only `KoekkenNoPopulationError`, so without this explicit check the July cron run would die on the new guard. |
| `reconcile_month` | Log and no-op. |
| `declare_fridag` | Delete the `Vagt` rows (claims cascade), **never** call `allocate_month`, re-post obligation as normal, and notify every resident whose claim disappeared, with the "bortfaldet" message. **As of the Amendment 5 supplement of 2026-10-04 (main design doc), this is no longer a SOMMER special case: every periode behaves this way**, and the report names the removed residents. |
| `post_obligation`, `mark_udfoert`, `resolve_anmeldelse` | **Unchanged.** |

**This part is needed regardless of P3's other pieces and regardless of the vote.** Once
`KOEKKEN_JOBS_ENABLED=1` (planned 2 Dec 2026), the 1 Jul 2027 roll-forward would otherwise allocate
July algorithmically, and the 1 May deadline batch and `declare_fridag`'s force re-allocation would
allocate SOMMER too — the one thing the stakeholder explicitly ruled out.

### Generation

The full `VagtRegel` schedule, as the old list had every slot. Fridage still apply through the seam.
Today's cron generates only the periode containing today, so SOMMER would first appear on 1 July and
nobody could claim early-July shifts in advance. **Change:** the monthly generation task additionally
generates the *next* periode when that periode is SOMMER and its deadline (1 May) has passed — so
claiming opens on the first cron run after 1 May. Køkkengruppen can still generate it by hand earlier.
No other periode's generation timing changes.

## 5. Preference machinery — SOMMER becomes transparent

SOMMER preferences now have no consumer. Leaving the A1.3 machinery as it is would (a) show a banner in
late April asking for summer preferences that do nothing, (b) route preference writes made Dec–Apr to
a SOMMER row nothing reads, and (c) worst, make Efterår's missed-deadline fallback read the empty
SOMMER periode and silently drop every resident's Forår `weekday_unavailable` declaration.

So, everywhere a preference rule says "next" or "previous" periode, it means the next/previous
**allocated** periode:

- **`in_preference_window`** checks the windows of the next allocated periodes, skipping SOMMER. No
  summer window, no summer banner. **This also fixes an existing bug:** today it checks only the
  current and next periode, and between 24 and 30 June the next periode is SOMMER, so Efterår's window
  (24 Jun–1 Jul) is detected only on 1 July, its last day. Reproduced 2026-10-04:
  `in_preference_window(at=2027-06-24)` and `(at=2027-06-30)` return `None`; `(at=2027-07-01)` returns
  Efterår 2027. Must be fixed before 24 June 2027.
- **`_redirect_past_deadline` and `_redirect_past_deadline_pure`**: "next" skips SOMMER.
- **Living inside SOMMER**: for preference resolution the resident's "current" periode is the
  **following Efterår**. The A1.3 mid-period-arrival exemption therefore applies to Efterår (someone
  arriving in August with no Efterår row may create one; it affects only Efterår months not yet
  allocated, exactly A1.3's intent); otherwise the write redirects past Efterår as usual. Applies to
  both `_preference_write_target` and `preference_target_periode`.
- **`_effective_weekday_unavailable_ids` fallback** looks back to the previous allocated periode, so
  Efterår falls back to Forår.

Every pure (no-DB) helper and its write-path twin change in lockstep — the same discipline the P2
review rounds established. This is the riskiest part of P3: it touches rules that went through several
review rounds. The A1.3 supplement's "window wins first" ordering, the deadline-day-belongs-to-the-
resident rule, and the no-write-on-GET invariant are all unchanged.

## 6. Claiming

`claim_vagt(resident, vagt)` — atomic, `select_for_update` on the `Vagt` row. Refuses (Danish
`KoekkenAllocationError`) unless:

- the vagt belongs to a SOMMER periode;
- the shift has not started (live `VagtRegel.start_time`, the same value `marking_window` reads);
- it has a free place, counting rows of any status;
- the resident does not already hold it (backed by the existing unique constraint; an `IntegrityError`
  from a race is turned into the same refusal);
- the resident is in that month's population (real `Residency` list or the A2.2 projection) and has no
  `move_out_date` before the shift date.

Deliberately **not** checked: away ranges (informational) and any per-resident claim limit (left to the
vote). No time-overlap check is needed: morgen 06–07, frokost 12–13 and aften 17–20 cannot overlap.

There is **no unclaim function.** Køkkengruppen uses the existing `override_remove`; `override_assign`
can fill an empty summer shift. A claimed row is `TILDELT` like any other, so it counts in the projected
balance — summer claims made before the Efterår batch on 2 July affect autumn ranking, as they should.

## 7. UI

Danish throughout, P2 §10 conventions (shared context builder per partial, htmx POST returns the
partial, an unavailable action's button is absent rather than disabled, hand-rendered forms).

**`/intern/koekken/sommer/`**, behind `access_required`:

- **The shift grid**, week by week: each shift with its claimants' names and free places. A "Tag vagt"
  button only where `claim_vagt` would succeed, with an htmx confirm: *"Vagten er bindende — kun
  Køkkengruppen kan fjerne den."* Until Amendment 4 (`2026-10-04-koekkenvagter-a4-design.md`) ships, the page also says to contact Køkkengruppen
  if you cannot take a shift you claimed.
- **"Væk denne uge"**: for each week, the names of residents whose away ranges overlap it.
- **"Mit fravær"**: your own ranges, an add form (fra/til), and delete on ranges that have not ended.

Elsewhere:

- The resident index page links to the summer page from the moment summer shifts exist until 31 August.
  No banner: there is no deadline.
- Køkkengruppen's page lists unclaimed summer shifts in the next 14 days — visibility only; what
  happens to them is the vote's question.
- The preference explanation page gains one paragraph on summer: obligation as normal, claim your own
  shifts, claims are binding.
- **Implementation notes (step 2).** The summer page shows the *target summer*: this year's SOMMER while
  today <= 31 Aug, otherwise next year's. The page is always reachable by URL; the index link shows only
  from that summer's deadline (1 May) through 31 Aug. Built as pure functions (`target_summer`,
  `summer_link_visible`) so a GET never writes a `Periode`.
- `demo.py`: a summer month with claims, an unclaimed shift and two away ranges.

## 8. Swapping and hand-off: Amendment 4, not P3

The stakeholder, on approving this design: claims must never be droppable — *"that should not be an
option any time"* — *"but you should still be able to swap a shift or let someone else take it, if they
offer to."* That is exactly Amendment 4's two modes (take-over and trade), and "any time" makes it
system-wide. **Amendment 4 is therefore approved in direction and is the next design round**, ahead of
P3 steps 2–3, because regular allocated shifts start on 1 Feb 2027 and need it before summer claims do.

**Now designed in `2026-10-04-koekkenvagter-a4-design.md`.** P3 step 3's `claim_vagt` must reuse its
`may_hold` helper rather than write its own population/move-out check.

It needs nothing special from P3: summer claims are ordinary future `TILDELT` rows, which is exactly
what A4.3 operates on. The stakeholder's "binding" answer also largely resolves A4.6's open question —
an offer nobody takes expires at the shift's start and **the offerer stays responsible** — to be
confirmed in the A4 round.

## 9. Left to the house vote — deliberately not built

- What happens to an unclaimed summer shift (v1: stays empty, visible to Køkkengruppen).
- A cap on claims per resident (v1: none).
- Whether declaring absence is mandatory, and any deadline for it (v1: optional, no deadline).
- Whether being away blocks claiming (v1: it does not).
- Full versus reduced summer schedule (v1: full).
- Whether algorithmic summer allocation returns later.

## 10. Build order

| step | contents | deadline |
| --- | --- | --- |
| 1 | §4 (except the generation change) and §5, including the window-bug fix. | 2 May 2027 (window fix: 24 Jun 2027) |
| — | Amendment 4 design and build. | ideally 1 Feb 2027 |
| 2 | `Fravaer` and its UI. **Built** (e31086f). | before claiming opens, May 2027 |
| 3 | Claiming, the summer page, §4's generation change, Køkkengruppen visibility, demo. | May 2027 |
| — | Amendment 6 (Køkkengruppen awarding event credit). | after the above |

## 11. Implementation-time check

`post_obligation` reads only real `Residency` rows — it never projects. If indstilling does not publish
July and August lists, summer obligation is silently zero. Confirm summer lists are published; if not,
raise it as a decision rather than improvising (the fix would need the A2.2 projection, today used by
allocation only).

## 12. Tests

- Every allocation entry point refuses a SOMMER month; roll-forward and reconcile no-op without
  raising; the Køkkengruppen form shows the message.
- `declare_fridag` in SOMMER removes the claims, notifies with the summer message, re-posts
  obligation, and never allocates.
- Claiming: succeeds on an open future shift; refused when full, already started, already held, after
  the resident's move-out, or for a non-SOMMER shift; two concurrent claims on the last place yield
  exactly one row; no resident unclaim URL exists; the claim button is absent where claiming would fail.
- A claimed summer shift marked done posts normal `ARBEJDE` credit; summer obligation equals supply
  divided by the roster.
- Preferences: Efterår's window is detected on 24–30 June (regression for the bug); there is no SOMMER
  window or banner; no write ever lands on SOMMER; Efterår's fallback reads Forår; a mid-summer arrival
  creates an Efterår row.
- `Fravaer`: overlap refused; start after end refused; only the owner can add or delete; the weekly
  "who is away" listing is correct across month and periode boundaries.
- Generation: the task generates SOMMER after 1 May; extend `test_scheduled_tasks.py` if the task
  changes.
- Existing tests that assumed SOMMER is allocated are **rewritten, not deleted**: the A2.8 batch-clamp
  test now asserts refusal; the SOMMER preference tests move to an Efterår→Forår boundary; the
  DevClock visibility walk's invariant becomes "allocated ahead outside SOMMER".

## Rejected alternatives

- **Scaling summer slot volume to reported presence** (a binary per-week rule, a presence step table,
  or a fixed reduced schedule) with algorithmic allocation per week. Designed and analysed — the
  architecture doc's own claim that "obligation = supply / present" sizes the summer load automatically
  holds only if the whole house is in or out together; with 20 of 61 home a full week costs each of them
  ~2.4 h/week — and then set aside entirely when the stakeholder chose self-signup.
- **Weekly presence checkboxes (`FerieUge`).** Not precise enough; the first and last summer weeks are
  partial.
- **Suspending the ledger for summer** ("record but post nothing") until the vote. Proposed as the
  policy-neutral default; the stakeholder chose normal obligation and credit instead.
- **A summer-only hand-off mechanism inside P3.** It would be a second copy of Amendment 4's logic, and
  A4 has interactions with allocation (`force` re-runs, reconciliation, fridag deletion) that must be
  designed once, for every periode.
