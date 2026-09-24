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
import logging
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from core.models import DevClock, Room
from koekken.models import KoekkenPost, Periode, Praeference, Vagt, VagtRegel, VagtTildeling
from koekken.services import (
    KoekkenAllocationError,
    allocate_tier_a,
    balance_for,
    generate_vagter,
    house_mean,
    periode_deadline,
    post_obligation,
    rebase_to_zero_mean,
    reconcile_month,
    resolve_periode,
    roll_forward_allocation,
    set_preference,
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
    silently double-book a vagt, and must leave the surviving row completely untouched.

    force=True on the re-run because this month already has TILDELT rows from the first run --
    Amendment 1's A1.2 guard now requires it for ANY re-run, including this "genuine correction"
    one; the guard is tested on its own further down (test_reallocation_refuses_without_force...)."""
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

    result = allocate_tier_a(year, month, force=True)  # must not raise

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


# ----------------------------------------------------------------- Amendment 1, A1.1: FCFS tiebreak


def test_fcfs_tiebreak_earlier_declared_at_wins(make_resident: Callable) -> None:
    year, month = 2030, 9  # EFTERAAR 2030
    periode = _build_month(year, month, weekday_capacity=0, weekend_capacity=1)
    early = make_resident(email="early@gahk.dk")
    late = make_resident(email="late@gahk.dk")
    for r in (early, late):
        _place(r, year, month)
    # Identical (zero) balances -- the only thing that can decide the single weekend seat is FCFS.
    Praeference.objects.create(
        resident=early, periode=periode, weekday_unavailable=True, declared_at=date(2030, 6, 1)
    )
    Praeference.objects.create(
        resident=late, periode=periode, weekday_unavailable=True, declared_at=date(2030, 6, 2)
    )

    result = allocate_tier_a(year, month)

    assert result.weekend_assigned == [early]
    assert late in result.refused_weekend


def test_fcfs_tiebreak_reversing_declaration_order_reverses_outcome(make_resident: Callable) -> None:
    year, month = 2030, 10
    periode = _build_month(year, month, weekday_capacity=0, weekend_capacity=1)
    a = make_resident(email="a_rev@gahk.dk")
    b = make_resident(email="b_rev@gahk.dk")
    for r in (a, b):
        _place(r, year, month)
    Praeference.objects.create(
        resident=a, periode=periode, weekday_unavailable=True, declared_at=date(2030, 6, 5)
    )
    Praeference.objects.create(
        resident=b, periode=periode, weekday_unavailable=True, declared_at=date(2030, 6, 4)
    )

    result = allocate_tier_a(year, month)

    assert result.weekend_assigned == [b]  # b declared earlier this time -- outcome reversed
    assert a in result.refused_weekend


def test_fcfs_sort_key_non_declarer_sorts_last_against_tied_declarer(make_resident: Callable) -> None:
    """A1.1's comparator directly: two residents with an identical projected balance, one with a
    declared_at for this periode and one without, must order the non-declarer AFTER the declarer --
    declaring is what earns tie priority, never a null that could sort first by accident.

    This can't be observed end-to-end through allocate_tier_a: by P1 design, weekday-unavailable
    declarers are always seated ahead of the non-declarer weekend draft regardless of relative
    balance (see allocate_tier_a's docstring), and a declarer can never land in the weekday pool
    either -- so a declarer and a non-declarer never actually compete in the same ranked list there.
    This tests the shared `_tier_a_sort_key` comparator directly instead."""
    from koekken.services import _tier_a_sort_key

    declarer = make_resident(email="key_declarer@gahk.dk")
    non_declarer = make_resident(email="key_nondeclarer@gahk.dk")
    balances = {declarer.pk: -100, non_declarer.pk: -100}
    declared_at_by_id = {declarer.pk: date(2030, 1, 1)}

    ranked = sorted([non_declarer, declarer], key=lambda r: _tier_a_sort_key(r, balances, declared_at_by_id))

    assert ranked == [declarer, non_declarer]


def test_sequential_months_spread_load_via_projected_balance(make_resident: Callable) -> None:
    """Amendment 1's headline regression (A1.2, A1.5): allocating 3 consecutive months in sequence
    must not keep re-picking the same "most behind" resident just because the ledger hasn't caught
    up yet -- ranking on the PROJECTED balance (ledger + not-yet-credited TILDELT hours) is what
    prevents it. Three residents, tied ledger balance (all 0, nobody declared), one weekday slot per
    month, three consecutive months: raw-balance ranking with a stable pk tiebreak (the
    pre-Amendment-1 behaviour) would hand the lowest-pk resident every single month, since a plain
    TILDELT row never posts credit. Projected balance must instead rotate the pick each month."""
    year = 2031
    r1 = make_resident(email="spread1@gahk.dk")
    r2 = make_resident(email="spread2@gahk.dk")
    r3 = make_resident(email="spread3@gahk.dk")
    residents = [r1, r2, r3]

    picked: list[Resident] = []
    for y, m in [(year, 9), (year, 10), (year, 11)]:
        _build_month(y, m, weekday_capacity=1, weekend_capacity=0)
        for r in residents:
            _place(r, y, m)
        result = allocate_tier_a(y, m)
        assert len(result.weekday_assigned) == 1
        picked.append(result.weekday_assigned[0])

    assert picked == [r1, r2, r3]  # rotates -- never the same resident picked twice
    assert len(set(picked)) == 3


# --------------------------------------------------------------- Amendment 1, A1.2: look-ahead guard


def test_reallocation_refuses_without_force_and_proceeds_with_force(make_resident: Callable) -> None:
    year, month = 2031, 12
    periode = _build_month(year, month, weekday_capacity=2, weekend_capacity=0)
    a = make_resident(email="force_a@gahk.dk")
    b = make_resident(email="force_b@gahk.dk")
    for r in (a, b):
        _place(r, year, month)

    first = allocate_tier_a(year, month)
    assert set(first.weekday_assigned) == {a, b}

    with pytest.raises(KoekkenAllocationError):
        allocate_tier_a(year, month)  # already allocated -- refuses without --force

    assert VagtTildeling.objects.filter(vagt__periode=periode).count() == 2  # unchanged by the refusal

    _adjust(a, periode, -1000)  # change the ranking so a force re-run visibly reshuffles
    second = allocate_tier_a(year, month, force=True)
    assert set(second.weekday_assigned) == {a, b}  # still both, re-picked under --force


def test_allocate_koekkenvagter_command_refuses_rerun_without_force(make_resident: Callable) -> None:
    year, month = 2032, 1
    periode = resolve_periode(date(year, month, 15))
    generate_vagter(periode)
    r = make_resident(email="cmd_force@gahk.dk")
    _place(r, year, month)

    call_command("allocate_koekkenvagter", str(year), str(month), verbosity=0)

    with pytest.raises(CommandError):
        call_command("allocate_koekkenvagter", str(year), str(month), verbosity=0)

    call_command("allocate_koekkenvagter", str(year), str(month), "--force", verbosity=0)  # no raise


def test_allocate_koekkenvagter_batch_allocates_periodes_first_three_months(make_resident: Callable) -> None:
    periode = resolve_periode(date(2032, 9, 15))  # EFTERAAR 2032
    generate_vagter(periode)
    residents = [make_resident(email=f"batch{i}@gahk.dk") for i in range(3)]
    for r in residents:
        for month in (9, 10, 11):
            _place(r, 2032, month)

    call_command("allocate_koekkenvagter", "2032", "9", "--batch", verbosity=0)

    for month in (9, 10, 11):
        assert VagtTildeling.objects.filter(
            vagt__date__year=2032, vagt__date__month=month, status=VagtTildeling.Status.TILDELT
        ).exists()
    assert not VagtTildeling.objects.filter(vagt__date__year=2032, vagt__date__month=12).exists()


def test_allocate_koekkenvagter_batch_clamps_to_periode_length_for_sommer(make_resident: Callable) -> None:
    """A2.8: --batch must clamp to the periode's actual length. SOMMER is only 2 months (Jul-Aug), so
    a fixed 3-month walk from its start would step into the following EFTERAAR's September -- which
    has no Vagt rows generated yet (this command would raise KoekkenAllocationError on it) and, even
    if it did, would be allocating a month before ITS OWN periode's preference deadline has passed,
    inverting Amendment 1's locking rule. The fix clamps the walk to `periode.end_date`."""
    periode = resolve_periode(date(2033, 7, 15))  # SOMMER 2033 (Jul-Aug only)
    generate_vagter(periode)
    residents = [make_resident(email=f"sommerbatch{i}@gahk.dk") for i in range(3)]
    for r in residents:
        for month in (7, 8):
            _place(r, 2033, month)

    call_command("allocate_koekkenvagter", "2033", "7", "--batch", verbosity=0)  # must not raise

    for month in (7, 8):
        assert VagtTildeling.objects.filter(
            vagt__date__year=2033, vagt__date__month=month, status=VagtTildeling.Status.TILDELT
        ).exists()
    assert not VagtTildeling.objects.filter(vagt__date__year=2033, vagt__date__month=9).exists()


def test_roll_forward_koekkenvagter_command_dry_run_writes_nothing(make_resident: Callable) -> None:
    """Only THIS month gets hand-built Vagt rows (unlike a real generate_vagter, which would create
    the whole periode's slots and so give roll_forward_allocation earlier, population-less months to
    trip over first) -- see _build_month's own docstring for why tests build capacity this way."""
    year, month = 2033, 6
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    _place(make_resident(email="dryroll@gahk.dk"), year, month)

    call_command("roll_forward_koekkenvagter", "--date", f"{year}-{month:02d}-10", "--dry-run", verbosity=0)

    assert VagtTildeling.objects.count() == 0


# ------------------------------------------------------------ Amendment 1, A1.3: preference locking


def test_set_preference_mid_period_arrival_creates_for_current_periode(make_resident: Callable) -> None:
    r = make_resident(email="arrival@gahk.dk")
    row = set_preference(r, True, at=date(2033, 1, 15))  # deep into EFTERAAR 2032 -- deadline long past

    assert row.periode == resolve_periode(date(2033, 1, 15))
    assert row.weekday_unavailable is True
    assert row.declared_at == date(2033, 1, 15)


def test_set_preference_edit_after_deadline_targets_next_periode_and_leaves_current_row_untouched(
    make_resident: Callable,
) -> None:
    r = make_resident(email="lock@gahk.dk")
    efteraar = resolve_periode(date(2031, 9, 15))
    foraar = resolve_periode(date(2032, 2, 15))
    sommer = resolve_periode(date(2032, 7, 15))
    assert periode_deadline(foraar) == date(2031, 12, 1)

    # Mid-period declaration for the period the resident is currently in (EFTERAAR).
    current_row = set_preference(r, True, at=date(2031, 9, 10))
    assert current_row.periode == efteraar

    # Before FORAAR's deadline (Dec 1): the next declaration targets FORAAR directly, in place.
    foraar_row = set_preference(r, True, at=date(2031, 10, 15))
    assert foraar_row.periode == foraar
    assert foraar_row.weekday_unavailable is True

    # After FORAAR's deadline (still standing in EFTERAAR): the edit is redirected past FORAAR to
    # SOMMER instead, and FORAAR's already-set value is left completely untouched.
    sommer_row = set_preference(r, False, at=date(2031, 12, 20))
    assert sommer_row.periode == sommer
    assert sommer_row.weekday_unavailable is False

    foraar_row.refresh_from_db()
    assert foraar_row.weekday_unavailable is True  # untouched
    assert Praeference.objects.filter(resident=r, periode=efteraar).count() == 1
    assert Praeference.objects.filter(resident=r, periode=foraar).count() == 1
    assert Praeference.objects.filter(resident=r, periode=sommer).count() == 1


def test_mid_period_arrival_preference_only_affects_unallocated_months(make_resident: Callable) -> None:
    year = 2033
    periode = _build_month(year, 9, weekday_capacity=1, weekend_capacity=1)

    early = make_resident(email="early_res@gahk.dk")
    _place(early, year, 9)
    allocate_tier_a(year, 9)
    before = set(
        VagtTildeling.objects.filter(vagt__date__year=year, vagt__date__month=9).values_list(
            "resident_id", flat=True
        )
    )

    # The mid-period arrival: no Praeference row yet, this periode's deadline long past.
    newcomer = make_resident(email="newcomer@gahk.dk")
    row = set_preference(newcomer, True, at=date(year, 10, 5))
    assert row.periode == periode  # created directly for the CURRENT periode despite the deadline
    assert row.weekday_unavailable is True

    # The already-allocated month is untouched by the newcomer's later declaration.
    after = set(
        VagtTildeling.objects.filter(vagt__date__year=year, vagt__date__month=9).values_list(
            "resident_id", flat=True
        )
    )
    assert after == before

    # A not-yet-allocated month honours it: weekend capacity 1, only the newcomer declared.
    _build_month(year, 10, weekday_capacity=0, weekend_capacity=1)
    _place(newcomer, year, 10)
    result = allocate_tier_a(year, 10)
    assert result.weekend_assigned == [newcomer]


def test_allocation_crossing_periode_boundary_uses_target_month_periode_preferences(
    make_resident: Callable,
) -> None:
    year = 2034
    foraar = resolve_periode(date(year, 5, 15))  # FORAAR (Feb-Jun)
    sommer = resolve_periode(date(year, 7, 15))  # SOMMER (Jul-Aug)

    r = make_resident(email="boundary@gahk.dk")
    # Declares weekday_unavailable for FORAAR, but the OPPOSITE for SOMMER.
    Praeference.objects.create(
        resident=r, periode=foraar, weekday_unavailable=True, declared_at=date(year, 1, 1)
    )
    Praeference.objects.create(
        resident=r, periode=sommer, weekday_unavailable=False, declared_at=date(year, 1, 1)
    )

    # Zero weekend capacity in July: if the allocator wrongly consulted FORAAR's declaration
    # (weekday_unavailable=True), r would be refused rather than given a weekday slot.
    _build_month(year, 7, weekday_capacity=1, weekend_capacity=0)
    _place(r, year, 7)

    result = allocate_tier_a(year, 7)

    assert result.weekday_assigned == [r]
    assert result.refused_weekend == []
    assert result.unassigned == []


def test_missing_preference_row_falls_back_to_previous_periode_value(make_resident: Callable) -> None:
    year = 2035
    foraar = resolve_periode(date(year, 4, 15))
    sommer = resolve_periode(date(year, 7, 15))

    r = make_resident(email="carryforward@gahk.dk")
    Praeference.objects.create(
        resident=r, periode=foraar, weekday_unavailable=True, declared_at=date(year, 1, 1)
    )
    # No Praeference row at all for SOMMER -- a missed deadline.

    _build_month(year, 7, weekday_capacity=0, weekend_capacity=1)  # only a weekend seat available
    _place(r, year, 7)

    result = allocate_tier_a(year, 7)  # must not raise, and must route r to the weekend pool

    assert result.weekend_assigned == [r]
    # A2.8: r is the ONLY resident this month, so the mandatory weekend draft would have seated them
    # anyway even with the fallback completely disabled -- that made the assertion above pass for the
    # wrong reason. `drafted == []` is the discriminating assertion: it proves r arrived via the
    # DECLARER path (the fallback correctly read SOMMER's carried-forward weekday_unavailable=True),
    # not via the draft, which is what would happen if the fallback silently defaulted to available.
    assert result.drafted == []
    assert Praeference.objects.filter(periode=sommer, resident=r).count() == 0  # no physical row copy


def test_devclock_walk_across_periode_boundary_keeps_two_months_allocated_ahead(
    make_resident: Callable,
) -> None:
    """A1.5's visibility invariant, walked with DevClock exactly as the design doc's Feb-Jun worked
    example describes: the Dec-1 deadline batch (Feb/Mar/Apr), the monthly roll-forward filling in
    May and June as the clock advances, and the May-1 deadline batch for SOMMER (Jul/Aug) -- at every
    point along the walk, at least two months from "today" onward are already allocated."""
    r = make_resident(email="walker@gahk.dk")
    months = [
        (2036, 2),
        (2036, 3),
        (2036, 4),
        (2036, 5),
        (2036, 6),  # FORAAR 2036
        (2036, 7),
        (2036, 8),  # SOMMER 2036
    ]
    for y, m in months:
        _build_month(y, m, weekday_capacity=1, weekend_capacity=0)
        _place(r, y, m)

    def is_allocated(y: int, m: int) -> bool:
        return VagtTildeling.objects.filter(
            vagt__date__year=y, vagt__date__month=m, status=VagtTildeling.Status.TILDELT
        ).exists()

    def months_ahead_allocated(today: date) -> int:
        """Consecutive allocated months strictly AFTER `today`'s own month -- the design doc's "N
        months ahead" framing; the current month itself doesn't count."""
        count = 0
        y, m = today.year, today.month
        m += 1
        if m == 13:
            y, m = y + 1, 1
        while (y, m) in months and is_allocated(y, m):
            count += 1
            m += 1
            if m == 13:
                y, m = y + 1, 1
        return count

    with override_settings(DEBUG=True):
        # Dec 1 2035: FORAAR's deadline -- Køkkengruppen's own batch, first three months.
        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(2035, 12, 1)})
        for y, m in months[:3]:
            allocate_tier_a(y, m)

        # The monthly roll-forward, walked month by month from January onward.
        for step in (date(2036, 1, 1), date(2036, 2, 1), date(2036, 3, 1), date(2036, 4, 1)):
            DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": step})
            roll_forward_allocation(step)

        # May 1 2036: SOMMER's own deadline-triggered batch (only 2 months exist to batch).
        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(2036, 5, 1)})
        for y, m in months[5:7]:
            allocate_tier_a(y, m)
        assert months_ahead_allocated(date(2036, 5, 1)) == 3  # June, July, August
        assert months_ahead_allocated(date(2036, 5, 1)) >= 2

        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(2036, 6, 1)})
        roll_forward_allocation(date(2036, 6, 1))  # no-op: FORAAR is already fully allocated
        assert months_ahead_allocated(date(2036, 6, 1)) == 2  # July, August
        assert months_ahead_allocated(date(2036, 6, 1)) >= 2


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


