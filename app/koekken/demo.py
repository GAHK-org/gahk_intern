"""Demo køkkenvagter for `manage.py seed_demo`.

Lives here rather than inside seed_demo so the Danish copy sits with the feature (the same split
opslagstavle.demo/events.demo made). Builds every P1 edge state a developer would otherwise have to
construct by hand, plus (Amendment 1) the FCFS tiebreak, a three-month allocated look-ahead window
and a locked preference:

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
  * (**Amendment 1, A1.2**) a **three-month allocated look-ahead window**, alongside the current
    month, so the effect of the batch + monthly roll-forward mechanism is visible without waiting a
    quarter for cron to build it up;
  * (**Amendment 1, A1.1**) a **tie broken by `declared_at`**: two residents with an identical
    balance and a single contested weekend seat, seated in declaration order rather than by an
    arbitrary `pk`.

Uses residents' EXISTING `Residency` rows (written earlier in `seed_demo.handle` by
`_seed_residencies`) rather than taking a `rooms` argument, to keep the same `seed(residents, now,
rng)` signature every other domain's demo.py uses.
"""

import random
from collections.abc import Iterator
from datetime import datetime, timedelta

from residents.models import Residency, Resident

from .models import KoekkenPost, Periode, Praeference, Vagt, VagtRegel
from .services import (
    allocate_tier_a,
    generate_vagter,
    post_obligation,
    rebase_to_zero_mean,
    resolve_periode,
    set_preference,
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
# window, mirroring the design doc's "batch allocates the period's first three months" (A1.2).
WINDOW_EXTRA_MONTHS = 2


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

    # Amendment 1, A1.2: a three-month allocated look-ahead window (the current month above, plus up
    # to two more), so the effect of the batch + monthly-roll-forward mechanism is visible without
    # waiting on cron. Best-effort: a short periode (SOMMER) may not have enough spare months left.
    window_months = _next_unused_months(periode, used_months, WINDOW_EXTRA_MONTHS)
    for year, month in window_months:
        _ensure_residency(residents, year, month)
        allocate_tier_a(year, month)
    used_months.update(window_months)

    # Amendment 1, A1.1: a tie broken by declared_at, isolated to its own month so it never
    # interacts with the scenarios above. Also best-effort.
    tiebreak_months = _next_unused_months(periode, used_months, 1)
    if tiebreak_months:
        _demo_fcfs_tiebreak(residents, periode, *tiebreak_months[0])

    # Launch-seeded balances, some positive and some negative after rebasing.
    _seed_launch_balances(residents, periode)

    return KoekkenPost.objects.count()
