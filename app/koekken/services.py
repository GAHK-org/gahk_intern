"""Køkkenvagter's decision layer — P1 (slots, tier-A allocation, ledger, obligation posting) plus
Amendment 1 (FCFS tiebreak, allocation look-ahead, preference locking), Amendment 2 (where a
three-months-out population comes from) and Amendment 3 (reconciliation eligibility, residents who
arrive with no preference).

There is no view layer yet (see the design doc's "Phasing"), so this module, not a views.py, is the
primary interface: management commands are thin wrappers around the functions below, and so will the
eventual views be. Read `docs/plans/2026-09-21-koekkenvagter-design.md` — particularly "Allocation",
"Ledger and obligation" and the "Amendment 1" section at the end — before changing any of this; the
choices below (soft floor, projected-balance ranking, largest-remainder obligation split, the
look-ahead guard, preference locking) are explained there, not repeated here beyond a pointer.

**Amendment 1 in one paragraph:** ties in every tier-A ranking now break on `Praeference.declared_at`
(earlier wins; no row sorts last) rather than on an arbitrary, stably-arbitrary `pk` order; the
balance used for that ranking is *projected* (ledger balance plus not-yet-credited `TILDELT` hours),
so a look-ahead window of several months allocated in one sitting doesn't keep handing the next
month to the same "most behind" residents before the ledger has caught up; `allocate_tier_a` now
refuses to silently re-shuffle a month that already has `TILDELT` rows unless `force=True`; and
`set_preference` resolves which `Periode`'s row a preference edit actually lands in, per the
locking rule in A1.3 -- supplemented 2026-10-01 so that an open preference window wins first,
ahead of A1.3's own rules, which are otherwise unchanged (see `_preference_write_target`).
`post_obligation` and everything below "Ledger and obligation" in the design doc are explicitly
untouched.

**P3 step 1** (`docs/plans/2026-10-04-koekkenvagter-p3-design.md` §4-§5): the summer periode
(`SOMMER`) is never allocated -- residents claim its shifts themselves (a later P3 step). One
predicate, `periode_is_allocated`, is the single definition of that rule. Every allocation entry point
refuses a SOMMER month (`allocate_tier_a`/`allocate_tier_b`/`allocate_month`/`allocate_batch` raise
`KoekkenAllocationError`; `roll_forward_allocation` and `reconcile_month` log and no-op; `declare_fridag`
deletes the shifts but never re-allocates in ANY periode, per the A5 supplement of 2026-10-04), while `post_obligation` is deliberately UNCHANGED -- summer
obligation posts exactly as any other month. The preference machinery treats SOMMER as transparent:
everywhere A1.3 says "next"/"previous" periode it now means the next/previous ALLOCATED one, and a
resident living inside SOMMER has the following Efterår as their preference "home" periode
(`_preference_home_periode`/`_preference_home_periode_pure`). `in_preference_window` also stops missing
Efterår's 24-30 June window (a pre-existing bug, see its docstring).

**P3 step 2** (same design doc §2/§3/§7, section "P3 step 2: away ranges" at the bottom): `Fravaer`, the
informational away ranges residents register for summer (`add_fravaer`/`delete_fravaer`, add and delete
only) and the house-wide weekly listing (`away_by_week`). Nothing here is read by allocation, claiming,
obligation or generation.

**Amendment 4, step 1** (`docs/plans/2026-10-04-koekkenvagter-a4-design.md`): hand-off of vagter. A
resident offers a future `TILDELT` row (`offer_tildeling`); another takes it over (`take_over`, which
MOVES the existing row) or, on a two-person shift, the offerer's partner takes the whole shift
(`take_over_whole`, which collapses the `Vagt` to `(1, 2d)`). Offers expire when the shift starts, derived
and never written (`has_started`), so every read goes through it. Completed hand-offs survive a force
re-run: the three TILDELT deletes first lock the candidate rows (`select_for_update`) and only then
delete with `handed_off_tildeling_filter()` excluded (see `_delete_replaceable_tildelinger`). Every write
locks rows, then the Vagt, then the offer (see the comment above `has_started`).

**Amendment 4, step 3**: an offer can be shared in Den Hurtige's `koekken` channel under the offerer's
own name, and the post is archived whenever the offer leaves `AABEN`. This is the FIRST time any feature
posts into Den Hurtige (via `den_hurtige.services.publish_post`); the dependency runs one way only.

**Amendment 4, step 2** (same design doc): trading. A resident proposes one of their own future rows Y
in exchange for an open offer's row X (`propose_trade`); the offerer accepts (`accept_trade`, an atomic
two-row swap of the residents, pks unchanged), declines, or the proposer withdraws. Lock order is now
four levels (rows ascending pk -> Vagt -> offers ascending pk -> proposals ascending pk), every service
locks BOTH X and Y before touching an offer or proposal, and every bulk close (`_close_invalidated_offers`,
`_close_forslag`) selects its rows `FOR UPDATE` in ascending pk order before updating them -- never a bare
`.filter().update()`. A row that is the `modydelse` of an `ACCEPTERET` proposal is a completed hand-off
too (`handed_off_tildeling_filter`), so both sides of a trade survive a force re-run.

**Amendments 2 and 3 in one paragraph:** the look-ahead window Amendment 1 allocates into has no
real `Residency` list to draw on (nothing in this codebase ever creates one more than a month
ahead), so `_resolve_population` projects one in memory -- the most recent published list at or
before the target month, minus anyone whose `move_out_date` has already passed -- and is never
written back (see A2.2; writing it would make køkken a silent co-owner of `Residency`). When the
real list is later published, `reconcile_month` corrects the projection additively: assignments for
anyone the projection got wrong are vacated and the freed slots, plus any genuine new arrival, are
re-seated by the exact same eligibility rules `allocate_tier_a` uses -- both now call a shared
`_seat_tier_a` core (A3.1 correcting A2.3's original pool-blind wording), so a weekday slot can
never land on a resident who declared `weekday_unavailable=True` and a slot with no eligible
candidate stays unfilled and queued rather than forced. Every assignment for a resident present on
both the projection and the real list is left completely untouched.

**P2** (`docs/plans/2026-09-28-koekkenvagter-p2-design.md`) adds everything below the Amendment 3
section: `allocate_tier_b` (aftenvagt, a single algorithmic pass -- never a claim-based signup --
that reads THIS MONTH'S already-written tier-A outcome for the weekend-compensation ordering and the
aftenvagt-avoidance exclusion, so it must run after `allocate_tier_a`; see `allocate_month`), a
`_fill_leftover_tier_a` follow-up step used ONLY by `allocate_tier_a` (never by `reconcile_month`,
whose Amendment-3 behaviour is untouched) that makes "no tier-A slot left open" actually hold and
gives the aftenvagt-avoidance pattern its extra tier-A slot when capacity allows, the day-preference
write path (`set_preference_dage`/`set_preferences`) alongside Amendment 1's `set_preference`, the
marking-done self-report flow (`mark_udfoert`, `marking_window`, read live off `VagtRegel.start_time`
-- see that field's docstring for why it is never snapshotted), and the flag/adjudication flow
(`flag_tildeling`, `resolve_anmeldelse`). **The single most important invariant added here:**
preferences decide WHICH slot a resident gets, never WHO is picked next -- `allocate_tier_b`'s
ranking never reads a day preference, only the existing balance/declared_at/pk comparator (plus the
one already-approved exception, weekend compensation) -- see that function's docstring.
"""

import itertools
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import cast

from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet, Sum
from django.utils import timezone

from core.clock import current_date, current_datetime
from core.danish import MONTHS, WEEKDAYS
from residents.models import Residency, Resident

from .models import (
    Fravaer,
    Fridag,
    KoekkenPost,
    Periode,
    Praeference,
    PraeferenceDag,
    Vagt,
    VagtAnmeldelse,
    VagtBytte,
    VagtBytteForslag,
    VagtRegel,
    VagtTildeling,
)

logger = logging.getLogger(__name__)

TOPIC = "koekken"  # core.models.TOPIC_FIELDS key -- P2 design doc §6's flag-ruling notification (F4).


class KoekkenAllocationError(Exception):
    """Base for allocation failures that must be surfaced, never swallowed."""


class KoekkenNoPopulationError(KoekkenAllocationError):
    """Raised specifically when `_resolve_population` could resolve no population at all for a
    month -- no real `Residency` list AND no earlier published list to project from (A2.2's genuine
    impossibility, not a normal case). A narrower subclass than the generic `KoekkenAllocationError`
    on purpose: `roll_forward_allocation` needs to catch exactly this and no-op, per its own
    docstring's promise never to let a scheduled task die, without also silently swallowing the
    "no vagter generated" or "already allocated, use --force" errors `allocate_tier_a` raises for
    other reasons -- those are bugs if they ever reach that catch, not the expected no-op case."""


@dataclass
class TierAResult:
    """What one `allocate_tier_a` run did, for the management command to report and for tests to
    assert on. `unassigned` is the soft floor in the flesh — non-empty in a shortfall month
    (February, per the design doc) and that is expected, not an error.

    `refused_weekend` is a subset of `unassigned`: declarers who did not fit the weekend pool's
    capacity (design doc finding 2 — "if they exceed capacity the excess are refused with a clear
    message"). They land in the same soft-floor bucket as anyone else who missed out this month —
    never in `weekday_assigned`, which they said they can't do — but are broken out separately here
    so an officer can see *why*, not just that they got no slot.
    """

    weekend_assigned: list[Resident] = field(default_factory=list)
    drafted: list[Resident] = field(default_factory=list)  # subset of weekend_assigned who did NOT declare
    weekday_assigned: list[Resident] = field(default_factory=list)
    unassigned: list[Resident] = field(default_factory=list)
    refused_weekend: list[Resident] = field(default_factory=list)


@dataclass
class TierBResult:
    """What one `allocate_tier_b` run did -- P2 design doc §4. `assigned` is every resident seated
    into an aftenvagt slot this run (may include a resident more than once if capacity genuinely
    exceeds population -- see `allocate_tier_b`'s overflow handling). `preference_honoured` is the
    subset who were seated onto a day they actually declared for aftenvagt in `PraeferenceDag`;
    everyone else in `assigned` still got a slot, just not necessarily their declared day (the soft
    floor equivalent for tier B -- a preference is best-effort, never a guarantee). `skipped_avoidance`
    is who was excluded from this run's candidate pool entirely because they already picked up a
    second tier-A shift via `_fill_leftover_tier_a`'s avoidance mechanic -- the whole point of that
    mechanic is that they get MORE tier-A instead of an aftenvagt, so putting them in `assigned` too
    would defeat it.
    """

    assigned: list[Resident] = field(default_factory=list)
    preference_honoured: list[Resident] = field(default_factory=list)
    skipped_avoidance: list[Resident] = field(default_factory=list)


@dataclass
class ReconciliationResult:
    """What one `reconcile_month` run did -- Amendment 2 (A2.3), corrected by Amendment 3 (A3.1).

    `vacated` are residents who held a `TILDELT` row that got deleted because they are no longer on
    the real `Residency` list for this month. `seated` is the outcome of re-seating the resulting
    unfilled capacity via the shared `_seat_tier_a` core, restricted to residents who did not already
    hold a slot this month -- its `weekend_assigned`/`weekday_assigned` are who newly got a slot,
    `unassigned` is who was eligible and available but still missed out (never forced, never handed a
    slot they're ineligible for). `still_unfilled` is the A3.1 queue for Køkkengruppen: every `Vagt`
    row that remains short of headcount after this run -- **note this is looser than "reconciliation
    had no eligible candidate for a specific vacated/new slot" (F6)**: a month with a pre-existing
    structural shortfall baked in from normal allocation (February's soft floor, design doc finding
    3) reports those already-short vagter here too, not only ones reconciliation itself tried and
    failed to fill. Treat it as "currently short of headcount", not "reconciliation specifically
    failed on this one".
    """

    vacated: list[Resident] = field(default_factory=list)
    seated: TierAResult = field(default_factory=TierAResult)
    still_unfilled: list[Vagt] = field(default_factory=list)


@dataclass
class FridagResult:
    """What one `declare_fridag` call did -- Amendment 5 (A5.4, as simplified by the 2026-10-04
    supplement), for the management command to report on and for tests to assert on.

    `created` is the new `Fridag` rows actually written (a `(date, kind)` pair already declared is
    left untouched, not duplicated). `deleted_vagter` is how many already-generated `Vagt` rows for
    those pairs were removed -- 0 when the affected month had not been generated yet, in which case
    `generate_vagter`'s own seam (A5.3) is the only mechanism that will ever apply.

    `removed` is every `(resident, vagt)` pair whose `TILDELT` assignment was deleted along with its
    `Vagt` row, ordered by date, kind, then resident name; a resident holding two deleted shifts
    appears twice. Captured BEFORE the delete, so these `Vagt` instances are already deleted when read
    later: use only `.date`, `.kind` and `get_kind_display()`, never `.pk` or a lazy relation.

    `obligation_reposted_months` is every `(year, month)` whose obligation was re-posted -- gated
    independently of `removed` (see `declare_fridag`'s docstring).

    `notifications` is one entry per distinct removed resident: `(resident, audience, message)` --
    `audience` already narrowed via `koekken.access.allowed_subscribers` exactly as
    `resolve_anmeldelse` narrows its own, ready for the caller to hand straight to
    `core.push.send(audience, "Køkkenvagt", message, "/intern/koekken/", background=False)`.
    **Never sent here**: a management command's own `--dry-run` rolls its wrapping transaction back
    *after* `declare_fridag` returns, so dispatch is the CALLER's job, done only once it knows the
    transaction actually committed (i.e. only when `not dry_run`).
    """

    created: list[Fridag] = field(default_factory=list)
    deleted_vagter: int = 0
    removed: list[tuple[Resident, Vagt]] = field(default_factory=list)
    obligation_reposted_months: list[tuple[int, int]] = field(default_factory=list)
    notifications: list[tuple[Resident, QuerySet, str]] = field(default_factory=list)


def _periode_bounds(for_date: date) -> tuple[str, int, date, date]:
    """(kind, year, start, end) for the semester/summer period containing `for_date`.

    Calendar-anchored per the design doc: EFTERAAR is always Sep 1 .. Jan 31, FORAAR Feb 1 .. Jun 30,
    SOMMER Jul 1 .. Aug 31. `year` is the year the period *starts* in, so January belongs to the
    EFTERAAR that started the previous September.
    """
    month = for_date.month
    if 2 <= month <= 6:
        year = for_date.year
        return Periode.Kind.FORAAR, year, date(year, 2, 1), date(year, 6, 30)
    if 7 <= month <= 8:
        year = for_date.year
        return Periode.Kind.SOMMER, year, date(year, 7, 1), date(year, 8, 31)
    if month == 1:
        year = for_date.year - 1
    else:
        year = for_date.year
    return Periode.Kind.EFTERAAR, year, date(year, 9, 1), date(year + 1, 1, 31)


def periode_is_allocated(kind: str) -> bool:
    """Whether a periode of `kind` is run through the allocation algorithm at all -- P3 design doc §4.
    False only for SOMMER, whose shifts residents claim themselves. The ONE definition of that rule:
    every guard in this module (and anywhere else) asks this rather than comparing against
    `Periode.Kind.SOMMER` inline, so the rule cannot drift between call sites."""
    return kind != Periode.Kind.SOMMER


def resolve_periode(for_date: date) -> Periode:
    """The `Periode` containing `for_date`, creating it (calendar-anchored bounds) if it doesn't
    exist yet. Idempotent: re-resolving the same date always returns the same row."""
    kind, year, start, end = _periode_bounds(for_date)
    periode, _ = Periode.objects.get_or_create(
        kind=kind, year=year, defaults={"start_date": start, "end_date": end}
    )
    return periode


# NOTE: `_next_periode` and `_previous_periode` below currently have no callers. They are kept on
# purpose, not forgotten: they do NOT skip SOMMER, so reaching for them where a periode that is
# actually allocated is needed would reintroduce the window bug fixed in P3 step 1 (a
# periode-resolution path that doesn't skip SOMMER -- see the P3 design doc §5). Use
# `_next_allocated_periode` / `_previous_allocated_periode` for that; use these two only where SOMMER
# genuinely is the wanted answer.
def _next_periode(periode: Periode) -> Periode:
    """The `Periode` immediately following `periode`. Periods are calendar-anchored and contiguous
    (EFTERAAR's Jan 31 is followed by FORAAR's Feb 1, FORAAR's Jun 30 by SOMMER's Jul 1, SOMMER's Aug
    31 by the next EFTERAAR's Sep 1) — see the design doc's data model — so "the day after this one
    ends" always resolves to the right next periode, created via the same idempotent `resolve_periode`
    the rest of this module uses. Amendment 1 (A1.3): the target of a locked-preference redirect."""
    return resolve_periode(periode.end_date + timedelta(days=1))


# Intentionally kept though uncalled -- see the note above `_next_periode` (P3 step 1 window-bug fix).
def _previous_periode(periode: Periode) -> Periode:
    """The `Periode` immediately preceding `periode` — the mirror of `_next_periode`. Amendment 1
    (A1.3, missed deadline): the source a resident's effective preference falls back to when they
    have no row yet for `periode`."""
    return resolve_periode(periode.start_date - timedelta(days=1))


def _next_allocated_periode(periode: Periode) -> Periode:
    """The next `Periode` after `periode` that is allocated (`periode_is_allocated`) -- `_next_periode`
    that skips SOMMER (P3 design doc §5). Write-path twin of `_next_allocated_periode_pure`. The skip
    is decided on the pure periode first, so only the allocated target's row is ever materialised --
    never a transient SOMMER `Periode` row nothing will use."""
    return resolve_periode(_next_allocated_periode_pure(periode).start_date)


def _previous_allocated_periode(periode: Periode) -> Periode:
    """The closest preceding `Periode` that is allocated -- `_previous_periode` that skips SOMMER (P3
    design doc §5): Efterår's missed-deadline fallback must read Forår, not an empty SOMMER."""
    target = _periode_from_bounds(periode.start_date - timedelta(days=1))
    if not periode_is_allocated(target.kind):
        target = _periode_from_bounds(target.start_date - timedelta(days=1))
    return resolve_periode(target.start_date)


def _preference_home_periode(today: date) -> Periode:
    """The periode a resident "lives in" for preference resolution on `today` (write path): the one
    containing `today`, EXCEPT inside SOMMER, where it is the following Efterår (P3 design doc §5) --
    SOMMER has no preferences, so the A1.3 mid-period-arrival exemption and the redirect both anchor
    on Efterår. Persists the row via `resolve_periode`; pure twin: `_preference_home_periode_pure`."""
    return resolve_periode(_preference_home_periode_pure(today).start_date)


def _deadline_from_start(start_date: date) -> date:
    """The pure arithmetic behind `periode_deadline`, taking a bare start date instead of a
    `Periode` row -- split out so `in_preference_window` can compute a window with NO database
    access at all when `at` doesn't fall inside one (see that function's docstring for why that
    split matters, not just for tidiness)."""
    month = start_date.month - 2
    year = start_date.year
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, start_date.day)


def periode_deadline(periode: Periode) -> date:
    """`periode`'s preference deadline — Amendment 1, A1.2: exactly two calendar months before its
    start (Feb-Jun's is 1 December, Sep-Jan's is 1 July, Jul-Aug's is 1 May). Derived, not stored:
    every `Periode.start_date` is the 1st of a month, so this is exact date arithmetic, not an
    approximation."""
    return _deadline_from_start(periode.start_date)


def is_fridag(for_date: date, kind: str) -> bool:
    """Whether `(for_date, kind)` is excluded from generation entirely -- Amendment 5 (A5.3). The
    SINGLE question `generate_vagter` asks per `(date, kind)` pair, built as a seam rather than an
    inline `Fridag.objects.filter(...)` check, so any additional exclusion source would have exactly
    one place to plug in rather than growing a second, independent skip path beside this one.

    **No second source exists, and none is currently planned.** Amendment 5 built this seam for a
    P3 summer-presence mechanism (`FerieUge`), but P3 as designed does not need it: summer generates the
    full schedule and presence (away ranges) does not affect generation -- see the P3 design doc §3
    (`2026-10-04-koekkenvagter-p3-design.md`). The seam stays as it is; do not go looking for a summer
    source that was never built.
    """
    return Fridag.objects.filter(date=for_date, kind=kind).exists()


def generate_vagter(periode: Periode) -> list[Vagt]:
    """Create the `Vagt` rows for every day in `periode`, one per applicable `VagtRegel` -- except a
    `(date, kind)` pair declared a `Fridag` (Amendment 5, A5.3), which `is_fridag` skips before ever
    reaching `get_or_create` below.

    Idempotent via `get_or_create` on the `(date, kind)` constraint — re-running after `VagtRegel`
    has changed does NOT touch already-generated `Vagt` rows (the snapshot invariant; see
    koekken.models' module docstring). Returns the newly created rows only.
    """
    regler_by_weekend: dict[bool, list[VagtRegel]] = defaultdict(list)
    for regel in VagtRegel.objects.all():
        regler_by_weekend[regel.weekend].append(regel)

    created: list[Vagt] = []
    current = periode.start_date
    one_day = timedelta(days=1)
    while current <= periode.end_date:
        is_weekend = current.weekday() >= 5  # Saturday=5, Sunday=6
        for regel in regler_by_weekend[is_weekend]:
            if is_fridag(current, regel.kind):
                continue
            vagt, was_created = Vagt.objects.get_or_create(
                date=current,
                kind=regel.kind,
                defaults={
                    "periode": periode,
                    "headcount": regel.headcount,
                    "duration_minutes": regel.duration_minutes,
                },
            )
            if was_created:
                created.append(vagt)
        current += one_day
    return created


def _fill_slots(
    vagter: Iterable[Vagt], residents: list[Resident], surviving_by_vagt: dict[int, int] | None = None
) -> None:
    """Assign `residents`, in order, into `vagter`'s open headcount, one shift per resident.

    `surviving_by_vagt` (vagt id -> count of non-`TILDELT` rows already sitting on that vagt from a
    prior run) reduces each vagt's open headcount by however many slots those surviving rows already
    occupy — see `allocate_tier_a`'s re-run handling. Omitted, every vagt's full headcount is open.
    """
    surviving_by_vagt = surviving_by_vagt or {}
    pool = iter(residents)
    for vagt in vagter:
        open_slots = vagt.headcount - surviving_by_vagt.get(vagt.pk, 0)
        for _ in range(max(open_slots, 0)):
            resident = next(pool, None)
            if resident is None:
                return
            VagtTildeling.objects.create(vagt=vagt, resident=resident, status=VagtTildeling.Status.TILDELT)


def _tier_a_sort_key(
    resident: Resident, balances: dict[int, int], declared_at_by_id: dict[int, date]
) -> tuple[int, date, int]:
    """`(projected_balance ASC, declared_at ASC, pk ASC)` — Amendment 1's A1.1 comparator, shared by
    every ranking in `allocate_tier_a` (declarers, the weekend draft, the weekday pool). `pk` is the
    final fallback so the ordering is always total. A resident with no `declared_at` for this
    `Periode` (no `Praeference` row) sorts LAST among ties via the `date.max` sentinel — declaring is
    what earns FCFS priority, so a non-declarer must never sort ahead of a declarer on a tie; never a
    null, which could sort first by accident."""
    return (balances[resident.pk], declared_at_by_id.get(resident.pk, date.max), resident.pk)


def _resolve_population(year: int, month: int) -> list[Resident]:
    """Who tier-A allocation considers eligible for (year, month) -- Amendment 2, A2.2's population
    resolver. A real `Residency` list for that month wins whenever one exists (indstilling's actual
    published roster is always authoritative over a guess). Otherwise this **projects** one in
    memory: the most recent published `Residency` list at or before the target month, minus anyone
    whose `Resident.move_out_date` falls before the month begins.

    **Never written.** This is the whole point of A2.2 -- `Residency` is indstilling's table, read by
    roles, the alumneliste, the kvotient lottery and the stamtræ, and a kitchen feature writing rows
    into it would make it a silent co-owner of the roster every other feature trusts. Do not persist
    this, even via something that looks like the existing `rooms.views_soegvaerelse.
    _carry_roster_forward` roster-carry helper -- calling that (or anything like it) from here was
    explicitly rejected in the design doc for exactly this reason.

    Returns `[]` when no population can be resolved at all -- no real list for the month AND no
    published list to project from ever existed. That is a genuine impossibility (this codebase
    always has at least one published `Residency` list once the house has residents), not a normal
    case: `allocate_tier_a` still raises on it like any other empty-population run, and it is
    `roll_forward_allocation`'s job specifically (per its own docstring) to catch that and no-op
    rather than let a scheduled task die on it.
    """
    real = list(
        Resident.objects.filter(residencies__year=year, residencies__month=month).distinct().order_by("pk")
    )
    if real:
        return real

    latest = (
        Residency.objects.filter(Q(year__lt=year) | Q(year=year, month__lte=month))
        .order_by("-year", "-month")
        .values("year", "month")
        .first()
    )
    if not latest:
        logger.info(
            "koekken._resolve_population: ingen beboerliste er nogensinde blevet offentliggjort -- "
            "kan ikke projicere en befolkning for %s-%02d.",
            year,
            month,
        )
        return []

    month_start = date(year, month, 1)
    projected = list(
        Resident.objects.filter(residencies__year=latest["year"], residencies__month=latest["month"])
        .exclude(move_out_date__isnull=False, move_out_date__lt=month_start)
        .distinct()
        .order_by("pk")
    )
    if not projected:
        # Distinct from the "never published anything" case above (F5): a projection SOURCE existed
        # (latest["year"]/["month"]) but excluding everyone's move_out_date emptied it completely --
        # a mass move-out, which is either real or a data problem worth a human noticing, so this
        # logs at a higher level even though both cases currently no-op identically.
        logger.warning(
            "koekken._resolve_population: seneste offentliggjorte liste (%s-%02d) fandtes, men blev "
            "tom for %s-%02d efter udelukkelse af fraflyttede -- undersøg om dette er en reel "
            "masseudflytning eller en datafejl.",
            latest["year"],
            latest["month"],
            year,
            month,
        )
    return projected