def test_reconcile_koekkenvagter_command_dry_run_writes_nothing(make_resident: Callable) -> None:
    year, month = 2039, 4
    departing = make_resident(email="cmd_dryrecon_dep@gahk.dk")
    _place(departing, year, month - 1)
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    allocate_tier_a(year, month)
    before = set(VagtTildeling.objects.values_list("pk", flat=True))

    call_command(
        "reconcile_koekkenvagter", "--year", str(year), "--month", str(month), "--dry-run", verbosity=0
    )

    assert set(VagtTildeling.objects.values_list("pk", flat=True)) == before


def test_reconcile_koekkenvagter_command_runs_reconciliation(make_resident: Callable) -> None:
    year, month = 2039, 5
    departing = make_resident(email="cmd_recon_dep@gahk.dk")
    _place(departing, year, month - 1)
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    allocate_tier_a(year, month)
    assert VagtTildeling.objects.filter(resident=departing).count() == 1

    arrival = make_resident(email="cmd_recon_arrival@gahk.dk")
    _place(arrival, year, month)

    call_command("reconcile_koekkenvagter", "--year", str(year), "--month", str(month), verbosity=0)

    assert VagtTildeling.objects.filter(resident=departing).count() == 0
    assert VagtTildeling.objects.filter(resident=arrival, status=VagtTildeling.Status.TILDELT).count() == 1


