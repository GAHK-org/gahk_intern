"""Køkkenvagter — P1: slot generation, tier-A allocation, the ledger and obligation posting, launch
seeding. Full design: `docs/plans/2026-09-21-koekkenvagter-design.md`.

No views exist yet (see that doc's "Phasing"), so everything here goes through `koekken.services`
and the management commands directly, not through a Client.

Tier-A capacity in most of these tests is built by hand (a handful of `Vagt` rows on chosen dates)
rather than through `generate_vagter` against a real calendar month — a real month's weekend pool is
16-20 slots, too big to exercise "exceeds capacity" or "shortfall" with a test-sized population
without an unwieldy number of residents. `generate_vagter`/`VagtRegel` themselves are exercised by
the management-command dry-run tests, which use a real month.
"""

import calendar
import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import Room
from koekken.models import KoekkenPost, Periode, Praeference, Vagt, VagtRegel, VagtTildeling
from koekken.services import (
    KoekkenAllocationError,
    allocate_tier_a,
    balance_for,
    generate_vagter,
    house_mean,
    post_obligation,
    rebase_to_zero_mean,
    resolve_periode,
)
from residents.models import Residency, Resident

pytestmark = pytest.mark.django_db

_room_seq = iter(range(1, 10_000))


def _room() -> Room:
    n = next(_room_seq)
    return Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")


def _place(resident: Resident, year: int, month: int) -> Residency:
    return Residency.objects.create(resident=resident, room=_room(), year=year, month=month)


def _weekday_and_weekend_dates(year: int, month: int) -> tuple[list[date], list[date]]:
    _, days_in_month = calendar.monthrange(year, month)
    all_days = [date(year, month, d) for d in range(1, days_in_month + 1)]
    return [d for d in all_days if d.weekday() < 5], [d for d in all_days if d.weekday() >= 5]


def _build_month(year: int, month: int, *, weekday_capacity: int, weekend_capacity: int) -> Periode:
    """A Periode plus exactly `weekday_capacity` weekday and `weekend_capacity` weekend tier-A slots
    (one MORGEN Vagt per slot, headcount 1) for (year, month). FROKOST is deliberately left out —
    allocate_tier_a only cares about the combined MORGEN+FROKOST capacity, so one kind is enough to
    control it precisely."""
    periode = resolve_periode(date(year, month, 15))
    weekdays, weekends = _weekday_and_weekend_dates(year, month)
    assert len(weekdays) >= weekday_capacity
    assert len(weekends) >= weekend_capacity
    for d in weekdays[:weekday_capacity]:
        Vagt.objects.create(
            periode=periode, date=d, kind=VagtRegel.Kind.MORGEN, headcount=1, duration_minutes=60
        )
    for d in weekends[:weekend_capacity]:
        Vagt.objects.create(
            periode=periode, date=d, kind=VagtRegel.Kind.MORGEN, headcount=1, duration_minutes=60
        )
    return periode


def _adjust(resident: Resident, periode: Periode, minutes: int) -> None:
    """Set up a resident's balance for ranking tests, via an ordinary ledger entry."""
    KoekkenPost.objects.create(
        resident=resident, periode=periode, kind=KoekkenPost.Kind.JUSTERING, delta_minutes=minutes
    )


# ------------------------------------------------------------------------- tier-A: weekend declarers