def _effective_weekday_unavailable_ids(resident_ids: list[int], periode: Periode) -> set[int]:
    """Which of `resident_ids` are effectively weekday-unavailable in `periode` — Amendment 1's
    missed-deadline fallback (A1.3): a resident with no `Praeference` row yet for `periode` reads as
    if they carried forward their previous `Periode`'s value, rather than defaulting to available
    (which could hand them a weekday shift they said last periode they could not do) or blocking
    allocation entirely. This supplies only the boolean — a resident who falls back this way still
    has no `declared_at` for `periode` and so still sorts last on an FCFS tie (A1.1); the fallback
    value is never treated as if they had declared it themselves this periode. "Previous" means the
    previous ALLOCATED periode (P3 design doc §5): nobody writes SOMMER preference rows any more, so
    Efterår falls back to Forår rather than landing on an empty SOMMER."""
    rows = Praeference.objects.filter(periode=periode, resident_id__in=resident_ids).values(
        "resident_id", "weekday_unavailable"
    )
    current = {row["resident_id"]: row["weekday_unavailable"] for row in rows}
    missing = [rid for rid in resident_ids if rid not in current]
    fallback: dict[int, bool] = {}
    if missing:
        previous = _previous_allocated_periode(periode)
        previous_rows = Praeference.objects.filter(periode=previous, resident_id__in=missing).values(
            "resident_id", "weekday_unavailable"
        )
        fallback = {row["resident_id"]: row["weekday_unavailable"] for row in previous_rows}
    return {rid for rid in resident_ids if current.get(rid, fallback.get(rid, False))}


def _seat_tier_a(
    weekend_vagter: list[Vagt],
    weekday_vagter: list[Vagt],
    population: list[Resident],
    periode: Periode,
    balances: dict[int, int],
    declared_at_by_id: dict[int, date],
    surviving_by_vagt: dict[int, int] | None = None,
    *,
    log_label: str = "",
) -> TierAResult:
    """The seating core shared by `allocate_tier_a` and `reconcile_month` -- Amendment 3, A3.1's fix
    to Amendment 2's originally pool-blind reconciliation rule. Declarers (per
    `_effective_weekday_unavailable_ids`) go to the weekend pool first, ranked by `_tier_a_sort_key`;
    declarers beyond weekend capacity are refused rather than forced onto a weekday slot they said
    they can't do (design doc finding 2). Remaining weekend capacity is drafted from the rest of
    `population`, same ranking (the weekend pool is mandatory overflow, not opt-in). Weekday slots are
    filled last, same ranking, from whoever is left -- so a weekday slot can never land on a
    weekday-unavailable resident, whether the caller is a fresh month-wide allocation or a
    reconciliation run re-seating only leftover capacity.

    Pure seating logic plus the actual `VagtTildeling` writes (via `_fill_slots`) -- it has no opinion
    on *which* slots or *which* residents are in play. `allocate_tier_a` passes the whole month's
    slots and full population; `reconcile_month` passes the same vagter but with `surviving_by_vagt`
    reflecting who already holds a row (so only the leftover capacity is actually open) and
    `population` restricted to residents who do not already hold one (A3.1: reconciliation never
    hands out a second shift while an eligible unassigned resident exists). Extracting this was the
    point of A3.1 -- two hand-written copies of this ranking would drift, and the direction they
    would drift in is exactly the pool-blind bug A3.1 exists to fix.
    """
    surviving_by_vagt = surviving_by_vagt or {}
    weekend_capacity = sum(max(v.headcount - surviving_by_vagt.get(v.pk, 0), 0) for v in weekend_vagter)
    weekday_capacity = sum(max(v.headcount - surviving_by_vagt.get(v.pk, 0), 0) for v in weekday_vagter)

    declared_ids = _effective_weekday_unavailable_ids([r.pk for r in population], periode)
    declarers = sorted(
        (r for r in population if r.pk in declared_ids),
        key=lambda r: _tier_a_sort_key(r, balances, declared_at_by_id),
    )

    accepted_declarers = declarers[:weekend_capacity]
    refused_declarers = declarers[weekend_capacity:]
    refused_ids = {r.pk for r in refused_declarers}
    if refused_declarers:
        logger.warning(
            "%d beboer(e) meldte sig hverdage-utilgængelige ud over weekend-puljens kapacitet "
            "på %d for %s og fik ingen tier-A-vagt denne måned: %s",
            len(refused_declarers),
            weekend_capacity,
            log_label or periode,
            ", ".join(r.full_name for r in refused_declarers),
        )

    weekend_assigned = list(accepted_declarers)
    remaining_pop = [r for r in population if r.pk not in declared_ids]
    drafted: list[Resident] = []
    needed = weekend_capacity - len(weekend_assigned)
    if needed > 0:
        remaining_by_balance = sorted(
            remaining_pop, key=lambda r: _tier_a_sort_key(r, balances, declared_at_by_id)
        )
        drafted = remaining_by_balance[:needed]
        weekend_assigned += drafted

    assigned_ids = {r.pk for r in weekend_assigned}
    # Refused declarers never enter the weekday pool: they said they can't do weekdays, and forcing
    # one on them would contradict the declaration rather than merely miss the floor.
    weekday_pool = sorted(
        (r for r in population if r.pk not in assigned_ids and r.pk not in refused_ids),
        key=lambda r: _tier_a_sort_key(r, balances, declared_at_by_id),
    )

    _fill_slots(weekend_vagter, weekend_assigned, surviving_by_vagt)
    _fill_slots(weekday_vagter, weekday_pool, surviving_by_vagt)

    weekday_assigned = weekday_pool[:weekday_capacity]
    assigned_all_ids = assigned_ids | {r.pk for r in weekday_assigned}
    unassigned = [r for r in population if r.pk not in assigned_all_ids]

    return TierAResult(
        weekend_assigned=weekend_assigned,
        drafted=drafted,
        weekday_assigned=weekday_assigned,
        unassigned=unassigned,
        refused_weekend=refused_declarers,
    )


def _avoidance_resident_ids(resident_ids: list[int], periode: Periode) -> set[int]:
    """Which of `resident_ids` are signalling the aftenvagt-avoidance pattern in `periode` -- P2
    design doc §4: "selecting several morgen/frokost days while leaving aftenvagt empty is a
    deliberate signal". Derived entirely from existing data, no new field:

        declared_at IS NOT NULL      (a Praeference row exists for this periode at all)
        AND >= 1 morgen-or-frokost PraeferenceDag day
        AND 0 aften PraeferenceDag days

    `declared_at` is never actually NULL on a `Praeference` row that exists (it defaults to the date
    the row was written) -- "IS NOT NULL" in the design doc's own wording is exactly "a row exists
    for this periode", i.e. what `_effective_weekday_unavailable_ids`'s fallback already treats as
    the "did they engage at all" signal. The middle clause is the discriminator the doc calls out:
    someone who declared and selected NOTHING anywhere is genuinely no-opinion, not avoidance.
    """
    declared_ids = set(
        Praeference.objects.filter(periode=periode, resident_id__in=resident_ids).values_list(
            "resident_id", flat=True
        )
    )
    if not declared_ids:
        return set()
    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    has_tier_a_day = set(
        PraeferenceDag.objects.filter(
            praeference__periode=periode, praeference__resident_id__in=declared_ids, kind__in=tier_a_kinds
        ).values_list("praeference__resident_id", flat=True)
    )
    has_aften_day = set(
        PraeferenceDag.objects.filter(
            praeference__periode=periode, praeference__resident_id__in=declared_ids, kind=VagtRegel.Kind.AFTEN
        ).values_list("praeference__resident_id", flat=True)
    )
    return (declared_ids & has_tier_a_day) - has_aften_day


def _fill_leftover_tier_a(
    weekend_vagter: list[Vagt],
    weekday_vagter: list[Vagt],
    population_all: list[Resident],
    periode: Periode,
    declared_ids: set[int],
    balances: dict[int, int],
    declared_at_by_id: dict[int, date],
    result: TierAResult,
) -> None:
    """Fill any tier-A headcount `_seat_tier_a`'s one-shift-per-resident pass left open -- P2 design
    doc §1's correction ("the allocator must be *willing* to assign a second slot where the numbers
    require it") and §4's aftenvagt-avoidance mechanic ("give them extra tier-A instead of an
    aftenvagt when capacity allows"). Mutates `result` in place (moves any now-filled resident out of
    `result.unassigned` and into `weekend_assigned`/`weekday_assigned`).

    **Called ONLY from `allocate_tier_a`, never from `reconcile_month`.** `_seat_tier_a` itself stays
    completely untouched (it is the shared core both `allocate_tier_a` and `reconcile_month` call),
    and so does Amendment 3's reconciliation behaviour: A3.1 is explicit that reconciliation must
    leave a slot with no eligible UNASSIGNED candidate in `still_unfilled` rather than double-book an
    existing holder, and that rule is untouched here -- this only ever runs as a follow-up step
    inside a fresh, month-wide `allocate_tier_a` run.

    A resident may pick up at most ONE extra tier-A shift this way (two total this month) -- enough
    for the design doc's "at most one weekday slot per month" forced-doubling case and for a tiny
    test population's leftover capacity, without letting one person's ranking silently absorb an
    entire month's shortfall. `weekday_unavailable` stays a HARD exclusion here exactly as it is in
    `_seat_tier_a`: `declared_ids` (every resident routed to the weekend pool this periode, accepted
    or refused) never enters a weekday vagt's eligible pool, extra shift or not.

    Avoidance-pattern residents (`_avoidance_resident_ids`) get first claim on any leftover capacity,
    on BOTH pools -- that is what "extra tier-A instead of an aftenvagt" means. Everyone else is
    still eligible too, so a genuine capacity shortfall (population short of tier-A capacity) is
    fully absorbed even when nobody at all opted into the avoidance pattern.
    """
    avoidance_ids = _avoidance_resident_ids([r.pk for r in population_all], periode)
    # Seeded from the DATABASE, not from `result` (F5) -- `result.weekend_assigned`/`weekday_assigned`
    # only reflects residents `_seat_tier_a` itself just wrote into `population` (this run's fresh
    # TILDELT rows). A resident holding a SURVIVING non-TILDELT row from a PRIOR run (self-reported/
    # flagged, P2) is deliberately excluded from `_seat_tier_a`'s own `population` -- see
    # `allocate_tier_a`'s docstring -- so they are simply absent from `result` too, and `counts` would
    # silently start at 0 for them even though they may already hold 2 tier-A shifts this month.
    # Counting every VagtTildeling row (whatever its status) already sitting on this month's tier-A
    # vagter is a resident's TRUE total for the month -- both this run's freshly-written rows AND any
    # surviving one -- which is what the "two total this month" cap must be enforced against.
    counts: dict[int, int] = defaultdict(int)
    for resident_id, n in (
        VagtTildeling.objects.filter(vagt__in=weekend_vagter + weekday_vagter)
        .values("resident_id")
        .annotate(n=Count("id"))
        .values_list("resident_id", "n")
    ):
        counts[resident_id] = n

    def ranked(pool: list[Resident]) -> list[Resident]:
        return sorted(pool, key=lambda r: _tier_a_sort_key(r, balances, declared_at_by_id))

    for is_weekend, vagter in ((True, weekend_vagter), (False, weekday_vagter)):
        for vagt in vagter:
            open_slots = vagt.headcount - vagt.tildelinger.count()
            if open_slots <= 0:
                continue
            holder_ids = set(vagt.tildelinger.values_list("resident_id", flat=True))
            eligible = [
                r
                for r in population_all
                if r.pk not in holder_ids
                and counts.get(r.pk, 0) < 2
                and (is_weekend or r.pk not in declared_ids)
            ]
            candidates = ranked([r for r in eligible if r.pk in avoidance_ids]) + ranked(
                [r for r in eligible if r.pk not in avoidance_ids]
            )
            for resident in candidates[:open_slots]:
                VagtTildeling.objects.create(
                    vagt=vagt, resident=resident, status=VagtTildeling.Status.TILDELT
                )
                counts[resident.pk] += 1
                bucket = result.weekend_assigned if is_weekend else result.weekday_assigned
                bucket.append(resident)
                if resident in result.unassigned:
                    result.unassigned.remove(resident)


def _delete_replaceable_tildelinger(vagter: Iterable[Vagt]) -> None:
    """The `TILDELT` delete shared by `allocate_tier_a`/`allocate_tier_b`/`allocate_month`: clears the
    plain `TILDELT` rows on `vagter` but spares completed Amendment 4 hand-offs (design doc §5.5).

    **The exclusion alone is NOT race-safe.** Django splits a cascading `.delete()` into several
    statements: an unlocked SELECT (which is where the exclusion is evaluated), then separate DELETEs
    by pk list on `VagtAnmeldelse`/`VagtBytte` and finally `VagtTildeling`. A hand-off committed by a
    concurrent `take_over` after that SELECT would be deleted anyway (and Postgres's row re-check of a
    concurrently updated row does not re-run a `NOT EXISTS` subquery, so a single DELETE would not help
    either). So the candidate rows are locked FIRST (`select_for_update`, ascending pk, materialised),
    then deleted by that locked pk list with the exclusion applied, as a separate statement.

    Why that closes it under READ COMMITTED: each statement takes a fresh snapshot. Once we hold the row
    locks, a concurrent `take_over` on any of those rows blocks on its own `_lock_tildelinger` until we
    commit (after which the row is gone and it gets a clean "findes ikke længere"). A hand-off that
    committed BEFORE our lock was granted is visible to the exclusion statement that runs after it, and
    a row it updated while we waited is re-checked by the lock query against its new version. Both
    sides lock `VagtTildeling` rows first, in ascending pk order, so they block rather than deadlock
    (see the LOCK ORDER comment above `has_started`)."""
    vagt_list = list(vagter)
    locked_pks = list(
        VagtTildeling.objects.select_for_update(of=("self",))
        .filter(vagt__in=vagt_list, status=VagtTildeling.Status.TILDELT)
        .order_by("pk")
        .values_list("pk", flat=True)
    )  # materialised: the lock is taken here
    lock_cascade_dependents(locked_pks)  # offers and proposals the cascade reaches, ascending
    VagtTildeling.objects.filter(pk__in=locked_pks, status=VagtTildeling.Status.TILDELT).exclude(
        handed_off_tildeling_filter()
    ).delete()


def allocate_tier_a(year: int, month: int, *, force: bool = False, _skip_clear: bool = False) -> TierAResult:
    """Tier-A (morgen + frokost) allocation for one calendar month — design doc "Allocation", step 1,
    as amended by Amendment 1 (A1.1 FCFS tiebreak, A1.2 look-ahead) and Amendment 2 (A2.2 population
    projection).

    Population is every resident on that month's `Residency` list, or -- when none exists yet, which
    is the normal case for a look-ahead month -- the in-memory projection `_resolve_population`
    computes per Amendment 2 (A2.2). Weekday-unavailable declarers
    (scoped to the `Periode` this month falls in — see `Praeference`, and note that a resident with
    no row for that periode reads as if they carried forward their previous periode's value, per
    `_effective_weekday_unavailable_ids`) are seated into the weekend pool first, ranked by
    `_tier_a_sort_key` (projected balance ascending, most behind first, ties broken by who declared
    earliest); declarers beyond weekend capacity are refused (design doc finding 2: "the excess are
    refused with a clear message") and land in `unassigned`/`refused_weekend` rather than aborting the
    run or being forced onto a weekday slot they said they can't do. Remaining weekend capacity is
    then drafted from the rest of the population (finding 2: the weekend pool is mandatory overflow,
    not opt-in), same ranking. Weekday slots are filled last, same ranking, from whoever is left.
    Anyone left over gets no tier-A slot this month — the soft floor; it is absorbed by the ledger,
    not an error (design doc finding 3: this is expected in roughly half the semester's months).

    **The ranking balance is projected, not raw** (Amendment 1, A1.2): ledger balance plus the
    duration of this resident's `TILDELT` (assigned, not yet credited) rows — see
    `bulk_projected_balances`. A look-ahead window allocates months before they're worked, so without
    this a multi-month run would keep re-picking the same "most behind" residents every month purely
    because the ledger hasn't caught up; projecting the already-assigned-but-uncredited hours is what
    stops that compounding. It is computed *after* this month's own `TILDELT` rows are cleared below,
    so a resident's own about-to-be-recomputed assignment for THIS month never inflates their own
    ranking balance — only other months' still-standing `TILDELT` rows do.

    **Already-allocated guard** (Amendment 1, A1.2): once residents may be relying on a published,
    look-ahead-window schedule, silently re-shuffling it on every re-run is no longer acceptable (it
    was fine, and remains the mechanism, for a single manually-triggered month). If this month already
    has any `TILDELT` rows, this raises `KoekkenAllocationError` unless `force=True` is passed for a
    deliberate correction — checked, and refused, before anything else runs.

    Idempotent (given `force=True` on a re-run), including across membership/ranking changes between
    runs: any existing `TILDELT` assignment on this month's tier-A `Vagt` rows is cleared and
    recomputed from current (projected) balances before writing. Assignments already moved past
    `TILDELT` (self-reported or flagged — P2) or completed Amendment 4 hand-offs are never touched or
    overwritten (rows moved past `TILDELT` or completed Amendment 4 hand-offs survive) — but they DO still
    occupy their vagt's headcount and their holder is excluded from this run's candidate population,
    so a re-run cannot try to hand them a second row (a `UniqueViolation` on `(vagt, resident)`) or
    silently exceed a vagt's headcount by recomputing capacity as if those rows didn't exist. Both the
    write path and the reported capacity/`unassigned` figures below account for surviving rows
    identically.

    `_skip_clear` is private, used only by `allocate_month`: when True, this function's own "clear my
    tier's TILDELT rows, then compute balances" step below is skipped (the caller has already cleared
    it, and tier-B's, together -- see `allocate_month`'s docstring for why that ordering matters), and
    `force` is expected to already be True by the time it reaches here since the caller's own combined
    guard has already run. Standalone callers (every P1/Amendment 1-3 test, `roll_forward_allocation`)
    never pass it and get exactly the behaviour described above.
    """
    if not periode_is_allocated(_periode_from_bounds(date(year, month, 1)).kind):
        raise KoekkenAllocationError(
            f"{year}-{month:02d} hører til sommerperioden, som ikke allokeres -- vagter tages af "
            "beboerne selv (P3)."
        )
    periode = resolve_periode(date(year, month, 1))
    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    vagter = list(
        Vagt.objects.filter(date__year=year, date__month=month, kind__in=tier_a_kinds).order_by(
            "date", "kind"
        )
    )
    if not vagter:
        raise KoekkenAllocationError(
            f"Ingen vagter fundet for {year}-{month:02d}. Kør generate_koekkenvagter først."
        )

    already_allocated = VagtTildeling.objects.filter(
        vagt__in=vagter, status=VagtTildeling.Status.TILDELT
    ).exists()
    if already_allocated and not force:
        raise KoekkenAllocationError(
            f"{year}-{month:02d} har allerede tildelte tier-A-vagter -- brug --force for at "
            "gentildele (Amendment 1, A1.2: en offentliggjort måned i look-ahead-vinduet må ikke "
            "stille om uden et eksplicit tilvalg)."
        )

    weekend_vagter = [v for v in vagter if v.date.weekday() >= 5]
    weekday_vagter = [v for v in vagter if v.date.weekday() < 5]

    population_all = _resolve_population(year, month)
    if not population_all:
        raise KoekkenNoPopulationError(
            f"Ingen beboere kunne findes for {year}-{month:02d} -- hverken en direkte alumneliste "
            "eller en tidligere offentliggjort liste at projicere ud fra (Amendment 2, A2.2)."
        )

    declared_at_by_id = dict(
        Praeference.objects.filter(
            periode=periode, resident_id__in=[r.pk for r in population_all]
        ).values_list("resident_id", "declared_at")
    )

    with transaction.atomic():
        if not _skip_clear:
            _delete_replaceable_tildelinger(vagter)

        # Rows that survived the delete above (self-reported/flagged, P2) still occupy headcount and
        # must not be handed a second row this month — see the docstring above.
        surviving_by_vagt: dict[int, int] = defaultdict(int)
        surviving_resident_ids: set[int] = set()
        for vagt_id, resident_id in VagtTildeling.objects.filter(vagt__in=vagter).values_list(
            "vagt_id", "resident_id"
        ):
            surviving_by_vagt[vagt_id] += 1
            surviving_resident_ids.add(resident_id)

        population = [r for r in population_all if r.pk not in surviving_resident_ids]

        # Projected balances, computed AFTER the delete above so this month's own (just-cleared)
        # TILDELT rows never feed back into its own ranking — see the docstring's "projected, not
        # raw" note.
        balances = bulk_projected_balances(population_all)

        result = _seat_tier_a(
            weekend_vagter,
            weekday_vagter,
            population,
            periode,
            balances,
            declared_at_by_id,
            surviving_by_vagt,
            log_label=f"{year}-{month:02d}",
        )
        # P2, §1/§4: fill any tier-A headcount the one-shift-per-resident pass above left open. Never
        # called from reconcile_month -- see _fill_leftover_tier_a's own docstring for why touching
        # it there would relitigate Amendment 3 (A3.1).
        declared_ids = _effective_weekday_unavailable_ids([r.pk for r in population_all], periode)
        _fill_leftover_tier_a(
            weekend_vagter,
            weekday_vagter,
            population_all,
            periode,
            declared_ids,
            balances,
            declared_at_by_id,
            result,
        )
        return result


def _resident_aften_preferences(resident_ids: list[int], periode: Periode) -> dict[int, set[int]]:
    """resident_id -> the set of weekdays (0-6) they declared for AFTEN in `PraeferenceDag`, for
    `periode`. A resident with no rows (or no `Praeference` row at all) is simply absent from the
    returned dict -- `allocate_tier_b` reads that as "no opinion", not as a refusal."""
    rows = PraeferenceDag.objects.filter(
        praeference__periode=periode, praeference__resident_id__in=resident_ids, kind=VagtRegel.Kind.AFTEN
    ).values_list("praeference__resident_id", "weekday")
    prefs: dict[int, set[int]] = defaultdict(set)
    for resident_id, weekday in rows:
        prefs[resident_id].add(weekday)
    return prefs