def test_reconcile_koekkenvagter_command_default_sweep_reconciles_once_real_list_arrives(
    make_resident: Callable,
) -> None:
    """F2: with no --year/--month, the command must reconcile every already-allocated month in the
    active periode's look-ahead window that now has a real list -- not a single hardcoded
    next_period() target. Proven by running the bare command twice, naming no month either time: once
    before the real list is published (must no-op, per F1) and once after (must find and reconcile
    `month` on its own)."""
    year, month = 2046, 4  # inside FORAAR (Feb-Jun)
    departing = make_resident(email="sweep_dep@gahk.dk")
    _place(departing, year, month - 1)  # projection source only
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    allocate_tier_a(year, month)
    assert VagtTildeling.objects.filter(resident=departing).count() == 1

    with override_settings(DEBUG=True):
        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(year, month, 1)})

        # No real Residency list published yet -- the sweep finds `month` (it has TILDELT rows), but
        # reconcile_month itself correctly no-ops on it (F1).
        call_command("reconcile_koekkenvagter", verbosity=0)
        assert VagtTildeling.objects.filter(resident=departing).count() == 1

        # The real list arrives: departing has left, a genuine arrival takes their place.
        arrival = make_resident(email="sweep_arrival@gahk.dk")
        _place(arrival, year, month)

        # Still no --year/--month -- the sweep must find and reconcile `month` on its own.
        call_command("reconcile_koekkenvagter", verbosity=0)

    assert VagtTildeling.objects.filter(resident=departing).count() == 0
    assert VagtTildeling.objects.filter(resident=arrival, status=VagtTildeling.Status.TILDELT).count() == 1