def test_weekend_declarers_seated_up_to_capacity_excess_refused(make_resident: Callable) -> None:
    """FIX 3 (design doc finding 2): only the EXCESS declarers are refused -- the whole month's
    allocation must still complete, and everyone else (declarers who fit, plus non-declarers) must
    be allocated normally rather than the run aborting."""
    year, month = 2027, 5
    periode = _build_month(year, month, weekday_capacity=2, weekend_capacity=2)

    worst = make_resident(email="worst@gahk.dk")
    middle = make_resident(email="middle@gahk.dk")
    best = make_resident(email="best@gahk.dk")  # least behind -> refused when capacity is 2
    for r in (worst, middle, best):
        _place(r, year, month)
    _adjust(worst, periode, -300)
    _adjust(middle, periode, -100)
    _adjust(best, periode, 0)
    for r in (worst, middle, best):
        Praeference.objects.create(resident=r, periode=periode, weekday_unavailable=True)

    # Two ordinary (non-declaring) residents so the weekday pool has someone to allocate normally.
    other1 = make_resident(email="other1@gahk.dk")
    other2 = make_resident(email="other2@gahk.dk")
    for r in (other1, other2):
        _place(r, year, month)

    result = allocate_tier_a(year, month)  # must not raise

    assert result.weekend_assigned == [worst, middle]  # accepted, balance ascending
    assert result.refused_weekend == [best]  # highest balance among declarers is the excess
    assert best in result.unassigned  # same soft-floor bucket as anyone else who missed out
    assert best not in result.weekday_assigned  # never forced onto a slot they declared they can't do
    assert set(result.weekday_assigned) == {other1, other2}  # everyone else allocated normally

    assert VagtTildeling.objects.filter(resident=best).count() == 0
    for r in (worst, middle, other1, other2):
        assert VagtTildeling.objects.filter(resident=r, status=VagtTildeling.Status.TILDELT).count() == 1


def test_weekend_pool_drafts_beyond_declarers(make_resident: Callable) -> None:
    """The weekend pool is mandatory overflow (design doc finding 2): when declarers don't fill it,
    the allocator drafts more residents in rather than leaving weekend slots empty."""
    year, month = 2027, 6
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=3)

    declarer = make_resident(email="declarer@gahk.dk")
    _place(declarer, year, month)
    _adjust(declarer, periode, -1000)  # most behind -> always seated first
    Praeference.objects.create(resident=declarer, periode=periode, weekday_unavailable=True)

    others = [make_resident(email=f"other{i}@gahk.dk") for i in range(4)]
    for i, r in enumerate(others):
        _place(r, year, month)
        _adjust(r, periode, -i * 10)  # others[3] is most behind (balance -30) among non-declarers

    result = allocate_tier_a(year, month)

    assert declarer in result.weekend_assigned
    assert declarer not in result.drafted  # drafted excludes the declarer
    assert len(result.drafted) == 2  # 3 weekend slots - 1 declarer
    assert set(result.drafted) == {others[2], others[3]}  # two most-behind non-declarers
    assert VagtTildeling.objects.filter(resident=declarer, status=VagtTildeling.Status.TILDELT).count() == 1


# ------------------------------------------------------------------------------- tier-A: re-running


def test_reallocation_after_status_change_does_not_crash_or_double_book(make_resident: Callable) -> None:
    """FIX 1: once any VagtTildeling has moved past TILDELT (self-reported/flagged -- P2), re-running
    allocate_tier_a for that month must neither crash with a UniqueViolation on (vagt, resident) nor
    silently double-book a vagt, and must leave the surviving row completely untouched."""
    year, month = 2027, 8
    periode = _build_month(year, month, weekday_capacity=2, weekend_capacity=0)

    a = make_resident(email="rerun_a@gahk.dk")
    b = make_resident(email="rerun_b@gahk.dk")
    _place(a, year, month)
    _place(b, year, month)

    first = allocate_tier_a(year, month)
    assert set(first.weekday_assigned) == {a, b}

    udfoert = VagtTildeling.objects.filter(resident__in=[a, b]).order_by("pk").first()
    assert udfoert is not None
    udfoert.status = VagtTildeling.Status.UDFOERT
    udfoert.save(update_fields=["status"])
    udfoert_pk, udfoert_vagt_id, udfoert_resident_id = udfoert.pk, udfoert.vagt_id, udfoert.resident_id

    # Change the ranking so the resident who kept a surviving row would otherwise be re-picked.
    _adjust(a, periode, -500)
    _adjust(b, periode, -500)

    result = allocate_tier_a(year, month)  # must not raise

    for vagt in Vagt.objects.filter(periode=periode, date__year=year, date__month=month):
        assert vagt.tildelinger.count() <= vagt.headcount

    refreshed = VagtTildeling.objects.get(pk=udfoert_pk)
    assert refreshed.status == VagtTildeling.Status.UDFOERT
    assert refreshed.vagt_id == udfoert_vagt_id
    assert refreshed.resident_id == udfoert_resident_id

    # The resident who kept the surviving row must not be handed a second tier-A row this month.
    assert VagtTildeling.objects.filter(resident_id=udfoert_resident_id).count() == 1
    assert (
        VagtTildeling.objects.filter(
            vagt__periode=periode, vagt__date__year=year, vagt__date__month=month
        ).count()
        == 2
    )
    assert result.unassigned == []


