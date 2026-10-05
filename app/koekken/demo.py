"""Demo køkkenvagter for `manage.py seed_demo`.

Lives here rather than inside seed_demo so the Danish copy sits with the feature (the same split
opslagstavle.demo/events.demo made). Builds every P1 edge state a developer would otherwise have to
construct by hand, plus (Amendment 1) the FCFS tiebreak, a two-month allocated look-ahead window
and a locked preference, plus (Amendments 2 and 3, F3) a genuinely PROJECTED month reconciled against
a real list arriving later -- a departure vacated and refilled, a no-preference arrival seated
correctly, and a vacated weekday slot an ineligible arrival does not inherit:

  * a **normal allocated month** (the current one), with tier-A allocation and obligation posted;
  * a couple of residents with **weekday_unavailable** set, so they show up routed into the weekend
    pool in that same month's allocation, rather than needing a second contrived scenario;
  * a **February-shaped shortfall month**: tier-A capacity is deliberately shrunk (see
    `_force_shortfall` below) so some residents end up with no tier-A slot at all — the soft floor
    from the design doc's finding 3, otherwise invisible until a real February;
  * a **seeded launch balance**, some residents ending up positive and some negative after the
    zero-mean rebase, exercising `koekken.services.rebase_to_zero_mean` the same way
    `seed_koekken_balances` does;
  * (**Amendment 1, A1.3**) a **locked preference**: editing an already-declared preference for the
    period a resident is currently living in always redirects to the next period's row instead;
  * (**Amendment 1, A1.2**) a **two-month allocated look-ahead window**, alongside the current
    month, so the effect of the batch + monthly roll-forward mechanism is visible without waiting a
    quarter for cron to build it up (kept to one extra month rather than the batch's real three, so a
    5-month semester periode still has room left for the FCFS-tiebreak and reconciliation scenarios
    below);
  * (**Amendment 1, A1.1**) a **tie broken by `declared_at`**: two residents with an identical
    balance and a single contested weekend seat, seated in declaration order rather than by an
    arbitrary `pk`;
  * (**Amendment 2, A2.6**) a **genuinely projected month**: no `Residency` row exists for it at
    allocation time, so `allocate_tier_a` must build its population from `_resolve_population`'s A2.2
    projection, not a real list — visibly distinct from the look-ahead window above, whose months get
    a real `Residency` row up front;
  * (**Amendment 2/3, A2.6/A3.4**) **reconciliation once the real list arrives**: one projected
    resident has since left — their assignment is vacated and the slot refilled — and one genuine new
    arrival with no `Praeference` row anywhere is seated into it, correctly defaulting to
    weekday-available (A3.2);
  * (**Amendment 3, A3.1/A3.4**) a **vacated weekday slot an ineligible arrival does not inherit**:
    the only real-list candidate for it declared `weekday_unavailable=True`, so the slot stays open
    and queued rather than being forced on them.

Uses residents' EXISTING `Residency` rows (written earlier in `seed_demo.handle` by
`_seed_residencies`) rather than taking a `rooms` argument, to keep the same `seed(residents, now,
rng)` signature every other domain's demo.py uses.
"""

import random
from collections.abc import Iterator
from datetime import date, datetime, timedelta

from core.clock import current_date
from residents.models import Residency, Resident

from .models import KoekkenPost, Periode, Praeference, Vagt, VagtBytteForslag, VagtRegel, VagtTildeling
from .services import (
    KoekkenAllocationError,
    accept_trade,
    allocate_tier_a,
    generate_vagter,
    held_by_vagt,
    month_population_ids,
    offer_tildeling,
    periode_is_allocated,
    post_obligation,
    propose_trade,
    rebase_to_zero_mean,
    reconcile_month,
    resolve_periode,
    set_preference,
    take_over,
    take_over_whole,
    withdraw_offer,
)

# How many residents declare weekday_unavailable for the current periode. Small and fixed rather than
# a fraction of `residents`, so `_force_shortfall`'s shrunk weekend capacity (set to match this
# number exactly, see below) stays correct regardless of --residents.
UNAVAILABLE_COUNT = 2