# ------------------------------------------------------- Amendment 2, A2.2: population projection


def test_population_projected_from_latest_published_list_excludes_moved_out(make_resident: Callable) -> None:
    """A2.7: allocating a month with no Residency rows at all falls back to the most recent published
    list, minus anyone whose move_out_date falls before that month begins."""
    year = 2040
    stayed = make_resident(email="proj_stayed@gahk.dk")
    left = make_resident(email="proj_left@gahk.dk", move_out_date=date(year, 5, 15))
    _place(stayed, year, 4)  # the "latest published list" -- April, not June
    _place(left, year, 4)

    _build_month(year, 6, weekday_capacity=2, weekend_capacity=0)

    result = allocate_tier_a(year, 6)  # no Residency rows at all for June -- must project April's list

    assert stayed in result.weekday_assigned
    assert left not in result.weekday_assigned
    assert left not in result.unassigned  # excluded from the projection entirely, not merely unseated
    assert set(result.weekday_assigned) | set(result.unassigned) == {stayed}


def test_no_published_residency_list_rollforward_logs_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A2.7: with no Residency rows anywhere -- real or to project from -- roll_forward_allocation
    must log and return None rather than let allocate_tier_a's KoekkenAllocationError propagate out of
    a scheduled task (A2.2's genuine-impossibility case)."""
    year, month = 2041, 3
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)  # Vagt rows exist; no Residency ever

    with caplog.at_level(logging.WARNING, logger="koekken.services"):
        result = roll_forward_allocation(date(year, month, 10))

    assert result is None
    assert VagtTildeling.objects.count() == 0
    assert any("kunne ikke allokere" in record.getMessage() for record in caplog.records)