def allocate_tier_b(year: int, month: int, *, force: bool = False, _skip_clear: bool = False) -> TierBResult:
    """Tier-B (aftenvagt) allocation for one calendar month -- P2 design doc §4. A single algorithmic
    pass, never a claim-based signup: there is no live claiming and no separate auto-assign deadline.
    **Must run AFTER `allocate_tier_a` for the same month** (see `allocate_month`) -- both the
    weekend-compensation ordering and the aftenvagt-avoidance exclusion below read THIS MONTH'S
    already-committed tier-A `VagtTildeling` rows straight out of the database.

    **`weekday_unavailable` is NOT an eligibility filter here** (design doc: the flag means "not home
    early-to-afternoon on weekdays", which says nothing about evenings) -- tier B has no eligibility
    constraint of its own, so every resident in the month's population is a candidate for every AFTEN
    vagt, weekday or weekend.

    **The ranking that decides who is picked next never reads a day preference** -- this is the
    feature's central fairness invariant (design doc: "preferences decide which slot, never who is
    next"). The order is `(not a weekend-tier-A-assignee this month, projected balance ASC,
    declared_at ASC, pk ASC)` -- the same `_tier_a_sort_key` comparator every tier-A ranking uses,
    with exactly one addition: a resident who holds a weekend tier-A slot THIS MONTH sorts strictly
    ahead of everyone who does not (the already-approved weekend-compensation mechanic -- "Weekend-
    tier-A assignees get aftenvagt preference priority", parent design doc's Decisions table). Once a
    resident's turn comes, THEIR declared AFTEN weekdays (if any) decide which of the still-open
    vagter they are offered; a resident with no matching open day, or no declaration at all, is
    offered the earliest still-open vagt instead -- never left out for lack of a preference.

    Avoidance-pattern residents (`_avoidance_resident_ids`) who picked up a SECOND tier-A shift this
    month via `_fill_leftover_tier_a`'s avoidance mechanic are excluded from the candidate pool
    entirely (`TierBResult.skipped_avoidance`) -- that extra tier-A shift was given to them INSTEAD
    of an aftenvagt, so assigning them one too would defeat the point. An avoidance-pattern resident
    who could NOT get the extra tier-A slot (no capacity) is simply an ordinary tier-B candidate --
    "gracefully assigns them an aftenvagt anyway when it doesn't [have capacity]", per the design doc.

    **No slot is ever left open** by ranking alone: if AFTEN capacity genuinely exceeds the candidate
    population (never expected at the real ~61-resident scale, but possible with a small population),
    the ranked list is cycled through again -- and again -- until every slot is filled or a full lap
    produces no further assignment (population genuinely exhausted, e.g. an empty candidate pool).

    A graceful no-op (`TierBResult()`) when this month has no AFTEN `Vagt` rows at all -- unlike
    `allocate_tier_a`'s hard raise on no vagter, this is not a misconfiguration to alert on: every
    P1/Amendment 1-3 test builds tier-A-only capacity by hand (`_build_month`, see
    `test_koekkenvagter.py`), and `generate_vagter` always creates AFTEN rows alongside tier-A ones in
    real operation, so an AFTEN-less month here is a deliberate test fixture, not a real outcome.

    Idempotent the same way `allocate_tier_a` is: refuses to re-run over existing `TILDELT` rows
    unless `force=True`, and any row already moved past `TILDELT` (self-reported/flagged) or completed
    Amendment 4 hand-off survives a re-run untouched and still occupies its vagt's headcount.

    `_skip_clear` is private, used only by `allocate_month` -- see `allocate_tier_a`'s docstring for
    what it does and why; the same note applies here verbatim.
    """
    if not periode_is_allocated(_periode_from_bounds(date(year, month, 1)).kind):
        raise KoekkenAllocationError(
            f"{year}-{month:02d} hører til sommerperioden, som ikke allokeres -- vagter tages af "
            "beboerne selv (P3)."
        )
    periode = resolve_periode(date(year, month, 1))
    vagter = sorted(
        Vagt.objects.filter(date__year=year, date__month=month, kind=VagtRegel.Kind.AFTEN),
        key=lambda v: v.date,
    )
    if not vagter:
        return TierBResult()

    already_allocated = VagtTildeling.objects.filter(
        vagt__in=vagter, status=VagtTildeling.Status.TILDELT
    ).exists()
    if already_allocated and not force:
        raise KoekkenAllocationError(
            f"{year}-{month:02d} har allerede tildelte tier-B-vagter -- brug --force for at gentildele."
        )

    population_all = _resolve_population(year, month)
    if not population_all:
        raise KoekkenNoPopulationError(f"Ingen beboere kunne findes for {year}-{month:02d} (tier B).")

    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    tier_a_vagter = Vagt.objects.filter(date__year=year, date__month=month, kind__in=tier_a_kinds)
    tier_a_counts: dict[int, int] = defaultdict(int)
    weekend_tier_a_ids: set[int] = set()
    for resident_id, vagt_date in VagtTildeling.objects.filter(vagt__in=tier_a_vagter).values_list(
        "resident_id", "vagt__date"
    ):
        tier_a_counts[resident_id] += 1
        if vagt_date.weekday() >= 5:
            weekend_tier_a_ids.add(resident_id)

    avoidance_ids = _avoidance_resident_ids([r.pk for r in population_all], periode)
    skipped = [r for r in population_all if r.pk in avoidance_ids and tier_a_counts.get(r.pk, 0) >= 2]
    skipped_ids = {r.pk for r in skipped}
    candidates = [r for r in population_all if r.pk not in skipped_ids]

    with transaction.atomic():
        if not _skip_clear:
            _delete_replaceable_tildelinger(vagter)

        # F2: projected balances (and declared_at/day-preferences, for the same reason) computed
        # AFTER the delete above, exactly mirroring `allocate_tier_a`'s identical fix (Amendment 1,
        # A1.2, ~line 671-674) -- so a candidate's own about-to-be-recomputed TILDELT row for THIS
        # run never inflates their own ranking balance. Without this, force-reallocating a month with
        # no underlying balance change could still reshuffle who gets which aftenvagt, purely because
        # each holder's own current assignment was counted against themselves before it was cleared --
        # the docstring's "idempotent the same way allocate_tier_a is" claim was false until this was
        # moved inside the transaction.
        balances = bulk_projected_balances(candidates)
        declared_at_by_id = dict(
            Praeference.objects.filter(
                periode=periode, resident_id__in=[r.pk for r in candidates]
            ).values_list("resident_id", "declared_at")
        )
        preferences = _resident_aften_preferences([r.pk for r in candidates], periode)

        def sort_key(r: Resident) -> tuple[int, int, date, int]:
            balance, declared_at, pk = _tier_a_sort_key(r, balances, declared_at_by_id)
            return (0 if r.pk in weekend_tier_a_ids else 1, balance, declared_at, pk)

        ranked = sorted(candidates, key=sort_key)

        held: dict[int, set[int]] = defaultdict(set)
        open_by_vagt: dict[int, int] = {}
        for vagt in vagter:
            open_by_vagt[vagt.pk] = vagt.headcount
        for vagt_id, resident_id in VagtTildeling.objects.filter(vagt__in=vagter).values_list(
            "vagt_id", "resident_id"
        ):
            held[resident_id].add(vagt_id)
            open_by_vagt[vagt_id] -= 1

        result = TierBResult(skipped_avoidance=skipped)
        remaining = sum(max(n, 0) for n in open_by_vagt.values())
        if ranked and remaining > 0:
            pool = itertools.cycle(ranked)
            stall = 0
            lap_bound = len(ranked)
            while remaining > 0 and stall <= lap_bound:
                resident = next(pool)
                open_vagter = [
                    v for v in vagter if open_by_vagt.get(v.pk, 0) > 0 and v.pk not in held[resident.pk]
                ]
                if not open_vagter:
                    stall += 1
                    continue
                prefs = preferences.get(resident.pk, set())
                preferred_open = [v for v in open_vagter if v.date.weekday() in prefs]
                chosen = preferred_open[0] if preferred_open else open_vagter[0]
                VagtTildeling.objects.create(
                    vagt=chosen, resident=resident, status=VagtTildeling.Status.TILDELT
                )
                open_by_vagt[chosen.pk] -= 1
                held[resident.pk].add(chosen.pk)
                result.assigned.append(resident)
                if preferred_open:
                    result.preference_honoured.append(resident)
                remaining -= 1
                stall = 0

    return result


def allocate_month(year: int, month: int, *, force: bool = False) -> tuple[TierAResult, TierBResult]:
    """The monthly allocation pass, tier-A then tier-B, in ONE run -- P2 design doc §4: "the monthly
    pass becomes: tier-A, then tier-B, in one run, with tier-A's outcomes feeding tier-B's ordering".
    This ordering is load-bearing (`allocate_tier_b`'s docstring); calling the two legs separately in
    the wrong order silently loses the weekend-compensation mechanic and the avoidance exclusion.

    The clear step spares rows moved past `TILDELT` or completed Amendment 4 hand-offs
    (`handed_off_tildeling_filter`, applied after the candidate rows are locked -- see
    `_delete_replaceable_tildelinger`).

    Both legs remain callable standalone -- every P1/Amendment 1-3 test calls `allocate_tier_a`
    directly and this wrapper changes nothing about that path; it exists for `allocate_koekkenvagter`
    and the resident/Køkkengruppen views, which always want the whole month done at once.

    **Both tiers' existing `TILDELT` rows for the month are cleared together, in one atomic step,
    BEFORE either tier's ranking runs** -- this is what makes a `force=True` re-run of the COMPOSITE
    pass actually idempotent, which calling `allocate_tier_a` and `allocate_tier_b` back to back did
    not achieve on its own even though each is independently idempotent in isolation: `allocate_tier_a`
    clears only ITS OWN (tier-A) rows before computing `bulk_projected_balances`, so this month's
    tier-B rows from the PREVIOUS run were still standing at that moment and inflated their holders'
    projected balance against themselves -- distorting tier-A's ranking, which changes tier-A's
    outcome, which changes tier-B's weekend-compensation-priority input (tier-B's ordering reads
    tier-A's THIS-month result straight out of the database), which changes tier-B's outcome. Nothing
    forced the two possible resolutions to agree, so repeated force re-runs with no underlying data
    change could oscillate between them forever. Clearing both tiers first means `allocate_tier_a`'s
    balances are computed against a state where this month's about-to-be-redone assignments, in
    EITHER tier, are already gone -- matching what each tier already does correctly for its own rows,
    now also relative to the other tier.

    Each leg's own "clear my tier's rows, then compute balances" step (see their docstrings) is
    skipped here via their private `_skip_clear=True` -- clearing twice would be harmless but
    redundant, and clearing tier-A's rows only right before `allocate_tier_a` runs (i.e. leaving the
    original per-leg ordering) is exactly the bug above, so the composite step must happen first,
    covering both tiers, not be delegated to either leg individually. `force` is required the normal
    way (a combined guard covering both tiers' existing rows) before anything is cleared; each leg is
    then called with `force=True` since the guard has already run.

    **Refuses a SOMMER month first** (P3 design doc §4, `periode_is_allocated`), before the deadline-
    timing guard below so an officer sees the right message, and via the pure `_periode_from_bounds` so a
    refused call leaves no `Periode` row behind.

    **Refuses to run at any point on or before the OWN periode's preference deadline** (2026-10
    review, F5; the same guard `allocate_batch` already enforced for its own `--batch` command, now
    also covering the two paths that call this function directly and so had no guard at all before:
    the manual single-month command (`allocate_koekkenvagter YEAR MONTH`, no `--batch`) and the
    Køkkengruppen "allokering" form in `koekken.views.allokering`). Both could silently allocate a
    periode's month before that periode's own preference deadline had passed, overtaking a
    deadline-day (or earlier) declaration -- exactly the failure Amendment 1's A1.3 supplement exists
    to prevent, which `--batch` alone did not close. `(year, month)`'s OWN periode is resolved purely
    (`_periode_from_bounds`, no DB write -- same reasoning as `allocate_batch`'s F4 fix: a refused call
    must not leave a `Periode` row behind) and checked against `periode_deadline`, exactly like
    `allocate_batch`'s check, with its own distinct message naming the month and the periode it
    belongs to.

    **This is a DIFFERENT check from the "already allocated" guard below, and the two coexist.** One
    is about timing (is it too early to read this periode's declarations at all), the other about
    re-allocation (has this exact month already been given an answer). Both can fire independently,
    both raise the same `KoekkenAllocationError` type but with distinct wording, and **`force` bypasses
    only the second one** -- it keeps its sole Amendment 1 meaning, "re-run an already-allocated
    month", and must never also mean "ignore the deadline timing" (the same "no escape hatch"
    principle `allocate_batch` already applies to its own guard).

    **`roll_forward_allocation` is unaffected.** It calls `allocate_tier_a`/`allocate_tier_b` directly,
    never through this function (see its own docstring: deliberately confined to the CURRENT periode,
    whose own deadline has, by construction, already passed by the time anyone is living inside it).
    This guard lives in `allocate_month` precisely so it only ever reaches callers choosing to go
    through the composite monthly pass -- the batch, the manual command, and the UI form -- never the
    scheduled roll-forward job.
    """
    pure_periode = _periode_from_bounds(date(year, month, 1))
    if not periode_is_allocated(pure_periode.kind):
        raise KoekkenAllocationError(
            f"{year}-{month:02d} hører til sommerperioden, som ikke allokeres -- vagter tages af "
            "beboerne selv (P3)."
        )
    today = current_date()
    deadline = periode_deadline(pure_periode)
    if today <= deadline:
        raise KoekkenAllocationError(
            f"{year}-{month:02d} hører til {pure_periode}, som har præferencefrist {deadline} -- "
            "allokering af denne måned kan tidligst ske dagen efter (Amendment 1, A1.3-tillægget af "
            "2026-10-01: fristdagen tilhører beboeren, ikke allokeringen; --force omgår kun 'allerede "
            "tildelt' nedenfor, ikke dette)."
        )

    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    month_vagter = list(
        Vagt.objects.filter(
            date__year=year, date__month=month, kind__in=[*tier_a_kinds, VagtRegel.Kind.AFTEN]
        )
    )
    already_allocated = VagtTildeling.objects.filter(
        vagt__in=month_vagter, status=VagtTildeling.Status.TILDELT
    ).exists()
    if already_allocated and not force:
        raise KoekkenAllocationError(
            f"{year}-{month:02d} har allerede tildelte vagter -- brug --force for at gentildele "
            "(Amendment 1, A1.2: en offentliggjort måned i look-ahead-vinduet må ikke stille om uden "
            "et eksplicit tilvalg)."
        )

    with transaction.atomic():
        _delete_replaceable_tildelinger(month_vagter)
        tier_a = allocate_tier_a(year, month, force=True, _skip_clear=True)
        tier_b = allocate_tier_b(year, month, force=True, _skip_clear=True)
    return tier_a, tier_b


def allocate_batch(
    year: int, month: int, *, force: bool = False
) -> list[tuple[int, int, TierAResult, TierBResult]]:
    """Allocate a periode's first three months in one call -- Amendment 1, A1.2: the deadline-
    triggered batch Køkkengruppen runs by hand at a periode's preference deadline. `(year, month)`
    names any date inside the periode to batch; the periode containing it is resolved and the walk
    starts at its `start_date`, clamped to `periode.end_date` (A2.8 -- a fixed three-month walk over a
    short periode would step into the next one, which either has no `Vagt` rows yet or belongs to a
    periode whose own preference deadline hasn't passed, inverting Amendment 1's locking rule either
    way; with SOMMER now refused, a clamp is a safety net for 5-month periodes only). Returns one `(year, month, TierAResult, TierBResult)` tuple per month allocated,
    in order, for the management command to report on.

    **Refuses a SOMMER periode first** (P3 design doc §4, `periode_is_allocated`), checked purely before
    the deadline guard and before any `Periode` row is materialised. Note the A2.8 clamp below now only
    ever matters for the 5-month periodes: SOMMER, the only 2-month one, can no longer be batched.

    **Refuses to run at any point on or before the periode's own preference deadline -- not only
    "on" the deadline day, but arbitrarily early too** (A1.3 supplement, approved 2026-10-01; this
    docstring was previously imprecise about that -- see the 2026-10 review). The preference WINDOW
    stays inclusive of the deadline date -- that is untouched here, residents keep their full
    declaration day -- but the batch that reads those declarations may not run until the day AFTER,
    checked as `today <= deadline`, so it only ever succeeds strictly after the deadline date, no
    matter how far before it this is called. Raised as `KoekkenAllocationError`, exactly like
    `allocate_tier_a`'s already-allocated guard, and read through `core.clock.current_date()` --
    never `django.utils.timezone` -- so `DevClock` can exercise both sides of the boundary in tests.

    **`force` is NOT an escape hatch for this guard.** It keeps its original Amendment 1 meaning here
    (overriding an already-allocated month inside the batch, passed straight through to
    `allocate_month`) and must never also bypass the deadline check: this guard protects residents'
    input integrity, not officer convenience, and every month this batch allocates is still two or
    more months out -- waiting one more day costs nothing, and there is no legitimate reason to ever
    skip it. There is deliberately no parameter that does.

    **The same deadline-timing check also lives in `allocate_month` itself** (2026-10 review, F5),
    covering the manual single-month command and the Køkkengruppen "allokering" form, which both call
    `allocate_month` directly and never went through this function's guard. This function's own check
    is therefore no longer the only place the rule is enforced, but it is kept here too: it is what
    lets this function raise BEFORE materialising a `Periode` row at all (see the ordering below), and
    its message is specific to the batch action. `roll_forward_allocation` remains unaffected either
    way -- see `allocate_month`'s docstring for why.

    **Checked against a PURE, unsaved `Periode` before any database write** (2026-10 review, F4).
    `resolve_periode` is a `get_or_create`; resolving it first and refusing afterward would leave a
    `Periode` row behind on every refusal unless the caller happens to wrap this in a transaction that
    rolls back on the raised exception (today's only caller, the management command, does -- but
    nothing about this function should depend on that). `_periode_from_bounds` (pure date arithmetic,
    no DB access) computes the same periode in memory, the deadline check runs against that, and only
    once it passes is `resolve_periode` called to materialise the row this function actually needs to
    read `start_date`/`end_date` off.
    """
    pure_periode = _periode_from_bounds(date(year, month, 1))
    if not periode_is_allocated(pure_periode.kind):
        raise KoekkenAllocationError(
            f"{year}-{month:02d} hører til sommerperioden, som ikke allokeres -- vagter tages af "
            "beboerne selv (P3)."
        )
    today = current_date()
    deadline = periode_deadline(pure_periode)
    if today <= deadline:
        raise KoekkenAllocationError(
            f"{pure_periode} har præferencefrist {deadline} -- batch-allokeringen kan tidligst køre "
            "dagen efter (Amendment 1, A1.3-tillægget af 2026-10-01: fristdagen tilhører beboeren, "
            "ikke allokeringen, og kan ikke omgås med --force)."
        )

    periode = resolve_periode(date(year, month, 1))
    return [(y, m, *allocate_month(y, m, force=force)) for y, m in batch_month_list(periode)]


def batch_month_list(periode: Periode) -> list[tuple[int, int]]:
    """The (year, month) pairs `allocate_batch` allocates for `periode`: its first three months,
    clamped to `periode.end_date` (A2.8). Extracted so the management command can report
    `force_rerun_impact` per month before allocating."""
    months: list[tuple[int, int]] = []
    cursor = periode.start_date
    while cursor <= periode.end_date and len(months) < 3:
        months.append((cursor.year, cursor.month))
        cursor = date(cursor.year + (1 if cursor.month == 12 else 0), cursor.month % 12 + 1, 1)
    return months


def reconcile_month(year: int, month: int) -> ReconciliationResult:
    """Correct one calendar month's tier-A assignments against the now-real `Residency` list --
    Amendment 2 (A2.3), corrected by Amendment 3 (A3.1). Additive only, and the counterpart to
    `allocate_tier_a`'s `force=True`: that is a deliberate Køkkengruppen re-shuffle that may move
    anyone, this never touches an assignment for a resident present on both the projection that was
    used and the real list now.

    A no-op (`ReconciliationResult()`, logged) for a SOMMER month, which is never allocated (P3 design
    doc §4) -- checked purely, before anything that could write. Likewise when this month has no `Vagt` rows yet, has never been
    allocated (no `TILDELT`/other `VagtTildeling` rows at all), or has no real `Residency` list
    published yet (F1: the normal state for any look-ahead month, since nothing in this codebase ever
    publishes a list more than a month ahead -- an empty real list means "nothing to compare the
    projection against yet", never "everyone left") -- there is nothing to reconcile against in any
    of these cases, and per the same discipline as `roll_forward_allocation` this must log and return
    rather than raise, since it may run in a scheduled task before a month has reached that point.

    **Vacate:** every current holder of a `TILDELT` row this month who is *not* on the real
    `Residency` list has that row deleted -- they were only ever a projection, or they have since
    left, either way they are not here. Rows already moved past `TILDELT` (self-reported/flagged, P2)
    are never touched, matching `allocate_tier_a`'s own survivor rule.

    **Re-seat:** the resulting open capacity (vacated slots, plus any slot that was never filled) is
    re-seated by the shared `_seat_tier_a` core -- the SAME eligibility rules `allocate_tier_a` uses,
    never a hand-rolled substitute (A3.1's explicit point: two copies of this ranking would drift, and
    the direction is exactly "a weekday slot mechanically inherited by whoever happens to be new",
    which is the bug A3.1 exists to fix). The candidate population is every real-list resident who
    does **not** already hold a row this month, in any status -- so reconciliation can never hand out
    a second slot to someone who already has one while an eligible unassigned resident exists, and a
    genuine new arrival competes on the exact same ranking as an existing resident who simply missed
    the floor the first time (no inheritance, no favouritism for "new"). A vacated weekend slot may go
    to anyone; a vacated weekday slot only to a weekday-available resident (declared or defaulted, per
    A3.2) -- if nobody eligible is unassigned, the slot stays open and is reported in
    `still_unfilled` rather than forced.

    **`still_unfilled` (F6) is every `Vagt` row still short of headcount after this run, not only the
    ones reconciliation itself tried and failed to seat** -- a month with a pre-existing structural
    shortfall (February's soft floor, design doc finding 3) reports those already-short vagter here
    too. See `ReconciliationResult`'s docstring.

    Idempotent: when the real list already matches who holds a slot, nothing is vacated and there is
    no open capacity to re-seat, so a repeat run writes nothing.
    """
    if not periode_is_allocated(_periode_from_bounds(date(year, month, 1)).kind):
        logger.info(
            "koekken.reconcile_month: %s-%02d hører til sommerperioden, som ikke allokeres -- intet "
            "at afstemme (P3).",
            year,
            month,
        )
        return ReconciliationResult()

    periode = resolve_periode(date(year, month, 1))
    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    vagter = list(
        Vagt.objects.filter(date__year=year, date__month=month, kind__in=tier_a_kinds).order_by(
            "date", "kind"
        )
    )
    if not vagter:
        logger.info(
            "koekken.reconcile_month: ingen vagter for %s-%02d endnu -- intet at afstemme.", year, month
        )
        return ReconciliationResult()

    existing = list(VagtTildeling.objects.filter(vagt__in=vagter).select_related("resident"))
    if not existing:
        logger.info(
            "koekken.reconcile_month: %s-%02d er ikke allokeret endnu -- intet at afstemme.", year, month
        )
        return ReconciliationResult()

    weekend_vagter = [v for v in vagter if v.date.weekday() >= 5]
    weekday_vagter = [v for v in vagter if v.date.weekday() < 5]

    real_population = list(
        Resident.objects.filter(residencies__year=year, residencies__month=month).distinct().order_by("pk")
    )
    if not real_population:
        # F1: no real Residency list published yet for this month -- the normal state for any
        # look-ahead month, since nothing in this codebase ever publishes one more than a month
        # ahead. There is nothing to reconcile a projection AGAINST here, so this must no-op rather
        # than treat an empty real list as "everyone left": that would vacate every TILDELT row in
        # the month and re-seat nothing (candidates would be empty too), directly inverting A2.3's
        # guarantee that an assignment shown to a resident still living in the dorm is never revoked
        # by reconciliation.
        logger.info(
            "koekken.reconcile_month: ingen reel beboerliste offentliggjort endnu for %s-%02d -- "
            "intet at afstemme imod.",
            year,
            month,
        )
        return ReconciliationResult()
    real_ids = {r.pk for r in real_population}

    with transaction.atomic():
        # Lock every row about to be vacated FIRST (LOCK ORDER above `has_started`) and re-read it:
        # the delete cascades into offers, and a concurrent take-over may have moved the row meanwhile.
        locked = _lock_tildelinger(
            row.pk
            for row in existing
            if row.status == VagtTildeling.Status.TILDELT and row.resident_id not in real_ids
        )
        lock_cascade_dependents(locked)  # offers and proposals the deletes cascade into, ascending
        vacated: list[Resident] = []
        for row in existing:
            current = locked.get(row.pk)
            if current is None or current.status != VagtTildeling.Status.TILDELT:
                continue
            if current.resident_id not in real_ids:
                vacated.append(current.resident)
                current.delete()

        # Re-read occupancy after vacating -- every status counts towards "already has a slot this
        # month" (mirrors allocate_tier_a's survivor handling), so a resident who already holds a row
        # is never a candidate for a second one (A3.1).
        surviving_by_vagt: dict[int, int] = defaultdict(int)
        holder_ids: set[int] = set()
        for vagt_id, resident_id in VagtTildeling.objects.filter(vagt__in=vagter).values_list(
            "vagt_id", "resident_id"
        ):
            surviving_by_vagt[vagt_id] += 1
            holder_ids.add(resident_id)

        candidates = [r for r in real_population if r.pk not in holder_ids]

        balances = bulk_projected_balances(candidates)
        declared_at_by_id = dict(
            Praeference.objects.filter(
                periode=periode, resident_id__in=[r.pk for r in candidates]
            ).values_list("resident_id", "declared_at")
        )

        seated = _seat_tier_a(
            weekend_vagter,
            weekday_vagter,
            candidates,
            periode,
            balances,
            declared_at_by_id,
            surviving_by_vagt,
            log_label=f"{year}-{month:02d} (afstemning)",
        )

        occupied_after: dict[int, int] = defaultdict(int)
        for vagt_id in VagtTildeling.objects.filter(vagt__in=vagter).values_list("vagt_id", flat=True):
            occupied_after[vagt_id] += 1
        still_unfilled = [v for v in vagter if occupied_after.get(v.pk, 0) < v.headcount]

    return ReconciliationResult(vacated=vacated, seated=seated, still_unfilled=still_unfilled)