# ------------------------------------------------------------------------------ tier-A: soft floor


def test_shortfall_month_completes_without_raising_and_leaves_residents_unassigned(
    make_resident: Callable,
) -> None:
    """A February-shaped month: population exceeds tier-A capacity. Must not raise (design doc
    finding 3, the soft floor), and posting obligation afterwards must not special-case or skip
    whoever missed out -- there is no spurious credit for a slot nobody worked, but no missing
    obligation charge either."""
    year, month = 2027, 2
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=1)

    residents = [make_resident(email=f"feb{i}@gahk.dk") for i in range(5)]
    for r in residents:
        _place(r, year, month)

    result = allocate_tier_a(year, month)  # must not raise

    assigned = set(result.weekend_assigned) | set(result.weekday_assigned)
    assert len(assigned) == 2  # exactly the 2 slots built above
    assert len(result.unassigned) == 3
    assert set(result.unassigned) | assigned == set(residents)

    written, present = post_obligation(periode, month)
    assert written == present == 5  # every present resident is charged, slot or no slot
    assert KoekkenPost.objects.filter(kind=KoekkenPost.Kind.ARBEJDE).count() == 0  # nothing to credit yet
    for r in residents:
        assert KoekkenPost.objects.filter(resident=r, kind=KoekkenPost.Kind.FORPLIGTELSE).exists()


def test_no_vagt_rows_raises_a_clear_error(make_resident: Callable) -> None:
    r = make_resident(email="novagt@gahk.dk")
    _place(r, 2028, 3)
    with pytest.raises(KoekkenAllocationError):
        allocate_tier_a(2028, 3)


# --------------------------------------------------------------------------------- obligation ledger


def test_obligation_posting_is_idempotent(make_resident: Callable) -> None:
    year, month = 2027, 7
    periode = _build_month(year, month, weekday_capacity=2, weekend_capacity=1)
    residents = [make_resident(email=f"obl{i}@gahk.dk") for i in range(3)]
    for r in residents:
        _place(r, year, month)

    written1, present1 = post_obligation(periode, month)
    total_after_first = sum(balance_for(r) for r in residents)

    written2, present2 = post_obligation(periode, month)
    total_after_second = sum(balance_for(r) for r in residents)

    assert (written1, present1) == (written2, present2) == (3, 3)
    assert KoekkenPost.objects.filter(kind=KoekkenPost.Kind.FORPLIGTELSE).count() == 3  # not 6
    assert total_after_first == total_after_second  # no double-charge


def test_obligation_split_sums_exactly_to_supply(make_resident: Callable) -> None:
    """Largest-remainder split: the total posted must equal the total supply exactly, never
    approximately (integer minutes, no float division)."""
    year, month = 2027, 9
    # 3 slots x 60 min = 180 minutes total supply, split across 4 residents (not evenly divisible).
    periode = _build_month(year, month, weekday_capacity=3, weekend_capacity=0)
    residents = [make_resident(email=f"split{i}@gahk.dk") for i in range(4)]
    for r in residents:
        _place(r, year, month)

    post_obligation(periode, month)

    total_charged = -sum(balance_for(r) for r in residents)  # FORPLIGTELSE deltas are negative
    assert total_charged == 180
    deltas = sorted(
        -KoekkenPost.objects.get(resident=r, kind=KoekkenPost.Kind.FORPLIGTELSE).delta_minutes
        for r in residents
    )
    assert deltas == [45, 45, 45, 45]  # 180 / 4 divides evenly here; see the remainder test below