# Tier-A capacity the shortfall month is shrunk to: UNAVAILABLE_COUNT weekend seats (exactly enough
# for the declarers above, so none of them land in `refused_weekend`) plus a couple of weekday
# seats. Anything beyond that is left with no slot -- the shortfall.
SHORTFALL_WEEKDAY_CAPACITY = 2

# Informal (hours) balances fed through the same rebase as seed_koekken_balances, spread around zero
# on purpose so the launch-seed demo shows both positive and negative outcomes.
LAUNCH_BALANCES_HOURS = [6.0, 3.0, 0.0, -2.0, -4.0, -1.0]

# Amendment 1 -- how many extra, otherwise-undeclared residents the FCFS-tiebreak demo needs.
TIEBREAK_COUNT = 2

# Amendment 1 -- how many extra months (beyond the current one) should form the demo's look-ahead
# window, mirroring the design doc's "batch allocates the period's first three months" (A1.2). Kept
# at 1 rather than 2: a 5-month semester periode (EFTERAAR/FORAAR) only has 5 months total, and the
# current month + shortfall month + this window + the reconciliation month + the FCFS-tiebreak month
# need to fit in it without any of them starving another out of ever running (see `_demo_reconciliation`
# and `_demo_fcfs_tiebreak` below).
WINDOW_EXTRA_MONTHS = 1


def _shrink_capacity(vagter: list[Vagt], target: int) -> None:
    """Force `vagter`'s combined headcount down to `target`, keeping the earliest slots. Demo-only:
    real capacity comes from VagtRegel + the calendar; this exists purely to make a shortfall
    reproducible regardless of how many residents --residents created."""
    remaining = target
    for vagt in vagter:
        new_headcount = min(vagt.headcount, remaining) if remaining > 0 else 0
        remaining -= new_headcount
        if new_headcount != vagt.headcount:
            vagt.headcount = new_headcount
            vagt.save(update_fields=["headcount"])


def _iter_months(periode: Periode) -> Iterator[tuple[int, int]]:
    """Every calendar (year, month) inside `periode`, in order."""
    year, month = periode.start_date.year, periode.start_date.month
    end = (periode.end_date.year, periode.end_date.month)
    while (year, month) <= end:
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


def _other_month_in_periode(periode: Periode, today_year: int, today_month: int) -> tuple[int, int]:
    """A calendar (year, month) inside `periode` other than the current one, for the shortfall demo
    to use without colliding with the normal month's allocation. Falls back to the current month if
    the period is too short to offer another (never happens for EFTERAAR/FORAAR in practice)."""
    for year, month in _iter_months(periode):
        if (year, month) != (today_year, today_month):
            return year, month
    return today_year, today_month


def _next_unused_months(periode: Periode, used: set[tuple[int, int]], count: int) -> list[tuple[int, int]]:
    """Up to `count` distinct (year, month) pairs inside `periode`, in order, not already in `used`.
    Amendment 1's demo scenarios (the look-ahead window, the FCFS tiebreak) each claim their own
    month(s) this way so they never collide with the pre-existing "current month" / shortfall-month
    scenarios above -- and simply return fewer than `count` (even zero) for a short periode (e.g.
    SOMMER's two months) rather than raising, since these are optional, best-effort demo dressing."""
    found: list[tuple[int, int]] = []
    for year, month in _iter_months(periode):
        if (year, month) not in used:
            found.append((year, month))
        if len(found) == count:
            break
    return found


def _ensure_residency(residents: list[Resident], year: int, month: int) -> None:
    """Give each of `residents` a Residency row for (year, month), copying their latest known room --
    demo-only convenience so a scenario can place residents in a month without a real room lottery."""
    for resident in residents:
        latest = resident.residencies.order_by("-year", "-month").first()
        if latest is None:
            continue
        Residency.objects.get_or_create(
            resident=resident, year=year, month=month, defaults={"room": latest.room}
        )


