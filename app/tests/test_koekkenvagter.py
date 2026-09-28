"""Køkkenvagter — P1 (slot generation, tier-A allocation, the ledger and obligation posting, launch
seeding) plus Amendments 1-3, plus P2 (tier-B allocation, the full preference model, verification/
flagging, the kitchen tablet, the four UI surfaces). Full design:
`docs/plans/2026-09-21-koekkenvagter-design.md` and `docs/plans/2026-09-28-koekkenvagter-p2-design.md`.

The P1/Amendment section below goes through `koekken.services` and the management commands directly,
not through a Client -- no views existed yet at that point. The P2 section at the bottom of this file
uses `render_to_string` for the §10 "closed action removes its button" assertions (a template-level
check of exactly the invariant that matters, independent of how the service computed the permission)
and Django's test `Client` for the kiosk IP gate and the rollout gate.

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
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.test import Client, override_settings
from django.utils import timezone

from core.models import DevClock, Room
from koekken.models import (
    KoekkenPost,
    Periode,
    Praeference,
    PraeferenceDag,
    Vagt,
    VagtAnmeldelse,
    VagtRegel,
    VagtTildeling,
)
from koekken.services import (
    KoekkenAllocationError,
    _avoidance_resident_ids,
    allocate_month,
    allocate_tier_a,
    allocate_tier_b,
    balance_for,
    can_mark_done,
    flag_tildeling,
    flagged_by_names,
    generate_vagter,
    house_mean,
    in_preference_window,
    mark_udfoert,
    marking_window,
    periode_deadline,
    post_obligation,
    rebase_to_zero_mean,
    reconcile_month,
    resolve_anmeldelse,
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


def test_reconcile_koekkenvagter_command_dry_run_sweep_creates_no_periode() -> None:
    """F2's default sweep resolves the active periode via `_allocated_months_in_window()`'s own
    `resolve_periode()` call (get_or_create) -- on an empty DB that alone creates a `Periode` row. It
    must happen inside the same `--dry-run` transaction as the rest of the command (matching the
    explicit --year/--month path, whose own resolve_periode() call already rolled back correctly), not
    leak one out from underneath it."""
    assert Periode.objects.count() == 0

    call_command("reconcile_koekkenvagter", "--dry-run", verbosity=0)

    assert Periode.objects.count() == 0


def test_reconcile_koekkenvagter_command_year_without_month_raises() -> None:
    """The command's own help text says --year "Kræver --month" -- confirm passing --year alone
    actually raises instead of silently falling through to a full sweep."""
    with pytest.raises(CommandError):
        call_command("reconcile_koekkenvagter", "--year", "2046", verbosity=0)


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


# =============================================================================================
# P2: tier-B allocation (design doc §4)
# =============================================================================================


def _aften_vagt(periode: Periode, d: date, *, headcount: int = 1) -> Vagt:
    return Vagt.objects.create(
        periode=periode, date=d, kind=VagtRegel.Kind.AFTEN, headcount=headcount, duration_minutes=180
    )


def test_no_slot_left_open_in_either_tier_after_allocate_month(make_resident: Callable) -> None:
    """The design doc's headline invariant, insisted on twice: after a run, NO slot in EITHER tier
    is unfilled. A real month (via generate_vagter, every VagtRegel kind) against a population large
    enough to exceed both tiers' capacity comfortably (real capacity tops out around 62 tier-A slots
    and 54 tier-B slots) -- every generated Vagt this month must end up at full headcount."""
    year, month = 2050, 3
    periode = resolve_periode(date(year, month, 15))
    generate_vagter(periode)
    residents = [make_resident(email=f"fill{i}@gahk.dk") for i in range(70)]
    for r in residents:
        _place(r, year, month)

    allocate_month(year, month)

    vagter = list(Vagt.objects.filter(date__year=year, date__month=month))
    assert vagter  # sanity: the month actually generated shifts
    kinds_present = {v.kind for v in vagter}
    assert kinds_present == {VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST, VagtRegel.Kind.AFTEN}
    for vagt in vagter:
        assert vagt.tildelinger.count() == vagt.headcount, f"{vagt} is short of headcount"


def test_tier_b_preferences_never_reorder_the_queue(make_resident: Callable) -> None:
    """The design doc's second headline invariant, and the one most likely to be got wrong:
    preferences decide WHICH slot, never WHO is next. Adversarial by construction -- exactly ONE
    open aftenvagt slot, on the one day a much-better-balance resident strongly prefers; a much-
    worse-balance resident has no preference anywhere. The worse-balance resident must still be
    picked for the sole slot, and the better-balance resident's preference buys them NOTHING."""
    year, month = 2051, 3
    periode = resolve_periode(date(year, month, 15))
    target_date = next(d for d in (date(year, month, day) for day in range(1, 8)) if d.weekday() == 2)
    _aften_vagt(periode, target_date)

    good = make_resident(email="good_balance@gahk.dk")
    bad = make_resident(email="bad_balance@gahk.dk")
    for r in (good, bad):
        _place(r, year, month)
    _adjust(good, periode, 10_000)  # far AHEAD of the house -- should be served last, if at all
    _adjust(bad, periode, -10_000)  # far BEHIND -- should be served first, preference or not

    good_pref = Praeference.objects.create(resident=good, periode=periode, declared_at=date(year, 1, 1))
    PraeferenceDag.objects.create(praeference=good_pref, kind=VagtRegel.Kind.AFTEN, weekday=2)
    # bad declares nothing anywhere.

    result = allocate_tier_b(year, month)

    assert result.assigned == [bad]  # worse balance wins the turn regardless of preference
    assert good not in result.assigned  # good's preference for the very slot available bought nothing
    assert VagtTildeling.objects.filter(resident=bad, status=VagtTildeling.Status.TILDELT).count() == 1
    assert not VagtTildeling.objects.filter(resident=good).exists()