def test_obligation_split_with_remainder_distributes_within_one_minute(make_resident: Callable) -> None:
    """The non-divisible branch of the largest-remainder split (previously untested by hand-
    calculation only -- see the design doc's "Ledger and obligation"): 1 weekday slot (60 minutes)
    split across 7 residents. divmod(60, 7) == (8, 4), so 4 residents are charged 9 minutes and 3
    are charged 8. The split must still sum to EXACTLY the total supply, and no one's charge may
    differ from another's by more than the unavoidable 1-minute remainder."""
    year, month = 2027, 12
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    residents = [make_resident(email=f"remainder{i}@gahk.dk") for i in range(7)]
    for r in residents:
        _place(r, year, month)

    post_obligation(periode, month)

    total_charged = -sum(balance_for(r) for r in residents)
    assert total_charged == 60  # exact, never approximate
    deltas = sorted(
        -KoekkenPost.objects.get(resident=r, kind=KoekkenPost.Kind.FORPLIGTELSE).delta_minutes
        for r in residents
    )
    assert deltas == [8, 8, 8, 9, 9, 9, 9]
    assert max(deltas) - min(deltas) <= 1


def test_balance_is_exact_sum_of_deltas(make_resident: Callable) -> None:
    year, month = 2027, 10
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    r = make_resident(email="sum@gahk.dk")

    KoekkenPost.objects.create(
        resident=r, periode=periode, kind=KoekkenPost.Kind.STARTSALDO, delta_minutes=120
    )
    KoekkenPost.objects.create(resident=r, periode=periode, kind=KoekkenPost.Kind.ARBEJDE, delta_minutes=60)
    KoekkenPost.objects.create(
        resident=r, periode=periode, kind=KoekkenPost.Kind.FORPLIGTELSE, delta_minutes=-45
    )
    KoekkenPost.objects.create(resident=r, periode=periode, kind=KoekkenPost.Kind.JUSTERING, delta_minutes=-7)

    assert balance_for(r) == 120 + 60 - 45 - 7 == 128


def test_no_post_written_for_resident_past_move_out(make_resident: Callable) -> None:
    year, month = 2027, 11
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    stayed = make_resident(email="stayed@gahk.dk")
    left = make_resident(email="left@gahk.dk", move_out_date=timezone.localdate() - timedelta(days=1))
    for r in (stayed, left):
        _place(r, year, month)

    written, present = post_obligation(periode, month)

    assert present == written == 1
    assert KoekkenPost.objects.filter(resident=stayed).exists()
    assert not KoekkenPost.objects.filter(resident=left).exists()


def test_post_obligation_rerun_after_move_out_reconciles_stale_charge(make_resident: Callable) -> None:
    """FIX 4: re-running post_obligation after a membership change (a resident's move_out_date moves
    to before the posted month) must not leave their stale FORPLIGTELSE row in place while
    recharging whoever remains the FULL supply -- that breaks "total obligation == supply", the
    self-balancing-by-construction property the module docstring calls out, mirroring
    `ak.services.apply_monthly_charge`'s own stale-row reconciliation."""
    year, month = 2028, 3
    periode = _build_month(year, month, weekday_capacity=2, weekend_capacity=0)  # 2 x 60 min = 120 total
    stays = make_resident(email="stays@gahk.dk")
    leaves = make_resident(email="leaves@gahk.dk")
    for r in (stays, leaves):
        _place(r, year, month)

    written1, present1 = post_obligation(periode, month)
    assert (written1, present1) == (2, 2)
    assert balance_for(stays) == -60
    assert balance_for(leaves) == -60

    leaves.move_out_date = date(2000, 1, 1)  # safely in the past, per core.clock.current_date()
    leaves.save(update_fields=["move_out_date"])

    written2, present2 = post_obligation(periode, month)

    assert (written2, present2) == (1, 1)
    total_charged = -(balance_for(stays) + balance_for(leaves))
    assert total_charged == 120  # equals supply -- not 180, which is what a stale, uncorrected row gives
    assert balance_for(stays) == -120  # the lone remaining resident now owes the full month's supply
    assert not KoekkenPost.objects.filter(resident=leaves, kind=KoekkenPost.Kind.FORPLIGTELSE).exists()


# --------------------------------------------------------------------------------- launch seeding


