"""Smoke test for the seed_demo dev fixture command: it should run, be idempotent, and produce
data that lights up the app's real queries (active period, roles, derived ølkælder balances)."""

import pytest
from django.core.management import call_command

from den_hurtige.models import QuickPost
from oelkaelder.models import Shopper
from residents.models import Resident, active_period
from residents.permissions import real_roles


@pytest.mark.django_db
def test_seed_demo_populates_and_is_idempotent() -> None:
    # --force because Django's test runner forces DEBUG=False, which the safety guard blocks.
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    first_count = Resident.objects.count()
    # >= rather than ==: koekken.demo seeds a couple of extra "genuine new arrival" residents on top
    # of the requested 12 (Amendment 2/3, F3's reconciliation scenarios) -- best-effort and calendar
    # dependent, so the exact extra count isn't pinned here, only that --residents is a floor.
    assert first_count >= 12

    # Re-running with --fresh must not duplicate or raise (idempotent).
    # --force because Django's test runner forces DEBUG=False, which the safety guard blocks.
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)
    assert Resident.objects.count() == first_count

    # active_period follows the seeded current-month residencies.
    year, month = active_period()
    from django.utils import timezone

    today = timezone.localdate()
    assert (year, month) == (today.year, today.month)

    # Documented logins exist with the expected roles and password.
    formand = Resident.objects.get(email="formand@gahk.dk")
    assert "administrator" in real_roles(formand)
    assert formand.check_password("demo1234")

    # Derived ølkælder balance is computable (no crash, integer øre).
    shopper = Shopper.objects.first()
    assert isinstance(shopper.balance_ore, int)

    from arkiv.models import ArchiveFile, ArchiveFolder

    shared = ArchiveFolder.objects.get(parent=None, name="Fælles dokumenter")
    billeder = ArchiveFolder.objects.get(parent=shared, name="Billeder")
    summer = ArchiveFolder.objects.get(parent=billeder, name="Sommerfest 2026")
    assert ArchiveFile.objects.filter(folder=summer, name="gruppebillede.jpg").exists()
    assert not ArchiveFolder.objects.filter(parent=None, name="Billeder").exists()


@pytest.mark.django_db
def test_seed_demo_koekken_reconciliation_and_fcfs_tiebreak_both_seed() -> None:
    """koekken.demo.seed's Amendment 2/3 reconciliation scenario and Amendment 1 FCFS-tiebreak
    scenario each need their own spare month inside the active periode (see `WINDOW_EXTRA_MONTHS`'s
    comment in koekken/demo.py) -- a regression that lets one silently starve the other out of a month
    (as `_demo_reconciliation` once did to `_demo_fcfs_tiebreak`) would otherwise pass every other
    assertion in this file unnoticed, since none of them look at either scenario's data. Checks each
    scenario's own signature: the reconciliation scenario's genuine-arrival residents, and the
    tiebreak scenario's two Praeference rows declared a day apart starting at the periode's own
    start_date."""
    from datetime import timedelta

    from django.utils import timezone

    from koekken.models import Praeference
    from koekken.services import resolve_periode

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    # _demo_reconciliation's signature: the genuine new-arrival residents it creates.
    assert Resident.objects.filter(email="koekken.demo.ankomst@gahk.dk").exists()
    assert Resident.objects.filter(email="koekken.demo.ankomst.hverdage.utilgaengelig@gahk.dk").exists()

    # _demo_fcfs_tiebreak's signature: two weekday_unavailable Praeference rows for the active
    # periode, declared_at exactly periode.start_date and periode.start_date + 1 day.
    periode = resolve_periode(timezone.localdate())
    early, late = periode.start_date, periode.start_date + timedelta(days=1)
    declared_ats = set(
        Praeference.objects.filter(
            periode=periode, weekday_unavailable=True, declared_at__in=[early, late]
        ).values_list("declared_at", flat=True)
    )
    assert declared_ats == {early, late}, "the FCFS-tiebreak demo scenario's signature data is missing"

    # ...and the tiebreak must actually go to the earlier declarer. The tiebreak month is the one
    # whose weekend morgen/frokost capacity was shrunk to exactly one seat.
    from collections import defaultdict

    from koekken.models import Vagt, VagtRegel, VagtTildeling

    seats: dict[tuple[int, int], int] = defaultdict(int)
    for vagt in Vagt.objects.filter(
        periode=periode, kind__in=[VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST]
    ):
        if vagt.date.weekday() >= 5:
            seats[(vagt.date.year, vagt.date.month)] += vagt.headcount
    tiebreak_months = [ym for ym, n in seats.items() if n == 1]
    assert len(tiebreak_months) == 1, "expected exactly one month with a single weekend seat"
    year, month = tiebreak_months[0]

    def weekend_seats(declared_at: object) -> int:
        resident = Praeference.objects.get(
            periode=periode, weekday_unavailable=True, declared_at=declared_at
        ).resident
        return sum(
            1
            for t in VagtTildeling.objects.filter(
                resident=resident,
                vagt__periode=periode,
                vagt__date__year=year,
                vagt__date__month=month,
                vagt__kind__in=[VagtRegel.Kind.MORGEN, VagtRegel.Kind.FROKOST],
            ).select_related("vagt")
            if t.vagt.date.weekday() >= 5
        )

    assert weekend_seats(early) == 1, "the earlier declarer should hold the tiebreak month's weekend seat"
    assert weekend_seats(late) == 0, "the later declarer must lose the tiebreak"