def test_tier_a_runs_before_tier_b_weekend_assignee_outranks_weekday_for_aftenvagt(
    make_resident: Callable,
) -> None:
    """Weekend-tier-A assignees outrank a weekday assignee for aftenvagt -- the already-approved
    weekend-compensation mechanic, which depends on tier-A having already run and written its
    result before tier-B reads it. `weekend_resident` has a far BETTER ledger balance than
    `weekday_resident` -- without the compensation mechanic, balance alone would hand the sole
    aftenvagt slot to `weekday_resident` instead."""
    year, month = 2051, 4
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=1)
    _aften_vagt(periode, date(year, month, 1))

    weekday_resident = make_resident(email="wd_outrank@gahk.dk")
    weekend_resident = make_resident(email="we_outrank@gahk.dk")
    for r in (weekday_resident, weekend_resident):
        _place(r, year, month)
    _adjust(weekend_resident, periode, 10_000)  # far AHEAD -- would lose tier B on balance alone
    _adjust(weekday_resident, periode, 0)
    Praeference.objects.create(resident=weekend_resident, periode=periode, weekday_unavailable=True)

    tier_a = allocate_tier_a(year, month)
    assert weekend_resident in tier_a.weekend_assigned
    assert weekday_resident in tier_a.weekday_assigned

    tier_b = allocate_tier_b(year, month)

    assert tier_b.assigned == [weekend_resident]  # compensation outranks the raw balance ordering


def test_undeclared_resident_is_assigned_in_tier_b(make_resident: Callable) -> None:
    """An undeclared resident is ranked and seated in tier B like anyone else, with no day
    preference to honour -- mirrors tier-A's own undeclared handling (Amendment 3, A3.2)."""
    year, month = 2051, 5
    periode = resolve_periode(date(year, month, 15))
    _aften_vagt(periode, date(year, month, 3))
    r = make_resident(email="undeclared_tb@gahk.dk")
    _place(r, year, month)

    result = allocate_tier_b(year, month)

    assert result.assigned == [r]


def test_weekday_unavailable_does_not_exclude_from_aftenvagt(make_resident: Callable) -> None:
    """§4: weekday_unavailable means "not home early-to-afternoon on weekdays" -- it must NOT
    restrict aftenvagt eligibility. A resident who declared it is still assigned an aftenvagt like
    anyone else."""
    year, month = 2051, 6
    periode = resolve_periode(date(year, month, 15))
    _aften_vagt(periode, date(year, month, 3))
    r = make_resident(email="unavailable_tb@gahk.dk")
    _place(r, year, month)
    Praeference.objects.create(resident=r, periode=periode, weekday_unavailable=True)

    result = allocate_tier_b(year, month)

    assert result.assigned == [r]


def test_avoidance_pattern_gets_extra_tier_a_instead_of_aftenvagt_when_capacity_allows(
    make_resident: Callable,
) -> None:
    """§4's aftenvagt-avoidance pattern: declared_at set, >=1 tier-A day preferred, 0 aften days.
    With 2 open weekday tier-A slots and only ONE candidate, the leftover-capacity mechanic
    (`_fill_leftover_tier_a`) gives them BOTH -- extra tier-A instead of an aftenvagt -- and
    `allocate_tier_b` then excludes them from its candidate pool entirely."""
    year, month = 2051, 7
    periode = _build_month(year, month, weekday_capacity=2, weekend_capacity=0)
    _aften_vagt(periode, date(year, month, 3))

    avoider = make_resident(email="avoider@gahk.dk")
    _place(avoider, year, month)
    pref = Praeference.objects.create(resident=avoider, periode=periode, weekday_unavailable=False)
    PraeferenceDag.objects.create(praeference=pref, kind=VagtRegel.Kind.MORGEN, weekday=0)
    # 0 aften days declared -> avoidance pattern.

    tier_a = allocate_tier_a(year, month)
    assert tier_a.weekday_assigned.count(avoider) == 2  # both leftover weekday slots, via the mechanic
    assert VagtTildeling.objects.filter(resident=avoider).count() == 2

    tier_b = allocate_tier_b(year, month)

    assert tier_b.assigned == []
    assert tier_b.skipped_avoidance == [avoider]