# ------------------------------------------------------------- Amendment 2/3: reconciliation (A2.3/A3.1)


def test_reconciliation_vacates_departed_and_refills_via_shared_seating(make_resident: Callable) -> None:
    """A2.7: reconciliation vacates a projected resident absent from the real list and refills the
    slot via the shared eligibility-aware seating logic."""
    year, month = 2042, 5
    departing = make_resident(email="recon_dep@gahk.dk")
    backup = make_resident(email="recon_backup@gahk.dk")
    _place(departing, year, 4)  # projection source month
    _place(backup, year, 4)
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    _adjust(departing, periode, -1000)  # most behind -> wins the sole slot at the projected run

    allocate_tier_a(year, month)  # projected population = {departing, backup}; departing wins
    assert VagtTildeling.objects.filter(resident=departing, status=VagtTildeling.Status.TILDELT).count() == 1
    assert not VagtTildeling.objects.filter(resident=backup).exists()

    _place(backup, year, month)  # the real list is published: backup only, not departing

    result = reconcile_month(year, month)

    assert departing in result.vacated
    assert VagtTildeling.objects.filter(resident=departing).count() == 0
    assert backup in result.seated.weekday_assigned
    assert VagtTildeling.objects.filter(resident=backup, status=VagtTildeling.Status.TILDELT).count() == 1
    assert result.still_unfilled == []


def test_reconciliation_seats_genuine_new_arrival_into_unfilled_slot(make_resident: Callable) -> None:
    """A2.7: reconciliation seats a genuine new arrival (never in the projection) into an unfilled
    slot freed by a departure."""
    year, month = 2042, 6
    departing = make_resident(email="recon_dep2@gahk.dk")
    _place(departing, year, 5)  # projection source month only
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)

    allocate_tier_a(year, month)  # projected population = {departing}; seated
    assert VagtTildeling.objects.filter(resident=departing).count() == 1

    arrival = make_resident(email="recon_arrival@gahk.dk")
    _place(arrival, year, month)  # real list: arrival only -- departing was never published here

    result = reconcile_month(year, month)

    assert departing in result.vacated
    assert arrival in result.seated.weekday_assigned
    assert VagtTildeling.objects.filter(resident=arrival, status=VagtTildeling.Status.TILDELT).count() == 1


