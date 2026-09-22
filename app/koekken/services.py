"""Køkkenvagter's decision layer — P1 (slots, tier-A allocation, ledger, obligation posting).

There is no view layer yet (see the design doc's "Phasing"), so this module, not a views.py, is the
primary interface: management commands are thin wrappers around the functions below, and so will the
eventual views be. Read `docs/plans/2026-09-21-koekkenvagter-design.md` — particularly "Allocation"
and "Ledger and obligation" — before changing any of this; the choices below (soft floor, raw-balance
ranking, largest-remainder obligation split) are explained there, not repeated here beyond a pointer.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db import transaction
from django.db.models import F, Sum

from core.clock import current_date
from residents.models import Resident

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


def allocate_tier_a(year: int, month: int) -> TierAResult:
    """Tier-A (morgen + frokost) allocation for one calendar month — design doc "Allocation", step 1.

    Population is every resident on that month's `Residency` list. Weekday-unavailable declarers
    (scoped to the `Periode` this month falls in — see `Praeference`) are seated into the weekend
    pool first, ranked by balance ascending (most behind first); declarers beyond weekend capacity
    are refused (design doc finding 2: "the excess are refused with a clear message") and land in
    `unassigned`/`refused_weekend` rather than aborting the run or being forced onto a weekday slot
    they said they can't do. Remaining weekend capacity is then drafted from the rest of the
    population (finding 2: the weekend pool is mandatory overflow, not opt-in). Weekday slots are
    filled last, balance ascending, from whoever is left. Anyone left over gets no tier-A slot this
    month — the soft floor; it is absorbed by the ledger, not an error (design doc finding 3: this is
    expected in roughly half the semester's months).

    Idempotent, including across membership/ranking changes between runs: any existing `TILDELT`
    assignment on this month's tier-A `Vagt` rows is cleared and recomputed from current balances
    before writing. Assignments already moved past `TILDELT` (self-reported or flagged — P2) are
    never touched or overwritten — but they DO still occupy their vagt's headcount and their holder
    is excluded from this run's candidate population, so a re-run cannot try to hand them a second
    row (a `UniqueViolation` on `(vagt, resident)`) or silently exceed a vagt's headcount by
    recomputing capacity as if those rows didn't exist. Both the write path and the reported
    capacity/`unassigned` figures below account for surviving rows identically.
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

    weekend_vagter = [v for v in vagter if v.date.weekday() >= 5]
    weekday_vagter = [v for v in vagter if v.date.weekday() < 5]

    population_all = list(
        Resident.objects.filter(residencies__year=year, residencies__month=month).distinct().order_by("pk")
    )
    if not population_all:
        raise KoekkenAllocationError(f"Ingen beboere på alumnelisten for {year}-{month:02d}.")

    balances = bulk_balances(population_all)

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

        weekend_capacity = sum(max(v.headcount - surviving_by_vagt.get(v.pk, 0), 0) for v in weekend_vagter)
        weekday_capacity = sum(max(v.headcount - surviving_by_vagt.get(v.pk, 0), 0) for v in weekday_vagter)

        declared_ids = set(
            Praeference.objects.filter(
                periode=periode, weekday_unavailable=True, resident__in=population
            ).values_list("resident_id", flat=True)
        )
        declarers = sorted((r for r in population if r.pk in declared_ids), key=lambda r: balances[r.pk])

        accepted_declarers = declarers[:weekend_capacity]
        refused_declarers = declarers[weekend_capacity:]
        refused_ids = {r.pk for r in refused_declarers}
        if refused_declarers:
            logger.warning(
                "%d beboer(e) meldte sig hverdage-utilgængelige ud over weekend-puljens kapacitet "
                "på %d for %s-%02d og fik ingen tier-A-vagt denne måned: %s",
                len(refused_declarers),
                weekend_capacity,
                year,
                month,
                ", ".join(r.full_name for r in refused_declarers),
            )

        weekend_assigned = list(accepted_declarers)
        remaining_pop = [r for r in population if r.pk not in declared_ids]
        drafted: list[Resident] = []
        needed = weekend_capacity - len(weekend_assigned)
        if needed > 0:
            remaining_by_balance = sorted(remaining_pop, key=lambda r: balances[r.pk])
            drafted = remaining_by_balance[:needed]
            weekend_assigned += drafted

        assigned_ids = {r.pk for r in weekend_assigned}
        # Refused declarers never enter the weekday pool: they said they can't do weekdays, and
        # forcing one on them would contradict the declaration rather than merely miss the floor.
        weekday_pool = sorted(
            (r for r in population if r.pk not in assigned_ids and r.pk not in refused_ids),
            key=lambda r: balances[r.pk],
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