def test_fill_leftover_tier_a_enforces_two_shift_cap_against_surviving_rows(make_resident: Callable) -> None:
    """F5: `_fill_leftover_tier_a`'s docstring promises "at most ONE extra tier-A shift this way (two
    total this month)" -- but its `counts` was seeded only from `result` (this run's freshly-seated
    residents), which deliberately EXCLUDES anyone holding a SURVIVING non-TILDELT row (self-reported/
    flagged, P2) from `_seat_tier_a`'s own `population` (see `allocate_tier_a`'s docstring). Such a
    resident was simply absent from `result`, so `counts` silently started at 0 for them even though
    they already held 2 tier-A shifts this month from a prior run -- letting the leftover-fill
    mechanic hand them a THIRD. Reproduction: a single-resident population who already holds 2
    surviving (non-TILDELT) rows on two of three weekday tier-A vagter for the month;
    force-reallocating must leave the third slot's leftover capacity UNFILLED rather than give it to
    them."""
    year, month = 2054, 3
    periode = _build_month(year, month, weekday_capacity=3, weekend_capacity=0)
    vagter = list(
        Vagt.objects.filter(
            periode=periode, date__year=year, date__month=month, kind=VagtRegel.Kind.MORGEN
        ).order_by("date")
    )
    assert len(vagter) == 3

    r = make_resident(email="cap_survivor@gahk.dk")
    _place(r, year, month)
    VagtTildeling.objects.create(vagt=vagter[0], resident=r, status=VagtTildeling.Status.UDFOERT)
    VagtTildeling.objects.create(vagt=vagter[1], resident=r, status=VagtTildeling.Status.IKKE_UDFOERT)

    allocate_tier_a(year, month, force=True)  # not already-TILDELT anywhere, but explicit per the review

    assert VagtTildeling.objects.filter(resident=r).count() == 2  # never a third
    assert not VagtTildeling.objects.filter(vagt=vagter[2]).exists()  # leftover slot stays open, unfilled


def test_avoidance_signal_requires_an_actual_declaration_not_mere_silence(make_resident: Callable) -> None:
    """The discriminator §4 calls out: a resident who declared nothing anywhere (no Praeference row
    at all) is genuinely no-opinion, never avoidance -- even though "0 aften days" is trivially true
    for them too. Proven by NOT getting the avoidance treatment: with a single open weekday slot and
    exactly one leftover candidate who never declared, they still only ever hold ONE tier-A shift
    (the ordinary leftover-fill "ranked, not avoidance-first" path), and remain an ordinary tier-B
    candidate rather than being skipped."""
    year, month = 2051, 8
    periode = _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    _aften_vagt(periode, date(year, month, 3))
    r = make_resident(email="silent_no_declare@gahk.dk")
    _place(r, year, month)
    # No Praeference row at all for r.

    tier_a = allocate_tier_a(year, month)
    assert tier_a.weekday_assigned.count(r) == 1  # ordinary single seat, not the avoidance double

    tier_b = allocate_tier_b(year, month)
    assert tier_b.skipped_avoidance == []
    assert tier_b.assigned == [r]


def test_avoidance_signal_ignores_a_declaration_with_zero_days_selected_anywhere(
    make_resident: Callable,
) -> None:
    """F8: the design doc's §4 discriminator has TWO halves -- `declared_at IS NOT NULL` AND "0 aften
    days" -- but the existing "requires an actual declaration" test above only ever covers the FIRST
    half (no `Praeference` row at all). This covers the second, untested half: a resident who HAS a
    `Praeference` row but selected NOTHING anywhere -- zero tier-A days AND zero aften days -- is
    "genuinely no-opinion, not avoidance" per the design doc, distinct from someone who selected
    tier-A days and explicitly left aften empty (which IS avoidance, per the test above). This should
    already pass against the existing, correct implementation -- it is purely a missing test that
    would have caught a real regression had the `& has_tier_a_day` clause ever been accidentally
    dropped from `_avoidance_resident_ids`."""
    year, month = 2051, 9
    periode = resolve_periode(date(year, month, 15))
    r = make_resident(email="declared_but_silent@gahk.dk")
    Praeference.objects.create(resident=r, periode=periode, weekday_unavailable=False)
    # A Praeference row exists, but no PraeferenceDag rows of any kind -- zero tier-A days, zero aften
    # days. Genuinely no-opinion, not the avoidance pattern.

    assert _avoidance_resident_ids([r.pk], periode) == set()


def test_tier_b_soft_day_preference_is_honoured_when_capacity_allows(make_resident: Callable) -> None:
    """§11: "a soft day preference is honoured when capacity allows" -- assert on which SPECIFIC slot
    the preferring resident receives, not just that they got assigned something (F9). Two open
    aftenvagt slots on different weekdays; without a preference, `allocate_tier_b`'s default choice is
    the EARLIEST open vagt (`open_vagter[0]`) -- so a resident who declares AFTEN for the LATER date's
    weekday, and actually receives it instead of the earlier default, proves the preference changed
    the outcome. A second, unpreferenced resident with a worse balance absorbs the other slot, so the
    preferring resident is never forced to take both."""
    year, month = 2055, 3
    periode = resolve_periode(date(year, month, 15))
    early = next(d for d in (date(year, month, day) for day in range(1, 8)) if d.weekday() == 0)  # Monday
    late = next(d for d in (date(year, month, day) for day in range(8, 15)) if d.weekday() == 2)  # Wednesday
    early_vagt = _aften_vagt(periode, early)
    late_vagt = _aften_vagt(periode, late)

    preferrer = make_resident(email="softpref_preferrer@gahk.dk")
    filler = make_resident(email="softpref_filler@gahk.dk")
    for r in (preferrer, filler):
        _place(r, year, month)
    _adjust(preferrer, periode, -100)  # ranked first -> gets first pick
    _adjust(filler, periode, 0)
    pref = Praeference.objects.create(resident=preferrer, periode=periode, declared_at=date(year, 1, 1))
    PraeferenceDag.objects.create(praeference=pref, kind=VagtRegel.Kind.AFTEN, weekday=late.weekday())
    # filler declares nothing -- gets whatever is left over.

    result = allocate_tier_b(year, month)

    assert set(result.assigned) == {preferrer, filler}
    assert result.preference_honoured == [preferrer]
    assert VagtTildeling.objects.get(resident=preferrer).vagt == late_vagt  # the declared day, not early
    assert VagtTildeling.objects.get(resident=filler).vagt == early_vagt  # leftover, no preference


