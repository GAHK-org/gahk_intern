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
"""

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db import transaction
from django.db.models import F, Q, Sum

from core.clock import current_date
from residents.models import Residency, Resident

from .models import KoekkenPost, Periode, Praeference, Vagt, VagtRegel, VagtTildeling

logger = logging.getLogger(__name__)


class KoekkenAllocationError(Exception):
    """Base for allocation failures that must be surfaced, never swallowed."""


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
class ReconciliationResult:
    """What one `reconcile_month` run did -- Amendment 2 (A2.3), corrected by Amendment 3 (A3.1).

    `vacated` are residents who held a `TILDELT` row that got deleted because they are no longer on
    the real `Residency` list for this month. `seated` is the outcome of re-seating the resulting
    unfilled capacity via the shared `_seat_tier_a` core, restricted to residents who did not already
    hold a slot this month -- its `weekend_assigned`/`weekday_assigned` are who newly got a slot,
    `unassigned` is who was eligible and available but still missed out (never forced, never handed a
    slot they're ineligible for). `still_unfilled` is the A3.1 queue: `Vagt` rows that remain short of
    headcount after reconciliation, because no eligible unseated candidate existed for them -- these
    are what should surface to Køkkengruppen rather than being silently left short.
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


def periode_deadline(periode: Periode) -> date:
    """`periode`'s preference deadline — Amendment 1, A1.2: exactly two calendar months before its
    start (Feb-Jun's is 1 December, Sep-Jan's is 1 July, Jul-Aug's is 1 May). Derived, not stored:
    every `Periode.start_date` is the 1st of a month, so this is exact date arithmetic, not an
    approximation."""
    month = periode.start_date.month - 2
    year = periode.start_date.year
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, periode.start_date.day)


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
        return []

    month_start = date(year, month, 1)
    return list(
        Resident.objects.filter(residencies__year=latest["year"], residencies__month=latest["month"])
        .exclude(move_out_date__isnull=False, move_out_date__lt=month_start)
        .distinct()
        .order_by("pk")
    )


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


def allocate_tier_a(year: int, month: int, *, force: bool = False) -> TierAResult:
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
        raise KoekkenAllocationError(
            f"Ingen beboere kunne findes for {year}-{month:02d} -- hverken en direkte alumneliste "
            "eller en tidligere offentliggjort liste at projicere ud fra (Amendment 2, A2.2)."
        )

    declared_at_by_id = dict(
        Praeference.objects.filter(
            periode=periode, resident_id__in=[r.pk for r in population_all]
        ).values_list("resident_id", "declared_at")
    )

    with transaction.atomic():
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

        return _seat_tier_a(
            weekend_vagter,
            weekday_vagter,
            population,
            periode,
            balances,
            declared_at_by_id,
            surviving_by_vagt,
            log_label=f"{year}-{month:02d}",
        )


def reconcile_month(year: int, month: int) -> ReconciliationResult:
    """Correct one calendar month's tier-A assignments against the now-real `Residency` list --
    Amendment 2 (A2.3), corrected by Amendment 3 (A3.1). Additive only, and the counterpart to
    `allocate_tier_a`'s `force=True`: that is a deliberate Køkkengruppen re-shuffle that may move
    anyone, this never touches an assignment for a resident present on both the projection that was
    used and the real list now.

    A no-op (`ReconciliationResult()`) when this month has no `Vagt` rows yet, or has never been
    allocated (no `TILDELT`/other `VagtTildeling` rows at all) -- there is nothing to reconcile
    against, and per the same discipline as `roll_forward_allocation` this must log and return rather
    than raise, since it may run in a scheduled task before a month has reached that point.

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
                    return allocate_tier_a(year, month)
                except KoekkenAllocationError as exc:
                    logger.warning(
                        "koekken.roll_forward_allocation: kunne ikke allokere %s-%02d (%s) -- logger "
                        "og springer over i stedet for at fejle (Amendment 2, A2.2).",
                        year,
                        month,
                        exc,
                    )
                    return None
        month_cursor = date(year + (1 if month == 12 else 0), month % 12 + 1, 1)
    return None


def set_preference(resident: Resident, weekday_unavailable: bool, *, at: date | None = None) -> Praeference:
    """Write `resident`'s weekday-unavailable preference, resolving which `Periode`'s row the write
    actually targets — Amendment 1, A1.3's preference locking.

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
    current_periode = resolve_periode(today)

    existing_current = Praeference.objects.filter(resident=resident, periode=current_periode).first()
    if existing_current is None:
        return Praeference.objects.create(
            resident=resident,
            periode=current_periode,
            weekday_unavailable=weekday_unavailable,
            declared_at=today,
        )

    target = _next_periode(current_periode)
    if today >= periode_deadline(target):
        target = _next_periode(target)

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