def roll_forward_allocation(today: date | None = None) -> TierAResult | None:
    """Allocate the next not-yet-allocated tier-A month inside the `Periode` containing `today`
    (default `core.clock.current_date()`) — Amendment 1's monthly roll-forward job (A1.2), the
    deliberate reversal of P1's "allocation is manual-only" decision (see the design doc's A1.4): a
    rolling look-ahead window nobody remembers to advance is not actually a window.

    Deliberately scoped to the CURRENT periode only — it never reaches into the next one. Crossing a
    periode boundary is Køkkengruppen's own deadline-triggered batch (the period's first three
    months), not this job's concern; see A1.2's "why the deadline is two months out" for why that
    keeps visibility at two-or-more everywhere without this job ever needing to guess at a periode
    whose `Praeference` rows may not exist yet.

    A no-op (returns `None`, logged, nothing written, nothing raised) when `today` falls inside SOMMER:
    summer is never allocated (P3 design doc §4), and without this explicit check the 1 July run would
    die on `allocate_tier_a`'s new refusal. Likewise once every month in the periode that has `Vagt` rows is already
    allocated, or if the periode has no `Vagt` rows at all yet. Either way there is nothing this job
    can safely do, and it must not raise: a scheduled task failing loudly every month after a periode
    is fully allocated (or before it has been generated) would be its own kind of noise.

    **Amendment 2, A2.2:** also a no-op, logged rather than raised, when `allocate_tier_a` cannot
    resolve a population at all for the next month -- no real `Residency` list and no earlier
    published list to project from. That is the genuine-impossibility case A2.2 describes (it can
    only happen before the house has ever had a published alumneliste); this is the specific place
    the design doc requires it be caught rather than left to kill a scheduled task.
    """
    today = today or current_date()
    if not periode_is_allocated(_periode_from_bounds(today).kind):
        logger.info(
            "koekken.roll_forward_allocation: %s ligger i sommerperioden, som ikke allokeres -- "
            "intet at gøre (P3).",
            today,
        )
        return None
    periode = resolve_periode(today)
    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    month_cursor = periode.start_date
    while month_cursor <= periode.end_date:
        year, month = month_cursor.year, month_cursor.month
        vagter = Vagt.objects.filter(
            periode=periode, date__year=year, date__month=month, kind__in=tier_a_kinds
        )
        if vagter.exists():
            already_allocated = VagtTildeling.objects.filter(
                vagt__in=vagter, status=VagtTildeling.Status.TILDELT
            ).exists()
            if not already_allocated:
                try:
                    tier_a_result = allocate_tier_a(year, month)
                except KoekkenNoPopulationError as exc:
                    logger.warning(
                        "koekken.roll_forward_allocation: kunne ikke allokere %s-%02d (%s) -- logger "
                        "og springer over i stedet for at fejle (Amendment 2, A2.2).",
                        year,
                        month,
                        exc,
                    )
                    return None
                # P2, §4: "the monthly pass becomes tier-A then tier-B, in one run" -- also true for
                # the scheduled roll-forward, not only the Køkkengruppen-triggered batch/manual run.
                # Best-effort and additive on top of the tier-A result above: this function's return
                # type and its tier-A behaviour (including every Amendment 1/2 test asserting on it)
                # are UNCHANGED by this call -- tier-B failures are logged, never raised, matching the
                # "a scheduled task must not die" promise this function already makes for tier-A.
                try:
                    allocate_tier_b(year, month)
                except KoekkenAllocationError as exc:
                    logger.warning(
                        "koekken.roll_forward_allocation: tier-B-allokering fejlede for %s-%02d (%s) "
                        "-- tier-A står stadig, logger og fortsætter.",
                        year,
                        month,
                        exc,
                    )
                return tier_a_result
        month_cursor = date(year + (1 if month == 12 else 0), month % 12 + 1, 1)
    return None


def _redirect_past_deadline(current_periode: Periode, today: date) -> Periode:
    """The "further still" half of Amendment 1's A1.3 locking rule, used by `_preference_write_target`:
    the ALLOCATED periode right after `current_periode` (SOMMER skipped, P3 design doc §5), pushed one allocated periode further again if `today` has already
    reached (or passed) THAT periode's own deadline -- "a later edit is written to the following
    period's row instead, taking effect then." This is the WRITE-PATH version -- it persists rows via
    `_next_allocated_periode`/`resolve_periode`, which is fine here because `_preference_write_target` only ever
    calls this from inside `set_preference`'s own legitimate write (a POST). The display-only mirror
    used by `preference_target_periode` is `_redirect_past_deadline_pure`, which must never write --
    see that function's docstring for why the two are kept separate rather than shared."""
    target = _next_allocated_periode(current_periode)
    if today >= periode_deadline(target):
        target = _next_allocated_periode(target)
    return target


def _periode_from_bounds(for_date: date) -> Periode:
    """Unsaved, in-memory `Periode` for the periode containing `for_date` -- pure date arithmetic via
    `_periode_bounds`, with NO database access at all. The no-DB counterpart to `resolve_periode`,
    shared by every display-only resolution path (`in_preference_window`, `preference_target_periode`)
    so none of them needs its own copy of "build a Periode from bounds", and so neither can drift into
    calling `resolve_periode` (a `get_or_create`, i.e. a write) by accident."""
    kind, year, start, end = _periode_bounds(for_date)
    return Periode(kind=kind, year=year, start_date=start, end_date=end)


def _next_periode_pure(periode: Periode) -> Periode:
    """The no-DB counterpart to `_next_periode`: an unsaved `Periode` for the one immediately
    following `periode`, built from `_periode_from_bounds`. Periods are calendar-anchored and
    contiguous (see `_next_periode`'s docstring), so this has identical bounds to `_next_periode`'s
    persisted row -- it only differs in never touching the database. Used by
    `_redirect_past_deadline_pure`."""
    return _periode_from_bounds(periode.end_date + timedelta(days=1))


def _next_allocated_periode_pure(periode: Periode) -> Periode:
    """The no-DB counterpart to `_next_allocated_periode`: an unsaved `Periode` for the next ALLOCATED
    one after `periode` (SOMMER skipped, P3 design doc §5), built from `_next_periode_pure`."""
    target = _next_periode_pure(periode)
    if not periode_is_allocated(target.kind):
        target = _next_periode_pure(target)
    return target


def _preference_home_periode_pure(today: date) -> Periode:
    """The no-DB counterpart to `_preference_home_periode`: an unsaved `Periode` for the one containing
    `today`, except inside SOMMER where it is the following Efterår. Used by
    `preference_target_periode`."""
    periode = _periode_from_bounds(today)
    if not periode_is_allocated(periode.kind):
        periode = _next_periode_pure(periode)
    return periode


def _redirect_past_deadline_pure(current_periode: Periode, today: date) -> Periode:
    """The no-DB counterpart to `_redirect_past_deadline`, for `preference_target_periode`'s
    display-only resolution (F2): the SAME rule ("push one periode further again if `today` has
    already reached that periode's own deadline"), built entirely from unsaved `Periode` instances
    (`_next_allocated_periode_pure`) and pure date arithmetic (`periode_deadline` reads only `start_date`, never
    the database). `preference_target_periode` runs on every authenticated page view while a window is
    open (via the banner) and on every GET to the preference form -- a `get_or_create` here would be
    both extra queries on a common path and a write triggered by a GET, independent of the query count
    (see `in_preference_window`'s docstring for the same reasoning, which this mirrors)."""
    target = _next_allocated_periode_pure(current_periode)
    if today >= periode_deadline(target):
        target = _next_allocated_periode_pure(target)
    return target


def _preference_write_target(resident: Resident, today: date) -> Periode:
    """Which `Periode`'s row a preference write from `resident` on `today` actually targets --
    Amendment 1, A1.3's locking rule plus its 2026-10-01 supplement, exactly as `set_preference`
    applies it. Pulled out of `set_preference` so the resolution itself has exactly one
    implementation and `set_preference` is just "resolve, then write" -- see that function's docstring
    for the rule in full.

    **An open preference window wins over everything else** (A1.3 supplement, approved 2026-10-01):
    checked FIRST. If `in_preference_window(at=today)` finds one open, the write targets THAT window's
    periode, full stop -- `resolve_periode` materialises its row here since this is already inside a
    legitimate write path. Without this check running first, the mid-period-arrival exemption below
    would capture the write instead: in the first real window every resident has no row yet for the
    periode they are living in, so the whole house's declaration would land on a periode whose
    deadline passed months earlier and which nothing will ever read again, while the banner truthfully
    reports that they have declared. This also makes the exemption below PROVABLY VACUOUS whenever a
    window is open -- by the time any window opens, the resident's current periode has already been
    fully allocated for months (see the design doc's A1.3 supplement for the schedule-dependent proof)
    -- so the exemption's own wording is left completely unchanged below; it simply never runs while a
    window is open.

    Otherwise, exactly as before this supplement (P3 design doc §5 changes only what "the periode
    `today` falls in" means: inside SOMMER it is the following Efterår, `_preference_home_periode`):
    a resident with no row yet for the periode `today` falls in gets one created directly there (the mid-period-arrival exemption); one who already has a
    row for it is instead redirected to the next periode, or the one after that once ITS deadline has
    also passed (`_redirect_past_deadline`).
    """
    window_periode = in_preference_window(at=today)
    if window_periode is not None:
        return resolve_periode(window_periode.start_date)

    current_periode = _preference_home_periode(today)
    if not Praeference.objects.filter(resident=resident, periode=current_periode).exists():
        return current_periode
    return _redirect_past_deadline(current_periode, today)


def set_preference(resident: Resident, weekday_unavailable: bool, *, at: date | None = None) -> Praeference:
    """Write `resident`'s weekday-unavailable preference, resolving which `Periode`'s row the write
    actually targets — Amendment 1, A1.3's preference locking (`_preference_write_target`), as
    supplemented 2026-10-01: **an open preference window wins over both rules below**, checked first
    by `_preference_write_target` -- see that function's docstring for the rule and why it is safe.
    Both rules below are otherwise completely unchanged, and only actually run once no window is open.

    **Mid-period arrival, exempt from the deadline entirely:** if `resident` has no `Praeference` row
    yet for the `Periode` containing `at` (default `core.clock.current_date()`) -- the period they
    are CURRENTLY living in -- this always creates one there directly. They have never had the chance
    to declare for it, and its own deadline (two months before ITS start) is, by construction, already
    in the past the moment any date falls inside it; exempting this case is the only way a resident
    who arrives mid-period could ever declare at all.

    **Otherwise, this is a declaration for the UPCOMING periode:** once a resident already has a row
    for the periode they're in, they've already been through this once, so a further call is read as
    a preference for what comes next -- editable (created or edited in place) until THAT periode's own
    deadline. At or after that deadline it is redirected one periode further still, exactly per A1.3
    ("a later edit is written to the following period's row instead, taking effect then") -- and the
    already-set periode it would otherwise have touched is left completely untouched, which is what
    keeps a change of mind mid-period from retroactively disturbing a periode that may already be
    substantially allocated.
    """
    today = at or current_date()
    target = _preference_write_target(resident, today)

    row, created = Praeference.objects.get_or_create(
        resident=resident,
        periode=target,
        defaults={"weekday_unavailable": weekday_unavailable, "declared_at": today},
    )
    if not created:
        row.weekday_unavailable = weekday_unavailable
        row.declared_at = today
        row.save(update_fields=["weekday_unavailable", "declared_at"])
    return row


def set_preference_dage(praeference: Praeference, kind: str, weekdays: Iterable[int]) -> None:
    """Replace `praeference`'s declared days for `kind` with `weekdays` -- P2 design doc §2/§3's soft
    day-preferences. A full replace (delete-then-recreate), not a diff: the preference form always
    submits the complete checked set for one kind in one POST, so there is nothing to diff against,
    and `PraeferenceDag`'s unique constraint would reject re-inserting an already-declared day anyway.
    """
    weekdays = sorted(set(weekdays))
    praeference.dage.filter(kind=kind).delete()
    PraeferenceDag.objects.bulk_create(
        [PraeferenceDag(praeference=praeference, kind=kind, weekday=w) for w in weekdays]
    )


def set_preferences(
    resident: Resident,
    *,
    weekday_unavailable: bool,
    morgen_dage: Iterable[int] = (),
    frokost_dage: Iterable[int] = (),
    aften_dage: Iterable[int] = (),
    at: date | None = None,
) -> Praeference:
    """The whole P2 preference form in one call: the one hard flag plus all three soft day-
    preferences, all landing on whichever `Periode` `set_preference`'s locking rule (Amendment 1,
    A1.3) resolves the write to. Kept as one function so a view never has to resolve that target
    Periode twice and risk the flag landing on one periode while a day-preference lands on another.
    """
    row = set_preference(resident, weekday_unavailable, at=at)
    set_preference_dage(row, VagtRegel.Kind.MORGEN, morgen_dage)
    set_preference_dage(row, VagtRegel.Kind.FROKOST, frokost_dage)
    set_preference_dage(row, VagtRegel.Kind.AFTEN, aften_dage)
    return row


def preference_window(periode: Periode) -> tuple[date, date]:
    """(opens, closes) -- the ~1-week preference collection window for `periode`, P2 design doc §3.
    Not a new schedule: it CLOSES exactly at `periode_deadline(periode)`, the deadline the look-ahead
    machinery already computes, and opens 7 days before that. See the design doc's §3 for why this is
    the deadline "to the day", not an approximation of it."""
    deadline = periode_deadline(periode)
    return deadline - timedelta(days=7), deadline


def in_preference_window(*, at: date | None = None) -> Periode | None:
    """The `Periode` whose preference window `at` (default today) currently falls inside, or `None`.
    Checks the periode `at` falls in AND the next TWO, dropping any that is SOMMER (it has no
    preferences and so no window or banner -- P3 design doc §5). The window that matters to a resident
    living in periode N is almost always periode N+1's (its deadline is still ahead; periode N's own
    deadline is already in the past the moment anyone is living inside it), but checking the periode
    itself keeps this correct right at a boundary without hardcoding which one it must be.

    **Why two periodes ahead, and a pre-existing bug this fixes:** this used to check only the current
    and the next periode. Efterår's window is 24 Jun-1 Jul, a date range in which the current periode is
    Forår and the NEXT periode is SOMMER, so Efterår -- the one after that -- was never looked at and its
    window was only detected on 1 July itself, when SOMMER became current (reproduced 2026-10-04:
    `at=2027-06-24` and `2027-06-30` returned `None`, `2027-07-01` returned Efterår). Looking two ahead
    and skipping SOMMER finds it for the whole week.

    **Never touches the database, in EVERY case -- not only the common "no window" one (F6).** This
    runs from `core.context_processors.navigation` on every single authenticated page view (it is
    what drives the base.html banner, §8), so a naive `resolve_periode(start)` (a `get_or_create`)
    would cost 1-2 extra queries on EVERY page view during the ~1-week window, several times a year
    -- and worse, it is a WRITE triggered from a GET request, which is bad practice independent of
    the query count. `_periode_bounds`/`_deadline_from_start` are pure date arithmetic, so the
    `Periode` this returns when `at` does fall inside a window is built straight from them, as an
    UNSAVED, in-memory instance -- never persisted, and never read back from the database either.

    This is deliberately not "read the row if it exists, else no banner": nothing in this codebase's
    scheduling creates the NEXT periode's row before its own deadline. `generate_koekkenvagter`,
    `roll_forward_allocation` and the reconciliation sweep are all scoped to the CURRENT periode only
    (see each one's own docstring) -- the earliest anything creates the next periode's row is either
    a resident's OWN preference submission (`set_preference`, once they already have a row for the
    current periode) or Køkkengruppen's deadline-triggered `--batch` allocation, which by definition
    runs AT the deadline, i.e. the day the window closes. A read-only lookup would therefore silently
    show no banner for exactly the periode that most needs one: the first time anyone is asked about
    it, before any row exists yet.

    **Neither `praeferencer`'s view/template NOR `core.context_processors.navigation` (the banner)
    read this function's result directly for display any more** -- both now go through the shared
    `preference_target_periode` instead (F3), which happens to equal THIS function's result whenever a
    window is open (the 2026-10-01 A1.3 supplement, "an open window wins over everything else", makes
    it so -- see that function's docstring), but is the one function either caller is allowed to
    assume agrees with the other, rather than each trusting this one independently. This function's own
    role is now purely the cheap "is a window open at all" gate: both callers call this first and read
    no further once it returns `None` -- the per-resident reads inside `preference_target_periode`/
    `resident_has_declared_for` only run once it has already found a window open (see those functions'
    docstrings on why that ordering is load-bearing, F6). An unsaved instance is exactly as useful as a
    persisted one for that gate and costs nothing.
    """
    today = at or current_date()
    current_kind, _year, current_start, current_end = _periode_bounds(today)
    candidates = [(current_kind, current_start)]
    cursor = current_end
    for _ in range(2):
        next_kind, _next_year, next_start, cursor = _periode_bounds(cursor + timedelta(days=1))
        candidates.append((next_kind, next_start))
    for kind, start in candidates:
        if not periode_is_allocated(kind):
            continue
        deadline = _deadline_from_start(start)
        if deadline - timedelta(days=7) <= today <= deadline:
            return _periode_from_bounds(start)
    return None


def resident_needs_to_declare(resident: Resident, *, at: date | None = None) -> bool:
    """Whether `resident` should see the "declare your kitchen preferences" dashboard todo card --
    P2 design doc §8, read literally: *"A resident who has NEVER declared gets a todo card"*, not
    scoped to any particular periode. True for a resident with NO `Praeference` row AT ALL, in ANY
    periode -- Amendment 3 (A3.2) already established `declared_at IS NULL` (no row) as "we are
    guessing, not reading a declaration"; this reuses that exact idea at the scope the design doc's
    own wording asks for (every periode, not just the current one).

    **Fixed 2026-10-01 review: this was previously scoped to the CURRENT periode only**
    (`Praeference.objects.filter(resident=resident, periode__kind=..., periode__year=...)`), which
    regressed the moment the window-first resolution (A1.3 supplement) shipped: a first-time
    declarer's submission made DURING an active preference window targets the WINDOW's periode, not
    the resident's current one (`_preference_write_target`/`set_preferences`), so a current-periode
    check never sees that row. The card became permanently unclearable for exactly the cohort it
    exists to onboard -- the only way to silence it drove the resident into submitting a SECOND,
    contradictory declaration against an already-dead periode. Checking "ever declared anywhere"
    instead sidesteps the problem entirely rather than chasing which periode a write landed on.

    **Deliberate long-term semantic, confirmed rather than assumed:** once a resident has declared a
    single time, in any periode past or present, this card never shows again for them -- even in a
    later periode they have not yet re-declared for. That is correct, not merely convenient, because
    this specific UI surface's job (§8: "a standing task rather than a deadline") is the ZERO-HISTORY
    case only. Amendment 1's missed-deadline fallback (A1.3: "a resident with no row for the new
    period carries forward the previous period's value as the default") is deliberately NOT meant to
    re-trigger a todo-card nag every periode -- ongoing, periode-scoped reminding for someone who has
    already declared at least once is the SEPARATE preference-window banner's job
    (`resident_has_declared_for`/`preference_target_periode`, via `core.context_processors`), which
    correctly re-triggers each periode and is unaffected by this change. Re-scoping this card to "has
    declared for the periode `preference_target_periode` currently resolves to" was considered and
    rejected: outside an open window that target is the CURRENT periode, so it would reproduce this
    exact bug for every ordinary mid-period resident who has not yet gotten around to re-declaring --
    fixing it would need its own window-open gating logic, which is unnecessary complexity this
    surface does not need.

    **`at` is accepted for API symmetry with this module's other "as of a date" helpers, but plays no
    role in the check any more** -- "ever declared anywhere" has no date-scoping left to apply.

    **Never writes (F2).** Two callers, both gated on the rollout gate being open (not only during a
    preference window, unlike the banner): `residents.views.dashboard` calls this on EVERY
    authenticated dashboard GET (the §8 todo card), and `koekken.views._resident_context` calls it for
    the equivalent "needs_to_declare" card on the resident's own `/intern/koekken/` page
    (`templates/koekken/index.html`) -- this query needs no `resolve_periode` (a `get_or_create`) at
    all now, which also means it no longer needs `_periode_from_bounds` either."""
    return not Praeference.objects.filter(resident=resident).exists()


def preference_target_periode(resident: Resident, *, at: date | None = None) -> Periode:
    """The `Periode` a preference write from `resident` would land on RIGHT NOW -- the pure, read-only
    mirror of `_preference_write_target`/`set_preference`'s real resolution (Amendment 1's A1.3, and
    its 2026-10-01 window-first supplement). Used for DISPLAY ONLY, by BOTH the preference-window
    banner (`core.context_processors.navigation`) and the preference form page
    (`koekken.views.praeferencer`) -- the two call this SAME function so they can never disagree about
    which periode's name to show (F3; see `events.views._rsvp_context` for this repo's existing
    precedent of one shared resolver feeding more than one render path, rather than each caller
    re-deriving its own answer).

    **Never writes to the database (F2) -- matches `in_preference_window`'s own deliberate no-write
    invariant** (see that function's docstring for the full reasoning: query cost on every
    authenticated page view during a window, and "a write triggered by a GET is bad practice
    independent of query count"). Every branch below returns either an UNSAVED, in-memory `Periode`
    (`_periode_from_bounds`/`_next_periode_pure`, pure date arithmetic) or `in_preference_window`'s own
    result, which is unsaved for the same reason -- this function never calls `resolve_periode` or
    `_next_periode` (both a `get_or_create`, i.e. a write), only read-only
    `Praeference.objects.filter(...).exists()` queries, which cost nothing extra once a window is
    already known to be open.

    Mirrors `_preference_write_target` branch for branch, materialising nothing:

    1. **An open window wins over everything else** (A1.3 supplement): if `in_preference_window(at=at)`
       finds one open, the target IS that window's periode -- always, regardless of `resident`'s own
       declaration history. This also means a first-time declarer's target is the window's periode
       both BEFORE and AFTER they submit (their write lands there too, per
       `_preference_write_target`), so unlike an earlier revision of this function, no extra
       bookkeeping is needed to stop the banner chasing a moving target -- window-first collapses that
       distinction away entirely.
    2. **Otherwise, the mid-period-arrival exemption** (A1.3): no row yet for the resident's home
       periode (the one `at` falls in, or the following Efterår when `at` is inside SOMMER --
       `_preference_home_periode_pure`, P3 design doc §5) -> the target is that periode directly.
    3. **Otherwise**, `resident` already has a row for their current periode, so the target is the next
       one -- redirected one further still if `at` has already reached THAT periode's own deadline
       (`_redirect_past_deadline_pure`, the no-DB mirror of `_redirect_past_deadline`).

    Matched by (kind, year), never by FK object identity, for the same reason
    `resident_has_declared_for` is: an unsaved `Periode`'s pk is `None`, and filtering on that would
    silently match nothing.
    """
    today = at or current_date()
    window_periode = in_preference_window(at=today)
    if window_periode is not None:
        return window_periode

    current_periode = _preference_home_periode_pure(today)
    has_row = Praeference.objects.filter(
        resident=resident, periode__kind=current_periode.kind, periode__year=current_periode.year
    ).exists()
    if not has_row:
        return current_periode
    return _redirect_past_deadline_pure(current_periode, today)