def test_reconciliation_never_touches_resident_present_on_both_lists(make_resident: Callable) -> None:
    """A2.7: the single most important guarantee -- a resident present on both the projection and the
    real list is never moved, touched or re-derived by reconciliation."""
    year, month = 2042, 7
    stays = make_resident(email="recon_stays@gahk.dk")
    _place(stays, year, month)  # real from the start -- no projection involved
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    allocate_tier_a(year, month)
    original = VagtTildeling.objects.get(resident=stays)
    original_pk, original_vagt_id, original_created_at = original.pk, original.vagt_id, original.created_at

    result = reconcile_month(year, month)

    assert result.vacated == []
    refreshed = VagtTildeling.objects.get(pk=original_pk)
    assert refreshed.vagt_id == original_vagt_id
    assert refreshed.created_at == original_created_at
    assert VagtTildeling.objects.filter(resident=stays).count() == 1


def test_reconciliation_idempotent_when_projection_and_reality_agree(make_resident: Callable) -> None:
    """A2.7: when the real list already matches who holds a slot, reconciliation is a true no-op --
    including on a second consecutive run."""
    year, month = 2042, 8
    a = make_resident(email="recon_idem_a@gahk.dk")
    b = make_resident(email="recon_idem_b@gahk.dk")
    for r in (a, b):
        _place(r, year, month)
    _build_month(year, month, weekday_capacity=2, weekend_capacity=0)
    allocate_tier_a(year, month)
    before = set(VagtTildeling.objects.values_list("pk", flat=True))

    first = reconcile_month(year, month)
    after_first = set(VagtTildeling.objects.values_list("pk", flat=True))
    second = reconcile_month(year, month)
    after_second = set(VagtTildeling.objects.values_list("pk", flat=True))

    assert first.vacated == [] and first.still_unfilled == []
    assert after_first == before
    assert second.vacated == [] and second.still_unfilled == []
    assert after_second == before


def test_koekken_never_creates_residency_rows(make_resident: Callable) -> None:
    """A2.7: no køkken code path -- projected allocation or reconciliation -- ever writes a
    Residency row. The projection in A2.2 is explicitly in-memory only."""
    year, month = 2043, 3
    r = make_resident(email="noresidency@gahk.dk")
    _place(r, year, 2)  # earlier month only -- the projection source
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    before = Residency.objects.count()

    allocate_tier_a(year, month)  # uses the in-memory projection
    # F1: the real list for this month is still empty -- reconcile_month must no-op rather than
    # vacate r's projected assignment (an empty real list means "not published yet", never "everyone
    # left").
    result = reconcile_month(year, month)

    assert Residency.objects.count() == before
    assert result.vacated == []
    assert result.still_unfilled == []
    assert VagtTildeling.objects.filter(resident=r, status=VagtTildeling.Status.TILDELT).count() == 1


def test_reconcile_month_no_real_list_leaves_tildelt_assignments_untouched(make_resident: Callable) -> None:
    """F1 (CRITICAL): reconciling a month with real-Residency-count == 0 must leave every existing
    TILDELT assignment completely untouched and return an empty result -- an empty real list means
    "not published yet", not "everyone left". Before the fix, `real_ids` was empty, so EVERY TILDELT
    row in the month got vacated and nothing was re-seated (`candidates` was empty too), inverting
    A2.3's headline guarantee that an assignment shown to a resident still living in the dorm is
    never revoked by reconciliation."""
    year, month = 2045, 3
    a = make_resident(email="f1_a@gahk.dk")
    b = make_resident(email="f1_b@gahk.dk")
    for r in (a, b):
        _place(r, year, month - 1)  # projection source only -- nothing published for `month` itself
    _build_month(year, month, weekday_capacity=2, weekend_capacity=0)
    allocate_tier_a(year, month)  # projects from month - 1, seats both a and b
    before = set(VagtTildeling.objects.values_list("pk", flat=True))
    assert len(before) == 2
    assert Residency.objects.filter(year=year, month=month).count() == 0  # real list genuinely absent

    result = reconcile_month(year, month)

    assert result.vacated == []
    assert result.seated.weekend_assigned == []
    assert result.seated.weekday_assigned == []
    assert result.seated.unassigned == []
    assert result.still_unfilled == []
    assert set(VagtTildeling.objects.values_list("pk", flat=True)) == before
    assert VagtTildeling.objects.filter(status=VagtTildeling.Status.TILDELT).count() == 2


# ---------------------------------------------------- Amendment 3, A3.1: reconciliation eligibility


def test_vacated_weekday_slot_not_inherited_by_ineligible_arrival(make_resident: Callable) -> None:
    """A3.5's adversarial case, and the exact defect A3.1 exists to fix: a resident who declared
    weekday_unavailable=True must NEVER be seated into a vacated weekday slot, even when it is the
    only unfilled slot in the month."""
    year, month = 2044, 4
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=0)

    departing = make_resident(email="adv_dep@gahk.dk")
    _place(departing, year, 3)  # projection source only
    allocate_tier_a(year, month)  # departing seated into the sole weekday slot
    assert VagtTildeling.objects.filter(resident=departing).count() == 1

    arrival = make_resident(email="adv_arrival@gahk.dk")
    _place(arrival, year, month)  # real list: arrival only
    Praeference.objects.create(resident=arrival, periode=periode, weekday_unavailable=True)

    result = reconcile_month(year, month)

    assert departing in result.vacated
    assert arrival not in result.seated.weekday_assigned
    assert arrival in result.seated.unassigned  # refused, not forced
    assert arrival in result.seated.refused_weekend
    assert VagtTildeling.objects.filter(resident=arrival).count() == 0
    weekday_vagt = Vagt.objects.get(
        periode=periode, date__year=year, date__month=month, kind=VagtRegel.Kind.MORGEN
    )
    assert weekday_vagt in result.still_unfilled


