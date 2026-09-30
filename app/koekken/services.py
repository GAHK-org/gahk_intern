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
locking rule in A1.3. `post_obligation` and everything below "Ledger and obligation" in the design
doc are explicitly untouched.

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

from django.db import transaction
from django.db.models import Count, F, Q, QuerySet, Sum
from django.utils import timezone

from core.clock import current_date, current_datetime
from residents.models import Residency, Resident

from .models import (
    KoekkenPost,
    Periode,
    Praeference,
    PraeferenceDag,
    Vagt,
    VagtAnmeldelse,
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


def resolve_periode(for_date: date) -> Periode:
    """The `Periode` containing `for_date`, creating it (calendar-anchored bounds) if it doesn't
    exist yet. Idempotent: re-resolving the same date always returns the same row."""
    kind, year, start, end = _periode_bounds(for_date)
    periode, _ = Periode.objects.get_or_create(
        kind=kind, year=year, defaults={"start_date": start, "end_date": end}
    )
    return periode


def _next_periode(periode: Periode) -> Periode:
    """The `Periode` immediately following `periode`. Periods are calendar-anchored and contiguous
    (EFTERAAR's Jan 31 is followed by FORAAR's Feb 1, FORAAR's Jun 30 by SOMMER's Jul 1, SOMMER's Aug
    31 by the next EFTERAAR's Sep 1) — see the design doc's data model — so "the day after this one
    ends" always resolves to the right next periode, created via the same idempotent `resolve_periode`
    the rest of this module uses. Amendment 1 (A1.3): the target of a locked-preference redirect."""
    return resolve_periode(periode.end_date + timedelta(days=1))


def _previous_periode(periode: Periode) -> Periode:
    """The `Periode` immediately preceding `periode` — the mirror of `_next_periode`. Amendment 1
    (A1.3, missed deadline): the source a resident's effective preference falls back to when they
    have no row yet for `periode`."""
    return resolve_periode(periode.start_date - timedelta(days=1))


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


def generate_vagter(periode: Periode) -> list[Vagt]:
    """Create the `Vagt` rows for every day in `periode`, one per applicable `VagtRegel`.

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
    value is never treated as if they had declared it themselves this periode."""
    rows = Praeference.objects.filter(periode=periode, resident_id__in=resident_ids).values(
        "resident_id", "weekday_unavailable"
    )
    current = {row["resident_id"]: row["weekday_unavailable"] for row in rows}
    missing = [rid for rid in resident_ids if rid not in current]
    fallback: dict[int, bool] = {}
    if missing:
        previous = _previous_periode(periode)
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
    `TILDELT` (self-reported or flagged — P2) are never touched or overwritten — but they DO still
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
            VagtTildeling.objects.filter(vagt__in=vagter, status=VagtTildeling.Status.TILDELT).delete()

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
    unless `force=True`, and any row already moved past `TILDELT` (self-reported/flagged) survives a
    re-run untouched and still occupies its vagt's headcount.

    `_skip_clear` is private, used only by `allocate_month` -- see `allocate_tier_a`'s docstring for
    what it does and why; the same note applies here verbatim.
    """
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
            VagtTildeling.objects.filter(vagt__in=vagter, status=VagtTildeling.Status.TILDELT).delete()

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
    """
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
        VagtTildeling.objects.filter(vagt__in=month_vagter, status=VagtTildeling.Status.TILDELT).delete()
        tier_a = allocate_tier_a(year, month, force=True, _skip_clear=True)
        tier_b = allocate_tier_b(year, month, force=True, _skip_clear=True)
    return tier_a, tier_b


def reconcile_month(year: int, month: int) -> ReconciliationResult:
    """Correct one calendar month's tier-A assignments against the now-real `Residency` list --
    Amendment 2 (A2.3), corrected by Amendment 3 (A3.1). Additive only, and the counterpart to
    `allocate_tier_a`'s `force=True`: that is a deliberate Køkkengruppen re-shuffle that may move
    anyone, this never touches an assignment for a resident present on both the projection that was
    used and the real list now.

    A no-op (`ReconciliationResult()`) when this month has no `Vagt` rows yet, has never been
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
        vacated: list[Resident] = []
        for row in existing:
            if row.status == VagtTildeling.Status.TILDELT and row.resident_id not in real_ids:
                vacated.append(row.resident)
                row.delete()

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

    A no-op (returns `None`) once every month in the periode that has `Vagt` rows is already
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
    """The "further still" half of Amendment 1's A1.3 locking rule, shared by `_preference_write_target`
    and `preference_target_periode`: the periode right after `current_periode`, pushed one periode
    further again if `today` has already reached (or passed) THAT periode's own deadline -- "a later
    edit is written to the following period's row instead, taking effect then." Split out purely so
    the two callers above can't drift on this half of the rule while differing on the other half (see
    `preference_target_periode`'s docstring for why they differ at all)."""
    target = _next_periode(current_periode)
    if today >= periode_deadline(target):
        target = _next_periode(target)
    return target


def _preference_write_target(resident: Resident, today: date) -> Periode:
    """Which `Periode`'s row a preference write from `resident` on `today` actually targets --
    Amendment 1, A1.3's locking rule, exactly as `set_preference` applies it. Pulled out of
    `set_preference` so the resolution itself has exactly one implementation and `set_preference` is
    just "resolve, then write" -- see that function's docstring for the rule in full.
    """
    current_periode = resolve_periode(today)
    if not Praeference.objects.filter(resident=resident, periode=current_periode).exists():
        return current_periode
    return _redirect_past_deadline(current_periode, today)


def set_preference(resident: Resident, weekday_unavailable: bool, *, at: date | None = None) -> Praeference:
    """Write `resident`'s weekday-unavailable preference, resolving which `Periode`'s row the write
    actually targets — Amendment 1, A1.3's preference locking (`_preference_write_target`).

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
    Checks the periode `at` falls in AND the next one -- the window that matters to a resident living
    in periode N is almost always periode N+1's (its deadline is still ahead; periode N's own deadline
    is already in the past the moment anyone is living inside it), but checking both keeps this
    correct right at a boundary without hardcoding which one it must be.

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

    `praeferencer`'s view/template still reads the result directly for display -- `str(periode)`
    ("Efterår 2026") -- never for its `pk` or a relation. `core.context_processors.navigation` (the
    banner) now uses this function only as the cheap "is a window open at all" gate -- once it returns
    non-`None`, the banner's own displayed periode and its per-resident check both come from
    `preference_target_periode`/`resident_has_declared_for` instead (see those functions' docstrings
    on why: this function's result is pure date arithmetic, not aware of any one resident's actual
    `Praeference` history). Either way, an unsaved instance is exactly as useful as a persisted one
    here and costs nothing.
    """
    today = at or current_date()
    _kind, _year, current_start, current_end = _periode_bounds(today)
    _next_kind, _next_year, next_start, _next_end = _periode_bounds(current_end + timedelta(days=1))
    for start in (current_start, next_start):
        deadline = _deadline_from_start(start)
        if deadline - timedelta(days=7) <= today <= deadline:
            kind, year, p_start, p_end = _periode_bounds(start)
            return Periode(kind=kind, year=year, start_date=p_start, end_date=p_end)
    return None


def resident_needs_to_declare(resident: Resident, *, at: date | None = None) -> bool:
    """Whether `resident` should see the "declare your kitchen preferences" dashboard todo card --
    P2 design doc §8. True for a resident with NO `Praeference` row at all for their current periode
    -- the exact `declared_at IS NULL` condition Amendment 3 (A3.2) already established as "we are
    guessing, not reading a declaration", reused here rather than a new check invented for the UI."""
    periode = resolve_periode(at or current_date())
    return not Praeference.objects.filter(resident=resident, periode=periode).exists()


def preference_target_periode(
    resident: Resident, window_periode: Periode, *, at: date | None = None
) -> Periode:
    """Which `Periode`'s row a preference write from `resident` would actually land on right now --
    the read-only question the preference-window banner (P2 design doc §8) must ask, in place of
    trusting `window_periode` (`in_preference_window()`'s result) itself. That result is pure date
    arithmetic -- it says nothing about `resident`'s own `Praeference` history -- while the real write
    path (`set_preference`, `_preference_write_target`) resolves its target from exactly that history
    (Amendment 1, A1.3): a first-time declarer's submission lands on their CURRENT periode, not the
    upcoming one the window is named after, and a resident who already has a row for their current
    periode gets redirected to the next periode, or the one after that once its own deadline has
    passed. Calling the banner's "has this resident already handled it" check against `window_periode`
    instead of THIS function's result is exactly the bug this exists to fix: it let the banner nag a
    first-time declarer forever, since their write never touched the periode the old check asked about.

    **Deliberately not a byte-for-byte replay of `_preference_write_target`** -- it shares
    `_redirect_past_deadline` for the "already has a row for the current periode, so redirect forward"
    half of the rule, but the "does resident already have a row for their current periode" half only
    counts a row declared BEFORE `window_periode`'s own window opened (`preference_window`), not one
    written by a submission made DURING this same window. Without that distinction, a genuine
    first-time declarer's bootstrap row (created by their own submission, landing on their current
    periode per A1.3's exemption) would flip THIS function's own branch on the very next call -- their
    new row now exists, so it would start asking about the periode AFTER that instead -- sending the
    banner chasing a moving target rather than clearing once they've done what §12's decision asks
    ("persists ... until a resident personally declares"). `set_preference` itself is completely
    unaffected by this -- it has no notion of a "window" and keeps resolving off plain row existence
    exactly as before; this refinement exists only in this read-only mirror, and only changes anything
    during the handful of days a window is actually open.

    Like `resident_has_declared_for`, this genuinely needs database reads (it is per-resident) and
    must only be called once `in_preference_window()` has already found a window open -- see that
    function's own docstring on why that discipline matters.
    """
    today = at or current_date()
    opens, _closes = preference_window(window_periode)
    current_periode = resolve_periode(today)
    has_prior_row = Praeference.objects.filter(
        resident=resident, periode=current_periode, declared_at__lt=opens
    ).exists()
    if not has_prior_row:
        return current_periode
    return _redirect_past_deadline(current_periode, today)


def resident_has_declared_for(resident: Resident, periode: Periode) -> bool:
    """Whether `resident` already has a `Praeference` row for `periode` -- the per-resident half of
    the preference-window banner (P2 design doc §8; §12's open item resolved: the banner persists
    for each resident individually until they've declared for the window's periode OR the window's
    time runs out, rather than showing for the window's whole duration regardless of whether that
    resident has already acted).

    NOT the same question as `resident_needs_to_declare` -- that one is scoped to the periode the
    resident is CURRENTLY living in (their own dashboard todo card, A3.2's zero-history case); this
    one is scoped to whatever `periode` the caller passes, which for the banner is specifically
    `preference_target_periode()`'s result -- the periode a submission from this resident would
    actually target right now, NOT simply `in_preference_window()`'s purely date-arithmetic display
    periode (see `preference_target_periode`'s docstring for why those two routinely diverge).

    Matched by `(periode.kind, periode.year)` rather than `periode=periode` on purpose:
    `in_preference_window()` returns an UNSAVED, in-memory `Periode` (see its docstring on why), so
    filtering on the FK by object identity would compare against a `None` pk and match nothing --
    silently showing the banner to every resident forever. `Periode.Meta.constraints` guarantees
    (kind, year) is exactly as selective as the pk would have been. (`preference_target_periode`'s
    result is always a SAVED periode, but matching this way costs nothing extra and keeps this
    function correct for either kind of caller.)

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