def resident_has_declared_for(resident: Resident, periode: Periode) -> bool:
    """Whether `resident` already has a `Praeference` row for `periode` -- the per-resident half of
    the preference-window banner (P2 design doc §8; §12's open item resolved: the banner persists
    for each resident individually until they've declared for the window's periode OR the window's
    time runs out, rather than showing for the window's whole duration regardless of whether that
    resident has already acted).

    NOT the same question as `resident_needs_to_declare` (2026-10 review, Finding 1) -- that one now
    checks "has this resident EVER declared, in any periode at all" (the dashboard todo card, §8 /
    A3.2's zero-history case; deliberately NOT scoped to any particular periode any more -- see its
    own docstring for why scoping it to the current periode was the regression that made the card
    permanently unclearable for a first-time declarer during a window). THIS function is scoped to
    whatever `periode` the caller passes, which for the banner is specifically
    `preference_target_periode()`'s result -- the periode a submission from this resident would
    actually target right now. Since the 2026-10-01 A1.3 supplement ("an open window wins over
    everything else"), that result now always EQUALS `in_preference_window()`'s own periode whenever a
    window is open -- the two no longer routinely diverge the way an earlier revision of this
    docstring described. The two functions are correctly complementary: this one re-triggers every
    periode for an established resident (the ongoing per-periode reminder), `resident_needs_to_declare`
    never does once a resident has declared once (the one-time onboarding nudge).

    Matched by `(periode.kind, periode.year)` rather than `periode=periode` on purpose:
    `in_preference_window()` returns an UNSAVED, in-memory `Periode` (see its docstring on why), so
    filtering on the FK by object identity would compare against a `None` pk and match nothing --
    silently showing the banner to every resident forever. `Periode.Meta.constraints` guarantees
    (kind, year) is exactly as selective as the pk would have been. `preference_target_periode`'s
    result is ALSO always an unsaved periode now (F2 -- it must never write), so this matching
    discipline is not just cheap insurance here, it is load-bearing for every caller.

    Callers must only call this once `in_preference_window()` has already returned a periode: unlike
    that function, this one genuinely needs a database read (it is per-resident), so it must not run
    on the ~355 days/year when no window is open -- see `in_preference_window`'s own docstring on why
    that discipline matters here."""
    return Praeference.objects.filter(
        resident=resident, periode__kind=periode.kind, periode__year=periode.year
    ).exists()


def vagt_regel_lookup() -> dict[tuple[str, bool], VagtRegel]:
    """Every `VagtRegel` row, keyed by (kind, weekend) -- there are only a handful in the whole table
    (one per kind per weekday/weekend, per that model's docstring), so a caller that needs
    `_vagt_regel_for` for MANY `Vagt` rows in one request (the kitchen tablet's shift list, F7) should
    build this ONCE and pass it through rather than pay one `VagtRegel.objects.get(...)` query per
    row."""
    return {(regel.kind, regel.weekend): regel for regel in VagtRegel.objects.all()}


def _vagt_regel_for(vagt: Vagt, regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None) -> VagtRegel:
    """The `VagtRegel` row governing `vagt` -- read LIVE, matching `VagtRegel.start_time`'s own
    docstring on why that field is never snapshotted onto `Vagt`. Pass `regel_lookup` (from
    `vagt_regel_lookup()`) to look it up in memory instead of running a fresh query -- omitted, this
    still queries directly, so every existing single-row caller/test keeps working unchanged."""
    key = (vagt.kind, vagt.date.weekday() >= 5)
    if regel_lookup is not None:
        return regel_lookup[key]
    return VagtRegel.objects.get(kind=key[0], weekend=key[1])


def marking_window(
    vagt: Vagt, *, regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None
) -> tuple[datetime, datetime]:
    """(opens_at, closes_at) for marking `vagt` done at the kitchen tablet -- P2 design doc §5, tz-
    aware Europe/Copenhagen wall-clock. Opens at the shift's own start time on its own date (read
    LIVE off `VagtRegel.start_time`), closes at the very start of the day AFTER the day after the
    shift -- i.e. inclusive through the end of the FOLLOWING day. A Tuesday shift's window therefore
    runs from Tuesday's start_time through the last instant of Wednesday.

    `regel_lookup` is forwarded to `_vagt_regel_for` (F7) -- see that function's docstring.
    """
    regel = _vagt_regel_for(vagt, regel_lookup)
    opens_at = timezone.make_aware(datetime.combine(vagt.date, regel.start_time))
    closes_at = timezone.make_aware(datetime.combine(vagt.date + timedelta(days=2), time.min))
    return opens_at, closes_at


def can_mark_done(
    vagt_tildeling: VagtTildeling,
    *,
    at: datetime | None = None,
    regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None,
) -> bool:
    """Whether `vagt_tildeling` may be marked done RIGHT NOW -- P2 design doc §5. Only a still-
    `TILDELT` row inside its marking window; a row already `UDFOERT`/`ANMELDT`/`IKKE_UDFOERT` has
    nothing further to self-report. The kitchen-tablet view calls this to decide whether to render
    the mark-done button at all (§10: a closed action removes its button, never disables it).

    `regel_lookup` is forwarded to `marking_window` (F7) -- pass `vagt_regel_lookup()`'s result once
    per request when calling this for many rows, rather than once per row."""
    if vagt_tildeling.status != VagtTildeling.Status.TILDELT:
        return False
    now = at or current_datetime()
    opens_at, closes_at = marking_window(vagt_tildeling.vagt, regel_lookup=regel_lookup)
    return opens_at <= now < closes_at


def mark_udfoert(vagt_tildeling: VagtTildeling, *, at: datetime | None = None) -> VagtTildeling:
    """Self-report `vagt_tildeling` done -- P2 design doc §5. Posts the work credit the moment the
    status flips: an `ARBEJDE` `KoekkenPost` (`delta_minutes = +vagt.duration_minutes`), reusing the
    `Kind.ARBEJDE` choice P1 already reserved for "credit for a self-reported UDFOERT shift" without
    ever wiring a view to trigger it.

    Raises `KoekkenAllocationError` when `can_mark_done` refuses -- outside the marking window, or a
    row that has already moved past `TILDELT`. The kitchen-tablet view is expected to have already
    removed the button for either case (§10), so reaching here means a stale page or a replayed POST;
    refusing loudly is correct for both rather than silently doing nothing.
    """
    if not can_mark_done(vagt_tildeling, at=at):
        raise KoekkenAllocationError(
            f"{vagt_tildeling} kan ikke markeres udført lige nu (uden for tidsvinduet, eller allerede afgjort)."
        )
    vagt_tildeling.status = VagtTildeling.Status.UDFOERT
    vagt_tildeling.save(update_fields=["status"])
    KoekkenPost.objects.create(
        resident=vagt_tildeling.resident,
        periode=vagt_tildeling.vagt.periode,
        kind=KoekkenPost.Kind.ARBEJDE,
        delta_minutes=vagt_tildeling.vagt.duration_minutes,
        vagt=vagt_tildeling.vagt,
    )
    return vagt_tildeling


def can_flag(vagt_tildeling: VagtTildeling, *, at: date | None = None) -> bool:
    """Whether `vagt_tildeling` may be flagged right now -- P2 design doc §6: any shift dated today
    or earlier, whatever its status, PROVIDED it has no open flag already (a second one would violate
    `VagtAnmeldelse`'s partial-unique constraint; checked here so the view never renders a flag
    button that would fail)."""
    today = at or current_date()
    if vagt_tildeling.vagt.date > today:
        return False
    return not vagt_tildeling.anmeldelser.filter(status=VagtAnmeldelse.Status.AABEN).exists()


def open_flag_tildeling_ids(tildeling_ids: Iterable[int]) -> set[int]:
    """The batched form of `can_flag`'s own open-flag check -- one query for however many
    `VagtTildeling` ids a caller has in hand, instead of `can_flag`'s per-row
    `vagt_tildeling.anmeldelser.filter(...).exists()` (F7: the resident index page's "seneste vagter"
    list otherwise runs that once per row -- 60-80 extra queries at realistic scale). A caller that
    already knows every row's date is <= today (as `_recent_context`'s date-windowed query does) needs
    nothing else from `can_flag`; one that doesn't should still apply that date check itself."""
    ids = list(tildeling_ids)
    if not ids:
        return set()
    return set(
        VagtAnmeldelse.objects.filter(
            status=VagtAnmeldelse.Status.AABEN, vagt_tildeling_id__in=ids
        ).values_list("vagt_tildeling_id", flat=True)
    )


def flagged_by_names(tildelinger: Iterable[VagtTildeling]) -> dict[int, str]:
    """tildeling id -> the flagger's full name, for whichever of `tildelinger` is currently showing a
    flag's consequence (an open `ANMELDT` row, or an upheld `IKKE_UDFOERT` one) -- P2 design doc §6:
    "flagger identity is fully visible, including to the flagged resident", not only to Køkkengruppen
    (F3). A tildeling not currently showing a flag (never flagged, or flagged-and-dismissed back to
    its previous status) is simply absent from the returned dict.

    One query per relevant `VagtAnmeldelse.Status`, mirroring `open_flag_tildeling_ids` above (F7).
    A tildeling can accumulate more than one `VagtAnmeldelse` over time (a dismissed flag may be
    re-flagged later, and a re-flag of an already-upheld shift may itself later be dismissed), so
    "most recent row regardless of its own outcome" is NOT the same as "the row responsible for the
    CURRENT status" -- e.g. flag A upheld (tildeling -> `IKKE_UDFOERT`), then flag B re-flags it and is
    later dismissed (tildeling reverts to `IKKE_UDFOERT`, per `resolve_anmeldelse`'s dismissed branch):
    the newest row is B, an `AFVIST` one, but the status is still explained by A's `OPRETHOLDT` row.
    So this filters each tildeling's candidate rows down to the one `VagtAnmeldelse.Status` that
    actually produces its CURRENT `VagtTildeling.Status` (`ANMELDT` <- `AABEN`, `IKKE_UDFOERT` <-
    `OPRETHOLDT`) before picking the newest (`-created_at`, `-pk` as a tiebreak -- `created_at` alone
    has none) among those.
    """
    status_wants_anmeldelse_status: dict[str, str] = {
        VagtTildeling.Status.ANMELDT: VagtAnmeldelse.Status.AABEN,
        VagtTildeling.Status.IKKE_UDFOERT: VagtAnmeldelse.Status.OPRETHOLDT,
    }
    ids_by_anmeldelse_status: dict[str, list[int]] = defaultdict(list)
    for t in tildelinger:
        anmeldelse_status = status_wants_anmeldelse_status.get(t.status)
        if anmeldelse_status is not None:
            ids_by_anmeldelse_status[anmeldelse_status].append(t.pk)
    if not ids_by_anmeldelse_status:
        return {}
    names: dict[int, str] = {}
    for anmeldelse_status, ids in ids_by_anmeldelse_status.items():
        for anmeldelse in (
            VagtAnmeldelse.objects.filter(vagt_tildeling_id__in=ids, status=anmeldelse_status)
            .select_related("flagged_by")
            .order_by("-created_at", "-pk")
        ):
            names.setdefault(anmeldelse.vagt_tildeling_id, anmeldelse.flagged_by.full_name)
    return names


def flag_tildeling(vagt_tildeling: VagtTildeling, flagged_by: Resident, reason: str = "") -> VagtAnmeldelse:
    """ "Denne vagt blev ikke udført" -- P2 design doc §6. ONE uniform action, whatever the
    assignment's current status. Freezes `previous_status` (what the assignment was AT THE MOMENT of
    flagging -- `UDFOERT` or `TILDELT` in practice) so `resolve_anmeldelse` later knows the ledger
    consequence and a dismissal knows what to restore, then moves the assignment itself to `ANMELDT`
    so it stops reading as settled while the flag is open.

    The caller (the view) is responsible for `can_flag`'s checks; this is the mechanical write, like
    every other function in this module.
    """
    with transaction.atomic():
        # Lock the row first (see the LOCK ORDER note below), then lapse any open offer on it: a flagged
        # row is hidden from the board, so the offer would otherwise sit as a phantom on the offerer's own
        # list (and survive a dismissal) without anybody being able to take it.
        locked = lock_tildeling(vagt_tildeling.pk)
        if locked is None:
            raise KoekkenAllocationError("Vagten findes ikke længere.")
        # From here on use the LOCKED row, not the caller's pre-lock copy: its status may have changed
        # (e.g. marked UDFOERT) between the view's read and the lock.
        vagt_tildeling = locked
        # Lock-then-update (LOCK ORDER): the offer on the row (and its proposals), then every open proposal
        # that uses the flagged row as `modydelse` -- a row with flag history can never be traded.
        # Lock level 3 (the row's open offers) and then the WHOLE level-4 set in one ascending pass, before
        # closing anything (the closes below only re-lock rows already held).
        offer_pks = _lock_byttes(
            VagtBytte.objects.filter(tildeling=vagt_tildeling, status=VagtBytte.Status.AABEN).values_list(
                "pk", flat=True
            )
        )
        _lock_forslag_for(offer_pks=offer_pks, row_pks=[vagt_tildeling.pk])
        _close_invalidated_offers([vagt_tildeling])
        _close_invalidated_forslag([vagt_tildeling])
        anmeldelse = VagtAnmeldelse.objects.create(
            vagt_tildeling=vagt_tildeling,
            flagged_by=flagged_by,
            previous_status=vagt_tildeling.status,
            reason=reason,
        )
        vagt_tildeling.status = VagtTildeling.Status.ANMELDT
        vagt_tildeling.save(update_fields=["status"])
    return anmeldelse


def resolve_anmeldelse(anmeldelse: VagtAnmeldelse, *, upheld: bool, resolved_by: Resident) -> VagtAnmeldelse:
    """Adjudicate an open `VagtAnmeldelse` -- P2 design doc §6's table, verbatim:

    * **Dismissed**: the assignment reverts to whatever it was before the flag (`previous_status`);
      no ledger entry, ever -- a dismissal means nothing happened, so nothing should change.
    * **Upheld, previously `UDFOERT`**: the assignment becomes `IKKE_UDFOERT` and the credit is
      reversed with a `TILBAGEFOERSEL` `KoekkenPost` (`delta_minutes = -vagt.duration_minutes`) --
      NEVER by deleting the original `ARBEJDE` row, matching the ledger's append-only shape
      everywhere else in this feature.
    * **Upheld, previously `TILDELT`**: the assignment becomes `IKKE_UDFOERT` but NOTHING is written
      to the ledger -- no credit was ever posted, so there is nothing to reverse. Design doc: "an
      operational alert, not an accounting event" -- the flag itself is still real (Køkkengruppen's
      queue shows it), it just has no ledger consequence.

    Either upheld branch pushes a notification to the flagged resident's own `wants_koekken` topic
    (`core.push`, per-topic, opt-in) -- a ruling against them is exactly the kind of thing nobody
    should discover only at move-out. The audience is narrowed through `koekken.access.
    allowed_subscribers` (F4), not a hand-rolled `PushSubscription.objects.filter(...)` -- without
    that gate-aware narrowing, a resident who opted in before the rollout gate closed (or before it
    is ever opened) could still be notified with a link that then 403s them (see `core.rollout.
    Gate.allowed_subscribers`'s own docstring).

    Raises `KoekkenAllocationError` if `anmeldelse` is not currently open -- resolving twice (a
    replayed POST) must not double-reverse a credit or double-notify.
    """
    if anmeldelse.status != VagtAnmeldelse.Status.AABEN:
        raise KoekkenAllocationError(f"{anmeldelse} er allerede afgjort.")
    vagt_tildeling = anmeldelse.vagt_tildeling

    if upheld:
        anmeldelse.status = VagtAnmeldelse.Status.OPRETHOLDT
        vagt_tildeling.status = VagtTildeling.Status.IKKE_UDFOERT
        if anmeldelse.previous_status == VagtTildeling.Status.UDFOERT:
            KoekkenPost.objects.create(
                resident=vagt_tildeling.resident,
                periode=vagt_tildeling.vagt.periode,
                kind=KoekkenPost.Kind.TILBAGEFOERSEL,
                delta_minutes=-vagt_tildeling.vagt.duration_minutes,
                vagt=vagt_tildeling.vagt,
                created_by=resolved_by,
            )
        from core.push import send, subscribers  # local: avoids a core.push import for every caller

        from . import access  # of this module that never resolves a flag; same reasoning for access.

        audience = access.allowed_subscribers(subscribers(TOPIC).filter(user=vagt_tildeling.resident))
        send(
            audience,
            "Køkkenvagt",
            f"{vagt_tildeling.vagt} blev meldt ikke udført, og afgørelsen er opretholdt.",
            "/intern/koekken/",
        )
    else:
        anmeldelse.status = VagtAnmeldelse.Status.AFVIST
        vagt_tildeling.status = anmeldelse.previous_status

    anmeldelse.resolved_by = resolved_by
    anmeldelse.resolved_at = current_datetime()
    anmeldelse.save(update_fields=["status", "resolved_by", "resolved_at"])
    vagt_tildeling.save(update_fields=["status"])
    return anmeldelse


# ---------------------------------------------------------------------------------------------------
# Amendment 4, step 1: hand-off of vagter (`VagtBytte`). Design doc `2026-10-04-koekkenvagter-a4-design.md`.
#
# LOCK ORDER (critical): every write below locks, in this order and never another, never skipping a
# level that it needs, never going back up:
#   (1) the `VagtTildeling` rows involved (for a trade BOTH X and Y), via `select_for_update`, in
#       ASCENDING pk order;
#   (2) the `Vagt` (whole-shift take-over only);
#   (3) the `VagtBytte` rows, ascending pk;
#   (4) the `VagtBytteForslag` rows (step 2), ascending pk;
#   (5) the Den Hurtige `QuickPost` that advertises an offer (step 3), taken last. Den Hurtige's own
#       hard-delete (inside its grace period) nulls `VagtBytte.hurtig_post` and then deletes the post,
#       and a post is written after the offer on the way in. Both sides therefore lock the offer before
#       the post, and nothing deadlocks, AS LONG AS no NEW lock is taken after level 5 except on rows
#       already held earlier in the same transaction, or brand-new inserts: every level-4 set is locked
#       up front, so statements that follow the post update (e.g. `override_remove`'s final
#       `VagtTildeling` delete, or the `_close_forslag` calls after `_close_invalidated_offers`) only
#       touch rows already held. `_archive_hurtig_posts` (and the `post_delete` receiver in
#       koekken.signals, which fires inside the deleter's transaction after the offer row is gone)
#       are the level-5 writes, and `_publish_offer` runs after the offer is created.
# Corollaries:
#   * Never lock the offer before its row. Anything that DELETES `VagtTildeling` rows cascades into
#     `VagtBytte` and `VagtBytteForslag` (offers/proposals deleted before the row), so a deleter that
#     does not lock the rows first would hold an offer or proposal and then ask for its row while a
#     take-over or trade holds the row and asks for it: a deadlock. Every deleter therefore locks the rows
#     first, in the same ascending order (`_delete_replaceable_tildelinger`, `override_remove`,
#     `reconcile_month`, `declare_fridag`), which makes the two sides block on each other instead. They
#     then also call `lock_cascade_dependents`: Django's cascade fast-deletes proposals in two separate
#     statements (by offer, by `modydelse`), so the offers and proposals it reaches are locked up front.
#   * A given level's FULL set of rows for one logical operation must be locked in ONE ascending-pk
#     statement (or a provably-consistent single sequence), never in several separate lock statements for
#     the same level within one transaction: two such sequences can interleave with another writer's
#     single ascending pass and cross (take-over / flag / whole take-over lock their proposal set with
#     `_lock_forslag_for` before closing anything, exactly as `accept_trade` does).
#   * Every INSERT of a dependent row first locks all its parent `VagtTildeling` rows (an offer: its row;
#     a proposal: X and Y). Otherwise a deleter's unlocked cascade-collect SELECT could miss a
#     just-inserted dependent and the deleter's commit would fail on the foreign key.
#   * Closing or lapsing SEVERAL offers or proposals SELECTs them `FOR UPDATE` in ascending pk order
#     first (`_lock_byttes`, `_lock_forslag`) and only then updates exactly the locked pks. A bare
#     `.filter(...).update(...)` makes no ordering promise, so two overlapping ones can deadlock.
# Every predicate (`can_offer`, `can_take`, `can_take_whole`, `proposable_rows`, `can_accept`, ...) is a
# pure read with no locking: the services re-check everything after locking.
# ---------------------------------------------------------------------------------------------------

_HANDED_OFF_STATUSES = [
    VagtBytte.Status.OVERTAGET,
    VagtBytte.Status.OVERTAGET_HEL,
    VagtBytte.Status.BYTTET,
]


def has_started(
    vagt: Vagt, *, at: datetime | None = None, regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None
) -> bool:
    """Whether `vagt`'s shift has started -- the moment an open offer expires (design doc §3/§4). The
    start is read live off `VagtRegel.start_time`, via `marking_window`'s opening time. Expiry is
    derived and never written, so every read of an open offer must go through this."""
    return (at or current_datetime()) >= marking_window(vagt, regel_lookup=regel_lookup)[0]


def month_population_ids(year: int, month: int) -> set[int]:
    """The ids of everyone in (year, month)'s population (real `Residency` list, or the A2.2
    projection) -- the batched input of `may_hold`."""
    return {r.pk for r in _resolve_population(year, month)}


def may_hold(resident: Resident, vagt: Vagt, *, population_ids: set[int] | None = None) -> bool:
    """Whether `resident` may hold `vagt`: in that month's population, and not moved out before the
    shift date (design doc §3). Deliberately NOT checked: `weekday_unavailable`, the avoidance
    pattern, the 2x cap, away ranges -- those limit what the allocator may impose, not what a
    resident may choose.

    `population_ids` is an optional precomputed `month_population_ids` for batching.

    **P3 step 3's `claim_vagt` must reuse this** and never write its own copy of the rule.
    """
    if resident.move_out_date is not None and resident.move_out_date < vagt.date:
        return False
    if population_ids is None:
        population_ids = month_population_ids(vagt.date.year, vagt.date.month)
    return resident.pk in population_ids


def handed_off_tildeling_filter() -> Q:
    """A condition matching every `VagtTildeling` that is a completed Amendment 4 hand-off: a
    `VagtBytte` with status `OVERTAGET`/`OVERTAGET_HEL`/`BYTTET` points at it (the offered side X of a
    take-over or trade), OR a `VagtBytteForslag` with status `ACCEPTERET` has it as `modydelse` (the
    proposer's side Y of a trade). Identified by query, never by a flag on the row (design doc §4). Usable
    in `.filter()` and `.exclude()`. Used by the delete of the three force re-run entry points
    (`_delete_replaceable_tildelinger`, after it has locked the rows) and by `force_rerun_impact`."""
    return Q(Exists(VagtBytte.objects.filter(tildeling=OuterRef("pk"), status__in=_HANDED_OFF_STATUSES))) | Q(
        Exists(
            VagtBytteForslag.objects.filter(
                modydelse=OuterRef("pk"), status=VagtBytteForslag.Status.ACCEPTERET
            )
        )
    )


def tildeling_ids_with_anmeldelse(tildeling_ids: Iterable[int]) -> set[int]:
    """Of `tildeling_ids`, those with ANY `VagtAnmeldelse`, in any status (batched `can_offer` input).
    A row with flag history cannot be offered: moving it would attach another resident's flag
    history to the taker."""
    ids = list(tildeling_ids)
    if not ids:
        return set()
    return set(
        VagtAnmeldelse.objects.filter(vagt_tildeling_id__in=ids).values_list("vagt_tildeling_id", flat=True)
    )


def tildeling_ids_with_open_offer(tildeling_ids: Iterable[int]) -> set[int]:
    """Of `tildeling_ids`, those with an `AABEN` offer (batched `can_offer`/`can_take_whole` input)."""
    ids = list(tildeling_ids)
    if not ids:
        return set()
    return set(
        VagtBytte.objects.filter(tildeling_id__in=ids, status=VagtBytte.Status.AABEN).values_list(
            "tildeling_id", flat=True
        )
    )


def held_by_vagt(resident: Resident, vagt_ids: Iterable[int] | None = None) -> dict[int, VagtTildeling]:
    """`resident`'s rows keyed by `vagt_id` (every status), optionally restricted to `vagt_ids` -- the
    batched `held` input of `can_take`/`can_take_whole`."""
    qs = VagtTildeling.objects.filter(resident=resident)
    if vagt_ids is not None:
        qs = qs.filter(vagt_id__in=list(vagt_ids))
    return {row.vagt_id: row for row in qs}


def _population_for(vagt: Vagt, cache: dict[tuple[int, int], set[int]]) -> set[int]:
    key = (vagt.date.year, vagt.date.month)
    if key not in cache:
        cache[key] = month_population_ids(*key)
    return cache[key]


def can_offer(
    tildeling: VagtTildeling,
    resident: Resident,
    *,
    at: datetime | None = None,
    regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None,
    flagged_ids: set[int] | None = None,
    offered_ids: set[int] | None = None,
) -> bool:
    """Whether `resident` may offer `tildeling` (UI predicate, pure read -- `offer_tildeling` re-checks
    everything under lock). `flagged_ids`/`offered_ids` are optional precomputed
    `tildeling_ids_with_anmeldelse`/`tildeling_ids_with_open_offer` sets, so the context builder can
    batch."""
    if tildeling.resident_id != resident.pk or tildeling.status != VagtTildeling.Status.TILDELT:
        return False
    if has_started(tildeling.vagt, at=at, regel_lookup=regel_lookup):
        return False
    if flagged_ids is None:
        flagged_ids = tildeling_ids_with_anmeldelse([tildeling.pk])
    if tildeling.pk in flagged_ids:
        return False
    if offered_ids is None:
        offered_ids = tildeling_ids_with_open_offer([tildeling.pk])
    return tildeling.pk not in offered_ids