def test_allocate_tier_b_force_rerun_is_idempotent_and_does_not_reshuffle_from_own_assignment(
    make_resident: Callable,
) -> None:
    """F2: `bulk_projected_balances` (and `declared_at_by_id`/`preferences`) must be computed AFTER
    this run's own delete of prior tier-B TILDELT rows, exactly like `allocate_tier_a` (Amendment 1,
    A1.2) -- otherwise a resident's own about-to-be-recomputed assignment inflates their own ranking
    balance, and a force-reallocation with no underlying balance change can reshuffle who gets which
    aftenvagt. Reproduction: A's true ledger balance is worse than B's (-100 vs 0), so a fair run
    always seats A first, and A picks the earlier-dated (here, also longer) vagt by default. Without
    the fix, computing balances BEFORE the delete lets A's own 300-minute TILDELT row inflate their
    projected balance past B's 60-minute one on the re-run, flipping the ranking and handing A's slot
    to B purely because of the run's own bookkeeping order."""
    year, month = 2053, 3
    periode = resolve_periode(date(year, month, 15))
    early = date(year, month, 3)
    late = date(year, month, 10)
    long_vagt = Vagt.objects.create(
        periode=periode, date=early, kind=VagtRegel.Kind.AFTEN, headcount=1, duration_minutes=300
    )
    short_vagt = Vagt.objects.create(
        periode=periode, date=late, kind=VagtRegel.Kind.AFTEN, headcount=1, duration_minutes=60
    )

    a = make_resident(email="tierb_idem_a@gahk.dk")
    b = make_resident(email="tierb_idem_b@gahk.dk")
    _place(a, year, month)
    _place(b, year, month)
    _adjust(a, periode, -100)
    _adjust(b, periode, 0)

    first = allocate_tier_b(year, month)
    assert first.assigned == [a, b]
    assert VagtTildeling.objects.get(vagt=long_vagt).resident == a
    assert VagtTildeling.objects.get(vagt=short_vagt).resident == b

    allocate_tier_b(year, month, force=True)  # no underlying balance change since the first run

    assert VagtTildeling.objects.get(vagt=long_vagt).resident == a  # unchanged
    assert VagtTildeling.objects.get(vagt=short_vagt).resident == b  # unchanged


# =============================================================================================
# P2: verification -- marking done at the tablet (design doc §5)
# =============================================================================================


def _tildeling_for_marking(year: int, month: int, day: int, kind: str, resident: Resident) -> VagtTildeling:
    periode = resolve_periode(date(year, month, day))
    vagt = Vagt.objects.create(
        periode=periode, date=date(year, month, day), kind=kind, headcount=1, duration_minutes=60
    )
    return VagtTildeling.objects.create(vagt=vagt, resident=resident, status=VagtTildeling.Status.TILDELT)


@pytest.mark.parametrize(
    "kind,start_hour",
    [(VagtRegel.Kind.MORGEN, 6), (VagtRegel.Kind.FROKOST, 12), (VagtRegel.Kind.AFTEN, 17)],
)
def test_marking_window_boundaries_per_kind(make_resident: Callable, kind: str, start_hour: int) -> None:
    """§5's table, one kind at a time: opens at the shift's own start time on its own date, closes
    at the very start of the day AFTER the following day (inclusive through the end of that day)."""
    year, month, day = 2052, 3, 6  # a Wednesday
    r = make_resident(email=f"markwin_{kind}@gahk.dk")
    tildeling = _tildeling_for_marking(year, month, day, kind, r)

    opens_at, closes_at = marking_window(tildeling.vagt)
    assert opens_at == timezone.make_aware(datetime.combine(date(year, month, day), time(start_hour, 0)))
    assert closes_at == timezone.make_aware(datetime.combine(date(year, month, day + 2), time.min))

    assert can_mark_done(tildeling, at=opens_at - timedelta(minutes=1)) is False  # not open yet
    assert can_mark_done(tildeling, at=opens_at) is True  # opens exactly at start_time
    assert can_mark_done(tildeling, at=closes_at - timedelta(seconds=1)) is True  # last instant open
    assert can_mark_done(tildeling, at=closes_at) is False  # closed