def _force_shortfall(residents: list[Resident], periode: Periode, year: int, month: int) -> None:
    """Give every resident a Residency row for (year, month), then shrink that month's tier-A
    capacity well below the population so allocate_tier_a leaves some of them unassigned -- the
    February-shaped shortfall the design doc's finding 3 describes."""
    _ensure_residency(residents, year, month)

    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    month_vagter = list(
        Vagt.objects.filter(periode=periode, date__year=year, date__month=month, kind__in=tier_a_kinds)
    )
    weekend_vagter = [v for v in month_vagter if v.date.weekday() >= 5]
    weekday_vagter = [v for v in month_vagter if v.date.weekday() < 5]
    _shrink_capacity(weekend_vagter, UNAVAILABLE_COUNT)
    _shrink_capacity(weekday_vagter, SHORTFALL_WEEKDAY_CAPACITY)

    allocate_tier_a(year, month)
    post_obligation(periode, month)


def _next_lookahead_month(periode: Periode, used: set[tuple[int, int]]) -> tuple[int, int] | None:
    """One calendar month strictly after every month already claimed by an earlier demo scenario,
    still inside `periode`. Amendment 2's projection demo needs a month with NO `Residency` row yet
    (see `_demo_projected_departure` below), which only works if there is an earlier month `used`
    already has a full-population `Residency` list for `_resolve_population` to project from --
    unlike `_next_unused_months` (which can return an early gap in `periode` before any such list
    exists), this always looks strictly forward from the latest claimed month. `None` (best-effort,
    like `_next_unused_months`) if the periode is too short to offer one."""
    if not used:
        return None
    year, month = max(used)
    candidate = date(year + (1 if month == 12 else 0), month % 12 + 1, 1)
    if candidate > periode.end_date:
        return None
    return candidate.year, candidate.month


def _demo_reconciliation(
    residents: list[Resident], periode: Periode, year: int, month: int, rng: random.Random
) -> None:
    """Amendment 2 (A2.6) + Amendment 3 (A3.4), all in one month so it costs only a single spare
    look-ahead slot (a 5-month semester periode has exactly two left after the current month,
    shortfall month and 1-month window above -- this takes one of them, the FCFS tiebreak below
    takes the other).

    Shrinks (year, month)'s tier-A capacity to exactly match `residents` (mirroring
    `_force_shortfall`'s shrink pattern) so a plain allocation leaves nobody unassigned and nothing
    unfilled, then allocates it WITHOUT giving anyone a `Residency` row for it first -- unlike the
    look-ahead window above, so `allocate_tier_a` has no choice but to build its population from
    `_resolve_population`'s A2.2 projection (this is what makes the month "projected"). Two of that
    month's weekday holders then "leave": their real list is published without them, vacating both
    weekday slots, alongside two genuine new arrivals who were never in the projection at all -- one
    with no `Praeference` row anywhere, one who declared `weekday_unavailable=True`. Reconciling then
    demonstrates all of A3.4's cases at once: the no-preference arrival is correctly seated (A3.2)
    into one of the two vacated slots, while the declared-unavailable arrival is refused (an excess
    weekend declarer, capacity already exactly matched the pre-existing declarers) and does NOT
    inherit the other one (A3.1) -- which stays open and queued in `still_unfilled` instead, since
    nobody else is left to fill it.

    Best-effort, like the rest of this module's optional scenarios: no-ops if fewer than two
    residents land on a weekday slot to vacate, or if `year`/`month` predates every resident's known
    room (so there is nothing to hand the arrivals)."""
    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    month_vagter = list(
        Vagt.objects.filter(periode=periode, date__year=year, date__month=month, kind__in=tier_a_kinds)
    )
    weekend_vagter = [v for v in month_vagter if v.date.weekday() >= 5]
    weekday_vagter = [v for v in month_vagter if v.date.weekday() < 5]
    # Dynamic, not UNAVAILABLE_COUNT: by the time this runs, the tiebreak scenario below may not have
    # run yet, but a locked-preference redirect above may have changed who's currently declared for
    # this periode -- counting live keeps the shrink exact regardless of call order.
    declared_count = Praeference.objects.filter(
        periode=periode, resident__in=residents, weekday_unavailable=True
    ).count()
    _shrink_capacity(weekend_vagter, declared_count)
    _shrink_capacity(weekday_vagter, max(len(residents) - declared_count, 0))

    projected = allocate_tier_a(year, month)  # no Residency row exists yet -- projects (A2.2)
    if len(projected.weekday_assigned) < 2:
        return

    departing_a, departing_b = rng.sample(projected.weekday_assigned, k=2)
    stayed = [r for r in residents if r.pk not in {departing_a.pk, departing_b.pk}]
    _ensure_residency(stayed, year, month)

    latest = departing_a.residencies.order_by("-year", "-month").first()
    if latest is None:
        return
    room = latest.room

    available_arrival, created = Resident.objects.get_or_create(
        email="koekken.demo.ankomst@gahk.dk",
        defaults={"first_name": "Ny", "last_name": "Tilflytter"},
    )
    if created:
        available_arrival.set_password("demo1234")
        available_arrival.save()
    Residency.objects.get_or_create(
        resident=available_arrival, year=year, month=month, defaults={"room": room}
    )

    unavailable_arrival, created = Resident.objects.get_or_create(
        email="koekken.demo.ankomst.hverdage.utilgaengelig@gahk.dk",
        defaults={"first_name": "Ny", "last_name": "Utilgaengelig"},
    )
    if created:
        unavailable_arrival.set_password("demo1234")
        unavailable_arrival.save()
    Residency.objects.get_or_create(
        resident=unavailable_arrival, year=year, month=month, defaults={"room": room}
    )
    Praeference.objects.update_or_create(
        resident=unavailable_arrival, periode=periode, defaults={"weekday_unavailable": True}
    )

    reconcile_month(year, month)