def _offer_is_live(
    bytte: VagtBytte, *, at: datetime | None, regel_lookup: dict[tuple[str, bool], VagtRegel] | None
) -> bool:
    """Open, unexpired, and still on the offerer's own `TILDELT` row. Needs `bytte.tildeling.vagt`."""
    tildeling = bytte.tildeling
    return (
        bytte.status == VagtBytte.Status.AABEN
        and tildeling.status == VagtTildeling.Status.TILDELT
        and tildeling.resident_id == bytte.tilbudt_af_id
        and not has_started(tildeling.vagt, at=at, regel_lookup=regel_lookup)
    )


def can_take(
    bytte: VagtBytte,
    resident: Resident,
    *,
    at: datetime | None = None,
    regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None,
    population: dict[tuple[int, int], set[int]] | None = None,
    held: dict[int, VagtTildeling] | None = None,
    flagged_ids: set[int] | None = None,
) -> bool:
    """Whether `resident` may take `bytte` over (UI predicate, pure read). False exactly when
    `can_take_whole` is the one that applies: a resident already holding a row on that `Vagt` (the
    offerer's partner) never gets "Tag vagten". `population` is a per-month cache (filled in place);
    `held` is `resident`'s rows keyed by `vagt_id`. A row with flag history (any status) is not takeable
    (design doc §3); `flagged_ids` is an optional precomputed `tildeling_ids_with_anmeldelse`."""
    if resident.pk == bytte.tilbudt_af_id or not _offer_is_live(bytte, at=at, regel_lookup=regel_lookup):
        return False
    if flagged_ids is None:
        flagged_ids = tildeling_ids_with_anmeldelse([bytte.tildeling_id])
    if bytte.tildeling_id in flagged_ids:
        return False
    vagt = bytte.tildeling.vagt
    if held is None:
        held = held_by_vagt(resident)
    if vagt.pk in held:
        return False
    if population is None:
        population = {}
    return may_hold(resident, vagt, population_ids=_population_for(vagt, population))


def can_take_whole(
    bytte: VagtBytte,
    resident: Resident,
    *,
    at: datetime | None = None,
    regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None,
    population: dict[tuple[int, int], set[int]] | None = None,
    held: dict[int, VagtTildeling] | None = None,
    offered_ids: set[int] | None = None,
    flagged_ids: set[int] | None = None,
) -> bool:
    """Whether `resident` may take the WHOLE shift through `bytte` (UI predicate, pure read): the
    shift has headcount 2 (read off the `Vagt`'s own snapshot, never `VagtRegel`), `resident` holds
    the other row on it, that row is `TILDELT`, and it has no open offer of its own. The offered row
    must have no flag history either (`flagged_ids` as in `can_take`): taking the whole shift would
    delete it, and its history with it."""
    if resident.pk == bytte.tilbudt_af_id or not _offer_is_live(bytte, at=at, regel_lookup=regel_lookup):
        return False
    if flagged_ids is None:
        flagged_ids = tildeling_ids_with_anmeldelse([bytte.tildeling_id])
    if bytte.tildeling_id in flagged_ids:
        return False
    vagt = bytte.tildeling.vagt
    if vagt.headcount != 2:
        return False
    if held is None:
        held = held_by_vagt(resident)
    mine = held.get(vagt.pk)
    if mine is None or mine.status != VagtTildeling.Status.TILDELT:
        return False
    if offered_ids is None:
        offered_ids = tildeling_ids_with_open_offer([mine.pk])
    if mine.pk in offered_ids:
        return False
    if population is None:
        population = {}
    return may_hold(resident, vagt, population_ids=_population_for(vagt, population))


def proposable_rows(
    resident: Resident,
    bytte: VagtBytte,
    *,
    at: datetime | None = None,
    regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None,
    population: dict[tuple[int, int], set[int]] | None = None,
    flagged_ids: set[int] | None = None,
    offered_ids: set[int] | None = None,
    held_by_offerer: dict[int, VagtTildeling] | None = None,
    own_rows: Iterable[VagtTildeling] | None = None,
    held: dict[int, VagtTildeling] | None = None,
    proposed_ids: set[int] | None = None,
) -> list[VagtTildeling]:
    """`resident`'s own rows Y that they may propose in exchange for `bytte`'s row X (UI predicate, pure
    read -- `propose_trade` re-checks everything under lock). Empty unless X's offer is live (open,
    unexpired, X still the offerer's own `TILDELT` row, no flag history), `resident` is not the offerer,
    holds no row on X's `Vagt` and `may_hold` it. Each Y must satisfy `can_offer` (so: `resident`'s own,
    `TILDELT`, unstarted, no flag history, and no open offer of its own), the offerer must not already hold
    a row on Y's `Vagt` and must `may_hold` it, and Y must not already be an open proposal of `resident`
    on this offer. Batching inputs: `flagged_ids`/`offered_ids` as for `can_offer` (covering X and the
    rows), `held_by_offerer` = the offerer's rows keyed by `vagt_id`, `held` = `resident`'s rows keyed by
    `vagt_id`, `own_rows` = `resident`'s candidate rows (with `vagt` loaded), `proposed_ids` = Y ids with
    an open proposal by `resident` on this offer; `population` is the per-month cache of `can_take`."""
    if resident.pk == bytte.tilbudt_af_id or not _offer_is_live(bytte, at=at, regel_lookup=regel_lookup):
        return []
    x = bytte.tildeling
    if own_rows is None:
        own_rows = VagtTildeling.objects.filter(
            resident=resident, status=VagtTildeling.Status.TILDELT
        ).select_related("vagt")
    own = list(own_rows)
    if flagged_ids is None:
        flagged_ids = tildeling_ids_with_anmeldelse([x.pk, *(r.pk for r in own)])
    if x.pk in flagged_ids:
        return []
    if held is None:
        held = held_by_vagt(resident, [x.vagt_id])
    if x.vagt_id in held:
        return []
    if population is None:
        population = {}
    if not may_hold(resident, x.vagt, population_ids=_population_for(x.vagt, population)):
        return []
    if offered_ids is None:
        offered_ids = tildeling_ids_with_open_offer(r.pk for r in own)
    if held_by_offerer is None:
        held_by_offerer = held_by_vagt(bytte.tilbudt_af, [r.vagt_id for r in own])
    if proposed_ids is None:
        proposed_ids = set(
            VagtBytteForslag.objects.filter(
                bytte=bytte, foreslaaet_af=resident, status=VagtBytteForslag.Status.AABEN
            ).values_list("modydelse_id", flat=True)
        )
    result = []
    for y in own:
        if y.pk in proposed_ids or y.vagt_id in held_by_offerer:
            continue
        if not can_offer(
            y, resident, at=at, regel_lookup=regel_lookup, flagged_ids=flagged_ids, offered_ids=offered_ids
        ):
            continue
        if not may_hold(bytte.tilbudt_af, y.vagt, population_ids=_population_for(y.vagt, population)):
            continue
        result.append(y)
    return result


def forslag_is_live(
    forslag: VagtBytteForslag,
    *,
    at: datetime | None = None,
    regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None,
    flagged_ids: set[int] | None = None,
) -> bool:
    """Whether an open proposal is still live (derived, never written): `AABEN`, its offer live (X
    unstarted, `TILDELT`, still the offerer's, no flag history) and Y still the proposer's own unstarted,
    `TILDELT`, flag-free row. Needs `forslag.bytte.tildeling.vagt` and `forslag.modydelse.vagt` loaded."""
    bytte, y = forslag.bytte, forslag.modydelse
    if forslag.status != VagtBytteForslag.Status.AABEN:
        return False
    if not _offer_is_live(bytte, at=at, regel_lookup=regel_lookup):
        return False
    if y.resident_id != forslag.foreslaaet_af_id or y.status != VagtTildeling.Status.TILDELT:
        return False
    if has_started(y.vagt, at=at, regel_lookup=regel_lookup):
        return False
    if flagged_ids is None:
        flagged_ids = tildeling_ids_with_anmeldelse([bytte.tildeling_id, y.pk])
    return bytte.tildeling_id not in flagged_ids and y.pk not in flagged_ids


def can_accept(
    forslag: VagtBytteForslag,
    resident: Resident,
    *,
    at: datetime | None = None,
    regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None,
    population: dict[tuple[int, int], set[int]] | None = None,
    flagged_ids: set[int] | None = None,
    vagt_residents: set[tuple[int, int]] | None = None,
) -> bool:
    """Whether `resident` may accept `forslag` (UI predicate, pure read): they are the offerer, the proposal
    is `forslag_is_live`, neither side already holds a row on the other's `Vagt`, and both `may_hold`
    checks pass. The "Y has no open offer" rule of `propose_trade` deliberately does NOT apply here: the
    swap lapses such an offer. `vagt_residents` is an optional batched set of `(vagt_id, resident_id)`
    pairs covering both shifts."""
    bytte = forslag.bytte
    if resident.pk != bytte.tilbudt_af_id:
        return False
    if not forslag_is_live(forslag, at=at, regel_lookup=regel_lookup, flagged_ids=flagged_ids):
        return False
    vagt_x, vagt_y, proposer = bytte.tildeling.vagt, forslag.modydelse.vagt, forslag.foreslaaet_af
    if vagt_residents is None:
        vagt_residents = set(
            VagtTildeling.objects.filter(vagt_id__in=[vagt_x.pk, vagt_y.pk]).values_list(
                "vagt_id", "resident_id"
            )
        )
    if (vagt_x.pk, proposer.pk) in vagt_residents or (vagt_y.pk, resident.pk) in vagt_residents:
        return False
    if population is None:
        population = {}
    return may_hold(proposer, vagt_x, population_ids=_population_for(vagt_x, population)) and may_hold(
        resident, vagt_y, population_ids=_population_for(vagt_y, population)
    )


def can_decline(forslag: VagtBytteForslag, resident: Resident) -> bool:
    """Whether `resident` may decline `forslag`: they are the offerer and it is `AABEN`. Allowed even when
    it has gone stale or expired, as cleanup."""
    return forslag.status == VagtBytteForslag.Status.AABEN and resident.pk == forslag.bytte.tilbudt_af_id


def can_withdraw_proposal(forslag: VagtBytteForslag, resident: Resident) -> bool:
    """Whether `resident` may withdraw `forslag`: they are the proposer and it is `AABEN`."""
    return forslag.status == VagtBytteForslag.Status.AABEN and resident.pk == forslag.foreslaaet_af_id


def _lock_tildelinger(pks: Iterable[int]) -> dict[int, VagtTildeling]:
    """Lock the given `VagtTildeling` rows in ASCENDING pk order (lock step 1). Rows that no longer
    exist are simply absent from the result. `of=("self",)` so no joined table is locked."""
    rows = VagtTildeling.objects.select_for_update(of=("self",)).filter(pk__in=list(pks)).order_by("pk")
    return {row.pk: row for row in rows}


def lock_tildeling(pk: int) -> VagtTildeling | None:
    """Lock one `VagtTildeling` row (lock step 1) for a caller that is about to DELETE it, or None if it
    is gone. Must run inside `transaction.atomic()`."""
    return _lock_tildelinger([pk]).get(pk)


def _lock_byttes(pks: Iterable[int]) -> dict[int, VagtBytte]:
    """Lock the given `VagtBytte` rows in ASCENDING pk order (lock step 3), materialised, keyed by pk.
    Rows that no longer exist are simply absent. Same pattern as `_lock_tildelinger`."""
    rows = VagtBytte.objects.select_for_update(of=("self",)).filter(pk__in=list(pks)).order_by("pk")
    return {row.pk: row for row in rows}


def _lock_forslag(pks: Iterable[int]) -> dict[int, VagtBytteForslag]:
    """Lock the given `VagtBytteForslag` rows in ASCENDING pk order (lock step 4), materialised, keyed by
    pk. Rows that no longer exist are simply absent. Same pattern as `_lock_tildelinger`."""
    rows = VagtBytteForslag.objects.select_for_update(of=("self",)).filter(pk__in=list(pks)).order_by("pk")
    return {row.pk: row for row in rows}


def lock_cascade_dependents(rows: Iterable[VagtTildeling | int]) -> None:
    """Before DELETING `VagtTildeling` rows (whose level-1 locks the caller already holds): lock every
    `VagtBytte` on them (level 3, ascending) and then every `VagtBytteForslag` the cascade will delete
    (level 4: proposals on those offers OR using a row as `modydelse`) in ONE ascending statement.

    Django's cascade would otherwise fast-delete the proposals in two separate statements
    (`WHERE bytte_id IN ...`, `WHERE modydelse_id IN ...`), each locking in its own order, which can cross
    an `accept_trade` that locks its whole proposal set in one ascending pass. Locking everything up front
    in one ascending pass makes the cascade's own statements lock-free (we already hold the rows)."""
    pks = [r if isinstance(r, int) else r.pk for r in rows]
    if not pks:
        return
    _lock_byttes(VagtBytte.objects.filter(tildeling_id__in=pks).values_list("pk", flat=True))  # lock 3
    list(
        VagtBytteForslag.objects.filter(Q(bytte__tildeling__in=pks) | Q(modydelse__in=pks))
        .select_for_update(of=("self",))
        .order_by("pk")
    )  # lock 4: one ascending statement, materialised


def _lock_forslag_for(*, offer_pks: Iterable[int], row_pks: Iterable[int]) -> dict[int, VagtBytteForslag]:
    """Lock (level 4) in ONE ascending pass every proposal this operation can touch: those on the given
    offers plus those using one of `row_pks` as `modydelse`. Callers hold the rows and offers already."""
    pks = VagtBytteForslag.objects.filter(
        Q(bytte_id__in=list(offer_pks)) | Q(modydelse_id__in=list(row_pks))
    ).values_list("pk", flat=True)
    return _lock_forslag(pks)


def _close_forslag(qs: QuerySet[VagtBytteForslag]) -> None:
    """Lapse (`BORTFALDET`) every `AABEN` proposal in `qs`. The candidates are first locked `FOR UPDATE` in
    ascending pk order (`_lock_forslag`), status is re-checked on the locked versions, and only those
    exact pks are updated -- never a bare multi-row `UPDATE` over a filter. The caller must already hold
    the `VagtTildeling` rows (lock 1) that make the candidate set complete."""
    candidates = list(qs.filter(status=VagtBytteForslag.Status.AABEN).values_list("pk", flat=True))
    if not candidates:
        return
    locked = _lock_forslag(candidates)
    ids = [pk for pk, f in locked.items() if f.status == VagtBytteForslag.Status.AABEN]
    if ids:
        VagtBytteForslag.objects.filter(pk__in=ids).update(
            status=VagtBytteForslag.Status.BORTFALDET, closed_at=current_datetime()
        )


def _close_invalidated_forslag(
    rows: Iterable[VagtTildeling], *, keep: VagtBytteForslag | None = None
) -> None:
    """After a move (design doc §5.6): every `AABEN` proposal that uses a moved row as `modydelse` lapses,
    except `keep` (the proposal being accepted)."""
    qs = VagtBytteForslag.objects.filter(modydelse__in=list(rows))
    if keep is not None:
        qs = qs.exclude(pk=keep.pk)
    _close_forslag(qs)


def _close_invalidated_offers(tildelinger: Iterable[VagtTildeling], *, keep: VagtBytte | None = None) -> None:
    """After a move or a flag (design doc §5.6): every OTHER open offer on a touched row lapses to
    `BORTFALDET`, because the row now belongs to someone who never offered it (or can no longer be
    offered), and so do the open proposals on each offer it closes (`_close_forslag`). The target offers are
    locked `FOR UPDATE` in ascending pk order BEFORE the update, never a bare `.update()` over a filter.
    Offers are normally unique per row (partial unique constraint), but a trade moves TWO rows, so Y's own
    open offer is a real target here."""
    qs = VagtBytte.objects.filter(tildeling__in=list(tildelinger), status=VagtBytte.Status.AABEN)
    if keep is not None:
        qs = qs.exclude(pk=keep.pk)
    candidates = list(qs.values_list("pk", flat=True))
    if not candidates:
        return
    locked = _lock_byttes(candidates)  # lock 3
    ids = [pk for pk, b in locked.items() if b.status == VagtBytte.Status.AABEN]  # re-check when locked
    if not ids:
        return
    VagtBytte.objects.filter(pk__in=ids).update(
        status=VagtBytte.Status.BORTFALDET, closed_at=current_datetime()
    )
    _close_forslag(VagtBytteForslag.objects.filter(bytte_id__in=ids))  # lock 4
    _archive_hurtig_posts(ids)  # lock 5: the posts, LAST


def _archive_posts(post_ids: Iterable[int]) -> None:
    """Archive (`expires_at = now`) the given Den Hurtige posts -- Den Hurtige already defines "expired" as
    "archived", so no new delete semantics. A post that is already expired is untouched by the
    `expires_at__gt` filter, and an unknown pk matches nothing. Soft-deletion (`QuickPost.soft_delete`)
    never touches `expires_at`, so a tombstone that has not expired yet IS archived along with its offer,
    which is harmless. LOCK ORDER level 5: after this, no NEW lock is taken except on rows already held
    earlier in the transaction (see the LOCK ORDER comment)."""
    from den_hurtige.models import QuickPost  # local: den_hurtige is not otherwise a dependency here

    ids = sorted(set(post_ids))  # ascending, like every other multi-row write here
    if not ids:
        return
    now = current_datetime()
    QuickPost.objects.filter(pk__in=ids, expires_at__gt=now).update(expires_at=now)


def _archive_hurtig_posts(bytte_ids: Iterable[int]) -> None:
    """Archive the Den Hurtige post of every offer in `bytte_ids` that has one. Call it at EVERY point where
    an offer leaves `AABEN`, as the last statement of the transaction (LOCK ORDER level 5). Cascade
    deletes are covered by the `post_delete` receiver in koekken.signals instead."""
    ids = list(bytte_ids)
    if not ids:
        return
    _archive_posts(
        VagtBytte.objects.filter(pk__in=ids, hurtig_post__isnull=False).values_list(
            "hurtig_post_id", flat=True
        )
    )


def _notify(resident: Resident, body: str) -> None:
    """Push to one resident, the `resolve_anmeldelse` way: the audience is narrowed through
    `access.allowed_subscribers`, and `send` (default background mode) dispatches after commit."""
    from core.push import send, subscribers  # local: same reasoning as resolve_anmeldelse

    from . import access

    audience = access.allowed_subscribers(subscribers(TOPIC).filter(user=resident))
    send(audience, "Køkkenvagt", body, "/intern/koekken/")


def _dansk_dato(day: date) -> str:
    """ "tirsdag 12. januar"."""
    return f"{WEEKDAYS[day.weekday()]} {day.day}. {MONTHS[day.month]}"


def _publish_offer(offer: VagtBytte, vagt: Vagt, by: Resident, hurtig_link: str) -> None:
    """Share `offer` in Den Hurtige's `koekken` channel, under the offerer's own name (design doc §8). A
    failed post must NEVER block the offer: the post is attempted in its own savepoint, so even a
    database-level failure rolls back only the post attempt, and any failure is logged and swallowed.
    Runs after the offer exists and writes the post last (LOCK ORDER level 5)."""
    from den_hurtige import (
        services as hurtig,
    )  # local: den_hurtige never imports koekken, only this way round

    # The earlier of the shift's start and 2 døgn (the longest duration Den Hurtige offers).
    expires_at = min(marking_window(vagt)[0], current_datetime() + timedelta(minutes=2880))
    content = (
        f"Jeg kan ikke tage min {vagt.get_kind_display().lower()} {_dansk_dato(vagt.date)}. Kan du? "
        f"Tag den under Køkkenvagter: {hurtig_link}"
    )
    try:
        with transaction.atomic():
            post = hurtig.publish_post(by, "koekken", content, expires_at)
            offer.hurtig_post = post
            offer.save(update_fields=["hurtig_post"])
    except Exception:
        offer.hurtig_post = None  # the savepoint rolled the link back; keep the in-memory offer honest
        logger.warning("Could not share offer %s in Den Hurtige", offer.pk, exc_info=True)


def offer_tildeling(tildeling: VagtTildeling, by: Resident, *, hurtig_link: str | None = None) -> VagtBytte:
    """Offer `tildeling` for take-over (design doc §3, §5.1). Moves nothing: `by` stays responsible
    until somebody takes it. Refuses (everything re-checked under lock) unless `by` holds the row, it is
    `TILDELT`, its shift has not started, it has no `VagtAnmeldelse` in ANY status and no open offer.
    Sends no notification of its own. When `hurtig_link` (the absolute URL of the Køkkenvagter page) is given,
    the offer is also shared as a post in Den Hurtige (`_publish_offer`); None posts nothing."""
    with transaction.atomic():
        row = _lock_tildelinger([tildeling.pk]).get(tildeling.pk)
        if row is None:
            raise KoekkenAllocationError("Vagten findes ikke længere.")
        if row.resident_id != by.pk:
            raise KoekkenAllocationError("Du kan kun tilbyde dine egne vagter.")
        if row.status != VagtTildeling.Status.TILDELT:
            raise KoekkenAllocationError(
                "Vagten kan ikke tilbydes: den er allerede meldt udført eller anmeldt."
            )
        vagt = Vagt.objects.get(pk=row.vagt_id)
        if has_started(vagt):
            raise KoekkenAllocationError("Vagten er allerede startet og kan ikke længere tilbydes.")
        if VagtAnmeldelse.objects.filter(vagt_tildeling=row).exists():
            raise KoekkenAllocationError(
                "Vagten kan ikke tilbydes, fordi den har en anmeldelse i sin historik."
            )
        already_open = "Vagten er allerede tilbudt."
        if VagtBytte.objects.filter(tildeling=row, status=VagtBytte.Status.AABEN).exists():
            raise KoekkenAllocationError(already_open)
        try:
            with transaction.atomic():  # savepoint: an IntegrityError must not poison the outer block
                offer = VagtBytte.objects.create(tildeling=row, tilbudt_af=by)
        except IntegrityError:
            raise KoekkenAllocationError(already_open) from None
        if hurtig_link is not None:
            _publish_offer(offer, vagt, by, hurtig_link)  # the post is written LAST (lock level 5)
        return offer


def withdraw_offer(bytte: VagtBytte, by: Resident) -> VagtBytte:
    """The offerer withdraws an open offer (`TRUKKET`). Not an unclaim: they simply keep the shift.
    Requires status `AABEN`, `by` == the offerer, and a shift that has not started. Sends nothing."""
    with transaction.atomic():
        row = _lock_tildelinger([bytte.tildeling_id]).get(bytte.tildeling_id)  # lock 1: the row
        if row is None:
            raise KoekkenAllocationError("Tilbuddet findes ikke længere.")
        offer = VagtBytte.objects.select_for_update().filter(pk=bytte.pk).first()  # lock 3: the offer
        if offer is None or offer.status != VagtBytte.Status.AABEN:
            raise KoekkenAllocationError("Tilbuddet er ikke længere åbent.")
        if offer.tilbudt_af_id != by.pk:
            raise KoekkenAllocationError("Du kan kun trække dine egne tilbud tilbage.")
        if has_started(Vagt.objects.get(pk=row.vagt_id)):
            raise KoekkenAllocationError("Vagten er allerede startet, så tilbuddet er udløbet.")
        offer.status = VagtBytte.Status.TRUKKET
        offer.closed_at = current_datetime()
        offer.save(update_fields=["status", "closed_at"])
        _close_forslag(VagtBytteForslag.objects.filter(bytte=offer))  # lock 4: its proposals lapse with it
        _archive_hurtig_posts([offer.pk])  # lock 5: the post, LAST
        return offer