def test_marking_devclock_walk_across_boundary(make_resident: Callable) -> None:
    """A DevClock walk across the marking-window boundary, per §11's requirement. DevClock only
    carries a date (midnight local time -- core.clock.current_datetime's own docstring), so a
    morgenvagt's 06:00 opening is never reachable via the simulated clock on ITS OWN day; walking to
    the FOLLOWING day (still inside the window) and then the day after (closed) is what actually
    exercises the boundary with date-only granularity."""
    year, month, day = 2052, 3, 10
    r = make_resident(email="devclockmark@gahk.dk")
    tildeling = _tildeling_for_marking(year, month, day, VagtRegel.Kind.MORGEN, r)

    with override_settings(DEBUG=True):
        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(year, month, day)})
        assert can_mark_done(tildeling) is False  # midnight of the shift's own day -- before 06:00

        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(year, month, day + 1)})
        assert can_mark_done(tildeling) is True  # still inside the following day

        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(year, month, day + 2)})
        assert can_mark_done(tildeling) is False  # window closed


def test_mark_udfoert_posts_credit_and_refuses_outside_window(make_resident: Callable) -> None:
    year, month, day = 2052, 4, 2
    r = make_resident(email="markudfoert@gahk.dk")
    tildeling = _tildeling_for_marking(year, month, day, VagtRegel.Kind.MORGEN, r)
    opens_at, _closes_at = marking_window(tildeling.vagt)

    with pytest.raises(KoekkenAllocationError):
        mark_udfoert(tildeling, at=opens_at - timedelta(minutes=1))
    assert not KoekkenPost.objects.filter(resident=r).exists()

    mark_udfoert(tildeling, at=opens_at)
    tildeling.refresh_from_db()
    assert tildeling.status == VagtTildeling.Status.UDFOERT
    post = KoekkenPost.objects.get(resident=r, kind=KoekkenPost.Kind.ARBEJDE)
    assert post.delta_minutes == tildeling.vagt.duration_minutes

    with pytest.raises(KoekkenAllocationError):  # already UDFOERT -- nothing left to self-report
        mark_udfoert(tildeling, at=opens_at)


def test_koekken_kiosk_gate_uses_forwarded_ip() -> None:
    """Mirrors test_features.py::test_oelkaelder_kiosk_gate_uses_forwarded_ip exactly, for the
    kitchen tablet's own KOEKKEN_KIOSK_IPS -- the last-hop X-Forwarded-For rule (§5/§7)."""
    with override_settings(DEBUG=False, KOEKKEN_KIOSK_IPS=["130.225.243.26"]):
        c = Client()
        ok = c.get("/intern/koekken/idag/", HTTP_X_FORWARDED_FOR="130.225.243.26")
        assert ok.status_code == 200  # kiosk open from the dorm egress IP
        blocked = c.get("/intern/koekken/idag/", HTTP_X_FORWARDED_FOR="203.0.113.9")
        assert blocked.status_code == 403  # any other IP is denied


def test_koekken_kiosk_mark_done_post_succeeds_without_login_from_whitelisted_ip(
    make_resident: Callable,
) -> None:
    """§11: the existing kiosk gate test above only covers GET (`idag`) -- marking a shift done is
    itself a POST (`marker_udfoert`), and it must equally succeed, with no login/session anywhere,
    from a whitelisted IP, and be refused from any other (F9). A morgenvagt dated YESTERDAY (real
    wall-clock, `start_time` 06:00) is guaranteed to still be inside its marking window for the whole
    of TODAY (§5: the window runs through the end of the day AFTER the shift), so this does not
    depend on what hour the test happens to run at."""
    r = make_resident(email="kiosk_post_mark@gahk.dk")
    yesterday = timezone.localdate() - timedelta(days=1)
    periode = resolve_periode(yesterday)
    vagt = Vagt.objects.create(
        periode=periode, date=yesterday, kind=VagtRegel.Kind.MORGEN, headcount=1, duration_minutes=60
    )
    tildeling = VagtTildeling.objects.create(vagt=vagt, resident=r, status=VagtTildeling.Status.TILDELT)
    url = f"/intern/koekken/idag/{tildeling.pk}/marker"

    with override_settings(DEBUG=False, KOEKKEN_KIOSK_IPS=["130.225.243.26"]):
        c = Client()
        blocked = c.post(url, HTTP_X_FORWARDED_FOR="203.0.113.9")
        assert blocked.status_code == 403
        tildeling.refresh_from_db()
        assert tildeling.status == VagtTildeling.Status.TILDELT  # untouched by the refused attempt

        ok = c.post(url, HTTP_X_FORWARDED_FOR="130.225.243.26")
        assert ok.status_code == 200

    tildeling.refresh_from_db()
    assert tildeling.status == VagtTildeling.Status.UDFOERT  # marked done, with no login anywhere
    assert KoekkenPost.objects.filter(resident=r, kind=KoekkenPost.Kind.ARBEJDE).exists()


# =============================================================================================
# P2: §5/§7 -- the tablet's shift list reaches the whole marking window, not just today (F1)
# =============================================================================================