def _demo_fcfs_tiebreak(residents: list[Resident], periode: Periode, year: int, month: int) -> None:
    """Amendment 1, A1.1: two residents with an identical (zero) balance and no other preference this
    periode, weekend capacity shrunk to exactly one seat -- whoever declared weekday_unavailable
    EARLIER wins it, rather than the arbitrary (and stably arbitrary) pk-order tie A1.1 replaces.
    No-ops if fewer than TIEBREAK_COUNT undeclared residents are available."""
    pool = [r for r in residents if not Praeference.objects.filter(resident=r, periode=periode).exists()]
    if len(pool) < TIEBREAK_COUNT:
        return
    early, late = pool[:TIEBREAK_COUNT]
    _ensure_residency([early, late], year, month)

    declared_base = periode.start_date
    Praeference.objects.create(
        resident=early, periode=periode, weekday_unavailable=True, declared_at=declared_base
    )
    Praeference.objects.create(
        resident=late,
        periode=periode,
        weekday_unavailable=True,
        declared_at=declared_base + timedelta(days=1),
    )

    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    month_vagter = list(
        Vagt.objects.filter(periode=periode, date__year=year, date__month=month, kind__in=tier_a_kinds)
    )
    weekend_vagter = [v for v in month_vagter if v.date.weekday() >= 5]
    _shrink_capacity(weekend_vagter, 1)  # exactly one seat for the tied pair -- one wins, one loses

    allocate_tier_a(year, month)