def _take(bytte: VagtBytte, by: Resident, *, whole: bool) -> VagtBytte:
    """The shared body of `take_over` and `take_over_whole`, so the lock order lives in one place."""
    gone = "Tilbuddet findes ikke længere."
    row_pks = [bytte.tildeling_id]
    partner_pk: int | None = None
    if whole:
        # An UNLOCKED read, only to learn which second row to lock; everything is re-verified under lock.
        vagt_id = (
            VagtTildeling.objects.filter(pk=bytte.tildeling_id).values_list("vagt_id", flat=True).first()
        )
        if vagt_id is None:
            raise KoekkenAllocationError(gone)
        partner_pk = (
            VagtTildeling.objects.filter(vagt_id=vagt_id, resident=by).values_list("pk", flat=True).first()
        )
        if partner_pk is None:
            raise KoekkenAllocationError(
                "Du har ikke en plads på denne vagt og kan derfor ikke tage hele vagten."
            )
        row_pks.append(partner_pk)

    lapsed: str | None = None
    with transaction.atomic():
        locked = _lock_tildelinger(row_pks)  # lock 1: rows, ascending pk
        row = locked.get(bytte.tildeling_id)
        if row is None:
            raise KoekkenAllocationError(gone)
        vagt_qs = Vagt.objects.select_for_update() if whole else Vagt.objects  # lock 2 (whole only)
        vagt = vagt_qs.get(pk=row.vagt_id)
        offer = VagtBytte.objects.select_for_update().filter(pk=bytte.pk).first()  # lock 3: the offer
        if offer is None:
            raise KoekkenAllocationError(gone)
        if offer.status == VagtBytte.Status.TRUKKET:
            raise KoekkenAllocationError("Tilbuddet er trukket tilbage.")
        if offer.status != VagtBytte.Status.AABEN or offer.tildeling_id != row.pk:
            raise KoekkenAllocationError("Tilbuddet er ikke længere åbent -- nogen andre var først.")
        if has_started(vagt):
            raise KoekkenAllocationError("Vagten er allerede startet, så tilbuddet er udløbet.")
        if row.resident_id != offer.tilbudt_af_id or row.status != VagtTildeling.Status.TILDELT:
            # Defensive (normal paths delete + cascade the offer): the offerer no longer holds the
            # row. Mark the offer dead, but raise only AFTER the block so the write is not rolled back.
            offer.status = VagtBytte.Status.BORTFALDET
            offer.closed_at = current_datetime()
            offer.save(update_fields=["status", "closed_at"])
            _archive_hurtig_posts([offer.pk])
            lapsed = "Tilbuddet er bortfaldet: tilbyderen står ikke længere på vagten."
        elif VagtAnmeldelse.objects.filter(vagt_tildeling=row).exists():
            # Re-checked at take time (design doc §3), not just at offer time: a row flagged after it
            # was offered and then dismissed back to TILDELT must not carry its flag history to the taker
            # (or lose it by cascade in the whole-shift case). The offer lapses, persisted as above.
            offer.status = VagtBytte.Status.BORTFALDET
            offer.closed_at = current_datetime()
            offer.save(update_fields=["status", "closed_at"])
            _archive_hurtig_posts([offer.pk])
            lapsed = "Tilbuddet er bortfaldet: vagten har en anmeldelse i sin historik."
        else:
            if by.pk == offer.tilbudt_af_id:
                raise KoekkenAllocationError("Du kan ikke overtage din egen vagt.")
            if not whole:
                if VagtTildeling.objects.filter(vagt=vagt, resident=by).exists():
                    hint = (
                        ' Brug i stedet "Tag hele vagten".'
                        if vagt.headcount == 2
                        and VagtTildeling.objects.filter(
                            vagt=vagt, resident=by, status=VagtTildeling.Status.TILDELT
                        ).exists()
                        else ""
                    )
                    raise KoekkenAllocationError(f"Du har allerede en plads på {vagt}.{hint}")
            else:
                if vagt.headcount != 2:
                    raise KoekkenAllocationError("Kun en vagt med to pladser kan overtages som helhed.")
                partner = locked.get(partner_pk) if partner_pk is not None else None
                if partner is None or partner.resident_id != by.pk or partner.vagt_id != vagt.pk:
                    raise KoekkenAllocationError("Din egen plads på vagten findes ikke længere.")
                if partner.status != VagtTildeling.Status.TILDELT:
                    raise KoekkenAllocationError(
                        "Din egen plads på vagten er allerede meldt udført eller anmeldt."
                    )
                if VagtBytte.objects.filter(tildeling=partner, status=VagtBytte.Status.AABEN).exists():
                    raise KoekkenAllocationError(
                        "Du har selv tilbudt din plads på vagten -- træk dit tilbud tilbage først."
                    )
            if not may_hold(by, vagt):
                raise KoekkenAllocationError(
                    "Du kan ikke tage denne vagt: du står ikke på beboerlisten for måneden, "
                    "eller du er fraflyttet inden vagtens dato."
                )
            now = current_datetime()
            offerer = offer.tilbudt_af
            if not whole:
                row.resident = by
                try:
                    with transaction.atomic():  # savepoint for the (vagt, resident) race backstop
                        row.save(update_fields=["resident"])
                except IntegrityError:
                    raise KoekkenAllocationError(f"Du har allerede en plads på {vagt}.") from None
                offer.status = VagtBytte.Status.OVERTAGET
                offer.overtaget_af = by
                offer.closed_at = now
                offer.save(update_fields=["status", "overtaget_af", "closed_at"])
                # The offer is closed: its open proposals lapse; and X moved, so proposals that offer X
                # elsewhere as their `modydelse` are invalid too. Lock that WHOLE level-4 set in one
                # ascending pass before closing anything (the closes below only re-lock held rows).
                _lock_forslag_for(offer_pks=[offer.pk], row_pks=[row.pk])
                _close_invalidated_offers([row], keep=offer)
                _close_forslag(VagtBytteForslag.objects.filter(bytte=offer))
                _close_invalidated_forslag([row])
                _archive_hurtig_posts([offer.pk])  # lock 5: the post, LAST
                _notify(
                    offer.tilbudt_af,
                    f"{by.full_name} har overtaget din {vagt}. Du er ikke længere på vagten.",
                )
            else:
                # (1) Re-point the offer at the surviving row and SAVE BEFORE the delete below:
                # `VagtBytte.tildeling` is CASCADE, so deleting the vacated row first would silently
                # take the offer record with it.
                offer.tildeling = cast(VagtTildeling, partner)
                offer.status = VagtBytte.Status.OVERTAGET_HEL
                offer.overtaget_af = by
                offer.closed_at = now
                offer.save(update_fields=["tildeling", "status", "overtaget_af", "closed_at"])
                # Lapse the proposals BEFORE the delete (the same cascade-trap lesson): the offer's own, and
                # those using the partner's surviving row as `modydelse` (it is now a different, whole
                # shift). Proposals using the vacated row as `modydelse` go with it by cascade (design doc §5.6).
                # ONE ascending lock of the whole level-4 set first: the offer's proposals plus those using
                # the partner row or the vacated row as `modydelse` (the latter reached by the delete's cascade).
                # The delete also cascades into EVERY other offer ever made on the vacated row (any status:
                # withdrawn, lapsed, ...) and their proposals, so those offers join the same single pass.
                # Only V's holder ever reaches them, and we hold V, so locking them here is safe.
                offer_pks = [offer.pk, *VagtBytte.objects.filter(tildeling=row).values_list("pk", flat=True)]
                _lock_forslag_for(offer_pks=offer_pks, row_pks=[row.pk, cast(VagtTildeling, partner).pk])
                _close_forslag(VagtBytteForslag.objects.filter(bytte=offer))
                _close_invalidated_forslag([cast(VagtTildeling, partner)])
                row.delete()  # (2) the vacated row
                vagt.headcount = 1  # (3) (2, d) -> (1, 2d): the one sanctioned snapshot change
                vagt.duration_minutes = 2 * vagt.duration_minutes
                vagt.save(update_fields=["headcount", "duration_minutes"])
                # Lock 5, LAST. (The vacated row's OTHER offers' posts were archived by the post_delete
                # receiver when the delete above cascaded into them.)
                _archive_hurtig_posts([offer.pk])
                _notify(  # (4)
                    offerer,
                    f"{by.full_name} har overtaget hele {vagt}. Du er ikke længere på vagten.",
                )
    if lapsed:
        raise KoekkenAllocationError(lapsed)
    return offer


def take_over(bytte: VagtBytte, by: Resident) -> VagtBytte:
    """`by` takes the offered row over (design doc §5.2). The existing `VagtTildeling` MOVES to `by`
    (pk unchanged): credit, flags and the tablet follow it. The offer becomes `OVERTAGET`, any other
    open offer on the moved row lapses (`_close_invalidated_offers`) and the offerer is notified.
    Refuses an expired/closed offer, the offerer themselves, a taker already on that `Vagt` (pointing at
    "Tag hele vagten" where that applies), and a taker `may_hold` refuses."""
    return _take(bytte, by, whole=False)


def take_over_whole(bytte: VagtBytte, by: Resident) -> VagtBytte:
    """`by`, the offerer's partner on a two-person shift, takes over the WHOLE shift (design doc §5.3):
    the offer is re-pointed at `by`'s own row (`OVERTAGET_HEL`) BEFORE the vacated row is deleted (the
    cascade trap), then the `Vagt` collapses from `(2, d)` to `(1, 2d)`. Only a `Vagt` whose own
    snapshot says `headcount == 2` qualifies; `by`'s row must be `TILDELT` with no open offer of its own."""
    return _take(bytte, by, whole=True)


def _forslag_rows(forslag: VagtBytteForslag) -> tuple[int, int] | None:
    """`(x_pk, y_pk)` of `forslag`: an UNLOCKED read, only to learn which rows to lock; every service
    re-verifies against the locked rows."""
    x_pk = VagtBytte.objects.filter(pk=forslag.bytte_id).values_list("tildeling_id", flat=True).first()
    return None if x_pk is None else (x_pk, forslag.modydelse_id)


def _close_own_forslag(
    forslag: VagtBytteForslag, by: Resident, *, as_offerer: bool, status: VagtBytteForslag.Status
) -> VagtBytteForslag:
    """The shared body of `withdraw_proposal` (proposer, `TRUKKET`) and `decline_proposal` (offerer,
    `AFVIST`): lock X and Y, then the offer, then the proposal."""
    gone = "Forslaget findes ikke længere."
    rows = _forslag_rows(forslag)
    if rows is None:
        raise KoekkenAllocationError(gone)
    with transaction.atomic():
        locked = _lock_tildelinger(rows)  # lock 1: X and Y, ascending pk
        if len(locked) != 2:
            raise KoekkenAllocationError(gone)
        offer = _lock_byttes([forslag.bytte_id]).get(forslag.bytte_id)  # lock 3
        fresh = _lock_forslag([forslag.pk]).get(forslag.pk)  # lock 4
        if offer is None or fresh is None or offer.tildeling_id != rows[0]:
            raise KoekkenAllocationError(gone)
        if fresh.status != VagtBytteForslag.Status.AABEN:
            raise KoekkenAllocationError("Forslaget er ikke længere åbent.")
        if as_offerer and by.pk != offer.tilbudt_af_id:
            raise KoekkenAllocationError("Kun tilbyderen kan afvise et forslag.")
        if not as_offerer and by.pk != fresh.foreslaaet_af_id:
            raise KoekkenAllocationError("Du kan kun trække dine egne forslag tilbage.")
        fresh.status = status
        fresh.closed_at = current_datetime()
        fresh.save(update_fields=["status", "closed_at"])
        return fresh


def propose_trade(bytte: VagtBytte, modydelse: VagtTildeling, by: Resident) -> VagtBytteForslag:
    """`by` proposes their own row `modydelse` (Y) in exchange for `bytte`'s row X (design doc §3, §5.4).
    Locks X and Y together FIRST (a proposal must never be inserted without both parents locked, or a
    deleter's commit could fail on the foreign key), then the offer, and re-checks everything: the offer is
    open, X unstarted, the offerer's own `TILDELT` and flag-free (else the offer lapses, persisted, and the
    call refuses); Y is `by`'s own `TILDELT`, unstarted row with no `VagtAnmeldelse` in any status and no
    open offer of its own; `by` is not the offerer, holds no place on X's `Vagt`, the offerer holds none on
    Y's, and both `may_hold`. The offerer gets a push."""
    gone = "Tilbuddet findes ikke længere."
    lapsed: str | None = None
    with transaction.atomic():
        locked = _lock_tildelinger([bytte.tildeling_id, modydelse.pk])  # lock 1: X and Y, ascending
        x = locked.get(bytte.tildeling_id)
        if x is None:
            raise KoekkenAllocationError(gone)
        y = locked.get(modydelse.pk)
        if y is None:
            raise KoekkenAllocationError("Din vagt findes ikke længere.")
        offer = _lock_byttes([bytte.pk]).get(bytte.pk)  # lock 3
        if offer is None:
            raise KoekkenAllocationError(gone)
        if offer.status != VagtBytte.Status.AABEN or offer.tildeling_id != x.pk:
            raise KoekkenAllocationError("Tilbuddet er ikke længere åbent -- nogen andre var først.")
        vagt_x = Vagt.objects.get(pk=x.vagt_id)
        vagt_y = Vagt.objects.get(pk=y.vagt_id)
        if has_started(vagt_x):
            raise KoekkenAllocationError("Vagten er allerede startet, så tilbuddet er udløbet.")
        offerer = offer.tilbudt_af
        if x.resident_id != offer.tilbudt_af_id or x.status != VagtTildeling.Status.TILDELT:
            lapsed = "Tilbuddet er bortfaldet: tilbyderen står ikke længere på vagten."
        elif VagtAnmeldelse.objects.filter(vagt_tildeling=x).exists():
            lapsed = "Tilbuddet er bortfaldet: vagten har en anmeldelse i sin historik."
        if lapsed:
            offer.status = VagtBytte.Status.BORTFALDET
            offer.closed_at = current_datetime()
            offer.save(update_fields=["status", "closed_at"])
            _close_forslag(VagtBytteForslag.objects.filter(bytte=offer))  # lock 4
            _archive_hurtig_posts([offer.pk])  # lock 5: the post, LAST
        else:
            if by.pk == offer.tilbudt_af_id:
                raise KoekkenAllocationError("Du kan ikke foreslå bytte på dit eget tilbud.")
            if y.resident_id != by.pk:
                raise KoekkenAllocationError("Du kan kun bytte med dine egne vagter.")
            if y.status != VagtTildeling.Status.TILDELT:
                raise KoekkenAllocationError(
                    "Din vagt kan ikke bruges i et bytte: den er allerede meldt udført eller anmeldt."
                )
            if has_started(vagt_y):
                raise KoekkenAllocationError("Din vagt er allerede startet og kan ikke længere byttes.")
            if VagtAnmeldelse.objects.filter(vagt_tildeling=y).exists():
                raise KoekkenAllocationError(
                    "Din vagt kan ikke byttes, fordi den har en anmeldelse i sin historik."
                )
            if VagtBytte.objects.filter(tildeling=y, status=VagtBytte.Status.AABEN).exists():
                raise KoekkenAllocationError(f"Træk dit eget tilbud på {vagt_y} tilbage først.")
            if VagtTildeling.objects.filter(vagt=vagt_x, resident=by).exists():
                raise KoekkenAllocationError(f"Du har allerede en plads på {vagt_x}.")
            if VagtTildeling.objects.filter(vagt=vagt_y, resident=offerer).exists():
                raise KoekkenAllocationError(f"{offerer.full_name} har allerede en plads på {vagt_y}.")
            if not may_hold(by, vagt_x):
                raise KoekkenAllocationError(
                    f"Du kan ikke tage {vagt_x}: du står ikke på beboerlisten for måneden, "
                    "eller du er fraflyttet inden vagtens dato."
                )
            if not may_hold(offerer, vagt_y):
                raise KoekkenAllocationError(
                    f"{offerer.full_name} kan ikke tage {vagt_y}: "
                    "vedkommende står ikke på beboerlisten for måneden eller er fraflyttet inden vagtens dato."
                )
            already = "Du har allerede foreslået dette bytte."
            try:
                with transaction.atomic():  # savepoint: an IntegrityError must not poison the outer block
                    forslag = VagtBytteForslag.objects.create(bytte=offer, modydelse=y, foreslaaet_af=by)
            except IntegrityError:
                raise KoekkenAllocationError(already) from None
            _notify(
                offerer,
                f"{by.full_name} foreslår at bytte din {vagt_x} med {vagt_y} — svar i app'en.",
            )
            return forslag
    raise KoekkenAllocationError(lapsed or gone)


def withdraw_proposal(forslag: VagtBytteForslag, by: Resident) -> VagtBytteForslag:
    """The proposer withdraws an open proposal (`TRUKKET`). Locks X and Y, then the offer, then the
    proposal. Sends nothing."""
    return _close_own_forslag(forslag, by, as_offerer=False, status=VagtBytteForslag.Status.TRUKKET)


def decline_proposal(forslag: VagtBytteForslag, by: Resident) -> VagtBytteForslag:
    """The offerer declines an open proposal (`AFVIST`), allowed even when it has gone stale or expired
    (cleanup). Locks X and Y, then the offer, then the proposal. Sends nothing."""
    return _close_own_forslag(forslag, by, as_offerer=True, status=VagtBytteForslag.Status.AFVIST)


def accept_trade(forslag: VagtBytteForslag, by: Resident) -> VagtBytteForslag:
    """The offerer accepts a proposal (design doc §5.4): the residents of X and Y are exchanged in ONE
    savepoint (never two take-overs), the proposal becomes `ACCEPTERET`, the offer `BYTTET` with
    `overtaget_af` = the proposer, everything the move invalidated lapses (§5.6) and the proposer is
    notified.

    Lock order: X and Y (ascending pk), then every offer that will be touched (this one and any open offer
    on X or Y, ascending), then every proposal that will be touched (ascending) -- all taken up front, so
    the closes below only re-lock rows this transaction already holds. Every §3 rule is re-checked against
    the locked state. On a stale condition the lapse is PERSISTED (`BORTFALDET` on the proposal, and on the
    offer when X is the stale side) and the error is raised only AFTER the transaction block, so the write is
    not rolled back. Y may have gained an open offer since the proposal was made: that is NOT a refusal,
    the swap lapses it."""
    gone = "Forslaget findes ikke længere."
    rows = _forslag_rows(forslag)
    if rows is None:
        raise KoekkenAllocationError(gone)
    x_pk, y_pk = rows
    lapsed: str | None = None
    with transaction.atomic():
        locked = _lock_tildelinger([x_pk, y_pk])  # lock 1: X and Y, ascending pk
        x, y = locked.get(x_pk), locked.get(y_pk)
        if x is None or y is None:
            raise KoekkenAllocationError(gone)
        offer_pks = {forslag.bytte_id}
        offer_pks.update(
            VagtBytte.objects.filter(
                tildeling_id__in=[x_pk, y_pk], status=VagtBytte.Status.AABEN
            ).values_list("pk", flat=True)
        )
        offers = _lock_byttes(offer_pks)  # lock 3: the offer and Y's own open offer, ascending
        offer = offers.get(forslag.bytte_id)
        proposal_pks = {forslag.pk}
        proposal_pks.update(
            VagtBytteForslag.objects.filter(status=VagtBytteForslag.Status.AABEN)
            .filter(Q(bytte_id__in=list(offers)) | Q(modydelse_id__in=[x_pk, y_pk]))
            .values_list("pk", flat=True)
        )
        fresh = _lock_forslag(proposal_pks).get(forslag.pk)  # lock 4, ascending
        if offer is None or fresh is None or offer.tildeling_id != x_pk:
            raise KoekkenAllocationError(gone)
        if fresh.status != VagtBytteForslag.Status.AABEN:
            raise KoekkenAllocationError("Forslaget er ikke længere åbent.")
        if by.pk != offer.tilbudt_af_id:
            raise KoekkenAllocationError("Kun tilbyderen kan acceptere et forslag.")
        vagt_x, vagt_y = Vagt.objects.get(pk=x.vagt_id), Vagt.objects.get(pk=y.vagt_id)
        proposer = fresh.foreslaaet_af
        lapse_offer = False
        if offer.status != VagtBytte.Status.AABEN:
            lapsed = "Tilbuddet er ikke længere åbent."
        elif has_started(vagt_x):
            lapsed = "Vagten er allerede startet, så tilbuddet er udløbet."
        elif x.resident_id != offer.tilbudt_af_id or x.status != VagtTildeling.Status.TILDELT:
            lapsed, lapse_offer = "Tilbuddet er bortfaldet: du står ikke længere på vagten.", True
        elif VagtAnmeldelse.objects.filter(vagt_tildeling=x).exists():
            lapsed, lapse_offer = "Tilbuddet er bortfaldet: vagten har en anmeldelse i sin historik.", True
        elif y.resident_id != fresh.foreslaaet_af_id or y.status != VagtTildeling.Status.TILDELT:
            lapsed = "Forslaget er bortfaldet: forslagsstilleren står ikke længere på sin vagt."
        elif has_started(vagt_y):
            lapsed = "Forslaget er bortfaldet: forslagsstillerens vagt er allerede startet."
        elif VagtAnmeldelse.objects.filter(vagt_tildeling=y).exists():
            lapsed = "Forslaget er bortfaldet: forslagsstillerens vagt har en anmeldelse i sin historik."
        elif VagtTildeling.objects.filter(vagt=vagt_x, resident=proposer).exists():
            lapsed = f"Forslaget er bortfaldet: {proposer.full_name} har allerede en plads på {vagt_x}."
        elif VagtTildeling.objects.filter(vagt=vagt_y, resident=by).exists():
            lapsed = f"Forslaget er bortfaldet: du har allerede en plads på {vagt_y}."
        elif not may_hold(proposer, vagt_x) or not may_hold(by, vagt_y):
            lapsed = "Forslaget er bortfaldet: en af jer står ikke på beboerlisten eller er fraflyttet."
        if lapsed is None:
            offerer = offer.tilbudt_af
            x.resident, y.resident = proposer, offerer
            try:
                with transaction.atomic():  # ONE savepoint: both updates or neither
                    x.save(update_fields=["resident"])
                    y.save(update_fields=["resident"])
            except IntegrityError:
                # A row inserted concurrently by a path that does not lock VagtTildeling rows
                # (override_assign): the savepoint rolled the swap back. The proposal can never succeed now.
                lapsed = "En af jer har fået en plads på den anden vagt imens."
            else:
                now = current_datetime()
                fresh.status = VagtBytteForslag.Status.ACCEPTERET
                fresh.closed_at = now
                fresh.save(update_fields=["status", "closed_at"])
                offer.status = VagtBytte.Status.BYTTET
                offer.overtaget_af = proposer
                offer.closed_at = now
                offer.save(update_fields=["status", "overtaget_af", "closed_at"])
                _close_invalidated_offers([x, y], keep=offer)  # Y's own offer, and ITS proposals
                _close_forslag(VagtBytteForslag.objects.filter(bytte=offer))  # the offer's other proposals
                _close_invalidated_forslag([x, y], keep=fresh)
                _archive_hurtig_posts(
                    [offer.pk]
                )  # lock 5: the post, LAST (Y's offer's went in its own close)
                _notify(
                    proposer,
                    f"{by.full_name} har accepteret byttet: du har nu {vagt_x} i stedet for {vagt_y}.",
                )
                return fresh
        # Stale: persist the lapse, raise after the block.
        fresh.status = VagtBytteForslag.Status.BORTFALDET
        fresh.closed_at = current_datetime()
        fresh.save(update_fields=["status", "closed_at"])
        if lapse_offer:
            offer.status = VagtBytte.Status.BORTFALDET
            offer.closed_at = fresh.closed_at
            offer.save(update_fields=["status", "closed_at"])
            _close_forslag(VagtBytteForslag.objects.filter(bytte=offer))
            _archive_hurtig_posts([offer.pk])  # lock 5: the post, LAST
    raise KoekkenAllocationError(lapsed or gone)


def open_offers(
    *,
    exclude_resident: Resident | None = None,
    within_days: int | None = None,
    at: datetime | None = None,
    regel_lookup: dict[tuple[str, bool], VagtRegel] | None = None,
) -> list[VagtBytte]:
    """Open, unexpired offers, soonest first (date, then chronological shift kind). Expired offers stay
    `AABEN` in the database, so the not-yet-started filter is applied here in Python and EVERY read of
    offers must go through this or `has_started`. Offers whose row is no longer the offerer's own
    `TILDELT` row, or whose row has flag history in any status, are not listed either. `within_days` serves Køkkengruppen's list (14)."""
    now = at or current_datetime()
    today = timezone.localtime(now).date()
    qs = VagtBytte.objects.filter(
        status=VagtBytte.Status.AABEN,
        tildeling__status=VagtTildeling.Status.TILDELT,
        tildeling__resident=F("tilbudt_af"),
        tildeling__vagt__date__gte=today,
    ).exclude(Exists(VagtAnmeldelse.objects.filter(vagt_tildeling=OuterRef("tildeling_id"))))
    if within_days is not None:
        qs = qs.filter(tildeling__vagt__date__lte=today + timedelta(days=within_days))
    if exclude_resident is not None:
        qs = qs.exclude(tilbudt_af=exclude_resident)
    offers = list(qs.select_related("tildeling__vagt", "tilbudt_af"))
    if regel_lookup is None:
        regel_lookup = vagt_regel_lookup()
    kind_order = [kind.value for kind in VagtRegel.Kind]
    live = [o for o in offers if not has_started(o.tildeling.vagt, at=now, regel_lookup=regel_lookup)]
    live.sort(key=lambda o: (o.tildeling.vagt.date, kind_order.index(o.tildeling.vagt.kind), o.pk))
    return live