@pytest.mark.django_db
def test_seed_demo_fills_the_board_including_the_two_awkward_cases() -> None:
    """The board needs demo content, but two specific rows are what make it useful locally: a pinned
    post (so the pinned-first layout and the 📌 marker are visible without anyone pinning something)
    and one several years old (the board keeps its archive, so the demo should show a genuinely old
    opslag still sitting there and paginating)."""
    from datetime import timedelta

    from django.utils import timezone

    from opslagstavle.models import Notice

    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    assert Notice.objects.count() >= 6
    assert Notice.objects.pinned().count() == 1
    assert Notice.objects.pinned().first().pinned_by is not None, "a pin with no attributor"
    # An old post is kept on purpose: the board has no retention, so the demo should show that a
    # years-old opslag is still there rather than that something is about to remove it.
    old = timezone.now() - timedelta(days=365 * 2)
    assert Notice.objects.filter(created_at__lt=old).exists(), "no archive post in the demo board"
    # Every category represented, so the filter chips are all reachable in the demo.
    assert len({n.category for n in Notice.objects.all()}) >= 5
    assert not hasattr(Notice.objects, "expired"), (
        "retention was removed from opslagstavlen; a reinstated expired() needs the spec updated too"
    )


@pytest.mark.django_db
def test_seed_demo_leaves_one_tombstone_with_its_replies_intact() -> None:
    """Awkward to reach by hand — deleting a freshly posted message takes the silent path — so the
    seeder makes one, with replies."""
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    tombstones = QuickPost.objects.filter(deleted_at__isnull=False)

    assert tombstones.count() == 1
    tombstone = tombstones.get()
    assert tombstone.content == "", "a tombstone holds no content"
    assert tombstone.comments.exists(), "the replies outlive the message"


def _skip_unless_allocated_current_month() -> None:
    """Skip when seed() cannot have allocated the current month. Summer (July/August) is never allocated
    -- seed() only generates and seeds claims there -- so the assertions below do not apply. Also skipped in
    the rare window where seed()'s `timezone.now().date()` (UTC) and `current_date()` (local) name
    different days, which can disagree about the current month on a month boundary (predates these tests;
    accepted as a skip rather than a flake)."""
    from django.utils import timezone

    from core.clock import current_date
    from koekken.services import periode_is_allocated, resolve_periode

    today = current_date()
    if timezone.now().date() != today:
        pytest.skip("UTC and local date disagree; seed() and the test may pick different months")
    if not periode_is_allocated(resolve_periode(today).kind):
        pytest.skip("summer periode is never allocated by seed()")


@pytest.mark.django_db
def test_seed_demo_koekken_allocates_aftenvagt_for_the_current_month() -> None:
    """Tier B runs for the current month, so the kitchen tablet has an aftenvagt crew to mark done
    (the demo used to run tier A only, leaving aftenvagt unassigned)."""
    from core.clock import current_date
    from koekken.models import Vagt, VagtRegel, VagtTildeling

    _skip_unless_allocated_current_month()
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    today = current_date()
    aften = Vagt.objects.filter(date__year=today.year, date__month=today.month, kind=VagtRegel.Kind.AFTEN)
    assert aften.exists()
    unassigned = aften.filter(tildelinger__isnull=True).count()
    assert unassigned == 0, f"{unassigned} aftenvagt left unassigned in the current month"
    assert VagtTildeling.objects.filter(vagt__in=aften, status=VagtTildeling.Status.TILDELT).exists()


@pytest.mark.django_db
def test_seed_demo_koekken_seeds_a_whole_shift_takeover() -> None:
    """The whole-shift take-over scenario runs on real tier-B output; guard against it silently going
    unseeded. `_demo_handoffs` tries every allocated month until one has a qualifying aftenvagt."""
    from core.clock import current_date
    from koekken.demo import whole_shift_candidates
    from koekken.models import Vagt, VagtBytte
    from koekken.services import month_population_ids, resolve_periode

    _skip_unless_allocated_current_month()
    call_command("seed_demo", "--fresh", "--force", "--residents", "12", verbosity=0)

    # Precondition, using the seeder's own definition of a qualifying shift (`whole_shift_candidates`): a
    # candidate in the current periode that the take-over left alone, or one already taken. Only the current
    # and window months get tier B, so late in a periode none may exist -- skip rather than flake.
    today = current_date()
    periode = resolve_periode(today)
    months = set(Vagt.objects.filter(periode=periode).values_list("date__year", "date__month"))
    had_candidate = VagtBytte.objects.filter(status=VagtBytte.Status.OVERTAGET_HEL).exists() or any(
        whole_shift_candidates(periode, year, month, today, set(month_population_ids(year, month)))
        for year, month in sorted(months)
    )
    if not had_candidate:
        pytest.skip("no future weekday 2-person tier-B aftenvagt left in the periode")
    assert VagtBytte.objects.filter(status=VagtBytte.Status.OVERTAGET_HEL).exists()