def test_todays_tildelinger_includes_yesterdays_still_markable_shift(make_resident: Callable) -> None:
    """F1: the marking window (§5) stays open from a shift's own start time through the end of the
    FOLLOWING day, but `todays_tildelinger` filtered to `vagt__date=day` alone, so a still-markable
    shift from yesterday never appeared on the tablet at all -- there was no way to mark it done. A
    still-`TILDELT` row dated yesterday must appear (and, via `can_mark_done`, still be markable)."""
    from koekken.services import todays_tildelinger

    year, month, day = 2057, 4, 10
    r = make_resident(email="yesterday_open@gahk.dk")
    yesterday_tildeling = _tildeling_for_marking(year, month, day - 1, VagtRegel.Kind.MORGEN, r)
    today_vagt = Vagt.objects.create(
        periode=resolve_periode(date(year, month, day)),
        date=date(year, month, day),
        kind=VagtRegel.Kind.FROKOST,
        headcount=1,
        duration_minutes=60,
    )
    today_tildeling = VagtTildeling.objects.create(
        vagt=today_vagt, resident=r, status=VagtTildeling.Status.TILDELT
    )

    rows = list(todays_tildelinger(today=date(year, month, day)))

    assert yesterday_tildeling in rows
    assert today_tildeling in rows
    assert can_mark_done(
        yesterday_tildeling, at=marking_window(yesterday_tildeling.vagt)[1] - timedelta(seconds=1)
    )


def test_todays_tildelinger_excludes_yesterdays_shift_once_its_window_has_closed(
    make_resident: Callable,
) -> None:
    """F1's other half: a shift from yesterday whose marking window has already CLOSED (self-reported
    or flagged, so no longer `TILDELT`) must NOT reappear on the tablet -- it has nothing left to do,
    and reopening settled history would just be noise."""
    from koekken.services import todays_tildelinger

    year, month, day = 2057, 4, 20
    r = make_resident(email="yesterday_closed@gahk.dk")
    tildeling = _tildeling_for_marking(year, month, day - 1, VagtRegel.Kind.MORGEN, r)
    mark_udfoert(tildeling, at=marking_window(tildeling.vagt)[0])  # settled yesterday -- no longer TILDELT

    rows = list(todays_tildelinger(today=date(year, month, day)))

    assert tildeling not in rows


def test_todays_tildelinger_excludes_shift_from_two_days_ago(make_resident: Callable) -> None:
    """A shift from two days ago is genuinely outside its marking window by the start of today (§5:
    the window closes at the very start of the day after the following day) -- `todays_tildelinger`
    must not resurrect it just because it is still `TILDELT`."""
    from koekken.services import todays_tildelinger

    year, month, day = 2057, 4, 30
    r = make_resident(email="two_days_ago@gahk.dk")
    tildeling = _tildeling_for_marking(year, month, day - 2, VagtRegel.Kind.MORGEN, r)

    rows = list(todays_tildelinger(today=date(year, month, day)))

    assert tildeling not in rows


# =============================================================================================
# P2: flagging and adjudication (design doc §6)
# =============================================================================================


def test_flag_udfoert_upheld_reverses_credit_via_tilbagefoersel_and_preserves_original(
    make_resident: Callable,
) -> None:
    year, month = 2052, 5
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    worker = make_resident(email="flagworker@gahk.dk")
    adjudicator = make_resident(email="flagadjudicator@gahk.dk")
    flagger = make_resident(email="flagreporter@gahk.dk")
    _place(worker, year, month)
    allocate_tier_a(year, month)
    tildeling = VagtTildeling.objects.get(resident=worker)
    mark_udfoert(tildeling, at=marking_window(tildeling.vagt)[0])
    tildeling.refresh_from_db()
    arbejde_post = KoekkenPost.objects.get(resident=worker, kind=KoekkenPost.Kind.ARBEJDE)

    anmeldelse = flag_tildeling(tildeling, flagger, "Køkkenet var ikke rent.")
    tildeling.refresh_from_db()
    assert tildeling.status == VagtTildeling.Status.ANMELDT
    assert anmeldelse.previous_status == VagtTildeling.Status.UDFOERT

    resolve_anmeldelse(anmeldelse, upheld=True, resolved_by=adjudicator)

    tildeling.refresh_from_db()
    assert tildeling.status == VagtTildeling.Status.IKKE_UDFOERT
    assert KoekkenPost.objects.filter(pk=arbejde_post.pk).exists()  # NEVER deleted
    tilbagefoersel = KoekkenPost.objects.get(resident=worker, kind=KoekkenPost.Kind.TILBAGEFOERSEL)
    assert tilbagefoersel.delta_minutes == -tildeling.vagt.duration_minutes
    anmeldelse.refresh_from_db()
    assert anmeldelse.status == VagtAnmeldelse.Status.OPRETHOLDT
    assert anmeldelse.resolved_by == adjudicator


def test_flag_tildelt_upheld_writes_no_ledger_entry(make_resident: Callable) -> None:
    """§6's table: a flagged shift that was still TILDELT (nobody ever marked it done) writes
    NOTHING to the ledger when upheld -- there was no credit to reverse. The flag itself still moves
    the assignment to IKKE_UDFOERT."""
    year, month = 2052, 6
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    worker = make_resident(email="flagnoop@gahk.dk")
    adjudicator = make_resident(email="flagnoop_adj@gahk.dk")
    flagger = make_resident(email="flagnoop_reporter@gahk.dk")
    _place(worker, year, month)
    allocate_tier_a(year, month)
    tildeling = VagtTildeling.objects.get(resident=worker)
    assert tildeling.status == VagtTildeling.Status.TILDELT

    anmeldelse = flag_tildeling(tildeling, flagger)
    resolve_anmeldelse(anmeldelse, upheld=True, resolved_by=adjudicator)

    tildeling.refresh_from_db()
    assert tildeling.status == VagtTildeling.Status.IKKE_UDFOERT
    assert not KoekkenPost.objects.filter(resident=worker).exists()  # nothing posted, ever