def test_duplicate_startsaldo_per_resident_is_rejected_by_constraint(make_resident: Callable) -> None:
    """FIX 5: a second STARTSALDO row for the same resident must be structurally impossible (a
    partial UniqueConstraint), not merely inconvenient -- `KoekkenPostAdmin` allows creating one
    directly, and both `seed_koekken_balances` and `koekken.demo` key an `update_or_create(resident=,
    kind=STARTSALDO)` on there being at most one; a second row would make that `get()` raise
    `MultipleObjectsReturned` as a bare traceback rather than a clean `CommandError`."""
    r = make_resident(email="dup_startsaldo@gahk.dk")
    periode = resolve_periode(date(2027, 9, 15))
    KoekkenPost.objects.create(
        resident=r, periode=periode, kind=KoekkenPost.Kind.STARTSALDO, delta_minutes=100
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            KoekkenPost.objects.create(
                resident=r, periode=periode, kind=KoekkenPost.Kind.STARTSALDO, delta_minutes=200
            )


def test_rebase_to_zero_mean_house_average_and_ordering(make_resident: Callable) -> None:
    a = make_resident(email="a@gahk.dk")
    b = make_resident(email="b@gahk.dk")
    c = make_resident(email="c@gahk.dk")
    # b is 300 minutes (5h) worse than a, before rebasing.
    entries = [(a, 600), (b, 300), (c, -300)]

    rebased = rebase_to_zero_mean(entries)

    assert sum(rebased.values()) == 0  # exactly zero, not approximately
    assert rebased[a.pk] - rebased[b.pk] == 300  # relative gap preserved exactly (n divides evenly)
    assert rebased[a.pk] > rebased[b.pk] > rebased[c.pk]  # ordering preserved


def test_seed_koekken_balances_command_zero_mean_and_dry_run(make_resident: Callable, tmp_path: Path) -> None:
    anna = make_resident(email="anna@gahk.dk")
    bo = make_resident(email="bo@gahk.dk")
    caroline = make_resident(email="caroline@gahk.dk")
    payload = [
        {"email": "anna@gahk.dk", "balance_hours": 6.0},
        {"email": "bo@gahk.dk", "balance_hours": 0.0},
        {"email": "caroline@gahk.dk", "balance_hours": -6.0},
    ]
    path = tmp_path / "balances.json"
    path.write_text(json.dumps(payload))

    call_command("seed_koekken_balances", str(path), "--dry-run", verbosity=0)
    assert KoekkenPost.objects.filter(kind=KoekkenPost.Kind.STARTSALDO).count() == 0

    call_command("seed_koekken_balances", str(path), verbosity=0)
    assert KoekkenPost.objects.filter(kind=KoekkenPost.Kind.STARTSALDO).count() == 3
    assert house_mean() == 0.0
    assert balance_for(anna) > balance_for(bo) > balance_for(caroline)  # ordering preserved

    # Re-running resets the anchor rather than accumulating a second STARTSALDO per resident.
    call_command("seed_koekken_balances", str(path), verbosity=0)
    assert KoekkenPost.objects.filter(kind=KoekkenPost.Kind.STARTSALDO).count() == 3


# --------------------------------------------------------------------------- dry-run writes nothing


def test_generate_koekkenvagter_dry_run_writes_nothing() -> None:
    call_command("generate_koekkenvagter", "--date", "2029-04-10", "--dry-run", verbosity=0)
    assert Periode.objects.count() == 0
    assert Vagt.objects.count() == 0


def test_allocate_koekkenvagter_dry_run_writes_nothing(make_resident: Callable) -> None:
    year, month = 2029, 4
    periode = resolve_periode(date(year, month, 10))
    generate_vagter(periode)
    for i in range(3):
        _place(make_resident(email=f"dryalloc{i}@gahk.dk"), year, month)

    call_command("allocate_koekkenvagter", str(year), str(month), "--dry-run", verbosity=0)

    assert VagtTildeling.objects.count() == 0


def test_post_koekken_obligation_dry_run_writes_nothing(make_resident: Callable) -> None:
    year, month = 2029, 4
    periode = resolve_periode(date(year, month, 10))
    generate_vagter(periode)
    for i in range(3):
        _place(make_resident(email=f"dryobl{i}@gahk.dk"), year, month)

    call_command(
        "post_koekken_obligation", "--year", str(year), "--month", str(month), "--dry-run", verbosity=0
    )

    assert KoekkenPost.objects.filter(kind=KoekkenPost.Kind.FORPLIGTELSE).count() == 0