def test_vacated_weekday_slot_with_no_eligible_candidate_stays_unfilled_and_queued(
    make_resident: Callable,
) -> None:
    """A3.5: a vacated weekday slot with no eligible unassigned candidate at all stays open and is
    surfaced in `still_unfilled` -- reconciliation's queue for Køkkengruppen -- rather than silently
    vanishing or being forced on someone."""
    year, month = 2044, 5
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=0)

    departing = make_resident(email="queue_dep@gahk.dk")
    _place(departing, year, 4)  # projection source only
    allocate_tier_a(year, month)
    assert VagtTildeling.objects.filter(resident=departing).count() == 1

    # F1: a REAL Residency list IS published for the target month -- an empty one would mean "not
    # published yet" and must no-op instead (see test_reconcile_month_no_real_list_leaves_tildelt_
    # assignments_untouched). Departing has left, and the only other resident on the real list
    # declared weekday_unavailable, so nobody eligible exists for the vacated weekday slot.
    unavailable = make_resident(email="queue_unavailable@gahk.dk")
    _place(unavailable, year, month)
    Praeference.objects.create(resident=unavailable, periode=periode, weekday_unavailable=True)

    result = reconcile_month(year, month)

    assert departing in result.vacated
    assert result.seated.weekday_assigned == []
    # `unavailable` is refused as an excess weekend declarer (weekend_capacity=0), not forced onto
    # the weekday slot they said they can't do.
    assert unavailable in result.seated.unassigned
    assert unavailable in result.seated.refused_weekend
    weekday_vagt = Vagt.objects.get(
        periode=periode, date__year=year, date__month=month, kind=VagtRegel.Kind.MORGEN
    )
    assert result.still_unfilled == [weekday_vagt]
    assert VagtTildeling.objects.filter(vagt=weekday_vagt).count() == 0


def test_vacated_weekend_slot_can_be_filled_by_either_kind_of_resident(make_resident: Callable) -> None:
    """A3.5: unlike a weekday slot, a vacated WEEKEND slot may go to a weekday-unavailable resident
    too -- the eligibility constraint runs one way only (design doc finding 2 / A3.1's table)."""
    year, month = 2044, 6
    periode = _build_month(year, month, weekday_capacity=0, weekend_capacity=1)

    departing = make_resident(email="wknd_dep@gahk.dk")
    _place(departing, year, 5)  # projection source only
    allocate_tier_a(year, month)
    assert VagtTildeling.objects.filter(resident=departing).count() == 1

    candidate = make_resident(email="wknd_candidate@gahk.dk")
    _place(candidate, year, month)  # real list: candidate only
    Praeference.objects.create(resident=candidate, periode=periode, weekday_unavailable=True)

    result = reconcile_month(year, month)

    assert departing in result.vacated
    assert candidate in result.seated.weekend_assigned
    assert VagtTildeling.objects.filter(resident=candidate, status=VagtTildeling.Status.TILDELT).count() == 1
    assert result.still_unfilled == []


def test_reconciliation_no_inheritance_existing_unassigned_resident_beats_new_arrival(
    make_resident: Callable,
) -> None:
    """A3.5: a departing resident's slot may go to an EXISTING resident who is further behind on
    balance, not automatically to the new arrival who happens to show up in their place -- "no
    inheritance", per A3.1."""
    year, month = 2044, 7
    departing = make_resident(email="noinherit_dep@gahk.dk")
    existing_unassigned = make_resident(email="noinherit_existing@gahk.dk")
    _place(departing, year, 6)  # both in the ORIGINAL projected population...
    _place(existing_unassigned, year, 6)
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    _adjust(departing, periode, -10_000)  # most behind -> wins the sole slot at the projected run
    _adjust(existing_unassigned, periode, -5_000)  # behind, but not enough -- misses out (soft floor)

    allocate_tier_a(year, month)
    assert VagtTildeling.objects.filter(resident=departing).count() == 1
    assert not VagtTildeling.objects.filter(resident=existing_unassigned).exists()

    arrival = make_resident(email="noinherit_arrival@gahk.dk")
    # Real list: existing_unassigned stays, departing leaves, arrival is a genuine newcomer with a
    # much BETTER (less negative) balance than existing_unassigned's.
    _place(existing_unassigned, year, month)
    _place(arrival, year, month)
    _adjust(arrival, periode, 0)

    result = reconcile_month(year, month)

    assert departing in result.vacated
    assert existing_unassigned in result.seated.weekday_assigned  # further behind -- wins the slot
    assert arrival not in result.seated.weekday_assigned  # NOT automatically inherited by the arrival
    assert VagtTildeling.objects.filter(resident=existing_unassigned).count() == 1
    assert VagtTildeling.objects.filter(resident=arrival).count() == 0