def force_rerun_impact(year: int, month: int) -> tuple[int, int]:
    """`(kept, lapsing)` for a `--force` re-run of (year, month), to be computed BEFORE it runs:
    `kept` = the month's `TILDELT` rows that are completed hand-offs (they survive), `lapsing` = open,
    not-yet-started offers on the month's `TILDELT` rows that are NOT hand-offs (they cascade away with
    their row)."""
    month_rows = VagtTildeling.objects.filter(
        vagt__date__year=year, vagt__date__month=month, status=VagtTildeling.Status.TILDELT
    )
    kept = month_rows.filter(handed_off_tildeling_filter()).count()
    candidates = list(
        VagtBytte.objects.filter(
            tildeling__in=month_rows.exclude(handed_off_tildeling_filter()), status=VagtBytte.Status.AABEN
        ).select_related("tildeling__vagt")
    )
    regel_lookup = vagt_regel_lookup()
    now = current_datetime()
    lapsing = sum(
        1 for o in candidates if not has_started(o.tildeling.vagt, at=now, regel_lookup=regel_lookup)
    )
    return kept, lapsing


def is_subscribed(resident: Resident) -> bool:
    """Whether any of `resident`'s devices wants Køkkenvagter push notifications -- the initial state
    of the resident index page's subscribe toggle (F4), mirroring `reparationer.services.
    is_subscribed`/`opslagstavle.services.is_subscribed`."""
    from core.push import subscribers

    return subscribers(TOPIC).filter(user=resident).exists()


def open_anmeldelser() -> QuerySet[VagtAnmeldelse]:
    """Køkkengruppen's flag queue (P2 design doc §7): every open flag, reason and flagger included,
    newest first."""
    return VagtAnmeldelse.objects.filter(status=VagtAnmeldelse.Status.AABEN).select_related(
        "vagt_tildeling__vagt", "vagt_tildeling__resident", "flagged_by"
    )


def unreported_tildelinger(*, before: date | None = None) -> QuerySet[VagtTildeling]:
    """Køkkengruppen's "ikke rapporteret" list (P2 design doc §7): past `TILDELT` shifts nobody
    marked done -- the silent-sweep half of §6, distinct from the active flag queue above. "Past"
    means the shift's own date is strictly before `before` (default today via `core.clock`) -- a
    shift dated today may still be inside its own marking window."""
    cutoff = before or current_date()
    return (
        VagtTildeling.objects.filter(status=VagtTildeling.Status.TILDELT, vagt__date__lt=cutoff)
        .select_related("vagt", "resident")
        .order_by("vagt__date")
    )


def todays_tildelinger(*, today: date | None = None) -> QuerySet[VagtTildeling]:
    """Every assignment for today's shifts, PLUS yesterday's shifts still inside their own marking
    window -- the kitchen tablet's whole page (P2 design doc §7). Every status is included for TODAY,
    not only `TILDELT`: a shift already marked done or already flagged still belongs on the tablet
    (its mark-done button just isn't rendered, per §10), so the tablet stays an honest picture of the
    day rather than one that empties out as people tap in.

    **F1: yesterday's shifts are included too, restricted to still-`TILDELT` ones.** The marking
    window (§5) opens at a shift's own start time and stays open through the END of the FOLLOWING
    day -- so filtering to `vagt__date=today` alone made half that window unreachable at the tablet: a
    shift from yesterday still inside its window had no way to ever be marked done, because it simply
    never appeared. A still-`TILDELT` row dated yesterday is, by construction, ALWAYS inside its own
    window for the whole of today (`marking_window`'s `closes_at` for a `vagt.date` of yesterday is
    the very start of tomorrow), so no further window check is needed here -- `can_mark_done` still
    decides per row whether to render the button, exactly as it already does for today's rows. A
    shift from yesterday that is no longer `TILDELT` (self-reported or flagged yesterday) has nothing
    left to do today and is deliberately left out, so the tablet does not reopen settled history.
    """
    day = today or current_date()
    yesterday = day - timedelta(days=1)
    return (
        VagtTildeling.objects.filter(
            Q(vagt__date=day) | Q(vagt__date=yesterday, status=VagtTildeling.Status.TILDELT)
        )
        .select_related("vagt", "resident")
        .order_by("vagt__date", "vagt__kind", "resident__first_name")
    )


def resident_tildelinger(resident: Resident) -> QuerySet[VagtTildeling]:
    """One resident's whole shift history, newest first -- the resident "my shifts" page (P2 design
    doc §7)."""
    return VagtTildeling.objects.filter(resident=resident).select_related("vagt").order_by("-vagt__date")


def _calendar_year_for_month(periode: Periode, month: int) -> int:
    """Which calendar year `month` (1..12) falls in within `periode` — needed because EFTERAAR spans
    a year boundary (Sep..Jan), so `periode.year` alone is not always the right year for `month`."""
    current = periode.start_date
    while current <= periode.end_date:
        if current.month == month:
            return current.year
        current = date(current.year + (1 if current.month == 12 else 0), current.month % 12 + 1, 1)
    raise KoekkenAllocationError(f"Måned {month} ligger ikke i {periode}.")


def post_obligation(periode: Periode, month: int, *, officer: Resident | None = None) -> tuple[int, int]:
    """Reconcile FORPLIGTELSE entries for one calendar month against that month's actual supply.

    Obligation = that month's total `Vagt` supply (headcount x duration, in minutes) split across
    every present resident (a `Residency` row for that month, excluding anyone whose `move_out_date`
    has already passed — see the design doc's "No ledger entry is ever written for a resident after
    their move_out_date"). The split uses the same largest-remainder method as `seed_demo`'s
    ølkælder purchase shares, so the posted total is EXACTLY the supply total — the "self-balancing
    by construction" property the design doc calls out — never approximately, and never via float
    division.

    Idempotent via `update_or_create` against `uniq_koekken_forpligtelse_per_period_month`, exactly
    `ak.services.apply_monthly_charge`'s pattern. Returns (written, present_count).
    """
    year = _calendar_year_for_month(periode, month)
    total_minutes = (
        Vagt.objects.filter(periode=periode, date__year=year, date__month=month).aggregate(
            total=Sum(F("headcount") * F("duration_minutes"))
        )["total"]
        or 0
    )

    today = current_date()
    present = list(
        Resident.objects.filter(residencies__year=year, residencies__month=month)
        .exclude(move_out_date__isnull=False, move_out_date__lt=today)
        .distinct()
        .order_by("pk")
    )
    present_ids = {r.pk for r in present}

    # Reconcile stale FORPLIGTELSE rows before reposting — exactly `ak.services.apply_monthly_charge`'s
    # first step (`existing.exclude(resident_id__in=member_ids).delete()`). FORPLIGTELSE already
    # follows that function's MONTHLY pattern, not the ledger's general append-only-correction rule:
    # it is idempotent via `update_or_create` keyed on (resident, periode, month), i.e. already
    # mutable in place for whoever stays `present`. Without this step, a resident who drops out of
    # `present` between runs (e.g. a backdated move_out_date) keeps their old, now-wrong charge while
    # the shrunk `present` set gets recharged the FULL total, breaking the "total obligation == supply"
    # invariant this function exists to guarantee. Deleting (rather than posting a compensating
    # JUSTERING/TILBAGEFOERSEL) mirrors the ak precedent exactly and keeps that invariant checkable by
    # a straight SUM, with no dangling FORPLIGTELSE row for someone no longer charged.
    KoekkenPost.objects.filter(periode=periode, month=month, kind=KoekkenPost.Kind.FORPLIGTELSE).exclude(
        resident_id__in=present_ids
    ).delete()

    if not present:
        return (0, 0)

    base, remainder = divmod(total_minutes, len(present))
    written = 0
    for index, resident in enumerate(present):
        amount = base + (1 if index < remainder else 0)
        KoekkenPost.objects.update_or_create(
            resident=resident,
            periode=periode,
            month=month,
            kind=KoekkenPost.Kind.FORPLIGTELSE,
            defaults={"delta_minutes": -amount, "vagt": None, "created_by": officer},
        )
        written += 1
    return (written, len(present))


def _fridag_notification_message(for_date: date, reason: str) -> str:
    """The ONE resident-facing message for `declare_fridag`, sent to each resident whose shift was
    deleted. It names no specific shift and points the resident at the app to see what they currently
    hold. It does name the affected MONTH (from `for_date`, never "denne måned"): a fridag is normally
    declared months ahead, so the affected month is routinely not the current one."""
    reason_text = reason or "fridag"
    month_text = f"{MONTHS[for_date.month]} {for_date.year}"
    return (
        f"En eller flere af dine køkkenvagter i {month_text} er bortfaldet som følge af en fridag "
        f"({reason_text}) -- se dine aktuelle vagter i app'en."
    )


def declare_fridag(for_date: date, kinds: Iterable[str], reason: str = "") -> FridagResult:
    """Declare `for_date` a fridag for each of `kinds` -- Amendment 5, A5.4 as simplified by the
    2026-10-04 supplement (design doc, start of "Amendment 5"). **This is the normal path, not an edge
    case**: a periode is allocated ~122 days before it starts, so a fridag declared in the months
    leading up to it is almost always against a month whose `Vagt` rows (and likely assignments)
    already exist, which `generate_vagter`'s seam (A5.3) cannot retroactively undo. One officer action,
    ONE atomic write, in every periode alike:

      1. Create the `Fridag` row(s) -- idempotent; a pair already declared is left as-is.
      2. Capture the residents holding a `TILDELT` assignment on the affected `Vagt` rows
         (`FridagResult.removed`), then delete those `Vagt` rows. `VagtTildeling` CASCADEs with its
         `Vagt` (A5.6); `KoekkenPost.vagt` SET_NULLs (A5.6) -- the ledger is left intact.
      3. Re-post the month's obligation (`post_obligation`) -- **only when `(year, month)` already has
         any `FORPLIGTELSE`-kind `KoekkenPost` row**, i.e. obligation was posted for it at some point
         and now needs reconciling against the reduced supply. This gate is independent of who held
         the deleted shifts: a fully settled month, or one where nobody held the deleted shift, still
         re-posts, while a month with assignments but no posted obligation does not. Safe because
         `post_obligation` derives the month's total purely from actual `Vagt` rows and reconciles via
         delete-stale-then-`update_or_create`; it never reads assignment state.
      4. Notify exactly the residents whose assignment was deleted, one notification per resident.

    **The month is never re-allocated** (supplement, point 1): a late fridag removes only that day's
    shifts, so nobody else's assignments move. Compensation needs no code -- losing a `TILDELT`
    assignment lowers the resident's projected balance, so the next allocation gives them more.

    **Two guards, both checked BEFORE any write** (A5.5), so a refusal leaves the database completely
    unchanged:

    * Refuses when any existing assignment for `for_date`/`kinds` already has status `UDFOERT` or
      `ANMELDT` -- you cannot retroactively un-hold a shift somebody worked or is disputing.
    * Refuses a `for_date` in the past, read via `core.clock.current_date()` (never
      `django.utils.timezone` directly).

    The notifications land in `FridagResult.notifications` -- see that dataclass's docstring for the
    shape and for why dispatch is NOT done here (a `--dry-run` rolls back only after this function
    returns). The audience is narrowed through `koekken.access.allowed_subscribers` exactly as
    `resolve_anmeldelse` does, so a resident is never sent a link that then 403s them.

    Declaring a fridag for a kind that has no `Vagt` row yet (a genuine look-ahead month) still writes
    its `Fridag` row -- there is simply nothing to delete, re-post or notify for it.
    """
    today = current_date()
    if for_date < today:
        raise KoekkenAllocationError(
            f"{for_date} ligger i fortiden -- der kan ikke erklæres fridag for en dato der er passeret."
        )

    kind_list = list(kinds)
    affected_vagter = list(Vagt.objects.filter(date=for_date, kind__in=kind_list))
    has_settled_assignment = VagtTildeling.objects.filter(
        vagt__in=affected_vagter,
        status__in=[VagtTildeling.Status.UDFOERT, VagtTildeling.Status.ANMELDT],
    ).exists()
    if has_settled_assignment:
        raise KoekkenAllocationError(
            f"{for_date} kan ikke erklæres fridag -- der findes allerede en udført eller anmeldt "
            "vagt på denne dato, som ikke kan omgøres."
        )

    year, month = for_date.year, for_date.month
    result = FridagResult()
    with transaction.atomic():
        # Lock the affected rows first (LOCK ORDER above `has_started`): deleting the `Vagt` cascades into
        # its rows and their offers, which a concurrent take-over locks in the opposite direction.
        fridag_locked = _lock_tildelinger(
            VagtTildeling.objects.filter(vagt__in=affected_vagter).order_by("pk").values_list("pk", flat=True)
        )
        lock_cascade_dependents(fridag_locked)  # offers and proposals the delete cascades into, ascending
        # Resolved INSIDE the atomic block, after both guards above -- `resolve_periode` is a
        # `get_or_create`, and a pure look-ahead call (no `Vagt` rows, no existing `Periode`) must not
        # leave a stray `Periode` behind just by being refused.
        periode = resolve_periode(for_date)
        for kind in kind_list:
            fridag, created = Fridag.objects.get_or_create(
                date=for_date, kind=kind, defaults={"reason": reason}
            )
            if created:
                result.created.append(fridag)

        # Checked BEFORE any Vagt row is touched; independent of who held the deleted shifts.
        has_existing_obligation = KoekkenPost.objects.filter(
            periode=periode, month=month, kind=KoekkenPost.Kind.FORPLIGTELSE
        ).exists()

        # Captured BEFORE the delete (the rows cascade away with their Vagt).
        removed_rows = list(
            VagtTildeling.objects.filter(vagt__in=affected_vagter, status=VagtTildeling.Status.TILDELT)
            .select_related("resident", "vagt")
            .order_by("vagt__date", "resident__first_name", "resident__last_name", "resident_id")
        )
        # Chronological within a day (morgen, frokost, aften) = `VagtRegel.Kind` declaration order; the
        # stored values sort alphabetically the wrong way round. Stable, so the name order is kept.
        kind_order = [kind.value for kind in VagtRegel.Kind]
        removed_rows.sort(key=lambda row: (row.vagt.date, kind_order.index(row.vagt.kind)))
        result.removed = [(row.resident, row.vagt) for row in removed_rows]

        result.deleted_vagter = len(affected_vagter)
        for vagt in affected_vagter:
            vagt.delete()

        holders_by_pk = {resident.pk: resident for resident, _vagt in result.removed}
        if holders_by_pk:
            from core.push import subscribers

            from . import access  # local -- see resolve_anmeldelse's own reasoning

            message = _fridag_notification_message(for_date, reason)
            for rid in sorted(holders_by_pk):
                audience = access.allowed_subscribers(subscribers(TOPIC).filter(user_id=rid))
                result.notifications.append((holders_by_pk[rid], audience, message))

        if affected_vagter and has_existing_obligation:
            post_obligation(periode, month)
            result.obligation_reposted_months.append((year, month))

    return result


def rebase_to_zero_mean(entries: list[tuple[Resident, int]]) -> dict[int, int]:
    """Subtract the mean from a set of (resident, minutes) balances so they sum to EXACTLY zero.

    Used by the `seed_koekken_balances` launch command and by the demo fixture. Same
    largest-remainder trick as `post_obligation`: plain float subtraction would leave a few minutes
    of drift (the house mean must be exactly zero, not approximately), so `total // n` is given to
    everyone and the `total % n` leftover is peeled off one extra minute at a time. Returns
    resident_id -> rebased minutes; ordering (by resident pk) is deterministic, so re-seeding the
    same input is reproducible.

    Relative standing between residents is preserved EXACTLY only when `total` divides evenly by
    `n` — the design doc's launch-migration claim assumes this. When it doesn't, the `total % n`
    remainder is necessarily spread across only *some* residents (one extra minute each, by pk
    order), so a gap between two residents can move by at most 1 minute versus its pre-rebase value.
    """
    if not entries:
        return {}
    ordered = sorted(entries, key=lambda pair: pair[0].pk)
    n = len(ordered)
    total = sum(minutes for _, minutes in ordered)
    base, remainder = divmod(total, n)
    return {
        resident.pk: minutes - (base + (1 if index < remainder else 0))
        for index, (resident, minutes) in enumerate(ordered)
    }


def balance_for(resident: Resident) -> int:
    """The resident's current balance, in minutes. `SUM(delta_minutes)` — see `ak.AkEntry.balance_for`
    for the identical precedent."""
    return KoekkenPost.objects.filter(resident=resident).aggregate(b=Sum("delta_minutes"))["b"] or 0


def bulk_balances(residents: Iterable[Resident]) -> dict[int, int]:
    """`balance_for` for many residents in one query. Missing entries (no ledger rows yet) are 0."""
    ids = [r.pk for r in residents]
    rows = (
        KoekkenPost.objects.filter(resident_id__in=ids).values("resident_id").annotate(b=Sum("delta_minutes"))
    )
    balances = {row["resident_id"]: row["b"] or 0 for row in rows}
    return {rid: balances.get(rid, 0) for rid in ids}


def projected_balance_for(resident: Resident) -> int:
    """`resident`'s projected balance, in minutes — Amendment 1, A1.2: ledger balance plus the
    duration of every `TILDELT` (assigned, not yet credited — credit posts on `UDFOERT`) row they
    currently hold. This is the ranking balance `allocate_tier_a` uses for its look-ahead window;
    `post_obligation` deliberately keeps using the plain, unprojected balance concept (see that
    function and the design doc's A1.4) — projection only ever feeds *ranking*, never the ledger
    itself, and never the obligation charge."""
    ledger = balance_for(resident)
    tildelt_minutes = (
        VagtTildeling.objects.filter(resident=resident, status=VagtTildeling.Status.TILDELT).aggregate(
            total=Sum("vagt__duration_minutes")
        )["total"]
        or 0
    )
    return ledger + tildelt_minutes


def bulk_projected_balances(residents: Iterable[Resident]) -> dict[int, int]:
    """`projected_balance_for` for many residents in one pair of queries, mirroring `bulk_balances`."""
    residents = list(residents)
    ids = [r.pk for r in residents]
    balances = bulk_balances(residents)
    rows = (
        VagtTildeling.objects.filter(resident_id__in=ids, status=VagtTildeling.Status.TILDELT)
        .values("resident_id")
        .annotate(total=Sum("vagt__duration_minutes"))
    )
    tildelt_by_id = {row["resident_id"]: row["total"] or 0 for row in rows}
    return {rid: balances[rid] + tildelt_by_id.get(rid, 0) for rid in ids}


def house_mean() -> float:
    """The mean balance across every resident who has at least one ledger row. Not scoped to
    current residents: someone who left with an unsettled balance still counts, because the ledger's
    whole point is that their balance is real until it is collected (see the design doc's
    move-out-penalty discussion, including the accepted drift risk from leavers)."""
    resident_ids = list(KoekkenPost.objects.values_list("resident_id", flat=True).distinct())
    if not resident_ids:
        return 0.0
    balances = bulk_balances(Resident.objects.filter(pk__in=resident_ids))
    return sum(balances.values()) / len(balances)


# ---------------------------------------------------------------------------------------------------
# P3 step 2: away ranges (`Fravaer`) -- docs/plans/2026-10-04-koekkenvagter-p3-design.md §2/§3/§7.
# Informational only: nothing above this line reads any of it. Ranges are inclusive; adjacent ranges
# (1-10 Jul, 11-20 Jul) are not an overlap and are never merged.
# ---------------------------------------------------------------------------------------------------


def summer_bounds(year: int) -> tuple[date, date]:
    """(1 July, 31 August) of `year` -- the SOMMER periode's bounds, pure, via `_periode_bounds`."""
    _kind, _year, start, end = _periode_bounds(date(year, 7, 1))
    return start, end


def target_summer(today: date | None = None) -> Periode:
    """The summer the away-range page is about: this year's SOMMER while `today` is on or before 31 August,
    otherwise next year's. An UNSAVED `Periode` (`_periode_from_bounds`) -- never `resolve_periode`, so a
    GET of the summer page cannot write a row (the same pure/write pairing as `preference_target_periode`)."""
    today = today or current_date()
    year = today.year if today <= summer_bounds(today.year)[1] else today.year + 1
    return _periode_from_bounds(summer_bounds(year)[0])


def summer_link_visible(today: date | None = None) -> bool:
    """Whether the index page links to the summer page: from the target summer's deadline (1 May) through
    its last day (31 August). The page itself is always reachable by URL."""
    today = today or current_date()
    target = target_summer(today)
    return periode_deadline(target) <= today <= target.end_date


def _fmt_dato_interval(start: date, end: date) -> str:
    """ "11.-20. juli" within one month, else "28. juni-3. juli" (en-dash; mirrors _fravaer.html's weeks)."""
    if start.month == end.month and start.year == end.year:
        return f"{start.day}.\u2013{end.day}. {MONTHS[end.month]}"
    return f"{start.day}. {MONTHS[start.month]}\u2013{end.day}. {MONTHS[end.month]}"


def add_fravaer(
    resident: Resident, start_date: date, end_date: date, *, today: date | None = None
) -> Fravaer:
    """Register an away range for `resident`. Refuses (`KoekkenAllocationError`, Danish message, nothing
    written) a reversed range, one that has already ended, one not intersecting the target summer (`target_summer(today)` -- the one the page displays), and
    one overlapping the resident's OWN existing ranges (`a.start <= b.end and b.start <= a.end`; another
    resident's range is irrelevant). An ongoing range is accepted. No row locking: the only race is the
    same resident double-submitting, whose worst case is a cosmetic duplicate row."""
    today = today or current_date()
    with transaction.atomic():
        if start_date > end_date:
            raise KoekkenAllocationError("Fra-datoen skal ligge før eller på til-datoen.")
        if end_date < today:
            raise KoekkenAllocationError("Fraværet er allerede slut.")
        summer = target_summer(today)
        if start_date > summer.end_date or end_date < summer.start_date:
            raise KoekkenAllocationError(
                f"Fravær kan kun registreres for sommerperioden {summer.start_date.year} (1. juli-31. august)."
            )
        clash = (
            Fravaer.objects.filter(resident=resident, start_date__lte=end_date, end_date__gte=start_date)
            .order_by("start_date")
            .first()
        )
        if clash is not None:
            raise KoekkenAllocationError(
                f"Overlapper dit fravær {_fmt_dato_interval(clash.start_date, clash.end_date)}."
            )
        return Fravaer.objects.create(resident=resident, start_date=start_date, end_date=end_date)


def delete_fravaer(fravaer: Fravaer, by: Resident, *, today: date | None = None) -> None:
    """Delete one away range. Only its owner may (defense in depth -- the view 403s first), and only while
    it has not ended."""
    today = today or current_date()
    if fravaer.resident_id != by.pk:
        raise KoekkenAllocationError("Du kan kun slette dit eget fravær.")
    if fravaer.end_date < today:
        raise KoekkenAllocationError("Afsluttet fravær kan ikke slettes.")
    fravaer.delete()


def resident_fravaer(resident: Resident, *, today: date | None = None) -> list[tuple[Fravaer, bool]]:
    """`resident`'s own ranges touching the target summer, by start date, each with a `can_delete` flag
    (not yet ended)."""
    today = today or current_date()
    summer = target_summer(today)
    rows = Fravaer.objects.filter(
        resident=resident, start_date__lte=summer.end_date, end_date__gte=summer.start_date
    ).order_by("start_date", "pk")
    return [(f, f.end_date >= today) for f in rows]


def away_by_week(summer: Periode) -> list[tuple[date, date, list[Resident]]]:
    """One `(first_day, last_day, residents)` row per ISO week (Monday-Sunday) from the week holding
    `summer`'s first day to the week holding its last. The first/last weeks are partial: days are clipped
    to the summer, and the clipped days are both the overlap test and the label. Residents are
    de-duplicated and sorted by name; one whose `move_out_date` precedes the week's first summer day is
    left out. One query, bucketed in Python."""
    ranges = list(
        Fravaer.objects.filter(start_date__lte=summer.end_date, end_date__gte=summer.start_date)
        .select_related("resident")
        .order_by("start_date", "pk")
    )
    rows: list[tuple[date, date, list[Resident]]] = []
    monday = summer.start_date - timedelta(days=summer.start_date.weekday())
    while monday <= summer.end_date:
        first = max(monday, summer.start_date)
        last = min(monday + timedelta(days=6), summer.end_date)
        present: dict[int, Resident] = {}
        for f in ranges:
            r = f.resident
            if f.start_date <= last and first <= f.end_date:
                if r.move_out_date is not None and r.move_out_date < first:
                    continue
                present[r.pk] = r
        rows.append((first, last, sorted(present.values(), key=lambda r: r.full_name)))
        monday += timedelta(days=7)
    return rows