def test_flag_dismissed_restores_previous_status_and_writes_nothing(make_resident: Callable) -> None:
    year, month = 2052, 7
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    worker = make_resident(email="flagdismiss@gahk.dk")
    adjudicator = make_resident(email="flagdismiss_adj@gahk.dk")
    flagger = make_resident(email="flagdismiss_reporter@gahk.dk")
    _place(worker, year, month)
    allocate_tier_a(year, month)
    tildeling = VagtTildeling.objects.get(resident=worker)
    mark_udfoert(tildeling, at=marking_window(tildeling.vagt)[0])

    anmeldelse = flag_tildeling(tildeling, flagger)
    resolve_anmeldelse(anmeldelse, upheld=False, resolved_by=adjudicator)

    tildeling.refresh_from_db()
    assert tildeling.status == VagtTildeling.Status.UDFOERT  # restored to what it was
    assert not KoekkenPost.objects.filter(kind=KoekkenPost.Kind.TILBAGEFOERSEL).exists()
    anmeldelse.refresh_from_db()
    assert anmeldelse.status == VagtAnmeldelse.Status.AFVIST


def test_resolving_an_already_resolved_flag_raises(make_resident: Callable) -> None:
    year, month = 2052, 8
    _build_month(year, month, weekday_capacity=1, weekend_capacity=0)
    worker = make_resident(email="flagtwice@gahk.dk")
    adjudicator = make_resident(email="flagtwice_adj@gahk.dk")
    flagger = make_resident(email="flagtwice_reporter@gahk.dk")
    _place(worker, year, month)
    allocate_tier_a(year, month)
    tildeling = VagtTildeling.objects.get(resident=worker)
    anmeldelse = flag_tildeling(tildeling, flagger)
    resolve_anmeldelse(anmeldelse, upheld=False, resolved_by=adjudicator)

    with pytest.raises(KoekkenAllocationError):
        resolve_anmeldelse(anmeldelse, upheld=True, resolved_by=adjudicator)


# =============================================================================================
# P2: §10 -- a closed/out-of-window action renders no button and no action URL
# =============================================================================================


def test_kiosk_mark_done_button_removed_when_cannot_mark(make_resident: Callable) -> None:
    year, month, day = 2052, 9, 5
    r = make_resident(email="tablet_btn@gahk.dk")
    tildeling = _tildeling_for_marking(year, month, day, VagtRegel.Kind.MORGEN, r)
    action_url = f"/intern/koekken/idag/{tildeling.pk}/marker"

    closed_html = render_to_string("koekken/_idag_vagter.html", {"rows": [(tildeling, False)]})
    assert action_url not in closed_html

    open_html = render_to_string("koekken/_idag_vagter.html", {"rows": [(tildeling, True)]})
    assert action_url in open_html


def test_flag_button_removed_when_cannot_flag(make_resident: Callable) -> None:
    year, month, day = 2052, 9, 6
    r = make_resident(email="recent_btn@gahk.dk")
    tildeling = _tildeling_for_marking(year, month, day, VagtRegel.Kind.MORGEN, r)
    action_url = f"/intern/koekken/vagt/{tildeling.pk}/anmeld"

    # F3/F7: `recent` is (tildeling, can_flag, flagged_by_name) triples -- see
    # koekken.views._recent_context and koekken/_recent.html's own comment.
    closed_html = render_to_string("koekken/_recent.html", {"recent": [(tildeling, False, None)]})
    assert action_url not in closed_html

    open_html = render_to_string("koekken/_recent.html", {"recent": [(tildeling, True, None)]})
    assert action_url in open_html


def test_flagger_name_shown_to_the_flagged_resident_on_both_resident_facing_surfaces(
    make_resident: Callable,
) -> None:
    """F3: the P2 design doc's §6 is explicit -- "flagger identity is fully visible, including to the
    flagged resident" -- but the resident-facing templates (index.html's own-shifts list, _recent.html's
    all-residents list) showed only date/kind/status, no flagger anywhere. Proven here at the template
    level, for both surfaces, via `koekken.services.flagged_by_names`."""
    year, month, day = 2052, 9, 20
    worker = make_resident(email="flagshown_worker@gahk.dk")
    flagger = make_resident(email="flagshown_flagger@gahk.dk")
    tildeling = _tildeling_for_marking(year, month, day, VagtRegel.Kind.MORGEN, worker)
    flag_tildeling(tildeling, flagger, "Køkkenet var ikke rent.")
    tildeling.refresh_from_db()
    assert tildeling.status == VagtTildeling.Status.ANMELDT

    names = flagged_by_names([tildeling])
    assert names == {tildeling.pk: flagger.full_name}

    recent_html = render_to_string(
        "koekken/_recent.html", {"recent": [(tildeling, False, names.get(tildeling.pk))]}
    )
    assert flagger.full_name in recent_html

    index_html = render_to_string(
        "koekken/index.html",
        {
            "resident": worker,
            "upcoming": [],
            "past": [(tildeling, names.get(tildeling.pk))],
            "balance_minutes": 0,
            "balance_hours": 0,
            "house_mean_minutes": 0,
            "house_mean_hours": 0,
            "needs_to_declare": False,
            "can_manage": False,
            "can_view_balance_export": False,
            "vapid_public_key": "",
            "push_subscribed": False,
            "recent": [],
        },
    )
    assert flagger.full_name in index_html