def test_reconciliation_does_not_hand_second_slot_to_existing_holder(make_resident: Callable) -> None:
    """A3.5: reconciliation must not force a second tier-A shift onto a resident who already holds one
    this month -- even if their balance would rank them first -- while an eligible unassigned resident
    exists."""
    year, month = 2044, 8
    holds_one = make_resident(email="secondslot_holder@gahk.dk")
    departing = make_resident(email="secondslot_dep@gahk.dk")
    _place(holds_one, year, 7)
    _place(departing, year, 7)
    periode = _build_month(year, month, weekday_capacity=2, weekend_capacity=0)

    allocate_tier_a(year, month)  # both projected -> both slots filled
    assert VagtTildeling.objects.filter(resident=holds_one).count() == 1
    assert VagtTildeling.objects.filter(resident=departing).count() == 1

    _place(holds_one, year, month)  # real list: holds_one stays -- departing does not
    other = make_resident(email="secondslot_other@gahk.dk")
    _place(other, year, month)  # eligible, unassigned real resident
    # holds_one gets the worst balance in the house -- if the "already has a slot" exclusion were
    # broken, ranking alone would hand them departing's freed slot too.
    _adjust(holds_one, periode, -1_000_000)
    _adjust(other, periode, -10)

    result = reconcile_month(year, month)

    assert departing in result.vacated
    assert other in result.seated.weekday_assigned
    assert VagtTildeling.objects.filter(resident=holds_one).count() == 1  # never a second row
    assert VagtTildeling.objects.filter(resident=other, status=VagtTildeling.Status.TILDELT).count() == 1


# ---------------------------------------------------------------- Amendment 3, A3.2: no preference at all


def test_zero_history_arrival_defaults_to_weekday_available(make_resident: Callable) -> None:
    """A3.5: a resident with NO Praeference row anywhere -- not even in a previous periode, a true
    first-ever residency -- is treated as weekday-available (the model default), and may take the
    weekday pool. `_effective_weekday_unavailable_ids`'s fallback already produces this correctly for
    someone with zero history (A3.2); this proves it end-to-end via allocate_tier_a."""
    year, month = 2045, 3
    newcomer = make_resident(email="zerohistory@gahk.dk")
    _place(newcomer, year, month)
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)

    result = allocate_tier_a(year, month)  # no Praeference row for newcomer, in ANY periode, ever

    assert result.weekday_assigned == [newcomer]
    assert result.refused_weekend == []


# ------------------------------------------------------------------- Amendment 3: shared-core guarantee


def test_shared_seating_core_gives_same_choice_via_allocate_and_reconcile(make_resident: Callable) -> None:
    """A3.5's regression guard: allocate_tier_a and reconcile_month must make the exact same seating
    choice given equivalent slots and population, because they share ONE seating core
    (`_seat_tier_a`) -- two hand-written copies of this ranking would drift, and the direction they
    would drift in is exactly the pool-blind bug A3.1 exists to fix."""
    year = 2046
    periode = resolve_periode(date(year, 5, 15))  # FORAAR 2046
    p = make_resident(email="shared_p@gahk.dk")
    q = make_resident(email="shared_q@gahk.dk")
    r = make_resident(email="shared_r@gahk.dk")
    # Balances spaced 1000 min apart so a single ~60-min TILDELT bump from M1's own run can never
    # reorder the ranking relative to M2's reconciliation run, which happens afterwards.
    _adjust(p, periode, -2000)
    _adjust(q, periode, -1000)
    _adjust(r, periode, 0)

    # M2's setup happens FIRST, and entirely before p/q/r appear in Residency at all: month 6 is
    # seeded with an unrelated, wrong projected population (x, y, z) -- the ONLY list that exists yet,
    # so _resolve_population has nothing else to prefer -- that later gets entirely vacated, simulating
    # "the projection turned out to be wrong".
    x = make_resident(email="shared_x@gahk.dk")
    y = make_resident(email="shared_y@gahk.dk")
    z = make_resident(email="shared_z@gahk.dk")
    for resident in (x, y, z):
        _place(resident, year, 4)
    _build_month(year, 6, weekday_capacity=1, weekend_capacity=1)
    allocate_tier_a(year, 6)  # seats (some of) x, y, z via the projection

    # M1: a plain, direct allocation -- the baseline "what should happen" for this population.
    for resident in (p, q, r):
        _place(resident, year, 5)
    _build_month(year, 5, weekday_capacity=1, weekend_capacity=1)
    m1 = allocate_tier_a(year, 5)

    # Now the real list for month 6 is published: p, q, r -- none of x, y, z. Reconciliation reaches
    # the SAME population as M1 via vacate + re-seat, not a direct allocation.
    for resident in (p, q, r):
        _place(resident, year, 6)

    m2 = reconcile_month(year, 6)

    assert {res.pk for res in m1.weekend_assigned} == {res.pk for res in m2.seated.weekend_assigned}
    assert {res.pk for res in m1.weekday_assigned} == {res.pk for res in m2.seated.weekday_assigned}
    assert {res.pk for res in m1.unassigned} == {res.pk for res in m2.seated.unassigned}