def _demo_handoffs(
    residents: list[Resident], periode: Periode, used_months: set[tuple[int, int]], rng: random.Random
) -> None:
    """Amendment 4: an open offer (step 2: with one pending trade proposal on it), a completed take-over,
    a completed trade between two other residents (offer, propose, accept) and a whole-shift take-over, all
    through the services (never raw writes) and all on FUTURE shifts of one already-allocated month --
    an offer on a started shift is refused. Best-effort like every optional scenario here: skips when no
    month has enough future shifts. The aftenvagt scenario hand-creates its two `TILDELT` rows, because
    the demo only runs tier A, so aftenvagt are generated but unallocated. Push is a no-op when VAPID is
    unconfigured, exactly as for `resolve_anmeldelse`."""
    today = current_date()
    tier_a_kinds = [VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    for year, month in sorted(used_months):
        rows = list(
            VagtTildeling.objects.filter(
                vagt__periode=periode,
                vagt__date__year=year,
                vagt__date__month=month,
                vagt__date__gt=today,
                vagt__kind__in=tier_a_kinds,
                status=VagtTildeling.Status.TILDELT,
            )
            .select_related("vagt", "resident")
            .order_by("vagt__date", "vagt__kind")
        )
        if len(rows) < 2:
            continue
        population = month_population_ids(year, month)
        in_population = [r for r in residents if r.pk in population]
        if len(in_population) < 3:
            continue
        try:
            open_offer = offer_tildeling(rows[0], rows[0].resident)  # left open
            handed = rows[1]
            held = {r.pk: held_by_vagt(r, [handed.vagt_id]) for r in in_population}
            takers = [
                r for r in in_population if r.pk != handed.resident_id and handed.vagt_id not in held[r.pk]
            ]
            if takers:
                take_over(offer_tildeling(handed, handed.resident), rng.choice(takers))

            # Step 2: one pending proposal on the open offer, and one completed trade between two OTHER
            # residents. Candidates are re-read (the take-over above may have moved a row); each attempt is
            # best-effort, because the services refuse any pairing the rules forbid.
            spare = list(
                VagtTildeling.objects.filter(
                    pk__in=[r.pk for r in rows[2:]], status=VagtTildeling.Status.TILDELT
                ).select_related("vagt", "resident")
            )
            proposer_id: int | None = None
            for y in spare:
                try:
                    propose_trade(open_offer, y, y.resident)
                except KoekkenAllocationError:
                    continue
                proposer_id = y.resident_id
                spare = [r for r in spare if r.pk != y.pk and r.resident_id != proposer_id]
                break
            traded = False
            for first in spare:
                if traded:
                    break
                if first.resident_id == rows[0].resident_id:
                    continue
                try:
                    offer = offer_tildeling(first, first.resident)
                except KoekkenAllocationError:
                    continue
                for second in spare:
                    if second.resident_id in (first.resident_id, rows[0].resident_id):
                        continue
                    try:
                        propose_trade(offer, second, second.resident)
                        accept_trade(offer.forslag.get(status=VagtBytteForslag.Status.AABEN), first.resident)
                    except KoekkenAllocationError:
                        continue
                    traded = True
                    break
                if not traded:
                    withdraw_offer(offer, first.resident)  # no partner found: not left dangling

            aften = list(
                Vagt.objects.filter(
                    periode=periode,
                    date__year=year,
                    date__month=month,
                    date__gt=today,
                    kind=VagtRegel.Kind.AFTEN,
                    headcount=2,
                    tildelinger__isnull=True,
                ).order_by("date")
            )
            weekday_aften = [v for v in aften if v.date.weekday() < 5]
            if weekday_aften:
                vagt = weekday_aften[0]
                offerer, partner = rng.sample(in_population, k=2)
                offered = VagtTildeling.objects.create(
                    vagt=vagt, resident=offerer, status=VagtTildeling.Status.TILDELT
                )
                VagtTildeling.objects.create(vagt=vagt, resident=partner, status=VagtTildeling.Status.TILDELT)
                take_over_whole(offer_tildeling(offered, offerer), partner)
        except KoekkenAllocationError:
            continue
        return


def _seed_launch_balances(residents: list[Resident], periode: Periode) -> None:
    pool = residents[: len(LAUNCH_BALANCES_HOURS)]
    entries = [
        (resident, round(hours * 60)) for resident, hours in zip(pool, LAUNCH_BALANCES_HOURS, strict=False)
    ]
    rebased = rebase_to_zero_mean(entries)
    for resident, _ in entries:
        KoekkenPost.objects.update_or_create(
            resident=resident,
            kind=KoekkenPost.Kind.STARTSALDO,
            defaults={"delta_minutes": rebased[resident.pk], "periode": periode, "vagt": None},
        )


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    if len(residents) < UNAVAILABLE_COUNT + SHORTFALL_WEEKDAY_CAPACITY + 1:
        return 0  # too small a demo house to show a real shortfall; nothing useful to build

    today = now.date()
    periode = resolve_periode(today)
    generate_vagter(periode)
    if not periode_is_allocated(periode.kind):
        # Summer is never allocated (P3 design doc §4): its shifts are generated for residents to claim
        # themselves, so none of the allocation scenarios below can run. Generation only, plus the
        # starting balances so a July/August demo run still shows them.
        _seed_launch_balances(residents, periode)
        return KoekkenPost.objects.count()

    # A few weekday-unavailable declarers for the WHOLE periode (Praeference is periode-scoped, not
    # month-scoped -- see koekken.models), so they show up routed to the weekend pool both in the
    # normal current month below AND in the shortfall month.
    declarers = rng.sample(residents, k=UNAVAILABLE_COUNT)
    for resident in declarers:
        Praeference.objects.update_or_create(
            resident=resident, periode=periode, defaults={"weekday_unavailable": True}
        )

    # A normal allocated month: the current one.
    allocate_tier_a(today.year, today.month)
    post_obligation(periode, today.month)
    used_months = {(today.year, today.month)}

    # A February-shaped shortfall, in a different month of the same periode.
    shortfall_year, shortfall_month = _other_month_in_periode(periode, today.year, today.month)
    _force_shortfall(residents, periode, shortfall_year, shortfall_month)
    used_months.add((shortfall_year, shortfall_month))

    # Amendment 1, A1.3: preferences lock at the period's deadline. `today` is always inside the
    # CURRENT periode, and a periode's own deadline (two calendar months before ITS start) is
    # therefore always already in the past by the time anyone is standing inside it -- so a second
    # declaration for one of the residents above always demonstrates the redirect to the next
    # periode's row, never an in-place edit of the current one (see
    # koekken.services.set_preference's docstring for why).
    if declarers:
        set_preference(declarers[0], False, at=today)

    # Amendment 1, A1.2: a two-month allocated look-ahead window (the current month above, plus one
    # more -- WINDOW_EXTRA_MONTHS, kept below the batch's real three so the reconciliation and
    # FCFS-tiebreak scenarios below still have a month each), so the effect of the batch +
    # monthly-roll-forward mechanism is visible without waiting on cron. Best-effort: the periode may
    # not have enough spare months left.
    window_months = _next_unused_months(periode, used_months, WINDOW_EXTRA_MONTHS)
    for year, month in window_months:
        _ensure_residency(residents, year, month)
        allocate_tier_a(year, month)
    used_months.update(window_months)

    # Amendment 2 (A2.6) + Amendment 3 (A3.4): a genuinely PROJECTED month (no Residency row at
    # allocation time, unlike the look-ahead window above), reconciled once a real list arrives -- a
    # departure vacated and a genuine no-preference arrival correctly seated into it, alongside a
    # second vacated slot whose only real-list candidate is weekday-unavailable and correctly stays
    # queued rather than inheriting it (see `_demo_reconciliation`). Picks the next month strictly
    # after everything used so far (see `_next_lookahead_month`), so `_resolve_population` always has
    # an earlier full-population list to project from. Best-effort: the periode may not have a spare
    # month left.
    recon_month = _next_lookahead_month(periode, used_months)
    if recon_month is not None:
        used_months.add(recon_month)
        _demo_reconciliation(residents, periode, *recon_month, rng)

    # Amendment 1, A1.1: a tie broken by declared_at, isolated to its own month so it never
    # interacts with the scenarios above. Also best-effort.
    tiebreak_months = _next_unused_months(periode, used_months, 1)
    if tiebreak_months:
        _demo_fcfs_tiebreak(residents, periode, *tiebreak_months[0])

    # Amendment 4, step 1: hand-offs on future shifts of an already-allocated month (best-effort).
    _demo_handoffs(residents, periode, used_months, rng)

    # Deliberately LAST: tier-A allocation orders by projected balance, so seeding STARTSALDO any
    # earlier would hand the FCFS-tiebreak pair (usually residents[0] and [1], who carry launch
    # balances) unequal balances and let the lower one win regardless of declared_at.
    _seed_launch_balances(residents, periode)
    return KoekkenPost.objects.count()