def test_flag_queue_buttons_removed_once_resolved(make_resident: Callable) -> None:
    year, month, day = 2052, 9, 7
    r = make_resident(email="queue_btn@gahk.dk")
    flagger = make_resident(email="queue_btn_flagger@gahk.dk")
    tildeling = _tildeling_for_marking(year, month, day, VagtRegel.Kind.MORGEN, r)
    anmeldelse = flag_tildeling(tildeling, flagger)
    uphold_url = f"/intern/koekken/gruppe/anmeldelse/{anmeldelse.pk}/opretholdt"
    dismiss_url = f"/intern/koekken/gruppe/anmeldelse/{anmeldelse.pk}/afvist"

    open_html = render_to_string("koekken/_gruppe_flags.html", {"open_flags": [anmeldelse]})
    assert uphold_url in open_html
    assert dismiss_url in open_html

    # Once resolved it is no longer in open_flags at all -- row and buttons disappear together.
    resolved_html = render_to_string("koekken/_gruppe_flags.html", {"open_flags": []})
    assert uphold_url not in resolved_html
    assert dismiss_url not in resolved_html


# =============================================================================================
# P2: §9 -- the rollout gate opens views and the sidebar together
# =============================================================================================


def test_rollout_gate_opens_views_and_sidebar_together(
    make_resident: Callable, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.context_processors import _nav_intern
    from koekken import access as koekken_access

    plain = make_resident(email="plain_resident_rollout@gahk.dk")

    def has_koekken_item(roles: set[str]) -> bool:
        sections = _nav_intern(roles, plain.pk)
        return any("Køkkenvagter" in [item[1] for item in items] for _section, items in sections)

    client.force_login(plain)
    assert client.get("/intern/koekken/").status_code == 403  # closed default: Køkkengruppen-only
    assert has_koekken_item(set()) is False

    monkeypatch.setattr(koekken_access, "ACCESS_ROLES", None)  # the documented one-line rollout switch

    assert client.get("/intern/koekken/").status_code == 200
    assert has_koekken_item(set()) is True


def test_rollout_gate_covers_the_banner_and_the_tablet_stays_outside_it(
    make_resident: Callable, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11's rollout-gate requirement, completed: the test above only covers views + the sidebar
    entry -- the preference-window banner (§8, `core.context_processors.navigation`'s
    `koekken_preference_window_periode` key) must open together with them too (F9). Also records,
    explicitly, that the kitchen tablet is DELIBERATELY OUTSIDE this gate entirely (§5/§7:
    unauthenticated by design, IP-gated instead of role-gated) -- so a future reader does not mistake
    that omission for a gap this gate should also have closed."""
    from koekken import access as koekken_access
    from residents.models import Role

    plain = make_resident(email="plain_resident_banner_rollout@gahk.dk")
    client.force_login(plain)

    with override_settings(DEBUG=True):
        DevClock.objects.update_or_create(pk=1, defaults={"simulated_date": date(2026, 11, 27)})
        assert in_preference_window() is not None  # sanity: this date really is inside a window

        closed = client.get("/intern/")
        assert "erklær dine præferencer" not in closed.content.decode()  # closed default: gated out

        monkeypatch.setattr(koekken_access, "ACCESS_ROLES", None)  # the documented one-line rollout switch
        opened = client.get("/intern/")
        assert "erklær dine præferencer" in opened.content.decode()

        monkeypatch.setattr(koekken_access, "ACCESS_ROLES", (Role.KOKKENGRUPPE,))  # gate closed again

    # The tablet needs none of the above: unauthenticated and IP-gated, never role-gated --
    # test_koekken_kiosk_gate_uses_forwarded_ip already proves this on its own, without ever touching
    # koekken.access; this restates it on purpose so it reads as a deliberate design choice, not a gap.
    with override_settings(DEBUG=False, KOEKKEN_KIOSK_IPS=["130.225.243.26"]):
        anon = Client()
        resp = anon.get("/intern/koekken/idag/", HTTP_X_FORWARDED_FOR="130.225.243.26")
        assert resp.status_code == 200


# =============================================================================================
# P2: §8 -- the preference-window banner never writes to the database (F6)
# =============================================================================================


def test_in_preference_window_never_creates_a_periode_row(make_resident: Callable) -> None:
    """F6: `in_preference_window` runs from `core.context_processors.navigation` on every single
    authenticated page view, so it must never be a `get_or_create` -- that would be both an extra 1-2
    queries on every page load during the ~1-week window, several times a year, AND a write triggered
    from a GET request. Proven directly: calling it for a date inside a window (the reviewer's own
    repro date) must not leave behind a `Periode` row that did not already exist."""
    assert Periode.objects.filter(kind=Periode.Kind.FORAAR, year=2027).exists() is False

    result = in_preference_window(at=date(2026, 11, 27))

    assert result is not None
    assert str(result) == "Forår 2027"
    assert Periode.objects.filter(kind=Periode.Kind.FORAAR, year=2027).exists() is False  # still none
