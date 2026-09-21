"""Demo køkkenvagter for `manage.py seed_demo`.

Lives here rather than inside seed_demo so the Danish copy sits with the feature (the same split
opslagstavle.demo/events.demo made). Builds every P1 edge state a developer would otherwise have to
construct by hand:

  * a **normal allocated month** (the current one), with tier-A allocation and obligation posted;
  * a couple of residents with **weekday_unavailable** set, so they show up routed into the weekend
    pool in that same month's allocation, rather than needing a second contrived scenario;
  * a **February-shaped shortfall month**: tier-A capacity is deliberately shrunk (see
    `_force_shortfall` below) so some residents end up with no tier-A slot at all — the soft floor
    from the design doc's finding 3, otherwise invisible until a real February;
  * a **seeded launch balance**, some residents ending up positive and some negative after the
    zero-mean rebase, exercising `koekken.services.rebase_to_zero_mean` the same way
    `seed_koekken_balances` does.

Uses residents' EXISTING `Residency` rows (written earlier in `seed_demo.handle` by
`_seed_residencies`) rather than taking a `rooms` argument, to keep the same `seed(residents, now,
rng)` signature every other domain's demo.py uses.
"""

import random
from datetime import datetime

from residents.models import Residency, Resident

from .models import KoekkenPost, Periode, Praeference, Vagt, VagtRegel
from .services import (
    allocate_tier_a,
    generate_vagter,
    post_obligation,
    rebase_to_zero_mean,
    resolve_periode,
)

# How many residents declare weekday_unavailable for the current periode. Small and fixed rather than
# a fraction of `residents`, so `_force_shortfall`'s shrunk weekend capacity (set to match this
# number exactly, see below) stays correct regardless of --residents.
UNAVAILABLE_COUNT = 2

# Tier-A capacity the shortfall month is shrunk to: UNAVAILABLE_COUNT weekend seats (exactly enough
# for the declarers above, so seating them never raises WeekendCapacityExceeded) plus a couple of
# weekday seats. Anything beyond that is left with no slot -- the shortfall.
SHORTFALL_WEEKDAY_CAPACITY = 2

# Informal (hours) balances fed through the same rebase as seed_koekken_balances, spread around zero
# on purpose so the launch-seed demo shows both positive and negative outcomes.
LAUNCH_BALANCES_HOURS = [6.0, 3.0, 0.0, -2.0, -4.0, -1.0]


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


def _other_month_in_periode(periode: Periode, today_year: int, today_month: int) -> tuple[int, int]:
    """A calendar (year, month) inside `periode` other than the current one, for the shortfall demo
    to use without colliding with the normal month's allocation. Falls back to the current month if
    the period is too short to offer another (never happens for EFTERAAR/FORAAR in practice)."""
    year, month = periode.start_date.year, periode.start_date.month
    while (year, month) <= (periode.end_date.year, periode.end_date.month):
        if (year, month) != (today_year, today_month):
            return year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return today_year, today_month


def _force_shortfall(residents: list[Resident], periode: Periode, year: int, month: int) -> None:
    """Give every resident a Residency row for (year, month), then shrink that month's tier-A
    capacity well below the population so allocate_tier_a leaves some of them unassigned -- the
    February-shaped shortfall the design doc's finding 3 describes."""
    for resident in residents:
        latest = resident.residencies.order_by("-year", "-month").first()
        if latest is None:
            continue
        Residency.objects.get_or_create(
            resident=resident, year=year, month=month, defaults={"room": latest.room}
        )

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
    for resident in rng.sample(residents, k=UNAVAILABLE_COUNT):
        Praeference.objects.update_or_create(
            resident=resident, periode=periode, defaults={"weekday_unavailable": True}
        )

    # A normal allocated month: the current one.
    allocate_tier_a(today.year, today.month)
    post_obligation(periode, today.month)

    # A February-shaped shortfall, in a different month of the same periode.
    shortfall_year, shortfall_month = _other_month_in_periode(periode, today.year, today.month)
    _force_shortfall(residents, periode, shortfall_year, shortfall_month)

    # Launch-seeded balances, some positive and some negative after rebasing.
    _seed_launch_balances(residents, periode)

    return KoekkenPost.objects.count()
